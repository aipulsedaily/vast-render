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


class ExecGpuRefused(RuntimeError):
    """The exec server refused this job the GPU. TERMINAL ON SIGHT.

    The mirror of `worker/exec_server.GpuContended`, and its own class for the
    same reason `StaleBundle` and `SceneStagingMismatch` have theirs: the retry
    budget is the wrong instrument. The thing the job collided with is the WARM
    RENDER WORKER, which by design holds its scene for the whole campaign — so
    three attempts buy three identical refusals, and the third one reports `3/3`
    as though a build had been tried and found wanting.

    Distinguished from `ExecMemoryShort`, which is the other "the box could not
    fit this" and is a WAIT, precisely because memory comes back and a resident
    render scene does not.

    The defect, 2026-08-07: an exec job set `cycles.device = GPU` and put a
    second 8 GB scene on the same 32 GB card as the render worker's. Another
    agent's `carhero` render died twice with `Out of memory in CUDA queue
    enqueue`, terminally the second time. Cancelling the exec job fixed the
    victim within seconds. Nothing in this broker or that server had any opinion
    about which process owned the card; both have one now.
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
        if scene is None and config.EXEC_BOOTSTRAP_SCENE.exists():
            # Nothing loaded and no render default — so there is no render
            # workload here to piggyback on, and waiting for one is waiting
            # forever. Deploy with the bootstrap scene instead; see
            # config.EXEC_BOOTSTRAP_SCENE for why this is its own knob and not
            # a default for SCENE.
            scene = config.EXEC_BOOTSTRAP_SCENE
            log.info(
                "no scene is loaded and the default scene %s does not exist, so "
                "there is no render job to bring a box up — deploying with the "
                "exec bootstrap scene %s (%.2f MB) instead of waiting for one. "
                "It exists only so an instance can be rented; the first render "
                "job switches the worker to its own scene.",
                config.SCENE, config.EXEC_BOOTSTRAP_SCENE.name,
                config.EXEC_BOOTSTRAP_SCENE.stat().st_size / 1e6)
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
                f"loaded, the default scene {config.SCENE} does not exist, and "
                f"neither does the exec bootstrap scene "
                f"{config.EXEC_BOOTSTRAP_SCENE}. With all three absent there is "
                f"nothing to deploy with and no render job can be assumed to "
                f"arrive. Point VASTRENDER_EXEC_BOOTSTRAP_SCENE at a small real "
                f".blend, or VASTRENDER_SCENE at the assembly.")
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
        self.check_deployed_code(pong)

    # The last mismatch reported, so a disagreement is logged once per version
    # pair rather than on every ping. It is a standing condition, not an event.
    _reported_code_mismatch: str = ""

    def check_deployed_code(self, pong: dict) -> None:
        """Is the exec server on the box the one in this tree? ASK, don't assume.

        THE DEFECT. On 2026-08-07 an 8 GB blend was pushed three times to the
        legacy default `/workspace/scene.blend` and refused three times, and the
        refusal that exists to make that terminal on first sight did not fire —
        because the running broker predated its own fix by nearly two hours.
        `SceneStagingMismatch`'s own message already names the symmetric case as
        its first hypothesis: "the most likely cause is a stale
        worker/exec_server.py on the box, which `ensure_ready` will NOT replace
        while the exec server is running".

        That hypothesis was never checkable. It is now: the exec server hashes
        itself at startup and reports it on every ping, this broker hashes the
        file it would have pushed, and the two are compared here — on the path
        that already runs on every adopt, so nobody has to remember to look.

        Warns, never refuses. A version skew is not automatically wrong (an
        older server that lacks a field degrades gracefully, by design, in
        several places in this file), and a broker that refused to dispatch on
        one would be a broker that stops working every time a deploy is half
        done. What it must never be is INVISIBLE.
        """
        theirs = pong.get("code_sha256")
        if not theirs:
            # An older exec server does not report it. Say so once, because
            # "the field is missing" is itself evidence of an old deploy.
            if self._reported_code_mismatch != "absent":
                self._reported_code_mismatch = "absent"
                log.info("the exec server on this instance does not report its "
                         "own code hash — it predates the deployed-vs-tree "
                         "check, which is itself a version skew worth knowing")
            return
        src = Path(__file__).resolve().parent.parent / "worker" / "exec_server.py"
        try:
            ours = hashlib.sha256(src.read_bytes()).hexdigest()[:12]
        except OSError:                                        # pragma: no cover
            return
        key = f"{ours}!={theirs}"
        if ours == theirs:
            self._reported_code_mismatch = ""
            return
        if self._reported_code_mismatch == key:
            return
        self._reported_code_mismatch = key
        log.warning(
            "DEPLOYED CODE DOES NOT MATCH THE TREE: the exec server on this "
            "instance is sha256:%s and %s is sha256:%s. `ensure_ready` will not "
            "replace a RUNNING exec server, so this will not fix itself — stop "
            "it explicitly on an idle box if the difference matters. A fix in "
            "the tree and not on the box is a fix that does not exist; this is "
            "the line that says which one you have.",
            theirs, src, ours)

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
        # THE RENDER PATH MAY ALREADY BE PUSHING THESE EXACT BYTES. Checked
        # after `scene_cached` and before anything expensive, because it is the
        # cheapest possible answer — one dict lookup of local state, no SSH.
        #
        # A PRECONDITION IN PREFERENCE TO A RETRY. The failure it replaces is
        # not a classification problem, it is two 8 GB streams contending for
        # one uplink: on 2026-08-07 `Fleet._deploy` was 189 s into pushing
        # film16_breach.blend when this method started pushing the same digest,
        # and the second stream was reset in twenty seconds
        # (`ssh: connect to host ... Connection timed out`). Refunding that
        # attempt is right and is now done, but the push should never have been
        # started: the bytes were 89 s from being resident, and the duplicate
        # was also stealing bandwidth from the deploy it was waiting on.
        #
        # `FleetUnavailable` rather than a local sleep, so the wait is visible in
        # the queue and bounded by the backoff every other wait uses. Content
        # addressing is what makes this safe to skip on: a matching digest is
        # the same assembly, so the push in flight is not merely similar work,
        # it is THIS work.
        staging = self.fleet.staging_digest()
        if staging == digest:
            raise remote.FleetUnavailable(
                f"the render path is already pushing scene {name} "
                f"({digest[:12]}) to {ep} — this exec job wants the same bytes, "
                f"by content, and a second stream up the same uplink does not "
                f"halve the bandwidth, it times out. Waiting for the push in "
                f"flight to land, which is not a verdict on this build.")
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
        # `stage_scene_tree` AND NOT A SECOND HAND-ROLLED SEQUENCE. This is
        # where the defect was. The three lines that used to be here were
        # `push_scene(ep, source)` — no `remote_path`, so the blend went to
        # `push_scene`'s legacy default `/workspace/scene.blend` — followed by
        # `push_scene_siblings`, which DID write to the content-addressed
        # directory, and then nothing. No `.complete` was ever written by this
        # path, by anyone, for any scene.
        #
        # So a scene that reached an instance via `rq exec` was unusable by
        # `rq exec`, while the same scene on an instance where a RENDER job had
        # pushed it worked perfectly — because `Fleet._ensure_scene_cached` did
        # the full sequence. The feature appeared to work for as long as exec
        # only ever ran on boxes the render path had already warmed.
        #
        # Sim caches matter here for the same reason they matter to the render
        # path: without them the scene opens and renders EMPTY rather than
        # failing, which is the silent-wrong-output class. They travel inside
        # `stage_scene_tree`, before the marker, where they cannot be forgotten.
        #
        # No `on_phase`: `Fleet.status` narrates the RENDER worker, and an exec
        # job that reported itself as "uploading-scene" there would read in
        # `rq status` as the render path doing something it is not doing. The
        # exec staging lines above and below are this path's own narration.
        siblings = scenes.sibling_dirs_for(source)
        seconds, files, cache_bytes = remote.stage_scene_tree(
            ep, source, digest, siblings)
        log.info("scene %s staged for exec in %.1fs — %s, %d sibling cache "
                 "file(s) (%.1f MB), and %s written last and verified readable "
                 "by the same check the exec server makes",
                 digest[:12], seconds, remote.scene_cache_path(digest, name),
                 files, cache_bytes / 1e6, remote.SCENE_COMPLETE)

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
        return self._hold_the_slot_and_wait(float(config.EXEC_BUSY_BACKOFF_SEC))

    def _hold_the_slot_and_wait(self, seconds: float) -> float:
        """Back off for `seconds` without giving up this job's exec slot.

        Factored out of `_wait_out_the_frame` so the TRANSPORT branch can have
        it too, because a refund without a backoff is not a fix — it is a
        different bug. `db.requeue` puts the row straight back on a queue
        `loop()` re-reads every second, so an immediately-requeued job is
        re-claimed, meets the same unfinished deploy, and requeues again. That
        spin is already documented at `config.EXEC_BUSY_BACKOFF_SEC` and was
        measured at ~10 requeues per second the first time the busy path shipped
        without a wait; the transport path shipped without one from the start
        and only escaped the same fate because it was, until now, nearly
        unreachable.
        """
        deadline = time.time() + max(seconds, 0.0)
        started = time.time()
        while time.time() < deadline and self.broker.running:
            time.sleep(min(2.0, max(deadline - time.time(), 0.0)))
        return time.time() - started

    def _run_guarded(self, job: dict, spec: dict) -> None:
        job_id = job["id"]
        try:
            self.run_one(job, spec)
        except Exception as exc:
            why = remote.diagnose(exc)
            self.last_error = why
            # A CANCELLED JOB IS NOT RETRIED, AND IS NOT REPORTED AS ANYTHING
            # ELSE. Every writer below already fails safe — `fail`,
            # `fail_terminal` and `requeue` are all guarded on `state='running'`
            # and a cancelled row is terminal — so this changes no state. What
            # it changes is the story: without it, a cancel taken mid-child
            # comes back through the transport branch and logs "requeued WITHOUT
            # spending an attempt" about a job that was not requeued, or through
            # the last branch and logs the deliberate stop at ERROR. Both read
            # in `rq status -v` as a build that broke.
            #
            # And it is the SECOND half of what `rq cancel` has to mean.
            # MAX_ATTEMPTS is 3, so before the row could go terminal a job that
            # was cancelled mid-run would be re-dispatched — measured today by
            # the r2851ab agent, whose crashed 12-minute build was automatically
            # run a second time in full against code already known to be broken.
            # Stopping this attempt and not the next one is not a cancellation.
            row = self.db.get(job_id)
            if row and row["state"] == "canceled":
                log.info("exec job %s was canceled — not retried, not failed. "
                         "The attempt ended with: %s", job_id, why)
                return
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
            elif isinstance(exc, remote.SceneStagingMismatch):
                # TERMINAL, AND THE WHOLE POINT IS THAT IT COSTS ONE PUSH.
                #
                # Job dea2b1d24914, 2026-08-07, 07:32-07:37: the exec staging
                # path pushed film16_R2851.blend (7.97 GB) to instance 47049525
                # and logged "staged for exec in 108.0s"; the exec server then
                # refused it as "not completely staged". Because the refusal
                # arrived as a plain `RuntimeError` it landed in the `else`
                # branch below, `db.fail` spent an attempt, and the dispatcher
                # re-claimed the job — three 8 GB pushes and three identical
                # refusals over five and a half minutes, ending in a terminal
                # failure whose message described a half-pushed blend. The blend
                # was not half-pushed. It was whole, at a path nothing read.
                #
                # Retrying is not merely useless here, it is actively
                # misleading: the retries are what made a fixed, deterministic
                # path bug look like a flaky transfer. `stage_scene_tree` now
                # raises this the moment its own read-back disagrees, so this
                # branch is reached after ONE push, and the message names both
                # paths so nobody has to infer them from a stack trace.
                self.db.fail_terminal(job_id, why)
                log.error(
                    "exec job %s FAILED on a SCENE STAGING MISMATCH and will "
                    "NOT be retried. The push succeeded and the readiness check "
                    "disagreed — that is a bug in this broker, not a bad "
                    "transfer, and re-pushing gigabytes to reproduce it is what "
                    "this refusal exists to prevent. %s", job_id, why)
            elif isinstance(exc, ExecGpuRefused):
                # TERMINAL, AND ON THE FIRST REFUSAL. See ExecGpuRefused: the
                # render worker holds its scene for the whole campaign, so a
                # retry is not "later", it is the same collision with a longer
                # log. Logged at ERROR with the server's own sentence, which
                # names the scene and the card, because the caller's next move
                # is to resubmit without `--gpu` or as a render job and neither
                # is guessable from "the job failed".
                self.db.fail_terminal(job_id, why)
                log.error("exec job %s was REFUSED THE GPU and will NOT be "
                          "retried — the card is held by something this broker "
                          "did not put there, and three attempts would find it "
                          "held three times: %s", job_id, why)
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
                # Do not name the cause here — this branch covers three of
                # them. "A render is in flight" was hardcoded, and it was
                # already wrong for ExecMemoryShort (the box is short of
                # memory) and is wrong again for FleetUnavailable (Blender is
                # not installed yet, because the deploy is still running). The
                # honest sentence is the one about the VERDICT, which is the
                # same for all three; `why` carries which.
                log.info("exec job %s waited %.0fs and was requeued WITHOUT "
                         "spending an attempt — this is a WAIT, not a verdict "
                         "on the build: %s", job_id, waited, why)
            elif isinstance(exc, remote.RemoteError):
                # EVERYTHING `remote` RAISES IS ABOUT THE BOX OR THE WIRE, AND
                # NOTHING IT RAISES IS ABOUT THE CALLER'S CODE. That is the rule,
                # and it is stated as THE BASE CLASS on purpose.
                #
                # This branch used to name four subclasses, and on 2026-08-07 the
                # two conditions that actually occurred were not among them:
                #
                #   * A bare `RemoteError`. `execremote` raises the base class at
                #     six sites — the exec server exiting immediately after
                #     launch, a survivor of `stop_exec_server`, a port still
                #     bound, a tunnel that exited or never bound, a server that
                #     never answered a ping. Every one of those is the box, and
                #     every one of them fell through to `fail()`. Job
                #     b0d427488e0f, 03:26:41-03:26:53: three attempts in twelve
                #     seconds against a Blender bundle that was still uploading,
                #     `failed` at 3/3 having never executed a line of its own
                #     code. The install finished seventeen seconds later.
                #   * `TransferError` — a dropped bulk push, or a fetch that came
                #     back the wrong length or the wrong bytes. Also unlisted.
                #     Job 88de1f4d5faf, 03:30:44: `scene push failed after 20.0s`,
                #     charged to the build. "A failed transfer must not destroy
                #     the instance" is already law here; a failed transfer must
                #     not destroy the job's retry budget either, and for exactly
                #     the same reason — THE TRANSFER IS NOT THE WORK.
                #
                # An allowlist of subclasses is the wrong shape for this. It
                # fails OPEN, in the expensive direction, every time a failure
                # mode is given a type of its own or raised as the base — and the
                # cost of failing open is a terminal verdict on somebody's build.
                # The base class fails CLOSED: a new transport type is refunded
                # the day it is invented.
                #
                # Nothing that IS the build's fault can reach here as a
                # `RemoteError`. A child that came back `ok: false` is raised by
                # `run_one` as a plain `RuntimeError` carrying the tail of its
                # log, and `worker_call` returns non-ok replies rather than
                # raising on them. The two `RemoteError`s that are NOT waits are
                # handled above this line and stay there: `DiskFull`, because
                # retrying cannot create space, and `StaleBundle`, because
                # retrying recomputes the same differing digest.
                waited = self._hold_the_slot_and_wait(
                    float(config.EXEC_TRANSPORT_BACKOFF_SEC))
                self.db.requeue(job_id, f"{why} [transport, attempt refunded]")
                log.warning("exec job %s waited %.0fs and was requeued WITHOUT "
                            "spending an attempt — this is the box or the wire, "
                            "not the build: %s", job_id, waited, why)
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
        # GETTING A BOX IS NOT THE BUILD, AND ITS FAILURES ARE NOT THIS JOB'S.
        #
        # `_run_guarded` refunds the attempt for everything `remote` raises,
        # on the stated rule that transport is never the caller's code. That
        # rule was right and the implementation had a hole: `ensure_ready` ends
        # up in `Fleet.ensure_ready`, and the resume path there raises
        # `vastctl.NotReachable` — a `VastError`, which is not a `RemoteError`
        # and never was. So it fell past every refunded branch into the final
        # `else` and spent one of three attempts.
        #
        # Measured 2026-08-07: instance 47040457 hibernated at 04:30, and
        # vast.ai then would not act on `start_instance` — `actual=exited,
        # intended=stopped` through three calls, 902 s per resume attempt. Job
        # 5534329f168f (agent occ-all6, a 2.41 GB bundle) was claimed at
        # 05:04:52, waited out the whole timeout, and came back
        # `attempts=2` with `err=NotReachable` — two thirds of its retry budget
        # gone to a control-plane fault, without a line of its own code having
        # run. A third would have written `failed` on it for good.
        #
        # The render dispatcher already does exactly this, at
        # `app.py:746` — every non-`WorkerBusy`/`FleetUnavailable`/`DiskFull`
        # exception out of `acquire_worker` is re-typed as `FleetUnavailable`
        # so a sequence stops and requeues instead of blaming the frame. This
        # is the same wrapper on the same reasoning, and it is put HERE rather
        # than in `_run_guarded` for the same reason it is there: the fix
        # belongs where the fleet is asked for hardware, so it covers whatever
        # `Fleet` raises next without needing a new name added to a list.
        #
        # `FleetUnavailable` is a refunded WAIT, not a verdict — the job goes
        # back on the queue and the next claim meets a replacement instance.
        try:
            self.ensure_ready()
        except (remote.WorkerBusy, remote.FleetUnavailable, remote.DiskFull):
            # Already correctly typed. DiskFull in particular must stay
            # terminal: re-typing it here would requeue forever against a disk
            # that cannot grow.
            raise
        except Exception as exc:
            raise remote.FleetUnavailable(
                f"no instance available for exec job {job_id}: "
                f"{remote.diagnose(exc)}"
            ) from exc
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

        # A FALSE DECLARATION IS SENT AS SILENCE, AND THAT IS DELIBERATE.
        #
        # `worker/exec_server.validate` REJECTS unknown spec fields on purpose —
        # "this server holds no job policy, so a field it does not understand is
        # a client bug". That rule is right, and it makes every new optional
        # field a deploy-order hazard in exactly the direction this whole
        # incident is about: `ensure_ready` will not replace a RUNNING exec
        # server, so a freshly restarted broker can be talking to a server from
        # before `gpu` existed. Sending `gpu: false` to that server would refuse
        # EVERY exec job on the box, on a field whose whole meaning is "carry
        # on as before".
        #
        # Omitting it costs nothing, because the server defaults it to False and
        # the clamp is applied on absence exactly as on `false`. The declaration
        # only ever needs to travel when it is TRUE — which is when it is also
        # true that an old server, unable to honour it, must refuse rather than
        # silently run the job on the card.
        if not payload.get("gpu"):
            payload.pop("gpu", None)

        # LAST CHECK BEFORE THE CHILD EXISTS. Everything above this line —
        # ensure_ready, the memory refusal, staging an 8 GB scene, pushing the
        # bundle — is minutes of work during which the job is `inflight` here
        # and completely unknown to the exec server, so `ExecService.cancel`
        # has nothing to signal and plants a tombstone instead. Reading the row
        # here catches the same window from this side and, unlike the
        # tombstone, catches it before the pushes rather than after. The row is
        # flipped terminal by the endpoint before it signals anything, so this
        # ordering is what makes the check meaningful.
        row = self.db.get(job_id)
        if row and row["state"] == "canceled":
            log.info("exec job %s was canceled while its inputs were being "
                     "staged — not dispatching it to the instance", job_id)
            return

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

        # A CANCEL IS NOT A FAILURE AND MUST NOT BE RETRIED. The exec server
        # marks it on the reply, exactly as it marks a `wait`, and for the same
        # reason: `fail()` on a cancelled row is already a no-op, but it would
        # log the deliberate stop at ERROR and read in `rq status -v` as a build
        # that broke. Returning here also releases the job directory, which the
        # error paths do not.
        if reply.get("canceled"):
            log.info("exec job %s: the instance confirms the cancel — %s",
                     job_id, reply.get("error") or "child stopped")
            with contextlib.suppress(Exception):
                execremote.exec_call({"cmd": "release", "job_id": job_id},
                                     timeout=120)
            return

        if not reply.get("ok"):
            # THE FAR SIDE IS ALLOWED TO SAY "NOT YET". `wait: true` means the
            # exec server refused ADMISSION — it never staged the bundle and
            # never forked a child, so there is no verdict on the caller's code
            # to report, only a box that could not afford the job at that
            # moment. See `worker/exec_server.py:ResourceWait`.
            #
            # Believing the far side, exactly as `adopt_slots` does with the
            # slot count. Everything the broker knows about what happened on the
            # box comes from this reply, and a distinction the server drew and
            # this line discarded is a distinction that does not exist. Job
            # 88de1f4d5faf and job 2a7e2a119e60, 03:43 on 2026-08-07: both
            # charged an attempt for `waited 602s for 20.0G of free memory and
            # only 3.7G was ever available`, which was the render worker and
            # eleven sibling builds holding the box, and was nothing whatever to
            # do with either build.
            #
            # An OLDER exec server does not send this field. It reads as absent,
            # the job spends an attempt exactly as it does today, and nothing
            # regresses — the field only ever turns a failure into a wait.
            if reply.get("wait"):
                raise ExecMemoryShort(
                    f"exec job {job_id} was not admitted by the exec server: "
                    f"{reply.get('error') or 'no reason given'}")
            # THE FAR SIDE REFUSED THE CARD, AND THAT IS NOT A BUILD FAILURE
            # EITHER. Same shape as `wait` above and the same reason for
            # believing the far side: the exec server is the only thing that can
            # see who holds the GPU, it looked, and it named what it found. An
            # older exec server does not send this field, in which case the job
            # is failed exactly as it is today and nothing regresses.
            if reply.get("gpu_refused"):
                raise ExecGpuRefused(
                    f"exec job {job_id} was refused the GPU by the exec server: "
                    f"{reply.get('error') or 'no reason given'}")
            err = reply.get("error") or "no reason given"
            # THE INSTANCE SAYS THE SCENE IS NOT STAGED, AND THIS SIDE JUST
            # STAGED IT AND VERIFIED IT. That is not a build failure and it is
            # not a transport failure; it is the reader and the writer looking
            # at different places, and it is the exact condition that cost three
            # 8 GB pushes on job dea2b1d24914. Re-typed here so it is terminal
            # on the FIRST refusal instead of the third.
            #
            # `ensure_scene_staged` ran minutes ago in this same call, and it
            # either found the scene cached or pushed it and read it back
            # through `scene_cached`. So reaching this line means the broker's
            # own predicate and the exec server's predicate disagree — most
            # likely an exec server binary older than this broker, which is a
            # live possibility because the two deploy separately.
            if remote.NOT_STAGED_MARK in err and spec.get("scene_digest"):
                raise remote.SceneStagingMismatch(
                    f"exec job {job_id}: this broker staged scene "
                    f"{spec.get('scene_name')} ({str(spec['scene_digest'])[:12]}) "
                    f"to {remote.scene_dir(spec['scene_digest'])} and verified it "
                    f"readable, and the exec server on the instance still says: "
                    f"{err}. The push is NOT being repeated — it would land in "
                    f"the same place and be refused for the same reason. Two "
                    f"things read this cache and they do not agree; the most "
                    f"likely cause is a stale worker/exec_server.py on the box, "
                    f"which `ensure_ready` will NOT replace while the exec "
                    f"server is running — stop it explicitly on an idle box.")
            tail = (reply.get("log") or "").strip().splitlines()[-12:]
            raise RuntimeError(
                f"exec job {job_id} failed: {err}"
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

        # A CLAMP THAT NOBODY EVER READS IS A SILENT DOWNGRADE. The exec server
        # gives every undeclared job an empty CUDA_VISIBLE_DEVICES, which is
        # what keeps a build off the render worker's card — but a build that
        # WANTED the card and quietly got the CPU is a wrong-looking-right
        # result, and this project's whole defect log is that shape. So the
        # files that asked are named here, at WARNING, against the job id.
        hints = reply.get("gpu_hints") or []
        if hints and reply.get("gpu_clamped"):
            log.warning("exec job %s ran CPU-ONLY though %d file(s) in its bundle "
                        "select a GPU device: %s. Exec jobs are clamped with "
                        "CUDA_VISIBLE_DEVICES='' unless they declare gpu, because "
                        "the render worker's scene is resident on that card. If "
                        "this job really needs the GPU, submit it with --gpu and "
                        "it will be refused by name when the worker holds the "
                        "card rather than racing it for VRAM.",
                        job_id, len(hints), ", ".join(map(str, hints[:6])))

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

    # --- cancellation ----------------------------------------------------

    def cancel(self, job_id: str) -> dict:
        """Actually stop a dispatched exec job on the instance.

        THE HALF `rq cancel` NEVER HAD. `DELETE /jobs/{id}` flipped a SQLite row
        and returned `{"canceled": true}`; there was no code path from it to the
        process. Measured on instance 47040457 on 2026-08-07: job a39bd71095f9
        was cancelled at 03:46, reported cancelled, and its Blender child kept
        running until its own `timeout_s` expired at 04:44 — 58 minutes holding
        6 of 12 exec slots and ~8 GB of a loaded assembly. In that same window
        two of another agent's jobs were refused by the memory gate (`only
        10.8G was ever available`, `only 3.7G`) and a third was OOM-killed
        immediately after `Read blend`. The row said cancelled; the box said
        otherwise, and the box is what other jobs run on.

        WHAT IS TARGETED, AND WHAT IS NOT. This sends `{"cmd": "cancel",
        "job_id": ...}` and nothing else. The exec server resolves that id to
        the `Popen` it created and kills that process's own group. No pattern,
        no `pgrep`, no name. Two entries in this project's defect log say why
        that matters and both are about this exact box: a `pkill -f` pattern
        matching the *remote* command line killed the render worker holding the
        warm scene, and killing by name is how a stale worker silently served
        the previous scene. `reap_orphans` is the precedent for the standard —
        "the honest signal is the working directory" — and it cannot be borrowed
        here, because every live exec child has a cwd inside the exec root, so
        it identifies the whole population rather than one member. A job id is
        strictly narrower than a cwd.

        NEVER RAISES. A cancel that fails because the tunnel is down must still
        leave the row cancelled — the caller asked for the job to stop, and
        refusing the whole request because the box is unreachable would be
        answering "I could not confirm it" with "I did nothing". It reports what
        happened instead, and an unreachable exec server is a server that is
        about to be restarted, which runs `reap_orphans` and collects the child
        anyway.

        WHO IS ASKED IS THE EXEC SERVER, NOT `self.inflight`. That dict is this
        broker's *belief* about what is running, and this entire defect class is
        "the row believed something the box did not". It can be wrong in the one
        direction that matters: a job whose `_run_guarded` thread has already
        exited — its socket timed out, its tunnel was reset, the broker was
        restarted and re-adopted the instance — is gone from `inflight` while its
        child runs on. Observed on 47040457 at 04:20 on 2026-08-07, and it is the
        worst case rather than an edge one, because an orphan nothing owns is
        exactly the one no other mechanism will collect: `rq status` showed one
        exec job in flight and the exec server's own ping showed a different one,
        `6f0e2c1d110a`, still holding 6 of 12 slots. Trusting `inflight` here
        would make that job the one kind of orphan `rq cancel` cannot touch.

        So the local view is reported and not obeyed. The remote call is skipped
        only when the row proves the job was NEVER dispatched — `attempts == 0`,
        meaning no dispatcher ever claimed it, meaning no child can exist. In
        every other case the exec server is asked, and it answers `running:
        false` harmlessly if it does not know the id.
        """
        with self.lock:
            info = self.inflight.get(job_id)
        row = self.db.get(job_id) or {}
        attempts = int(row.get("attempts") or 0)
        if info is None and attempts == 0:
            # Never claimed by any dispatcher, so no child can exist for it.
            # Cancelling the row is the whole of the cancellation.
            return {"dispatched": False, "signalled": False,
                    "detail": "never dispatched — no attempt was ever claimed, "
                              "so nothing can be running on the instance"}
        if self.tunnel is None or self.tunnel.poll() is not None:
            log.warning("exec job %s cancel: no live tunnel to the exec server, so "
                        "the child could not be signalled. The row is cancelled; "
                        "the child will be reaped by reap_orphans when the exec "
                        "server next starts.", job_id)
            return {"dispatched": info is not None, "signalled": False,
                    "error": "no live tunnel to the exec server",
                    "detail": "the job row is cancelled but the child on the "
                              "instance could NOT be signalled"}
        try:
            reply = execremote.exec_call({"cmd": "cancel", "job_id": job_id},
                                         timeout=120)
        except Exception as exc:
            why = remote.diagnose(exc)
            log.warning("exec job %s cancel: the exec server could not be reached "
                        "(%s) — the row is cancelled but the child was not "
                        "signalled", job_id, why)
            return {"dispatched": info is not None, "signalled": False,
                    "error": why,
                    "detail": "the job row is cancelled but the child on the "
                              "instance could NOT be signalled"}
        log.info("exec job %s canceled on the instance: %s", job_id,
                 reply.get("detail") or reply)
        return {"dispatched": info is not None, "signalled": True,
                "killed": bool(reply.get("killed")),
                "was_running": bool(reply.get("running")),
                "pid": reply.get("pid"), "pgid": reply.get("pgid"),
                "detail": reply.get("detail"),
                "cpu_slots": (info or {}).get("cpu_slots")}

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
