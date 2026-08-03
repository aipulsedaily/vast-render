#!/usr/bin/env python3
"""The broker's EXEC dispatcher: CPU work on the rented box, N at a time.

WHY A SECOND DISPATCHER AND NOT A BRANCH IN THE FIRST ONE
=========================================================
`Broker.dispatch_once` runs exactly one job per pass and blocks inside it,
because the thing it is feeding is one GPU and `worker/server.py` is strictly
serial by law. Every property that dispatcher has — scene batching, the OptiX
prewarm it exists to amortise, the reattach-instead-of-rerender rule — is about
a single resident scene on a single device.

An exec job is the opposite shape. It is CPU-bound single-threaded Python, the
box has 23 CPUs of cgroup quota, and the entire justification for moving it off
the local machine is that TWELVE run at once. Routing it through the render
dispatcher would serialise every build behind every render and deliver none of
the throughput this exists for.

So: its own thread, its own claim (`db.claim_exec`, kind-filtered on both
sides), its own tunnel on its own SSH connection, its own port. The render path
is untouched.

WHAT IT SHARES, DELIBERATELY
----------------------------
The queue table, so fair-share, leases, retries, cancellation and the audit
trail are the same code and the same guarantees for both kinds. `Fleet`, so
there is exactly one instance and exactly one thing that may rent or destroy it.
The heartbeat, which stays on its own 60 s daemon thread and is never in any
dispatch path — a 40-minute exec job cannot starve it, and that separation is
already an incident fix rather than a preference.

WHAT IT REFUSES TO SHARE
------------------------
`Fleet.lock`, except through `ensure_ready`. An exec job must never be able to
restart the render worker, and `ensure_ready` on the already-loaded scene is a
cheap no-op that returns the endpoint.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import config, execremote, remote
from .db import DB
from .fleet import Fleet

log = logging.getLogger("exec")

# The fields the worker accepts, READ FROM THE WORKER rather than restated here.
#
# Two copies of this list drift, and the failure mode of drift is silent in the
# expensive direction: a field the broker believes is optional and the worker
# rejects produces a job that is queued, dispatched, refused on the box, and
# retried to exhaustion — three rentals' worth of round trips for a typo. The
# worker module imports nothing but the standard library, precisely so it can be
# read from here without dragging bpy into the broker.
def _worker_required() -> frozenset:
    import importlib.util
    src = Path(__file__).resolve().parent.parent / "worker" / "exec_server.py"
    spec_ = importlib.util.spec_from_file_location("_exec_server_schema", src)
    if spec_ is None or spec_.loader is None:          # pragma: no cover
        raise ImportError(f"cannot read the exec worker's schema from {src}")
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    return frozenset(mod.EXEC_REQUIRED)


WORKER_FIELDS = _worker_required()
# What a CALLER must supply. The broker mints `job_id` and computes `bundle`
# from the code tree, so those two are the broker's to fill in and nobody
# else's — `job_id` because a client-supplied one was a path traversal, and
# `bundle` because a client-supplied digest could name somebody else's inputs.
CALLER_REQUIRED = WORKER_FIELDS - {"job_id", "bundle"}

# Roots a caller may name as a bundle root. Same rule and same reason as
# `config.SCENE_ROOTS`: this string arrives from a client and becomes a
# filesystem path here and a directory on the instance, so it is resolved FIRST
# and then required to sit inside one of these. A prefix compare would be
# defeated by `/home/zany/f1-round2-evil`; containment after resolution is not.
DEFAULT_BUNDLE_ROOTS = (
    "/home/zany/f1-round2",
    "/home/zany/opus5-car-render",
)


def bundle_roots() -> list[Path]:
    raw = os.environ.get("VASTRENDER_BUNDLE_ROOTS")
    parts = raw.split(":") if raw is not None else DEFAULT_BUNDLE_ROOTS
    roots: list[Path] = []
    for part in parts:
        if not part.strip():
            continue
        real = Path(part).expanduser().resolve()
        if raw is not None or real.is_dir():
            if real not in roots:
                roots.append(real)
    return roots


class ExecError(ValueError):
    """Rejected exec submission. Message is safe to hand back over HTTP."""


def resolve_bundle_root(raw: str) -> Path:
    if not raw or not str(raw).strip():
        raise ExecError("bundle_root is required — an exec job's input is a code tree")
    text = str(raw).strip()
    if "\x00" in text:
        raise ExecError("bundle_root contains a null byte")
    try:
        real = Path(text).expanduser().resolve()
    except OSError as exc:
        raise ExecError(f"cannot resolve bundle_root {text!r}: {exc}") from None
    roots = bundle_roots()
    if not any(real == root or root in real.parents for root in roots):
        raise ExecError(
            f"bundle_root {text!r} resolves to {real}, which is outside every "
            f"permitted root: {', '.join(str(r) for r in roots)}"
        )
    if not real.is_dir():
        raise ExecError(f"bundle_root {text!r} is not a directory at {real}")
    return real


def plan_bundle(root: str, patterns: list) -> execremote.Bundle:
    """Resolve and content-address the inputs a job will run against.

    Deliberately at SUBMIT time as well as at dispatch time. The row records a
    digest, so if a module is edited in between, dispatch sees a different
    digest and refuses rather than quietly building different code from the one
    the caller asked for — the same discipline `scene_hash` enforces for a
    .blend, and for the same reason: a job's output must depend only on the job.
    """
    real = resolve_bundle_root(root)
    if not isinstance(patterns, list) or not patterns or \
            any(not isinstance(p, str) for p in patterns):
        raise ExecError("bundle must be a non-empty list of glob patterns "
                        "relative to bundle_root")
    try:
        return execremote.prepare(real, patterns)
    except ValueError as exc:
        raise ExecError(str(exc)) from None


class ExecService:
    """Owns the exec server on the instance, its tunnel, and the dispatch thread."""

    def __init__(self, broker) -> None:
        self.broker = broker
        self.db: DB = broker.db
        self.fleet: Fleet = broker.fleet
        self.slots = execremote.EXEC_SLOTS
        self.tunnel: Optional[subprocess.Popen] = None
        # job_id -> (started, item description). Read by `rq status`.
        self.inflight: dict[str, dict] = {}
        self.lock = threading.Lock()
        # Serialises "is the exec server up on this instance?" so twelve
        # simultaneous first jobs do not each restart it — which would kill the
        # other eleven's children, since a restart SIGKILLs the process group.
        self.ready_lock = threading.Lock()
        self.server_started_at: Optional[float] = None
        self.last_error: str = ""
        self._last_purge = 0.0

    # --- lifecycle -------------------------------------------------------

    def stop(self) -> None:
        self.close_tunnel()

    def close_tunnel(self) -> None:
        if self.tunnel is not None:
            with contextlib.suppress(Exception):
                self.tunnel.terminate()
            self.tunnel = None

    # --- readiness -------------------------------------------------------

    def ensure_ready(self) -> None:
        """An instance, an exec server on it, and a live forward to that server.

        Everything here is idempotent and cheap in the steady state: one socket
        ping. The expensive branches only run after a hibernation, an instance
        replacement, or a broker restart — all of which end the exec server,
        because it is a process on a container that was stopped.
        """
        with self.ready_lock:
            # The scene the render worker already holds, so this is the no-op
            # fast path of ensure_ready rather than a scene switch. An exec job
            # must never restart the render worker.
            scene = self.fleet.scene_path or config.SCENE
            ep = self.fleet.ensure_ready(Path(scene))

            if self.tunnel is not None and self.tunnel.poll() is not None:
                log.warning("exec tunnel exited %s — reopening", self.tunnel.returncode)
                self.tunnel = None
            if self.tunnel is not None:
                try:
                    pong = execremote.exec_call({"cmd": "ping"}, timeout=30)
                    if pong.get("ok"):
                        self.adopt_slots(pong)
                        self.server_started_at = pong.get("started_at")
                        return
                except Exception as exc:
                    log.warning("exec ping failed (%s) — repairing before "
                                "condemning anything", remote.diagnose(exc))
                self.close_tunnel()

            running = execremote.exec_server_running(ep)
            if running is not True:
                log.info("starting exec server on %s (%d slots)", ep, self.slots)
                execremote.start_exec_server(ep, slots=self.slots)
            execremote.reap_stale_exec_tunnels()
            self.tunnel = execremote.open_exec_tunnel(ep)
            pong = execremote.wait_exec_server(timeout=300)
            self.adopt_slots(pong)
            self.server_started_at = pong.get("started_at")
            log.info("exec server ready: %s slot(s), %.1f GB free disk, "
                     "%.1f GB memory available",
                     pong["slots"]["total"], pong["disk"]["free"] / 1e9,
                     (pong.get("mem_available") or 0) / 1e9)

    def adopt_slots(self, pong: dict) -> None:
        """Take the slot count from the SERVER, not from this process's config.

        The two can legitimately disagree: the exec server survives a broker
        restart, and a broker started with a different `VASTRENDER_EXEC_SLOTS`
        would then dispatch more work than the box was told to accept. That does
        not fail loudly — the extra jobs sit inside `Slots.acquire` burning their
        own `timeout_s` in a queue the broker cannot see, and then come back as
        "no slot within Ns" for a build that never ran. Believing the far side is
        the same rule the scene path applies to `started_at`.
        """
        total = ((pong.get("slots") or {}).get("total"))
        if isinstance(total, int) and total > 0 and total != self.slots:
            log.warning("exec server reports %d slot(s), this broker was "
                        "configured for %d — using the server's number, because "
                        "it is the one enforcing it", total, self.slots)
            self.slots = total

    # --- dispatch --------------------------------------------------------

    def loop(self) -> None:
        """Run by `Broker.supervised`, so it is restarted if it ever escapes.

        The stale-tunnel reap is here rather than in a `start()` because
        `supervised` may re-enter this function: `kill -9` is the only sanctioned
        way to restart this broker, so a restarted broker cannot have cleaned up
        its own `ssh -L`, and the orphan holds the local port against every
        future forward.
        """
        stale = execremote.reap_stale_exec_tunnels()
        if stale:
            log.warning("reaped %d orphaned exec forward(s) on local port %d — "
                        "kill -9 is the only sanctioned broker restart, so it "
                        "cannot clean up its own tunnel", stale,
                        execremote.EXEC_LOCAL_PORT)
        log.info("exec dispatcher started — %d slot(s), local port %d",
                 self.slots, execremote.EXEC_LOCAL_PORT)
        while self.broker.running:
            try:
                if self.broker.paused:
                    time.sleep(2)
                    continue
                with self.lock:
                    busy = sum(j["cpu_slots"] for j in self.inflight.values())
                if busy >= self.slots:
                    time.sleep(1)
                    continue
                job = self.db.claim_exec(config.JOB_LEASE_SEC)
                if job is None:
                    self.maybe_purge()
                    time.sleep(1)
                    continue
                spec = json.loads(job["spec"])
                cost = int(spec.get("cpu_slots") or 1)
                with self.lock:
                    self.inflight[job["id"]] = {
                        "started": time.time(), "cpu_slots": cost,
                        "agent": job["agent"], "entry": spec.get("entry"),
                    }
                threading.Thread(target=self._run_guarded, args=(job, spec),
                                 name=f"exec-{job['id']}", daemon=True).start()
            except Exception as exc:
                log.exception("exec dispatcher error: %s", remote.diagnose(exc))
                time.sleep(5)
        log.info("exec dispatcher stopping")

    def maybe_purge(self) -> None:
        """Occasionally sweep job directories the release step failed to remove.

        Belt and braces: every delivered job is released as soon as its outputs
        are fetched, and a fresh exec server sweeps everything at startup. What
        neither covers is a release that failed on a flapping link while the
        server kept running — and on a 30 GB volume, one abandoned job directory
        holding a 2.4 GB test blend matters. Only ever run when the queue is
        empty, so it cannot slow dispatch.
        """
        if time.time() - self._last_purge < 1800:
            return
        self._last_purge = time.time()
        if self.tunnel is None or self.tunnel.poll() is not None:
            return
        try:
            reply = execremote.exec_call(
                {"cmd": "purge", "older_than_s": 6 * 3600}, timeout=300)
            if reply.get("removed"):
                log.info("purged %d abandoned exec job director(ies): %s",
                         len(reply["removed"]), ", ".join(reply["removed"][:8]))
        except Exception as exc:
            log.debug("exec purge skipped: %s", remote.diagnose(exc))

    def _run_guarded(self, job: dict, spec: dict) -> None:
        job_id = job["id"]
        try:
            self.run_one(job, spec)
        except Exception as exc:
            why = remote.diagnose(exc)
            self.last_error = why
            # An exec job that failed because the INSTANCE went away has not
            # failed — the broker merely lost the box. Requeue without spending
            # an attempt, exactly as the render path refunds a pass that lost
            # its transport rather than its render.
            if isinstance(exc, remote.DiskFull):
                # Terminal, and deliberately not retried — the same rule the
                # render path applies. The preflight measured the disk, evicted
                # what it could and found it still does not fit; three more
                # attempts measure the same bytes. Only a human can change the
                # disk or the job.
                self.db.fail_terminal(job_id, why)
                log.error("exec job %s FAILED on DISK and will NOT be retried — %s",
                          job_id, why)
            elif isinstance(exc, (remote.ConnectionDropped, remote.SshError,
                                  remote.WorkerUnreachable, remote.FleetUnavailable)):
                self.db.requeue(job_id, f"{why} [transport, attempt refunded]")
                log.warning("exec job %s requeued WITHOUT spending an attempt — "
                            "this is transport, not the build: %s", job_id, why)
            else:
                state = self.db.fail(job_id, why, config.MAX_ATTEMPTS)
                log.error("exec job %s %s: %s", job_id, state, why)
        finally:
            with self.lock:
                self.inflight.pop(job_id, None)
            # Same reasoning as the render dispatcher: stamped AFTER the job, so
            # a long build does not come back with the idle clock already past
            # the grace period and the instance stopped the instant it finishes.
            self.broker.last_work = time.time()

    def run_one(self, job: dict, spec: dict) -> None:
        job_id = job["id"]
        started = time.time()
        self.ensure_ready()
        ep = self.fleet.ep
        if ep is None:
            raise remote.FleetUnavailable("no instance endpoint for exec dispatch")

        # Re-address the inputs. If a module changed between submit and dispatch
        # the digest moves, and this job asked for the code that existed when it
        # was submitted. Building the new code and filing the result under the
        # old request is exactly the silent-wrong-output class this project keeps
        # paying for, so it is refused rather than accommodated.
        bundle = plan_bundle(spec["bundle_root"], spec["bundle_patterns"])
        if bundle.digest != job["bundle"]:
            raise ValueError(
                f"the input bundle changed between submit and dispatch: this job "
                f"was queued against {job['bundle']} and {spec['bundle_root']} now "
                f"hashes to {bundle.digest}. Resubmit if the new code is what you "
                f"want — a build filed under a request for different code is not "
                f"something this broker will do quietly."
            )
        info = execremote.push_bundle(ep, bundle,
                                      keep_scenes=self.fleet.protected_scenes())
        if not info.get("cached"):
            log.info("staged %s for exec job %s in %.1fs",
                     bundle.describe(), job_id, info.get("seconds", 0.0))

        payload = {k: spec[k] for k in WORKER_FIELDS if k in spec}
        payload["job_id"] = job_id          # broker-minted, always
        payload["bundle"] = bundle.digest

        renew = threading.Event()

        def keep_lease() -> None:
            # A build may legitimately run longer than JOB_LEASE_SEC. Without
            # this the lease lapses mid-build, `requeue_expired` puts the row
            # back on the queue, and the broker starts competing with itself for
            # a job it is already watching.
            while not renew.wait(60.0):
                self.db.renew(job_id, config.JOB_LEASE_SEC)

        lease_thread = threading.Thread(target=keep_lease, daemon=True,
                                        name=f"exec-lease-{job_id}")
        lease_thread.start()
        try:
            reply = execremote.exec_call(
                payload, timeout=int(spec["timeout_s"]) + execremote.EXEC_CALL_SLACK)
        finally:
            renew.set()

        if not reply.get("ok"):
            tail = (reply.get("log") or "").strip().splitlines()[-12:]
            raise RuntimeError(
                f"exec job {job_id} failed: {reply.get('error') or 'no reason given'}"
                + (f" — last lines of the child's log: " + " | ".join(tail) if tail else "")
            )

        outputs = self.collect(ep, job_id, reply)
        try:
            execremote.exec_call({"cmd": "release", "job_id": job_id}, timeout=120)
        except Exception as exc:
            # The job is delivered; a failed cleanup costs disk, not the result.
            # The exec server's own purge sweep and its startup sweep both catch
            # it later, so this is logged rather than raised.
            log.warning("could not release exec job %s on the instance (%s) — the "
                        "server's purge will collect it", job_id, remote.diagnose(exc))

        self.db.finish_exec(job_id, [str(p) for p in outputs],
                            float(reply.get("exec_sec") or 0.0))
        log.info("exec job %s done — %.1fs on the box, %.1fs total, %d output(s) -> %s",
                 job_id, reply.get("exec_sec") or 0.0, time.time() - started,
                 len(outputs), outputs[0].parent if outputs else "(none)")

    def collect(self, ep: remote.Endpoint, job_id: str, reply: dict) -> list[Path]:
        """Fetch every declared output, atomically and digest-verified.

        Deliberately NOT `Broker.collect`, which hard-wires PNG structure and
        the blank-frame gate. An exec output is a gate JSON, an interface
        sidecar, a macro PNG — the broker has no business asserting what is in
        it. What it does assert is that the bytes on this disk are the bytes the
        instance produced: size against the source and sha256 against the digest
        the exec server computed on the file it had just written.

        The digest is the half that matters. A size check catches the observed
        failure — scp erroring partway through a 1.9 MB PNG and leaving 783 KB
        that looks finished — and catches nothing at all about a flipped bit.
        """
        dest_dir = config.OUT_DIR / "exec" / job_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        landed: list[Path] = []
        for out in reply.get("outputs") or []:
            name = str(out["name"])
            # The name came back from the instance, but it originated with the
            # caller and it is about to become a path on THIS machine.
            safe = Path(name).name
            if not safe or safe in (".", "..") or not re.fullmatch(r"[\w.\-+]{1,128}", safe):
                raise ValueError(f"exec job {job_id} returned an unusable output name {name!r}")
            local = dest_dir / safe
            got = remote.fetch_file(ep, out["path"], local)
            if got != int(out["bytes"]):
                local.unlink(missing_ok=True)
                raise remote.TransferError(
                    f"fetch {out['path']}", str(ep),
                    f"{got} bytes locally against {out['bytes']} on the instance",
                    0.0, sent=got, expected=int(out["bytes"]))
            digest = hashlib.sha256()
            with open(local, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest() != out["sha256"]:
                local.unlink(missing_ok=True)
                raise remote.TransferError(
                    f"verifying {out['path']}", str(ep),
                    f"sha256 {digest.hexdigest()[:16]} locally against "
                    f"{out['sha256'][:16]} on the instance — the transfer was the "
                    f"right LENGTH and the wrong BYTES",
                    0.0, sent=got, expected=int(out["bytes"]))
            landed.append(local)
        if not landed:
            raise RuntimeError(f"exec job {job_id} reported ok with no outputs")
        return landed

    # --- reporting -------------------------------------------------------

    def busy(self) -> bool:
        """Is there exec work this broker must not stop the instance under?

        Both halves matter. In flight is obvious. Queued matters because the
        idle timer runs on the render dispatcher's clock, and an exec-only
        workload leaves that clock untouched — without this, the broker would
        stop the instance out from under a full exec queue and then wake it
        again one second later.
        """
        with self.lock:
            if self.inflight:
                return True
        try:
            return self.db.exec_waiting() > 0
        except Exception:
            return False

    def snapshot(self) -> dict:
        with self.lock:
            jobs = {k: dict(v) for k, v in self.inflight.items()}
        return {
            "slots": self.slots,
            "used": sum(j["cpu_slots"] for j in jobs.values()),
            "inflight": [
                {"job_id": k, "agent": v["agent"], "entry": v["entry"],
                 "elapsed_sec": round(time.time() - v["started"], 1)}
                for k, v in sorted(jobs.items(), key=lambda kv: kv[1]["started"])
            ],
            "server_started_at": self.server_started_at,
            "tunnel": bool(self.tunnel and self.tunnel.poll() is None),
            "last_error": self.last_error,
        }
