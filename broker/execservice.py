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

from . import config, execremote, remote, scenes
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
def _worker_required() -> tuple[frozenset, frozenset]:
    import importlib.util
    src = Path(__file__).resolve().parent.parent / "worker" / "exec_server.py"
    spec_ = importlib.util.spec_from_file_location("_exec_server_schema", src)
    if spec_ is None or spec_.loader is None:          # pragma: no cover
        raise ImportError(f"cannot read the exec worker's schema from {src}")
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    return frozenset(mod.EXEC_REQUIRED), frozenset(mod.EXEC_OPTIONAL)


WORKER_FIELDS, WORKER_OPTIONAL = _worker_required()
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


class ExecMemoryShort(RuntimeError):
    """The box has not got the memory to open this job's scene RIGHT NOW.

    A WAIT, not a verdict, and it exists as its own class so it cannot be
    mistaken for one. The alternative is what actually happened on 2026-08-04:
    the child is SIGKILLed at `Read blend`, `exited -9` is not a transport
    class, all three attempts burn inside ninety seconds, and the job reports
    `3/3` as though the build had been tried and found wanting.

    Held to the same rule as WorkerBusy: refunded, and backed off long enough
    that the retry lands somewhere different from the attempt.
    """


class StaleBundle(ValueError):
    """The code changed between submit and dispatch. TERMINAL ON SIGHT.

    Its own class purely so `_run_guarded` can tell it apart from a real error,
    because the retry budget is the wrong instrument for it in BOTH directions.

    Retrying cannot help: the digest is recomputed from the same tree on every
    attempt, so it will differ every time, and three attempts buy three
    identical refusals. Worse, it was reached with attempts ALREADY SPENT on
    something that was nobody's fault — on 2026-08-04 an instance replacement
    burned two, and the bundle check then reported `3/3` as though the job had
    been tried and found wanting. It had never run once.

    So: fail immediately, with the verdict rather than an attempt count, and
    say what to do about it. The refusal itself stays exactly as it was —
    building new code and filing it under a request for the old code is the
    silent-wrong-output class this project keeps paying for.
    """


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


def resolve_scene(raw: str) -> tuple[str, str, int]:
    """A local .blend a build wants to OPEN, as (digest, filename, bytes).

    THE ONE THING THIS EXISTS TO GET RIGHT IS THAT THE ANSWER IS A DIGEST.

    Exec was "code in, blend born on the box", and every build that was ever too
    big to run locally is the other shape: it opens an existing assembly. The
    assembly is already on the instance, content-addressed in the scene cache
    the render path fills, so a build can be handed the resident copy instead of
    moving gigabytes up a wire that cannot carry them.

    What must not happen is that the build asks for "film16_breach.blend" and
    gets whichever file currently answers to that name. This project has already
    paid for that once: two `breach_film.npz` files with the same name held
    different tables, and the 0.1449 m travel guard exists because of it.
    Rebuilding that trap inside exec — where the caller cannot even see which
    file it got — would be strictly worse. So the scene is hashed HERE, at
    submit, and the digest travels; the name is only ever used to find the file
    inside that digest's own directory on the box.

    Containment is `config.SCENE_ROOTS`, the same rule and the same reason as
    the render path: this string arrives from a client and becomes a filesystem
    path. Resolved first, then required to sit inside a root, because a prefix
    compare is defeated by a sibling directory with a longer name.
    """
    if not raw or not str(raw).strip():
        raise ExecError("scene is empty")
    text = str(raw).strip()
    if "\x00" in text:
        raise ExecError("scene contains a null byte")
    try:
        real = Path(text).expanduser().resolve()
    except OSError as exc:
        raise ExecError(f"cannot resolve scene {text!r}: {exc}") from None
    if not any(real == r or r in real.parents for r in config.SCENE_ROOTS):
        raise ExecError(
            f"scene {text!r} resolves to {real}, which is outside every "
            f"permitted scene root: "
            f"{', '.join(str(r) for r in config.SCENE_ROOTS)}")
    if not real.is_file():
        raise ExecError(f"scene {text!r} is not a file at {real}")
    return remote.scene_hash(real), real.name, real.stat().st_size


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

    def endpoint_without_disturbing_the_worker(self) -> remote.Endpoint:
        """A box to run exec work on, WITHOUT asking the render worker anything.

        THIS IS THE FIX FOR `rq exec` FAILING `WorkerBusy` BEHIND A RENDER.

        The old code called `fleet.ensure_ready(the_scene_already_loaded)` and
        described it as "the no-op fast path ... an exec job must never restart
        the render worker". The intent was right and the call was wrong:
        `Fleet.ensure_ready` runs `self._refuse_if_rendering()` as its FIRST
        statement, *before* the fast path it was being trusted to reach. So the
        no-op could not be reached while a frame was rendering — the guard fired
        first, every time, and exec was refused for asking a question it already
        knew the answer to. Measured 2026-08-04: three exec jobs from the
        circuit-surface agent died this way in thirty minutes, one of them on a
        box that was four minutes old.

        `_refuse_if_rendering` is correct and stays. It protects DEPLOYS — a
        worker restart that discards a frame in flight, which is how a
        40-minute 8K render was once thrown away. Exec is not a deploy. It
        needs an instance that is up; it does not need the render worker idle,
        it does not need a scene loaded, and it does not need `Fleet.lock`.

        WHY THIS CANNOT DISTURB A RENDER, structurally rather than by intent:

          * The exec server is a SEPARATE PROCESS on a SEPARATE PORT (8800)
            reached through a SEPARATE TUNNEL, with its own slots.
          * `stop_exec_server` kills by the pattern `{root}/exec_server.py`;
            `WORKER_PIDS` uses `{root}/server.py`. Neither string contains the
            other, so `pgrep -f` cannot cross them. Exec has no code path that
            can signal the render worker at all.

        AND WHY IT CANNOT STARVE. The whole point is that it does not wait: a
        14-hour sequence pass keeps `ep`, `last_ready` and a live box, which is
        exactly the condition this returns on immediately. Exec no longer
        queues behind the render worker because it no longer asks it anything.

        The slow branch is only reached when there is genuinely no usable box —
        no instance, hibernated, or a deploy that never completed. In every one
        of those states there is no render in flight to protect, so going
        through `Fleet` is both necessary and safe.
        """
        fleet = self.fleet
        ep = fleet.ep
        # DELIBERATELY NOT GATED ON `last_ready`. That flag means "the RENDER
        # WORKER came up", which is a question exec does not ask and must not
        # wait on. Gating on it cost a hot requeue loop the first time this
        # shipped: after a restart that ADOPTS a running instance, `ep` is set
        # immediately but `last_ready` stays False until the first render
        # dispatch completes, so every queued exec job fell to the slow branch,
        # raised, requeued and was re-claimed — measured at roughly ten
        # requeues per second until a render happened to finish.
        #
        # An endpoint that is not `stopped_at` is an instance this broker owns
        # and can reach. If it turns out not to be deployed, `start_exec_server`
        # below says so loudly against the real box, which is a far better
        # answer than this function guessing from a flag about a different
        # process.
        if ep is not None and not fleet.stopped_at:
            return ep

        # No usable box. Exec needs one deployed, and `Fleet.ensure_ready` is
        # the only thing that may rent or wake one — but it insists on a scene,
        # because the render path always has one.
        scene = fleet.scene_path or (config.SCENE if config.SCENE.exists() else None)
        if scene is None:
            # Do NOT raise FileNotFoundError here. It is not in the transport
            # class, so it would burn an attempt and fail the job outright —
            # which is what happened at 18:38:00 when `config.SCENE` pointed at
            # a scene.blend that has never existed. "There is no box yet" is a
            # WAIT, not a verdict on the build: FleetUnavailable requeues
            # without spending an attempt, so exec simply waits for the first
            # render job to bring a box up.
            raise remote.FleetUnavailable(
                f"exec needs a deployed instance and there is none: no scene is "
                f"loaded and the default scene {config.SCENE} does not exist, so "
                f"there is nothing to deploy with. Queue a render, or set "
                f"VASTRENDER_SCENE to a real .blend.")
        return fleet.ensure_ready(Path(scene))

    def ensure_ready(self) -> None:
        """An instance, an exec server on it, and a live forward to that server.

        Everything here is idempotent and cheap in the steady state: one socket
        ping. The expensive branches only run after a hibernation, an instance
        replacement, or a broker restart — all of which end the exec server,
        because it is a process on a container that was stopped.
        """
        with self.ready_lock:
            ep = self.endpoint_without_disturbing_the_worker()

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

    def refuse_if_memory_is_short(self, spec: dict) -> None:
        """Do not start a scene-opening job the box cannot hold. See
        config.EXEC_SCENE_MEM_FACTOR for the measurement and the reasoning.

        Asks the exec SERVER rather than reading /proc over ssh, because the
        server already reports `mem_available` on every ping and that is the
        number its own children will be competing for. A server that will not
        answer is not evidence of plenty, but it is also not this check's job to
        adjudicate - it says nothing and lets the existing readiness path deal
        with an unreachable server.
        """
        if not spec.get("scene_digest") or not spec.get("scene_bytes"):
            return
        need = int(spec["scene_bytes"]) * float(config.EXEC_SCENE_MEM_FACTOR)
        try:
            pong = execremote.exec_call({"cmd": "ping"}, timeout=30)
        except Exception:
            return
        avail = float((pong or {}).get("mem_available") or 0.0)
        if avail <= 0:
            return
        if avail < need:
            raise ExecMemoryShort(
                f"opening {spec['scene_name']} ({int(spec['scene_bytes'])/1e9:.2f} GB) "
                f"needs about {need/1e9:.1f} GB free and the box has "
                f"{avail/1e9:.1f} GB — the render worker is holding a scene of "
                f"its own. Waiting rather than being OOM-killed at `Read blend`, "
                f"which would spend this job's whole retry budget in ninety "
                f"seconds and report it as a failed build.")

    def ensure_scene_staged(self, ep: remote.Endpoint, spec: dict) -> None:
        """Put this job's input scene in the instance's cache, if it asked for one.

        AND DO IT WITHOUT GOING NEAR THE RENDER WORKER. `Fleet._switch_scene`
        would also get the bytes there, and it restarts the worker to load them
        — the one thing an exec job may never cause. The scene CACHE is not the
        worker: `push_scene` writes a content-addressed directory that nothing
        reads until something opens it, so filling it while a frame renders is
        as safe as filling it while the box is idle.

        Idempotent and usually free. The overwhelmingly common case is that the
        render path already has this scene resident — which is the entire reason
        this feature is cheap — and `scene_cached` answers that in one round
        trip. It is size-verified, so a half-pushed scene is a miss, not a
        near-hit.

        `keep` protects the render worker's resident scene from being evicted to
        make room for ours. Evicting the scene a frame is currently being drawn
        from would be a spectacular way to honour "never disturb the worker".
        """
        digest = spec.get("scene_digest")
        if not digest:
            return
        name, size = spec["scene_name"], int(spec["scene_bytes"])
        if remote.scene_cached(ep, digest, size, name):
            remote.touch_scene(ep, digest)
            return
        source = Path(spec["scene_path"])
        if not source.is_file():
            raise remote.FleetUnavailable(
                f"exec job needs scene {name} ({digest[:12]}) and it is neither "
                f"on the instance nor at {source} any more")
        reserve = int(config.DISK_RESERVE_GB * 1e9)
        state = remote.disk_state(ep)
        if state.ok and state.free < size + reserve:
            remote.evict_to_fit(
                ep, keep=set(self.fleet.protected_scenes()), incoming=size,
                budget=remote.cache_budget(state, reserve), reserve=reserve,
                state=state)
        log.info("staging scene %s (%s, %.2f GB) for exec — the render worker "
                 "is not touched", name, digest[:12], size / 1e9)
        seconds = remote.push_scene(ep, source)
        # Sim caches and anything else the blend references relatively. Same
        # call the render path makes: without them the scene opens and renders
        # EMPTY rather than failing, which is the silent-wrong-output class.
        siblings = scenes.sibling_dirs_for(source)
        if siblings:
            remote.push_scene_siblings(ep, digest, source.parent, siblings)
        log.info("scene %s staged for exec in %.1fs", digest[:12], seconds)

    def _wait_out_the_frame(self) -> float:
        """Sleep off a busy render worker, HOLDING THIS JOB'S EXEC SLOT.

        Holding the slot is the point, not a side effect. Requeueing straight
        away puts the row back on a queue the dispatcher re-reads in
        milliseconds, so the job is re-claimed, refused and requeued again —
        measured at roughly ten times a second. Staying `inflight` across the
        wait means one retry per backoff period, and it is honest: the job
        really is occupying capacity, waiting for the box.

        The other eleven slots stay free, so this never blocks exec work that
        could run. Interruptible so a broker shutdown is not held up for 90 s.
        """
        deadline = time.time() + float(config.EXEC_BUSY_BACKOFF_SEC)
        started = time.time()
        while time.time() < deadline and self.broker.running:
            time.sleep(min(2.0, deadline - time.time()))
        return time.time() - started

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
            if isinstance(exc, StaleBundle):
                # Terminal on sight, and NOT by exhausting a budget. See
                # StaleBundle: retrying recomputes the same differing digest,
                # and the attempts it would spend were often already gone to a
                # fleet failure that had nothing to do with this job.
                self.db.fail_terminal(job_id, why)
                log.error("exec job %s FAILED on a STALE BUNDLE and will NOT be "
                          "retried — the code moved under it, which no number of "
                          "attempts can fix. Resubmit to build the new code: %s",
                          job_id, why)
            elif isinstance(exc, remote.DiskFull):
                # Terminal, and deliberately not retried — the same rule the
                # render path applies. The preflight measured the disk, evicted
                # what it could and found it still does not fit; three more
                # attempts measure the same bytes. Only a human can change the
                # disk or the job.
                self.db.fail_terminal(job_id, why)
                log.error("exec job %s FAILED on DISK and will NOT be retried — %s",
                          job_id, why)
            elif isinstance(exc, (remote.WorkerBusy, remote.FleetUnavailable,
                                  ExecMemoryShort)):
                # A WAIT, NOT A VERDICT. `WorkerBusy` says "a frame is in flight
                # right now" — a condition that clears on its own, usually
                # within a minute. Failing the job for it is answering "not yet"
                # with "never": three escalating attempts inside four seconds
                # and then a terminal failure, measured 2026-08-04.
                #
                # After `endpoint_without_disturbing_the_worker` this should be
                # unreachable for exec, because exec no longer asks the render
                # worker anything. It is kept as the safety net for the one race
                # that remains — the render dispatcher holding `Fleet.lock`
                # mid-scene-switch while the previous frame is still finishing —
                # and it must not consume an attempt, or a busy afternoon would
                # exhaust the retries of a job that never got to run once.
                waited = self._wait_out_the_frame()
                self.db.requeue(job_id, f"{why} [worker busy, attempt refunded]")
                log.info("exec job %s waited %.0fs and was requeued WITHOUT "
                         "spending an attempt — a render is in flight and that "
                         "is a WAIT, not a failure: %s", job_id, waited, why)
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
            raise StaleBundle(
                f"the input bundle changed between submit and dispatch: this job "
                f"was queued against {job['bundle']} and {spec['bundle_root']} now "
                f"hashes to {bundle.digest}. Resubmit if the new code is what you "
                f"want — a build filed under a request for different code is not "
                f"something this broker will do quietly."
            )
        self.refuse_if_memory_is_short(spec)
        self.ensure_scene_staged(ep, spec)
        info = execremote.push_bundle(ep, bundle,
                                      keep_scenes=self.fleet.protected_scenes())
        if not info.get("cached"):
            log.info("staged %s for exec job %s in %.1fs",
                     bundle.describe(), job_id, info.get("seconds", 0.0))

        payload = {k: spec[k] for k in (WORKER_FIELDS | WORKER_OPTIONAL) if k in spec}
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
