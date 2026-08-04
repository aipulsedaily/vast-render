#!/usr/bin/env python3
"""The middle server.

Agents submit render jobs here; this process keeps exactly one warm GPU and
feeds it. Agents never touch the vast.ai API themselves — it rate-limits per
endpoint *and per client IP*, returns 429 with no Retry-After, and publishes no
thresholds. One poller here, fifty clients served from cached state.

Dispatch runs on a worker thread rather than the event loop because every
remote operation (ssh, scp, socket render) is blocking; HTTP stays responsive
on asyncio while renders take minutes.

Deliberately absent: deduplication. Two agents requesting identical parameters
get two renders. A params hash cannot observe scene state, so collapsing them
would silently serve a stale frame after a reassembly — the exact failure the
scene project's own notes warn about.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import (config, diagnostics, execremote, execservice, imgstat, remote,
               scenes, seq)
from .db import DB, TERMINAL
from .fleet import Fleet
from .lock import BrokerAlreadyRunning, SingleInstanceLock

# Safe here and only here: importing .fleet above has already put vastctl/ on
# sys.path. Used by /teardown to name the cards this broker cannot see.
import vastctl  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("broker")

# Before anything can fail. Two brokers died leaving an empty log and a running
# instance; from here every in-process death names itself, and the *absence* of
# such a line is itself the diagnosis (SIGKILL). See broker/diagnostics.py.
diagnostics.install("import")

# Process-wide. Acquired before any fleet call and held until the process dies.
LOCK = SingleInstanceLock(config.LOCK_PATH)

REQUIRED_SPEC = {
    "camera", "resolution", "samples", "engine", "denoiser", "denoise_gpu",
    "use_dof", "film_transparent", "border", "zoom", "exposure",
    "max_bounces", "adaptive_threshold",
    # A still says frame: null. A sequence job overwrites this per frame.
    "frame", "persistent_data", "require_caches",
}

# Consecutive frame failures inside one range before the job gives up on the
# range as a whole. One bad frame must not strand the 2,900 after it; a hundred
# bad frames in a row is not a frame problem and burning a day of GPU on it is
# the expensive mistake.
FRAME_FAIL_STREAK = 5


class BlankOutput(RuntimeError):
    """The render succeeded and produced an image with nothing in it.

    Its own class because it must not be retried. Every other failure this
    broker recognises is transport, scheduling or a dropped socket — try again
    and it may well work. A camera pointed at empty space renders black three
    times in a row for three times the money, so a still that comes back blank
    is failed terminally and the caller is told which flag overrides it.
    """


class JobRefused(RuntimeError):
    """The worker rejected the request itself, and will reject it again.

    Sibling of `BlankOutput`, and terminal for the same reason: it is a verdict
    about the job, not a failed attempt at it. The worker raises
    `server.Refused` for a spec it will not honour — an over-budget pixel
    count, a border that is not a rectangle, a frame whose physics caches would
    be simulated rather than read — and marks the reply `terminal`.

    Retrying a verdict cannot converge. Measured 2026-08-03: four separate jobs
    each logged the identical refusal three times before failing, and each of
    those attempts pulled a scene switch in behind it. Retry is for transport.
    """


class Broker:

    # Class-level defaults so EVERY construction path has them, including
    # `Broker.__new__` — which `test_broker.stub_broker` uses deliberately to
    # build a broker without a fleet or a network. Setting these only in
    # __init__ would make the veto bookkeeping an AttributeError under exactly
    # the harness that is supposed to prove it works.
    _veto_scene: Optional[str] = None
    _veto_since: float = 0.0
    _veto_promised: float = 0.0
    def __init__(self) -> None:
        config.ensure_dirs()
        self.db = DB(config.DB_PATH, default_scene=str(config.SCENE))
        self.fleet = Fleet()
        self.fleet.on_teardown = self.bank_spend
        # The fleet decides what to evict; only the broker knows what is still
        # wanted. Injected rather than given the fleet a queue of its own.
        self.fleet.scene_demand = self.demanded_scene_hashes
        self.running = True
        self.paused: Optional[str] = None
        self.last_work = time.time()
        self.thread: Optional[threading.Thread] = None
        self.hb_thread: Optional[threading.Thread] = None
        self.prog_thread: Optional[threading.Thread] = None
        self.exec_thread: Optional[threading.Thread] = None
        # Arms the destroy-on-exit path. False until start() has completed, so
        # an aborted startup can never destroy an instance — including one its
        # own dispatch thread may already have adopted.
        self.started = False
        # The job the dispatch thread is currently blocked on, so the progress
        # poller knows what it is observing. A plain reference assignment needs
        # no lock, and the poller tolerates it being None or momentarily stale.
        self.current_job: Optional[str] = None
        # The id the WORKER knows this render by. For a still it equals
        # current_job; for a frame in a sequence it is `<job>_f000123`, because
        # every frame is a separate render on the instance with its own PNG and
        # its own progress. progress.json reports this one, the database row is
        # keyed by the other, and conflating them makes the poller silently
        # discard every progress update of every animation job.
        self.current_key: Optional[str] = None
        self._stall_warned: dict[str, float] = {}
        # When the idle timer first failed to find out what the GPU was doing.
        # Bounds how long "I could not ask" may block a hibernate.
        self.idle_unknown_since: Optional[float] = None
        # Consecutive jobs served for the currently loaded scene, bounding how
        # long one scene can hold the worker. See next_job.
        self.scene_batch = 0
        # `cheaper_to_finish` veto bookkeeping: which scene it is
        # currently vetoing for, when that began, and the drain it
        # promised at the time. See config.SCENE_VETO_GRACE.
        self._veto_scene: Optional[str] = None
        self._veto_since = 0.0
        self._veto_promised = 0.0
        # Offers/machines that failed to come up, restored from the database so
        # a restart does not walk straight back into the host that just failed.
        self._blacklist_seen: dict = {"offers": {}, "machines": {}}
        # Frames the current dispatch pass has actually delivered. A pass that
        # got work done did not "fail" in the sense the retry budget means,
        # however it ended. See run_job.
        self.pass_delivered = 0
        # CPU work on the same rented box, dispatched EXEC_SLOTS at a time on a
        # thread of its own. Constructed last, because it holds references to
        # `self.db` and `self.fleet`.
        self.execsvc = execservice.ExecService(self)

    # --- dispatch --------------------------------------------------------

    def dispatch_loop(self) -> None:
        log.info("dispatcher started — scene %s", config.SCENE)
        # Before anything can try to forward: clear tunnels left by a previous
        # broker. `kill -9` is the only sanctioned restart, so a killed broker
        # CANNOT clean up its own `ssh -L` — the orphan holds the local port and
        # the next deploy dies with `Address already in use`. That was being
        # charged to the rented host, which is on the wrong side of the wire.
        stale = remote.reap_stale_tunnels(self.fleet.local_port)
        if stale:
            log.warning("reaped %d orphaned ssh forward(s) on local port %d left "
                        "by a previous broker — kill -9 cannot clean up after "
                        "itself, so this is expected after every restart",
                        stale, self.fleet.local_port)
        self.reclaim_orphans()
        self.load_blacklist()
        # Only on a genuinely fresh start. This runs again if the thread is ever
        # restarted, and re-adopting an instance this process already owns would
        # restart its spend clock and re-classify every *other* instance as a
        # stray to destroy — reconciliation is a startup act, not a retry.
        if self.fleet.instance_id is None:
            try:
                self.fleet.adopt_or_reap()
            except remote.ForeignBroker as exc:
                # Pause rather than rent around it: a second instance would bill
                # alongside the first, and this broker has nothing useful to do
                # while another one owns the hardware. Nothing was adopted, so
                # nothing can be destroyed on the way out.
                self.pause(remote.diagnose(exc))
            except Exception as exc:
                log.error("adopt/reap failed: %s", remote.diagnose(exc))
        self.restore_spend_after_adopt()
        self.dispatch_forever()

    def restore_spend_after_adopt(self) -> None:
        """Carry an adopted instance's earlier spend into the new process.

        Adoption resets every per-instance counter to zero — `started_at`
        becomes now — so a broker restarted six hours into a batch believed the
        GPU it had just taken over had cost nothing. Across a multi-day
        sequence, which restarts the broker several times, that made the
        cumulative cap unreachable.

        Reconstructed from the checkpoint the previous process was writing every
        minute: dollars back into seconds at the same hourly rate. It attributes
        the disk component to GPU time as well, so the result is a slight
        OVER-estimate — the correct direction of error for a spend cap.
        """
        live = self.db.get_meta("live_spend", {}) or {}
        if not isinstance(live, dict):
            return
        usd = float(live.get("usd") or 0.0)
        dph = self.fleet.dph or float(live.get("dph") or 0.0)
        if not usd or not dph or live.get("instance") != self.fleet.instance_id:
            return
        seconds = usd / dph * 3600.0
        if seconds > self.fleet.gpu_seconds:
            self.fleet.gpu_seconds = seconds
            log.warning(
                "adopted instance %s had already spent $%.3f before this broker "
                "started — carrying it forward (%.1f min) so the $%.2f cap still "
                "means what it says",
                self.fleet.instance_id, usd, seconds / 60, self.spend_cap(),
            )

    def reclaim_orphans(self) -> int:
        """Put every `running` row back on the queue.

        Run at startup *and* whenever the dispatch thread is restarted, because
        both mean the same thing: nothing in this process is executing a job, so
        a row claiming otherwise is stranded until its hour-long lease lapses.
        Requeueing a job the instance is genuinely still rendering is safe and
        intended — the next claim meets `WorkerBusy`, reattaches, and collects
        the frame rather than re-rendering it.
        """
        reclaimed = self.db.requeue_all_running()
        if reclaimed:
            log.warning("requeued %d job(s) orphaned by a previous broker", reclaimed)
        return reclaimed

    def dispatch_forever(self) -> None:
        while self.running:
            try:
                self.dispatch_once()
            except Exception as exc:
                log.exception("dispatcher error: %s", remote.diagnose(exc))
                time.sleep(5)

        log.info("dispatcher stopping")

    def dispatch_once(self) -> None:
        """One pass of the dispatch loop: enforce the cap, wind down idle
        hardware, and run at most one job."""
        # A hard ceiling in software, because vast.ai offers none.
        # Must be cumulative: fleet.spend covers only the *current*
        # instance and resets on every teardown and adoption, so a run
        # that rents ten times could spend ten times the cap without
        # ever tripping it. The persisted total is the real ceiling.
        cap = self.spend_cap()
        if self.spent() > cap:
            self.pause(f"cumulative spend ${self.spent():.2f} hit the "
                       f"${cap:.2f} cap — raise it deliberately with "
                       f"`rq budget --set N` and `rq resume`")

        if self.paused:
            # A paused broker must still wind hardware down. pause() tears
            # down a live *endpoint*, but an instance without one — a stopped
            # instance adopted while over the cap, or one whose deploy never
            # produced an endpoint — used to be skipped here forever: this
            # `continue` sat in front of maybe_idle_down, so the hibernation
            # deadline could never fire and a stopped container (which runs
            # no watchdog) billed storage until a human noticed.
            self.maybe_idle_down()
            time.sleep(2)
            return

        self.db.requeue_expired()
        job = self.next_job()

        if job is None:
            self.maybe_idle_down()
            time.sleep(1.0)
            return

        self.run_job(job)
        # Stamped *after* the job, not before. Stamping at claim time
        # means a long render returns with the idle clock already past
        # the grace period, and the instance is destroyed the instant it
        # finishes — right before the next job needs it.
        self.last_work = time.time()

    def settle(self, current: str, lease: float) -> Optional[dict]:
        """Give a just-drained scene a moment to produce more work.

        Returns the job if one arrives inside config.SCENE_DRAIN_GRACE_SEC,
        else None — meaning the scene really is finished and switching away
        from it is worth paying for.

        Exists because every client here is SERIAL: `r5090` blocks until its
        render returns, so the next camera in a sweep is submitted a second or
        two after the previous one lands. A dispatcher that gives up the
        instant the queue reads empty lands squarely in that gap and buys a
        scene switch between every pair of one client's jobs. See
        config.SCENE_DRAIN_GRACE_SEC for the incident that measured it.

        Three properties keep the wait from ever being the wrong trade:

        **It is only spent when a switch is actually imminent.** If no other
        scene has work queued, there is nothing to switch to — the dispatcher
        would idle a second and ask again — so waiting here would add latency
        to buy nothing. Return immediately and let it.

        **Fairness still outranks it.** A scene crossing `starve_threshold()`
        while we wait ends the wait, so this cannot become a new way to starve
        one.

        **It polls.** An active client is served in one poll interval, not in
        the whole grace, so the full cost is paid only when the scene is
        genuinely done — the one case where a switch was right anyway.
        """
        grace = float(config.SCENE_DRAIN_GRACE_SEC)
        if grace <= 0:
            return None
        # Nothing else wants the GPU, so nothing is being made to wait and no
        # switch is coming. Waiting would be pure added latency.
        if self.db.oldest_waiting_age(exclude_scene=current) is None:
            return None

        began = time.time()
        deadline = began + grace
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            time.sleep(min(0.5, remaining))
            waiting = self.db.oldest_waiting_age(exclude_scene=current)
            if waiting is None:
                # The competition drained too — whatever we do next costs
                # nothing now, so stop holding the dispatcher here.
                return None
            if waiting > self.starve_threshold():
                return None
            job = self.db.claim(lease, scene=current)
            if job is not None:
                log.info("held %s for %.1fs — more work arrived, so no scene "
                         "switch was paid for", scenes.label(Path(current)),
                         time.time() - began)
                return job

    def demanded_scene_hashes(self) -> set[str]:
        """Content hashes of every scene with a job still waiting on it.

        Feeds eviction ordering, so a scene the queue is about to need is not
        deleted ahead of one nothing wants. Hashing is memoised on (mtime,
        size), so this costs a `stat` per distinct scene, not a re-read.

        Anything that cannot be hashed — a scene deleted or renamed since its
        job was queued — is simply left out. The job will fail on its own terms
        when it is dispatched; a missing file must not break eviction, which is
        what stands between a long batch and a full disk.
        """
        out: set[str] = set()
        for path in self.db.depth_by_scene():
            if not path:
                continue
            with contextlib.suppress(OSError, ValueError):
                out.add(remote.scene_hash(Path(path)))
        return out

    def starve_threshold(self) -> float:
        """How long another scene must wait before preempting the loaded one.

        `config.SCENE_STARVE_SEC` is the FLOOR, not the answer. Preemption is
        only worth buying when the wait it relieves exceeds the wait it
        creates, and a switch is paid twice — once to leave the loaded scene
        and once to come back to it — so the threshold has to scale with what
        reloading that scene actually costs.

        Measured 2026-08-03, and this is the incident that produced the
        method: five agents held work against five scenes, so *some* scene had
        always waited longer than the flat 300 s. The starvation test therefore
        fired on every single dispatch, and `next_job` — whose entire purpose
        is to avoid a scene switch per job — performed nine consecutive
        switches logging "after 1 job(s)" each time, buying 13 s renders with
        100 s scene pushes. It was one job away from abandoning a 4.53 GB
        scene holding SIXTEEN queued jobs, at roughly 24 minutes a round trip,
        to serve a 3 MB scene holding one.

        Small scenes are unaffected: 2 x 120 s for a 0.2 GB scene is under the
        floor, so they still interleave exactly as before. Only a scene that is
        genuinely expensive to reload earns patience — and `SCENE_BATCH_MAX`,
        untouched, remains what actually bounds unfairness.
        """
        floor = float(config.SCENE_STARVE_SEC)
        cost = self.fleet.reload_cost_sec()
        return max(floor, float(config.SCENE_SWITCH_PAYBACK) * cost)

    def cheaper_to_finish(self, current: Optional[str],
                          waiting: Optional[float] = None) -> Optional[float]:
        """Seconds to drain the loaded scene, if that beats leaving and coming
        back. None when preempting is the better trade.

        The other half of the rule, and the half `starve_threshold` cannot
        express. That threshold compares a WAIT against a COST — and once every
        scene in a contended queue has waited far longer than any switch costs,
        every scene is "starving", the comparison stops discriminating, and the
        dispatcher round-robins one job at a time. Exactly the behaviour it
        exists to prevent, reached from the other direction.

        Measured 2026-08-03, after the threshold fix was already live: two
        292 MB scenes with 7 and 6 jobs queued, both waiting ~2400 s, traded the
        worker back and forth a job at a time — 07:51:29 to 07:52:23 is 54 s to
        switch scenes and render one 6.1 s frame. 89 % overhead, just a smaller
        multiple of it than the 4.5 GB case.

        So ask the question the wait cannot answer: **how much work is actually
        on each side?** If the loaded scene can be finished in less time than
        leaving it and coming back would cost, finishing it is better for
        everyone — the waiting scene is served a few seconds later and is spared
        paying for the return trip at all.

        Bounded by the same things as before: `SCENE_BATCH_MAX` still caps a
        batch, and the estimate is finite, so this delays a switch, never
        cancels one.
        """
        if not current:
            return None
        # THE VETO IS BOUNDED. Past SCENE_VETO_MAX_SEC of waiting, the loaded
        # scene stops being allowed to look cheap: an unbounded veto is the
        # "starvation cap that bounded nothing" in a second costume, and this
        # one really did bound nothing, because SCENE_BATCH_MAX counts only
        # CONSECUTIVE jobs and two alternating scenes reset it forever.
        # Priced by job class, not by job COUNT. A sequence job is worth its
        # remaining frames, not one still -- see db.pending_work_sec.
        drain = self.db.pending_work_sec(current)
        if drain <= 0:
            self._veto_scene = None
            return None
        round_trip = float(config.SCENE_SWITCH_PAYBACK) * self.fleet.reload_cost_sec()
        if drain > round_trip:
            self._veto_scene = None
            return None
        # THE VETO IS BOUNDED BY ITS OWN PROMISE. It just claimed this scene can
        # be drained in `drain` seconds; if it is still saying so long after
        # that, the scene was replenished and the claim was false. The promise
        # recorded is the FIRST one, so topping the scene up cannot extend the
        # grace. Floored at SCENE_STARVE_SEC so a tiny drain still gets a fair
        # chance to actually finish.
        now = time.time()
        if self._veto_scene != current:
            self._veto_scene = current
            self._veto_since = now
            self._veto_promised = drain
        else:
            grace = max(self._veto_promised * float(config.SCENE_VETO_GRACE),
                        float(config.SCENE_STARVE_SEC))
            if now - self._veto_since > grace:
                return None
        return drain

    def next_job(self) -> Optional[dict]:
        """Pick the next job, batching by scene without starving any scene.

        Switching scenes costs a worker restart plus a per-camera OptiX prewarm
        — 40-60 s measured — so taking jobs in submission order would pay that
        toll on every alternation between two agents working on two variants.
        Draining alone is not acceptable either: a busy scene would defer the
        others indefinitely.

        The policy is **drain the loaded scene, bounded twice, then switch to
        the scene with the oldest waiting job**:

          * drain while jobs exist for the currently loaded scene,
          * unless SCENE_BATCH_MAX jobs have been served consecutively,
          * or some other scene has had a job waiting longer than
            `starve_threshold()` — SCENE_STARVE_SEC, raised to cover the cost
            of the switch it is about to trigger.

        The switch target is always the oldest waiting job's scene, which is
        what bounds unfairness between scenes: however long a batch runs, the
        longest-waiting scene is served next, so nothing waits forever.
        Fair-share between *agents* is untouched — it still applies inside
        every claim, including scene-restricted ones.
        """
        lease = config.JOB_LEASE_SEC
        current = str(self.fleet.scene_path) if self.fleet.scene_path else None
        capped_yield = False

        if current is not None:
            waiting = self.db.oldest_waiting_age(exclude_scene=current)
            threshold = self.starve_threshold()
            starving = waiting is not None and waiting > threshold
            # ...unless finishing here is quicker than the round trip, in which
            # case yielding costs the waiting scene more than it saves it.
            finish = self.cheaper_to_finish(current, waiting) if starving else None
            if finish is not None:
                starving = False
            capped = self.scene_batch >= config.SCENE_BATCH_MAX
            # The cap outranks the batching preference. `cheaper_to_finish` can
            # keep a scene loaded indefinitely otherwise: a scene fed steadily
            # by an active client is always "nearly finished", and always would
            # have been. The cap is the thing that says how long "nearly" is
            # allowed to last.
            capped_yield = capped
            drained = False
            if not starving and not capped:
                job = self.db.claim(lease, scene=current)
                if job is not None:
                    self.scene_batch += 1
                    return job
                # Drained — but a serial client's next job may be a second
                # away, and switching inside that gap pays a full scene switch
                # for nothing. Ask again for a beat before giving the scene up.
                job = self.settle(current, lease)
                if job is not None:
                    self.scene_batch += 1
                    return job
                # Third way out, and the common one: the loaded scene simply has
                # no work left. Reporting that as "batch cap reached" was a lie
                # the first live switch told — 9 jobs served against a cap of 25.
                drained = True
            if starving:
                reason = (f"another scene has waited {waiting:.0f}s, over the "
                          f"{threshold:.0f}s this switch has to beat")
            elif capped:
                reason = f"batch cap {config.SCENE_BATCH_MAX} reached"
            else:
                reason = "no work left for it" if drained else "re-evaluating"
        else:
            reason = ""

        # Global re-evaluation: serve the scene whose oldest job has waited
        # longest. The batch counter resets here whichever scene wins — if the
        # answer is the scene already loaded, that starts a *fresh* batch rather
        # than leaving the cap permanently exceeded, which would degrade this
        # into job-by-job dispatch and reintroduce a scene switch per job.
        # A capped batch must actually yield. Re-asking without excluding the
        # loaded scene handed it straight back whenever it still held the
        # oldest job, and reset the counter — so the cap was reachable forever
        # and bounded nothing. It only falls back to the unrestricted question
        # when nothing else wants the GPU, where yielding would idle it.
        target, _ = self.db.oldest_waiting_scene(
            exclude_scene=current if capped_yield else None)
        if target is None and capped_yield:
            target, _ = self.db.oldest_waiting_scene()
        job = self.db.claim(lease, scene=target)
        if job is None:
            return None
        if target != current and reason:
            log.info("switching from %s to %s after %d job(s) — %s",
                     scenes.label(Path(current)), scenes.label(Path(target)),
                     self.scene_batch, reason)
        self.scene_batch = 1
        return job

    def renewer(self, job_id: str):
        """Keep a job's lease alive while the dispatcher waits on the instance.

        A reattach may legitimately run for the length of an 8K frame, which is
        longer than JOB_LEASE_SEC. Without this the lease lapses mid-wait,
        `requeue_expired` puts the row back on the queue, and the broker starts
        competing with itself for a render it is already watching.
        """
        def renew(_activity=None) -> None:
            self.db.renew(job_id, config.JOB_LEASE_SEC)
        return renew

    def acquire_worker(self, job_id: str, scene: Path,
                       row_id: Optional[str] = None) -> Optional[dict]:
        """Get a worker ready for this job — or the finished frame, if the
        instance turns out to be rendering it already.

        Returns None once the worker is ready for a fresh render, or a completed
        reply if this job was already in flight and has now been collected.

        `progress.json` carries the job id, so "busy" is not one state but
        three, and each has a different correct answer:

        | the worker is busy with | what this does |
        |---|---|
        | **my job** | reattach and collect it — never re-render, never fail |
        | **another job** | queue behind it — never kill it, never fail my job for it |
        | **nothing** (reachable) | deploy / redeploy as usual |

        The middle row is the one that was missing. `WorkerBusy` for a frame
        belonging to someone else fell through to the generic handler and was
        written to the job as a failure — so an agent's job could be failed by
        the mere fact that another agent's frame was on the GPU.

        The first row was present but decided the wrong way: it discarded the
        job id carried by the exception and re-queried the instance, and when
        that second query failed on a flapping SSH endpoint it re-raised. That
        is how job 54ed3b8bd22f was marked `failed` while the same instance was
        rendering it at sample 6896/8192. The identity is taken from the
        exception now, because `WorkerBusy` is only ever raised off a successful
        read that named the job.
        """
        # `job_id` is what the WORKER calls this render; `row_id` is the queue
        # row whose lease has to stay alive while we wait. They differ for every
        # frame of a sequence, and renewing the wrong one lets the row be
        # requeued out from under a render that is going fine.
        row_id = row_id or job_id
        deadline = time.time() + config.REATTACH_SEC
        waiting_for: object = object()          # sentinel: nothing logged yet

        while True:
            try:
                self.fleet.ensure_ready(scene)
                return None
            except remote.WorkerBusy as exc:
                owner = exc.job_id

                if owner == job_id:
                    log.warning(
                        "job %s is ALREADY RENDERING on the instance — reattaching to "
                        "it instead of restarting or failing it. %s",
                        job_id, remote.diagnose(exc))
                    reply = self.fleet.await_render(
                        job_id, max(60.0, deadline - time.time()),
                        on_poll=self.renewer(row_id))
                    if reply is not None:
                        return reply
                    raise RuntimeError(
                        f"the worker reported it was busy rendering {job_id}, but the "
                        f"instance then said it is not rendering it and holds no PNG "
                        f"for it: {remote.diagnose(exc)}"
                    ) from None

                # Someone else's frame. Waiting costs a queue slot; killing costs
                # however many GPU-minutes are already in it.
                if owner != waiting_for:
                    waiting_for = owner
                    log.warning(
                        "job %s is queued behind job %s, which the instance is still "
                        "rendering — waiting for that frame rather than killing it. %s",
                        job_id, owner or "(unnamed)", remote.diagnose(exc))
                if time.time() > deadline:
                    raise
                self.db.renew(row_id, config.JOB_LEASE_SEC)
                time.sleep(config.PROGRESS_INTERVAL)

    def must_not_fail(self, job_id: str) -> str:
        """Why this job must not be written `failed`, or "" if it may be.

        Asked **of the instance, at the point of failure**, because every
        expensive incident in this project's history ends with the broker
        recording a failure for work that was going fine. The last one is exact:

            job 54ed3b8bd22f  ->  failed: "WorkerBusy: refusing to restart ..."

        written at the same moment the instance reported
        `{"state":"rendering","job_id":"54ed3b8bd22f","sample":6896,...}` with
        one blender process on a GPU at 96%. The render finished with nobody
        waiting for it.

        Deliberately narrow: it answers only from evidence the instance
        supplies. "Could not ask" is not evidence and does not block a failure —
        it only means the queue's own retry budget decides, as it always did.
        """
        if not self.fleet.ep:
            return ""
        try:
            act = self.fleet.activity(attempts=4)
            if act.renders(job_id):
                return f"the instance is {act.describe()}"
            if self.fleet.collect_finished(job_id) is not None:
                return "the finished PNG for it is already on the instance"
        except Exception as exc:
            log.warning("could not ask the instance about job %s before failing it: %s",
                        job_id, remote.diagnose(exc))
        return ""

    @staticmethod
    def is_retry(job: dict) -> bool:
        """Has this row been dispatched before? Decides whether a claim first
        looks for a finished frame already on the instance.

        Two signals, both needed. `attempts > 1` — `claim()` reports the
        incremented count, so the first dispatch is exactly 1 — covers failures
        that spent an attempt and lease-expiry reclaims, which never refund.
        `err` covers the refunding requeues: `must_not_fail` and a
        frames-delivered pass give the attempt back, so a job requeued that way
        arrives back here with `attempts` == 1 forever — and gating on attempts
        alone kept the collect-finished recovery shut on precisely the retries
        it exists for, re-rendering a frame whose PNG was sitting finished on
        the instance. Every requeue writes `err`; a first-ever dispatch has
        none. The cost of a wrongly-true answer is one SSH stat.
        """
        return (job.get("attempts") or 0) > 1 or bool(job.get("err"))

    def render_one(self, key: str, spec: dict, scene: Path, row_id: str,
                   retry: bool) -> dict:
        """Get ONE image rendered on the instance and return the worker's reply.

        Everything hard-won about a single render lives here and is shared by
        stills and by every frame of a sequence: recover a frame that is already
        on the box, reattach rather than re-render when the socket dies, never
        restart a worker that is mid-frame. A sequence that re-implemented any
        of this would re-introduce every bug it protects against, three thousand
        times over.

        `key` is the worker-side identity (and the PNG's name); `row_id` is the
        queue row to keep leased. Does not fetch — the caller decides where the
        file goes and what "verified" means for it.
        """
        reply = None
        # A retry may be a retry of something that has already finished. The
        # worker writes its PNG independently of the socket that asked for it,
        # so a frame survives the broker losing interest in it — job
        # 54ed3b8bd22f rendered to completion *after* being marked failed. One
        # `stat` is worth 40 minutes of GPU.
        #
        # Gated on activity first: while the worker is MID-RENDER on this very
        # frame its PNG exists on disk half-written, and collecting it here
        # would fetch a truncated file and then blame the transfer. If it is
        # rendering this key, fall through — acquire_worker meets WorkerBusy
        # for our own job and reattaches, which waits for the real finish.
        if retry and self.fleet.ep:
            act = self.fleet.activity()
            if act.renders(key):
                log.info("%s: retry finds the instance still rendering it — "
                         "reattaching below rather than collecting a "
                         "half-written PNG", key)
            else:
                reply = self.fleet.collect_finished(key)
            if reply is not None:
                log.warning("%s was already rendered on the instance (%.1f MB) "
                            "— collecting it instead of re-rendering it",
                            key, reply["bytes"] / 1e6)

        if reply is None:
            try:
                reply = self.acquire_worker(key, scene, row_id=row_id)
            except (remote.WorkerBusy, remote.FleetUnavailable, remote.DiskFull):
                # DiskFull passes through untouched. Re-typed as FleetUnavailable
                # below it would be requeued forever against a disk that cannot
                # grow — the queue would spin, the GPU would bill, and the one
                # message naming the sizes would be buried under retries.
                raise
            except Exception as exc:
                # Getting a worker failed. That is the fleet's problem — no
                # offer would boot, a deploy could not finish, the scene would
                # not upload — and it says nothing whatsoever about this frame.
                # Re-typed here so a sequence stops and requeues instead of
                # blaming five innocent frames and burning its failure streak.
                raise remote.FleetUnavailable(
                    f"no worker available for {key}: {remote.diagnose(exc)}"
                ) from exc

        if reply is None:
            self.db.renew(row_id, config.JOB_LEASE_SEC)
        # From here the dispatch thread is blocked inside the worker call for
        # the whole render. The progress poller is the only thing that can say
        # anything about the job until it returns.
        try:
            if reply is None:
                reply = remote.worker_call(spec, self.fleet.local_port)
        except remote.ConnectionDropped as exc:
            # The socket died; the worker almost certainly did not. It renders
            # on its main thread and writes the PNG to disk independently of the
            # connection that asked for it, so the render is recoverable — and
            # re-running it costs the whole frame again. This is precisely how a
            # 40-minute 8K render was thrown away three times in a row.
            log.warning("%s: %s — reattaching over SSH rather than assuming the "
                        "render was lost", key, remote.diagnose(exc))
            self.fleet.repair_tunnel()
            reply = self.fleet.await_render(key, config.REATTACH_SEC,
                                            on_poll=self.renewer(row_id))
            if reply is None:
                raise RuntimeError(
                    f"{remote.diagnose(exc)} — reattached over SSH and the "
                    f"instance is not rendering this job either, so the worker "
                    f"really is gone. {self.fleet.worker_postmortem()}"
                ) from None
        if reply.get("ok"):
            # The other half of the load-vs-render ratio. Taken from the
            # worker's own number, so it is time the GPU spent on pixels — not
            # wall clock, which would silently fold the fetch and the queue
            # wait into "rendering" and make the ratio look healthy.
            with contextlib.suppress(TypeError, ValueError):
                self.fleet.render_sec += float(reply.get("render_sec") or 0.0)
        if not reply.get("ok"):
            why = reply.get("error", "worker reported failure")
            # A refusal on the merits, not a failed attempt. Retrying it buys
            # the identical answer three times and drags a scene selection
            # behind each one. See worker.server.Refused.
            if reply.get("terminal"):
                raise JobRefused(why)
            raise RuntimeError(why)
        return reply

    def collect(self, reply: dict, local: Path) -> tuple[int, dict]:
        """Fetch one rendered image, prove it is the one that was rendered, and
        measure what is in it. Returns (bytes, image statistics).

        Four checks now, none redundant, and the fourth is a different kind from
        the other three. `fetch_file` refuses to move a file into place whose
        byte count differs from the source — that is the guard against the
        observed scp failure that left 783 KB of a 1.9 MB PNG. The structural
        check catches a file that is the right length and still not a PNG. The
        digest catches everything else, and it is the only one that can tell a
        correct frame from a corrupted-but-plausible one, which in a 3,000-frame
        sequence nobody is going to spot by eye.

        All three verify the FILE, and all three passed on a 640x480 PNG that
        was entirely black. So the fourth asks what is in the image. It is done
        here, next to the others, rather than in a separate pass, because a
        frame is delivered exactly once and this is the moment the bytes are in
        hand and the job is still identifiable.

        The measurement itself is never fatal — `imgstat.measure` cannot raise —
        and this method does not decide anything. It reports. The decision about
        whether a blank image fails the job belongs to the caller, which knows
        whether this is a still or one frame of a shot; see `blank_gate`.

        The remote copy is deleted only after all of that passes. 3,000 4K PNGs
        at ~8 MB is ~24 GB against a 30 GB disk that already carries Blender and
        a 288 MB scene — a sequence that does not clean up as it goes fills the
        disk and every later frame fails on write.
        """
        assert self.fleet.ep
        fetch_began = time.time()
        size = remote.fetch_file(self.fleet.ep, reply["path"], local)
        # Throughput, sampled from the transfer that was happening anyway. The
        # broker had no measure of download speed at all, and a link too slow
        # to return frames never trips a check that counts failures.
        self.fleet.note_fetch(size, time.time() - fetch_began)

        want = reply.get("png") or {}
        info = seq.inspect_png(local)
        if not info["complete"]:
            local.unlink(missing_ok=True)
            raise RuntimeError(
                f"fetched {local.name} is not a complete PNG: "
                f"{info['reason'] or 'unknown'} ({size} bytes)"
            )
        if want.get("width") and (info["width"], info["height"]) != (
                want["width"], want["height"]):
            local.unlink(missing_ok=True)
            raise RuntimeError(
                f"fetched {local.name} is {info['width']}x{info['height']} but the "
                f"worker rendered {want['width']}x{want['height']}"
            )
        if want.get("sha256"):
            got = seq.sha256_of(local)
            if got != want["sha256"]:
                local.unlink(missing_ok=True)
                raise RuntimeError(
                    f"fetched {local.name} does not match the rendered file: sha256 "
                    f"{got[:16]} here vs {want['sha256'][:16]} on the instance "
                    f"({size} bytes) — the transfer corrupted it"
                )
        stats = imgstat.measure(local)

        remote.run(self.fleet.ep, f"rm -f {shlex.quote(reply['path'])}",
                   timeout=60, check=False)
        return size, stats

    def blank_history_note(self, scene: Optional[str]) -> str:
        """One sentence saying whether THIS scene has ever rendered a picture.

        Put in front of whoever reads the failure, because the alternative is
        what happened on 2026-08-04: three agents each got a black 4K frame,
        each correctly reported it, and the shared conclusion — "three
        unrelated scenes, so it must be the farm" — sent an afternoon into the
        broker while the GPU was provably fine, rendering other scenes
        correctly between the black ones. The counts below would have settled
        it in one line.
        """
        try:
            blk, ok, last_ok = self.db.scene_blank_verdict_history(scene)
        except Exception:                       # never let diagnostics fail a job
            return ""
        if not scene or (blk + ok) <= 1:
            return ""
        name = os.path.basename(scene)
        if ok == 0:
            return (f"SCENE HISTORY: {name} has now rendered blank {blk} time(s) "
                    f"and has NEVER returned a frame with a picture in it. A "
                    f"failing GPU blackens whatever runs on it next, so it would "
                    f"not spare this farm's other scenes — look at the .blend "
                    f"first (world, lights, camera, and whether the last build "
                    f"step actually wrote what you think it did).")
        when = (f", most recently {time.strftime('%Y-%m-%d %H:%M', time.localtime(last_ok))}"
                if last_ok else "")
        return (f"SCENE HISTORY: {name} HAS rendered fine before — {ok} good "
                f"frame(s){when}, against {blk} blank one(s). A scene that used "
                f"to work and now does not is the one shape of this failure that "
                f"really can be the farm: check what changed in the .blend since "
                f"then, and whether other scenes are still coming back OK.")

    @staticmethod
    def blank_gate(spec: dict, stats: dict, what: str, path: Path,
                   history: str = "") -> None:
        """Report what the image measured as, and refuse it if it has no content.

        Always reports. `OK` goes to the log at info level so a stats trail
        exists for every frame this farm has ever returned; anything else is a
        warning with the numbers in it, because the only thing worse than not
        checking is checking and saying nothing.

        Refuses only for BLACK, TRANSPARENT and UNIFORM — the three verdicts
        that mean "there is no picture here". SUSPICIOUS never fails a job: a
        genuinely dark or low-contrast frame is a thing artists make on purpose,
        and a check that refuses legitimate work gets switched off, after which
        it protects nothing.

        The file is deliberately LEFT on disk. Every other failure in `collect`
        unlinks, because a corrupt file must never be mistaken for a frame — but
        a blank frame is a perfectly readable PNG, and the only way anyone can
        decide whether it is a dead camera or an intentional fade is to look at
        it. The database row is what stops it being counted as delivered, and no
        `done` row is written for it.
        """
        verdict = stats.get("verdict")
        line = f"{what}: {imgstat.summary(stats)}"
        if verdict in imgstat.NOT_OK:
            log.warning("%s  <- %s", line, stats.get("detail") or "look at this frame")
        else:
            log.info("%s", line)

        if not imgstat.is_blank(verdict):
            return
        if spec.get("allow_blank") or not config.BLANK_FAILS_JOB:
            log.warning("%s: delivered anyway — %s", what,
                        "the job asked for --allow-blank"
                        if spec.get("allow_blank") else
                        "VASTRENDER_BLANK_FAILS_JOB is off farm-wide")
            return
        raise BlankOutput(
            f"{what} rendered {verdict}: {stats['detail']}. The file is intact — "
            f"correct size, correct dimensions, sha256 matches what the worker "
            f"computed — it simply has no picture in it, and every check before "
            f"this one passed it. Look at {path}. The usual causes are a camera "
            f"aimed at empty space, a scene with no world and no lights, or "
            f"film_transparent with nothing composited behind it. If this frame "
            f"is genuinely meant to be blank, resubmit with --allow-blank."
            + (f"\n{history}" if history else "")
        )

    def run_job(self, job: dict) -> None:
        """Run one queue row to a verdict.

        Stills and sequences differ in what they render and share, exactly, what
        happens when it goes wrong — including the rule that no job is ever
        written `failed` while the instance is still rendering it.
        """
        job_id = job["id"]
        self.pass_delivered = 0
        # Pin this job's scene for as long as the job runs, so the cache
        # eviction can never delete the .blend out from under it. The loaded
        # scene is protected anyway, but the two facts come apart between the
        # upload and the worker restart — `scene_hash` is deliberately only
        # written once a worker is actually serving that scene.
        pinned: Optional[str] = None
        with contextlib.suppress(Exception):
            path = scenes.resolve_scene(job.get("scene"))
            if path.exists():
                pinned = remote.scene_hash(path)
                self.fleet.pin_scene(pinned)
        try:
            if job.get("seq"):
                self.run_sequence(job)
            else:
                self.run_still(job)
        except remote.DiskFull as exc:
            # Terminal, and deliberately not retried. The preflight measured the
            # disk, evicted everything that was not pinned, measured it again and
            # found the scene still does not fit. Three more attempts measure the
            # same bytes; what has to change is the disk or the assembly, and
            # only a human can do either. The message carries every number.
            self.db.fail_terminal(job_id, remote.diagnose(exc))
            log.error("job %s FAILED on DISK and will NOT be retried — %s",
                      job_id, remote.diagnose(exc))
        except scenes.SceneError as exc:
            # The .blend named by the job is not there — or is not a .blend, or
            # is outside every configured scene root. A verdict about the
            # request, like the ones below: the file will be equally missing on
            # the second and third attempt, and only the agent that submitted
            # it can put one there.
            #
            # Observed 2026-08-03: two reliefpvg jobs for a blend the agent had
            # renamed each burned three dispatch passes restating
            # "does not exist", on a queue where a dispatch pass can drag a
            # scene switch behind it.
            self.db.fail_terminal(job_id, remote.diagnose(exc))
            log.error("job %s FAILED on its scene and will NOT be retried — %s",
                      job_id, remote.diagnose(exc))
        except JobRefused as exc:
            # The worker looked at the request and said no. It will say no
            # again. Failing it once puts the answer in front of the agent that
            # has to change the request, which is the only thing that can.
            self.db.fail_terminal(job_id, str(exc))
            log.error("job %s REFUSED and will NOT be retried — %s", job_id, exc)
        except BlankOutput as exc:
            # Usually not retried: the render finished, the file arrived, it
            # verified, and there is no picture in it. Rendering it twice more
            # produces the same black frame for twice the money, and the caller
            # is the only one who can fix the camera.
            #
            # ONE exception, and it is paid for in blood. On 2026-08-04
            # film14_breach_r6b.blend — byte-identical on disk throughout —
            # rendered four correct frames between 07:54 and 07:59 and four
            # all-black ones between 08:04 and 08:36 on the same instance,
            # which was also throwing CUDA OOM and OPTIX_ERROR_UNKNOWN in that
            # window. Cycles under VRAM exhaustion can hand back a zero-filled
            # buffer that Blender writes out as a perfectly valid PNG. Every
            # one of those jobs was failed terminally, so four agents lost work
            # to a fault in the box, and the identical blackness across
            # unrelated scenes was read as evidence about the scenes.
            #
            # So: a scene that has NEVER produced a picture is still terminal
            # (that is a scene bug, and retrying it burns GPU to learn nothing).
            # A scene with a good frame in its history gets exactly one retry —
            # which reloads the worker on the way back through dispatch — before
            # it is believed. Bounded in memory, so a restart re-arms at most
            # one more attempt and this can never become a loop.
            blk, ok, _ = self.db.scene_blank_verdict_history(job.get("scene"))
            retried = getattr(self, "_blank_retried", None)
            if retried is None:
                retried = self._blank_retried = set()
            if ok > 0 and job_id not in retried:
                retried.add(job_id)
                self.db.requeue(job_id, str(exc))
                log.error(
                    "job %s came back BLACK, but %s has returned %d good "
                    "frame(s) before — a scene that has worked and suddenly "
                    "has not is the shape of a farm fault, not a camera aimed "
                    "at nothing. Requeuing it ONCE on a reloaded worker rather "
                    "than failing it. If it comes back black again the scene is "
                    "believed and the job fails for good. %s",
                    job_id, os.path.basename(job.get("scene") or "?"), ok, exc)
            else:
                self.db.fail_terminal(job_id, str(exc))
                log.error("job %s FAILED and will NOT be retried — %s", job_id, exc)
        except Exception as exc:
            # diagnose(), never str(exc). `str()` on an exception carrying no
            # message is the empty string, and this line is where that reached
            # both the log and the job's stored error as
            # "job dc1b162d8d85 requeued: blender push failed: " — a failure
            # nobody could act on, followed by a healthy GPU being destroyed.
            why = remote.diagnose(exc)

            # THE RULE: never write `failed` for a job the instance is at this
            # moment rendering. This is the last gate before the queue records a
            # verdict, and it is the one that was missing — every path above can
            # raise for reasons that say nothing about the render (a dead
            # forward, a refused SSH connect, the busy-guard itself), and all of
            # them used to land here and burn an attempt. Ask the box.
            #
            # For a sequence the question is asked about the FRAME that was in
            # flight, not the row: the instance has never heard of the row, and
            # asking it about an id it does not know would answer "not rendering
            # that" for a frame it is very much rendering.
            blocked = self.must_not_fail(self.current_key or job_id)
            if blocked:
                # Requeued without spending an attempt: the job did not fail, the
                # broker merely lost track of it. The next claim reattaches or
                # collects the finished PNG.
                self.db.requeue(job_id, f"{why} [not failed: {blocked}]")
                log.error("job %s NOT failed — requeued, because %s. The error was: %s",
                          job_id, blocked, why)
                return

            # A pass that DELIVERED FRAMES does not spend an attempt either.
            #
            # `MAX_ATTEMPTS` counts how many times a job has gone wrong without
            # getting anywhere, and for a still those are the same thing. For a
            # 3,000-frame shot they are not remotely the same thing: the
            # in-container watchdog retires an instance every 12 h no matter what
            # it is doing, so a multi-day sequence is interrupted a dozen times
            # while working perfectly. With a budget of three, such a job is
            # written `failed` on its third instance rotation — with 2,000 frames
            # delivered, verified, and sitting on disk.
            #
            # So progress refunds the attempt. A pass that delivered nothing
            # still spends one, which is what stops a genuinely broken job from
            # retrying forever.
            if self.pass_delivered:
                self.db.requeue(job_id, why)
                log.warning(
                    "job %s requeued WITHOUT spending an attempt — this pass "
                    "delivered %d frame(s) before it stopped, so it made progress "
                    "rather than failing. It will resume from frame %s. Reason: %s",
                    job_id, self.pass_delivered,
                    (self.db.get(job_id) or {}).get("frame_current"), why,
                )
                return

            state = self.db.fail(job_id, why, config.MAX_ATTEMPTS)
            log.error("job %s %s: %s", job_id,
                      "failed" if state == "failed" else "requeued", why)
            if "credit" in why.lower() or "no RTX" in why:
                self.pause(why)
        finally:
            self.current_job = None
            self.current_key = None
            self._stall_warned.pop(job_id, None)
            if pinned:
                self.fleet.unpin_scene(pinned)
            # Asked here, between jobs, and never mid-render: the verdict ends
            # in a destroyed instance, so it must not be reachable while the
            # GPU is holding a frame. The pin is already released, so nothing
            # this job needs is lost either way.
            self.check_download_health()

    def check_download_health(self) -> None:
        """Replace an instance that renders fine and cannot return the results.

        The gap this closes is the shape of every defect found on 2026-08-03: a
        check that cannot fail on the condition that matters. Transport health
        was measured entirely in FAILURES — resets, timeouts, rounds that moved
        no bytes — and a link that delivers slowly produces none of them. It
        never times out. It never stalls. It spends no transport budget. So an
        instance measured at 14 KB/s down passed every probe, reported `ready`,
        and billed for 68% of a rental before anyone looked.

        Only the offer is condemned; see `Fleet.condemn_slow_link` for why the
        machine is deliberately left alone.
        """
        if not self.fleet.instance_id or self.paused:
            return
        why = self.fleet.download_too_slow()
        if not why:
            return
        try:
            self.fleet.condemn_slow_link(why)
        except Exception as exc:
            # A replacement that cannot be carried out is not a reason to take
            # the broker down; the next pass will ask again.
            log.error("could not replace the slow-link instance: %s",
                      remote.diagnose(exc))

    def run_sequence(self, job: dict) -> None:
        """Render a contiguous frame range, one frame at a time, resuming.

        One job, one range, one resident scene. The alternative — a job per
        frame — was rejected for a 3,000-frame shot: it re-pays queue,
        dispatch and lease overhead three thousand times, and the queue's own
        admission limits (200 deep, 25 per agent) make it unrepresentable
        anyway.

        The loop is deliberately serial and deliberately fetches between frames.
        Fetching in the background would keep the GPU busy during the ~2 s
        transfer, but it would also mean an unbounded number of unverified
        frames on a 30 GB disk and a second concurrency story in the one part of
        this system that must never lose work. At 4K final quality a frame is
        minutes; the fetch is noise.
        """
        job_id = job["id"]
        name = job["seq"]
        spec = json.loads(job["spec"])
        started = time.time()

        scene = scenes.resolve_scene(job.get("scene"))
        if not scene.exists():
            raise scenes.SceneError(f"scene not found: {scene}")
        digest = remote.scene_hash(scene)
        want_hash = job.get("spec_hash") or seq.spec_hash(spec, digest)

        frames = seq.frames_of(job)
        plan = seq.plan_range(self.db, name, frames, want_hash)

        if plan.conflict:
            # Never silently. Filling a gap with frames rendered from different
            # settings produces a seam in a shot whose entire premise is that it
            # has none, and the only person who can decide that is the caller.
            raise RuntimeError(
                f"sequence {name} already holds {len(plan.conflict)} frame(s) rendered "
                f"from a DIFFERENT spec or scene ({seq.summarise(plan.conflict)}). "
                f"Rendering the rest against spec {want_hash} would put a visible seam "
                f"in the middle of the shot. Use a new --name, or delete those frames "
                f"deliberately."
            )

        directory = seq.seq_dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        # Counted over the WHOLE range, not this pass, so a resumed job reads
        # "1841/3000" rather than restarting its own progress bar at zero every
        # time an instance is replaced.
        self.db.set_frame_progress(job_id, len(frames), len(plan.have))
        log.info(
            "sequence %s job %s: %d frame(s) requested, %d already delivered, "
            "%d to render%s",
            name, job_id, len(frames), len(plan.have), len(plan.todo),
            f" ({len(plan.stale)} re-render, their files no longer verify)"
            if plan.stale else "",
        )
        if plan.stale:
            log.warning("sequence %s: frames %s were recorded done but their files no "
                        "longer verify — re-rendering them",
                        name, seq.summarise(plan.stale))
        if not plan.todo:
            self.db.finish(job_id, str(directory), 0.0)
            log.info("sequence %s job %s: nothing to do, all %d frame(s) already "
                     "delivered and verified", name, job_id, len(frames))
            return

        done: list[int] = []
        failed: dict[int, str] = {}
        streak = 0
        retry_pass = self.is_retry(job)

        for frame in plan.todo:
            row = self.db.get(job_id) or {}
            if row.get("state") == "canceled":
                log.warning("sequence %s job %s canceled after %d/%d frame(s) — "
                            "stopping. Delivered frames are kept and a resubmit "
                            "will render only what is missing.",
                            name, job_id, len(done), len(plan.todo))
                return
            if not self.running:
                raise RuntimeError("broker is shutting down mid-sequence")

            key = f"{job_id}_f{frame:06d}"
            frame_spec = dict(spec, job_id=key, frame=frame)
            local = seq.frame_path(name, frame)
            self.current_job = job_id
            self.current_key = key
            self.db.set_frame_current(job_id, frame)
            began = time.time()
            try:
                # Only the FIRST frame of a retry pass can already be on the
                # instance: every frame before it was fetched and verified, and
                # every frame after it was never started. Asking about the rest
                # would be one SSH `stat` per frame — ten minutes of round trips
                # across a 3,000-frame range, to learn nothing 2,999 times.
                reply = self.render_one(key, frame_spec, scene, job_id,
                                        retry=retry_pass and frame == plan.todo[0])
                size, stats = self.collect(reply, local)
                # Before frame_done, never after. A `done` row is what a resume
                # trusts to skip a frame forever, so a blank one must never
                # reach the table — that is the resume-poisoning case, and it is
                # the reason this check exists at all.
                self.blank_gate(spec, stats, f"{name} frame {frame}", local,
                                self.blank_history_note(scene))
                png = reply.get("png") or {}
                self.db.frame_done(
                    name, frame, job_id, str(local), size,
                    png.get("width"), png.get("height"), png.get("sha256", ""),
                    float(reply.get("render_sec", 0.0)), want_hash, stats,
                )
                done.append(frame)
                self.pass_delivered = len(done)
                streak = 0
                eta = ""
                if len(done) >= 2:
                    per = (time.time() - started) / len(done)
                    left = (len(plan.todo) - len(done)) * per
                    eta = f", ~{left / 3600:.1f}h left at {per:.1f}s/frame"
                log.info("sequence %s frame %d done (%d/%d) — render %.1fs, %.1f MB%s",
                         name, frame, len(done), len(plan.todo),
                         reply.get("render_sec", 0), size / 1e6, eta)
            except (remote.FleetUnavailable, remote.WorkerBusy,
                    remote.TransferError, remote.SshError) as exc:
                # Infrastructure, not the frame. Hand it up so the job requeues
                # with everything already delivered still recorded — the retry
                # resumes rather than restarting the range.
                raise RuntimeError(
                    f"sequence {name} stopped at frame {frame} after {len(done)} "
                    f"frame(s) this pass: {remote.diagnose(exc)}"
                ) from exc
            except JobRefused:
                # A verdict about the REQUEST, so it condemns every frame that
                # would be asked the same question — an over-budget pixel count
                # or a border that is not a rectangle is identical at frame 401
                # and frame 402. Treated as a frame failure it burned
                # FRAME_FAIL_STREAK frames restating the same refusal before
                # anything stopped. Raised straight through so the job is
                # failed once, with the answer in front of the agent that has
                # to change the request. Delivered frames are already recorded
                # and a resubmit renders only what is missing.
                raise
            except Exception as exc:
                why = remote.diagnose(exc)
                streak += 1
                failed[frame] = why
                self.db.frame_failed(name, frame, job_id, why, want_hash)
                log.error("sequence %s frame %d FAILED (%.1fs, %d consecutive): %s",
                          name, frame, time.time() - began, streak, why)
                if streak >= FRAME_FAIL_STREAK:
                    raise RuntimeError(
                        f"sequence {name}: {streak} consecutive frames failed "
                        f"(last was frame {frame}: {why}) — stopping rather than "
                        f"burning the GPU on {len(plan.todo) - len(done)} more. "
                        f"{len(done)} frame(s) were delivered and are kept."
                    ) from None
            finally:
                # current_key is deliberately NOT cleared here: if this pass
                # ends by raising, the failure handler has to ask the instance
                # about the frame that was in flight, and that question needs
                # the worker's id for it. run_job clears it.
                self.last_work = time.time()
            # Written as the batch goes, not only at the end. A sequence
            # interrupted after 2,000 frames must still leave a manifest that
            # describes those 2,000.
            if done and len(done) % 25 == 0:
                seq.write_manifest(name, self.db)

        seq.write_manifest(name, self.db)
        if failed:
            # Not "done". A sequence with holes is not a deliverable, and the
            # holes are named so the caller can act on them.
            raise RuntimeError(
                f"sequence {name}: {len(done)} frame(s) delivered, "
                f"{len(failed)} FAILED ({seq.summarise(failed)}). "
                f"First reason: {next(iter(failed.values()))}"
            )
        self.db.finish(job_id, str(directory), time.time() - started)
        log.info("sequence %s job %s COMPLETE — %d frame(s) in %.1f min (%.1fs/frame), "
                 "all verified, in %s",
                 name, job_id, len(done), (time.time() - started) / 60,
                 (time.time() - started) / max(len(done), 1), directory)

    def run_still(self, job: dict) -> None:
        job_id = job["id"]
        spec = json.loads(job["spec"])
        # Re-validated at dispatch, not trusted from the row: the path was
        # checked at submit, but it is a filesystem path on two machines and
        # the scene root may have changed under a long-queued job.
        scene = scenes.resolve_scene(job.get("scene"))
        if not scene.exists():
            raise scenes.SceneError(f"scene not found: {scene}")

        started = time.time()
        self.current_job = job_id
        self.current_key = job_id

        reply = self.render_one(job_id, spec, scene, job_id,
                                retry=self.is_retry(job))
        local = config.OUT_DIR / f"{job_id}.png"
        size, stats = self.collect(reply, local)
        # Recorded before the gate can raise, so a job failed for being blank
        # still carries the numbers that condemned it. Arguing with the verdict
        # requires being able to see it.
        self.db.set_image_stats(job_id, stats)
        self.blank_gate(spec, stats, f"job {job_id}", local,
                        self.blank_history_note(scene))
        # `size` is what makes a deliberate calibration still usable as the disk
        # basis for the batch it was rendered to price. See mean_bytes_for_spec.
        self.db.finish(job_id, str(local), float(reply.get("render_sec", 0.0)),
                       stats, size=size)
        log.info(
            "job %s done — render %.1fs, total %.1fs, %.1f MB, %s",
            job_id, reply.get("render_sec", 0), time.time() - started, size / 1e6,
            imgstat.summary(stats),
        )
        # The worker's readback of what the SCENE held at render time, not what
        # the spec asked for. It was being computed and thrown away, which meant
        # the only record of an A/B's sampling and denoise state was the request
        # — and a request is exactly the thing a caller is trying to verify.
        effective = reply.get("effective")
        if isinstance(effective, dict) and effective:
            log.info(
                "job %s effective — %s", job_id,
                "  ".join(f"{k}={effective[k]}" for k in sorted(effective)),
            )

    def maybe_idle_down(self) -> None:
        """Wind the instance down in two stages once work stops.

        First stop it: GPU billing ends immediately, but the disk survives with
        Blender installed and the scene already uploaded, so the next job wakes
        in seconds instead of re-paying a full cold start. That disk costs about
        1.4 cents an hour.

        Then destroy it, because a stopped instance bills storage forever and is
        the classic forgotten-disk charge. Stop is a delay, never a destination.
        """
        # Keyed on instance_id, not ep: an instance whose deploy failed has no
        # endpoint but is still rented and still billing. Checking ep here would
        # let exactly that case run forever.
        if not self.fleet.instance_id:
            return

        if self.fleet.stopped_at:
            if self.fleet.hibernated_for > config.HIBERNATE_SEC:
                log.info("hibernated %.0f min — destroying", self.fleet.hibernated_for / 60)
                self.fleet.teardown("hibernation expired")
            return

        idle = time.time() - self.last_work
        if idle > config.IDLE_GRACE_SEC and self.execsvc.busy():
            # An idle RENDER queue is not an idle box. `last_work` is stamped by
            # both dispatchers, but an exec batch that is queued-but-not-yet-
            # dispatched touches neither clock, and stopping the instance under
            # it would stop it one second before waking it again — after
            # SIGKILLing every build in flight, because a stopped container runs
            # no processes.
            snap = self.execsvc.snapshot()
            log.info("idle %.0fs by the render queue, but %d exec job(s) are in "
                     "flight and %d waiting — NOT stopping the instance",
                     idle, len(snap["inflight"]), self.db.exec_waiting())
            self.last_work = time.time()
            return

        if idle > config.IDLE_GRACE_SEC:
            # "No queued jobs" is not the same as "the GPU is doing nothing".
            # Measured: the broker wrote off an 8K job after a tunnel drop, its
            # queue went empty, and 300 s later it stopped the instance — while
            # the worker was still at 99% GPU and 420 W on that very frame. Ask
            # the instance what it is doing before ending its life.
            act = self.fleet.activity()
            # Only the bounded-unknown branch below may set this: hibernate()
            # re-checks under its own lock and refuses on rendering AND on
            # unknown, so a caller that has deliberately decided to stop blind
            # has to say so explicitly.
            stop_blind = False

            if act.rendering:
                log.warning(
                    "idle %.0fs by the queue, but the instance is %s — NOT stopping "
                    "it. An idle queue is not an idle GPU.", idle, act.describe())
                self.last_work = time.time()
                self.idle_unknown_since = None
                return

            if act.unknown:
                # An unanswered probe is not permission to stop a GPU. Bounded,
                # because an instance we can never reach must not bill forever —
                # but the bound is deliberately longer than the in-container
                # watchdog's 30-minute heartbeat deadline (HEARTBEAT_STALE_SEC),
                # so on a genuinely unreachable box the watchdog destroys it
                # first and this branch never has to guess.
                if self.idle_unknown_since is None:
                    self.idle_unknown_since = time.time()
                blind = time.time() - self.idle_unknown_since
                if blind < config.IDLE_UNKNOWN_MAX_SEC:
                    log.warning(
                        "idle %.0fs by the queue, but %s — NOT stopping it on an "
                        "unanswered probe. Blind for %.0fs of %.0fs allowed.",
                        idle, act.describe(), blind, config.IDLE_UNKNOWN_MAX_SEC)
                    return
                log.error(
                    "idle %.0fs and the instance has not answered a progress probe "
                    "for %.0f min — stopping it anyway. If a render was in flight "
                    "this loses it, but an instance that cannot be reached at all "
                    "cannot be kept alive indefinitely either. Last: %s",
                    idle, blind / 60, act.describe())
                stop_blind = True
            else:
                self.idle_unknown_since = None

            log.info("idle %.0fs — stopping instance (disk kept)", idle)
            try:
                self.fleet.hibernate(force=stop_blind)
            except remote.WorkerBusy as exc:
                # The fleet re-checked under its own lock and found a render.
                # Two checks disagreeing is exactly what a flapping endpoint
                # produces, and the safe reading always wins.
                log.warning("stop refused: %s", remote.diagnose(exc))
                self.last_work = time.time()
                self.idle_unknown_since = None
            except Exception as exc:
                log.error("stop failed (%s) — destroying instead", remote.diagnose(exc))
                self.fleet.teardown("stop failed")

    def total_spend(self) -> float:
        """Everything spent across every instance this database has seen.

        Banked in the `meta` table, so it survives instance replacement,
        adoption, and broker restarts — all of which reset the per-instance
        counters to zero.
        """
        return float(self.db.get_meta("spend_usd", 0.0)) + self.fleet.spend + self.fleet.disk_spend

    def bank_spend(self) -> None:
        banked = float(self.db.get_meta("spend_usd", 0.0))
        self.db.set_meta("spend_usd", banked + self.fleet.spend + self.fleet.disk_spend)
        # Cleared in the same breath. This instance's cost has just moved into
        # the permanent total, and leaving the live record behind would make
        # `orphaned_spend` count it a second time for the rest of the run.
        self.db.set_meta("live_spend", {})

    # How long a host that failed to come up stays blacklisted. Long enough to
    # stop a restart walking straight back into it, short enough that a machine
    # having a bad hour is not written off for the week.
    BLACKLIST_TTL_SEC = 6 * 3600

    def load_blacklist(self) -> None:
        """Restore the failed-host blacklist across a broker restart.

        `Fleet.bad_offers` and `bad_machines` were per-process, and a restart is
        routine — it is how new code is loaded and the only supported way to
        stop a broker. So the blacklist evaporated exactly when it was most
        needed: on 2026-07-28 machine 91334 failed to start sshd, the broker was
        restarted to pick up a fix, and the dispatcher immediately rented **the
        same offer on the same machine** because it was still the cheapest. Both
        rentals failed the same way.

        Time-limited on load rather than pruned on write, so a machine that had
        a bad hour is not written off permanently.
        """
        stored = self.db.get_meta("bad_hosts", {}) or {}
        if not isinstance(stored, dict):
            return
        now = time.time()
        offers = {int(k): v for k, v in (stored.get("offers") or {}).items()
                  if now - float(v) < self.BLACKLIST_TTL_SEC}
        machines = {int(k): v for k, v in (stored.get("machines") or {}).items()
                    if now - float(v) < self.BLACKLIST_TTL_SEC}
        self.fleet.bad_offers |= set(offers)
        self.fleet.bad_machines |= set(machines)
        self._blacklist_seen = {"offers": offers, "machines": machines}
        if offers or machines:
            log.info("restored blacklist from a previous run: %d offer(s), "
                     "%d machine(s) that failed to come up in the last %.0f h",
                     len(offers), len(machines), self.BLACKLIST_TTL_SEC / 3600)

    def save_blacklist(self) -> None:
        """Persist anything newly blacklisted, stamped with when."""
        seen = getattr(self, "_blacklist_seen", None) or {"offers": {}, "machines": {}}
        now = time.time()
        changed = False
        for key, live in (("offers", self.fleet.bad_offers),
                          ("machines", self.fleet.bad_machines)):
            for item in live:
                if str(item) not in seen[key]:
                    seen[key][str(item)] = now
                    changed = True
        if changed:
            self._blacklist_seen = seen
            self.db.set_meta("bad_hosts", seen)

    def checkpoint_spend(self) -> None:
        """Persist the live instance's spend without waiting for a teardown.

        `bank_spend` only ran at teardown, which made the cumulative cap a lie in
        the one situation it is for: kill the broker with SIGKILL — the *only*
        supported way to restart it, because SIGTERM destroys the GPU — and
        everything the current instance had spent was simply forgotten. A
        multi-day sequence restarts the broker several times, so the cap could
        be exceeded several times over without ever tripping.

        Idempotent by construction: the current instance's accrual is stored
        under its own id and overwritten, never added, so calling this every
        heartbeat cannot double-count. `spend_usd` remains the sum of instances
        that are gone, and this covers the one that is not.
        """
        if not self.fleet.instance_id:
            self.db.set_meta("live_spend", {})
            return
        self.db.set_meta("live_spend", {
            "instance": self.fleet.instance_id,
            "usd": round(self.fleet.spend + self.fleet.disk_spend, 6),
            "dph": self.fleet.dph,
            "at": time.time(),
        })

    def orphaned_spend(self) -> float:
        """Spend recorded against an instance that is no longer the live one.

        A broker killed mid-batch leaves `live_spend` naming an instance this
        process never adopted. That money was spent; counting it is the
        difference between a cap that holds across restarts and one that resets
        every time the process does.
        """
        live = self.db.get_meta("live_spend", {}) or {}
        if not isinstance(live, dict) or not live.get("usd"):
            return 0.0
        if self.fleet.instance_id and live.get("instance") == self.fleet.instance_id:
            return 0.0                      # it IS the live one; fleet counts it
        return float(live.get("usd") or 0.0)

    def spend_cap(self) -> float:
        """The cumulative ceiling. Settable at runtime, because raising it must
        not require a broker restart — a restart is the riskiest routine act in
        this system, not a configuration mechanism."""
        stored = self.db.get_meta("max_batch_usd", None)
        try:
            return float(stored) if stored is not None else float(config.MAX_BATCH_USD)
        except (TypeError, ValueError):
            return float(config.MAX_BATCH_USD)

    def spent(self) -> float:
        return self.total_spend() + self.orphaned_spend()

    def cost_estimate(self, frames: int, mean_sec: Optional[float],
                      basis: str = "", samples: int = 0,
                      exact: bool = False) -> dict:
        """What a batch will cost, before it is allowed to start costing it.

        Deliberately arithmetic on a measured mean rather than a model: the only
        honest input is how long frames of this kind have actually taken, and
        when there is no such measurement this says so instead of inventing one.

        `basis` and `exact` exist because a measured number can still be the
        wrong measurement. A 2,978-frame 4K batch of a circuit with crowds and
        destruction caches projected from 1080p previews of a different .blend
        is arithmetic on a measurement and is off by more than an order of
        magnitude — and it reads as authoritative precisely because it is not a
        model. So the projection now says what it measured, how many renders it
        averaged, and whether that basis is the same spec and the same .blend
        as the batch being priced.
        """
        dph = self.fleet.dph or 0.31
        if not mean_sec or frames <= 0:
            return {
                "frames": frames, "dph": round(dph, 4),
                "known": False,
                "note": ("no completed render on record to extrapolate from — render "
                         "one frame of this scene at the batch's resolution and "
                         "samples, then ask again and this becomes a measurement"),
            }
        gpu_hours = frames * mean_sec / 3600.0
        # Wall-clock is longer than GPU time: fetch and per-frame overhead, plus
        # a cold start every time the in-container watchdog's wall-clock cap
        # retires an instance mid-batch.
        restarts = max(0, int(gpu_hours // 12))
        overhead_h = restarts * (10 / 60.0)
        return {
            "frames": frames,
            "sec_per_frame": round(mean_sec, 1),
            "gpu_hours": round(gpu_hours, 2),
            "dph": round(dph, 4),
            "usd": round((gpu_hours + overhead_h) * dph, 2),
            "wall_hours": round(gpu_hours + overhead_h, 2),
            "instance_restarts": restarts,
            "known": True,
            "exact": exact,
            "basis": basis,
            "basis_samples": samples,
            "note": (
                f"projection from a {mean_sec:.0f}s mean over {samples} "
                f"{basis or 'completed render(s)'}; {restarts} cold start(s) "
                f"allowed for the 12 h in-container wall-clock cap"
                + ("" if exact else
                   " — WARNING: this is NOT a render of this batch's spec and "
                   ".blend, so treat it as an order-of-magnitude hint only. "
                   "Render one frame at the batch's own resolution and samples "
                   "for a number worth budgeting against.")
            ),
        }

    def budget_report(self) -> dict:
        cap = self.spend_cap()
        spent = self.spent()
        return {
            "cap_usd": round(cap, 2),
            "spent_usd": round(spent, 4),
            "remaining_usd": round(cap - spent, 4),
            "banked_usd": round(float(self.db.get_meta("spend_usd", 0.0)), 4),
            "live_instance_usd": round(self.fleet.spend + self.fleet.disk_spend, 4),
            "orphaned_usd": round(self.orphaned_spend(), 4),
            "paused": self.paused,
            # vast.ai's own number. Everything above is this broker's
            # bookkeeping and can drift; the credit delta is what was actually
            # taken, and it is the only figure that settles an argument.
            "credit": self.db.get_meta("credit", {}),
        }

    def sample_credit(self) -> None:
        """Record vast.ai's own view of the money, occasionally.

        Every internal spend figure here is derived from a rate and a clock, and
        both can be wrong — an adopted instance's uptime is unknown, a stopped
        one bills disk this code models rather than measures. Credit is the
        ground truth, so it is sampled and the drop since the first sample is
        reported alongside the estimate. Rate-limited hard: vast throttles per
        endpoint AND per client IP.
        """
        record = self.db.get_meta("credit", {}) or {}
        if time.time() - float(record.get("at") or 0) < 600:
            return
        try:
            user = self.fleet.client.show_user()
            now = float(user.get("credit") or 0.0) + float(user.get("balance") or 0.0)
        except Exception as exc:
            log.debug("credit poll failed: %s", remote.diagnose(exc))
            return
        first = record.get("first")
        if first is None:
            first = now
        self.db.set_meta("credit", {
            "usd": round(now, 4), "first": round(float(first), 4),
            "spent_since_first": round(float(first) - now, 4),
            "at": time.time(),
        })

    def pause(self, reason: str) -> None:
        if self.paused:
            return
        self.paused = reason
        log.error("PAUSED: %s", reason)
        if self.fleet.ep:
            self.fleet.teardown("paused")

    def resume(self) -> None:
        self.paused = None
        self.last_work = time.time()
        log.info("resumed")

    # --- lifecycle -------------------------------------------------------

    def heartbeat_loop(self) -> None:
        """Keep the in-container watchdog satisfied, on its own thread.

        This must never share a thread with dispatch. The heartbeat used to be
        sent from the dispatch loop, which blocks for the whole of provisioning,
        a scene upload, or a render — so on a real instance it never fired once,
        the watchdog saw a 15-minute-stale file and destroyed a perfectly
        healthy GPU mid-deploy. Liveness signalling cannot depend on the thing
        whose liveness it reports.
        """
        while self.running:
            try:
                if self.fleet.ep and not self.fleet.stopped_at:
                    self.fleet.heartbeat()
            except Exception as exc:
                log.warning("heartbeat error: %s", remote.diagnose(exc))
            # Money bookkeeping rides this thread because it is the one that
            # keeps running through a multi-hour render, and because both of its
            # jobs are the same shape as the heartbeat's: cheap, periodic, and
            # worthless if they only happen when something ends cleanly.
            # `reap_doomed` retries destroys that vast never confirmed — the
            # dispatch thread cannot do it, being blocked for whole renders.
            # Disk sampling rides here for the same reason the money bookkeeping
            # does, and strictly AFTER the beat above: the beat is what keeps the
            # in-container watchdog from destroying the instance, so nothing that
            # measures anything may run in front of it. `sample_disk` is one SSH
            # command, rate-limited to DISK_SAMPLE_SEC, and its failure is logged
            # rather than raised — an unmeasurable disk must not stop the beat.
            for task in (self.checkpoint_spend, self.sample_credit,
                         self.save_blacklist, self.fleet.reap_doomed,
                         self.fleet.sample_disk):
                try:
                    task()
                except Exception as exc:
                    log.warning("%s failed: %s", task.__name__, remote.diagnose(exc))
            time.sleep(config.HEARTBEAT_INTERVAL)

    def progress_loop(self) -> None:
        """Read the worker's progress file off the instance, on its own thread.

        It has to be its own thread for the same reason the heartbeat does: the
        dispatch thread is blocked inside the render for the entire job, so
        anything that reports on that job cannot live there. And it has to come
        off a file over SSH rather than the job socket, because the worker is
        strictly serial and will not answer a ping mid-render.
        """
        # Only one job runs at a time, so a single last-seen pair is enough —
        # and unlike a dict keyed by job id it cannot grow across a long batch.
        seen_job: Optional[str] = None
        seen_pos: Optional[tuple] = None
        while self.running:
            time.sleep(config.PROGRESS_INTERVAL)
            job_id = self.current_job
            key = self.current_key
            try:
                if not job_id or not self.fleet.ep or self.fleet.stopped_at:
                    continue
                prog = remote.read_progress(self.fleet.ep)
                # Matched on the WORKER's id for this render, which for a frame
                # in a sequence is `<job>_f000123` and not the row id. Comparing
                # against the row id would discard every progress update an
                # animation ever produces, leaving exactly the "running with no
                # numbers" state that is indistinguishable from wedged.
                if not prog or prog.get("job_id") != (key or job_id):
                    # A stale file from the previous job says nothing about this
                    # one; better silent than confidently wrong.
                    continue

                sample = prog.get("sample")
                total = prog.get("total")
                tile, tiles = prog.get("tile"), prog.get("tiles")
                # Position is (tile, sample), not sample alone: on a tiled frame
                # the sample counter restarts at zero for every tile, so a tile
                # advancing while the sample number falls is progress, and
                # watching only `sample` would read it as a stall.
                # Keyed on the frame, not the row: moving to the next frame of a
                # sequence IS advancement, and a row-keyed comparison would read
                # frame N+1 restarting at sample 0 as a stalled counter.
                pos = (tile, sample)
                advanced = ((key or job_id) != seen_job) or (pos != seen_pos)
                seen_job, seen_pos = (key or job_id), pos
                self.db.set_progress(
                    job_id, sample, total, prog.get("pct"), prog.get("elapsed_sec"),
                    prog.get("remaining_sec"), prog.get("phase"), advanced,
                    tile=tile, tiles=tiles,
                )
                self.watch_for_stall(job_id, prog, advanced)
            except Exception as exc:
                # Never let reporting break rendering.
                log.debug("progress poll failed: %s", remote.diagnose(exc))

    def watch_for_stall(self, job_id: str, prog: dict, advanced: bool) -> None:
        """Warn loudly when the sample counter stops moving. Never kill.

        Killing on suspicion is the more expensive error and this session has
        the receipts: a healthy GPU destroyed over a retryable upload, and a
        fully pre-warmed worker killed every ten minutes by a launch that had
        actually succeeded. A human can read this line and decide.
        """
        row = self.db.get(job_id) or {}
        advanced_at = row.get("prog_advanced")
        if advanced or not advanced_at:
            return
        stalled = time.time() - advanced_at
        if stalled < config.STALL_WARN_SEC:
            return

        sample, total = row.get("prog_sample"), row.get("prog_total")
        tile, tiles = row.get("prog_tile"), row.get("prog_tiles")
        # Reaching the ceiling is not a stall: denoising, compositing and PNG
        # encoding all happen afterwards with the counter parked. Measured
        # locally at 2000x2000 — 18 s of real work after the final sample, which
        # on an 8K frame is minutes. On a tiled frame this only applies on the
        # LAST tile; finishing tile 3 of 12 and pausing really is a stall.
        at_end = sample is not None and total and sample >= total
        last_tile = (not tiles) or (tile is not None and tile >= tiles - 1)
        if at_end and last_tile:
            return

        # Rate-limit so a genuinely stuck job warns periodically, not per poll.
        last = self._stall_warned.get(job_id, 0.0)
        if time.time() - last < config.STALL_WARN_SEC:
            return
        self._stall_warned[job_id] = time.time()
        log.warning(
            "STALL WARNING: job %s has not advanced past sample %s/%s for %.0f min "
            "(phase %r, %.0f min into the render). NOT killing it — Cycles reports "
            "only at adaptive-sampling checkpoints and a big frame can be legitimately "
            "quiet for minutes. Check the GPU with: nvidia-smi over ssh, and the "
            "worker with: tail %s/worker.log",
            job_id, sample, total, stalled / 60, row.get("prog_phase"),
            (row.get("prog_elapsed") or 0) / 60, config.REMOTE_ROOT,
        )

    def supervised(self, name: str, body) -> None:
        """Run `body`, and put it back if it ever escapes.

        Each of the three threads owns a responsibility nothing else covers, and
        a thread that dies takes that responsibility with it *silently*: HTTP
        keeps answering with no dispatcher claiming jobs, or the heartbeat stops
        and the in-container watchdog destroys a healthy GPU fifteen minutes
        later. `except Exception` inside each loop is not enough — it does not
        cover `BaseException`, nor a raise from the `while` condition, the
        `time.sleep`, or the `except` clause itself.

        Restarting is always safe here because every loop is idempotent and
        stateless between iterations: dispatch reclaims its own orphaned rows,
        the heartbeat only touches a file, the poller only reads one.
        """
        fails = 0
        while self.running:
            began = time.time()
            try:
                body()
                if not self.running:
                    return
                log.error("%s thread returned while the broker is still running "
                          "— restarting it", name)
            except BaseException as exc:            # noqa: BLE001 — that is the point
                # A loop that ran for a while and then failed is a transient
                # fault, not a crash loop, so it must not inherit the backoff of
                # something that failed hours ago.
                if time.time() - began > 60:
                    fails = 0
                fails += 1
                log.critical("%s thread DIED (%d) — restarting it. %s",
                             name, fails, remote.diagnose(exc), exc_info=True)
            # Backoff, so a permanently broken loop cannot spin the log or the
            # CPU, but never so long that a transient fault costs a batch.
            time.sleep(min(30.0, 2.0 ** min(fails, 5)))

    def start(self) -> None:
        # The singleton lock must already be held. Asserting it here rather than
        # acquiring it keeps a single owner for the lock's lifetime and makes
        # "started the dispatcher without the lock" impossible to reach by
        # accident from a future caller.
        if not LOCK.held:
            raise RuntimeError("refusing to start the dispatcher without the singleton lock")
        diagnostics.report_process_identity()
        threads = (
            ("thread", "dispatch", self.dispatch_loop),
            ("hb_thread", "heartbeat", self.heartbeat_loop),
            ("prog_thread", "progress", self.progress_loop),
            # Its own thread for the same reason the heartbeat has one: it must
            # keep working while the render dispatcher is blocked for forty
            # minutes inside a single 8K frame.
            ("exec_thread", "exec", self.execsvc.loop),
        )
        for attr, name, body in threads:
            t = threading.Thread(target=self.supervised, args=(name, body),
                                 name=name, daemon=True)
            t.start()
            setattr(self, attr, t)
        self.started = True

    def stop(self) -> None:
        self.running = False
        with contextlib.suppress(Exception):
            self.execsvc.stop()

        # An aborted startup destroys nothing. This is the guard for the bug
        # where a second broker adopted the live instance during lifespan
        # startup, failed to bind port 8760, and then destroyed the GPU the
        # *first* broker was mid-batch on. Whatever this process managed to
        # adopt before it gave up belongs to the broker that is still running.
        if not self.started:
            log.warning(
                "startup did not complete — exiting without touching the fleet "
                "(instance_id=%s left alone)", self.fleet.instance_id,
            )
            with contextlib.suppress(Exception):
                self.db.close()
            return

        if self.thread:
            self.thread.join(timeout=30)

        if config.KEEP_ON_EXIT and self.fleet.instance_id:
            log.warning(
                "KEEP_ON_EXIT — leaving instance %s alive for the next broker. "
                "Its watchdog destroys it if no broker resumes the heartbeat.",
                self.fleet.instance_id,
            )
            self.db.close()
            return

        # An instance must never outlive its broker — but "shut down" is not a
        # licence to discard an hour of GPU time, and this path has already done
        # exactly that once: a broker was signalled at 05:58:59 and destroyed the
        # instance on the way out, so the next start had to rent fresh hardware
        # and re-push 481 MB.
        #
        # Same three-valued rule as everywhere else in this codebase: only a
        # reachable, parsed, definitely-not-rendering answer licenses a destroy.
        # A frame in flight is left with its GPU, and the two independent
        # backstops take over — the in-container watchdog destroys the instance
        # once the heartbeat has been stale for 30 minutes (HEARTBEAT_STALE_SEC),
        # and its 12 h wall-clock cap fires regardless. Worst case is ~30 min of
        # GPU billing (~$0.15) against a frame that is often 40 minutes of it,
        # and a broker restarted by scripts/brokerd.sh reattaches in seconds and
        # loses nothing at all.
        #
        # Gated on there being an endpoint at all. An instance whose deploy
        # never succeeded has no endpoint, therefore no worker, therefore no
        # frame to protect — and it is still rented and still billing, which is
        # precisely the case that must go on being destroyed here.
        keep = ""
        if self.fleet.instance_id and self.fleet.ep:
            try:
                act = self.fleet.activity(attempts=2)
                if act.rendering:
                    keep = act.describe()
                elif act.unknown:
                    keep = f"the instance did not answer ({act.describe()})"
            except Exception as exc:
                keep = f"could not ask the instance ({remote.diagnose(exc)})"

        if keep:
            log.error(
                "SHUTTING DOWN, but NOT destroying instance %s — %s. An idle "
                "broker is not an idle GPU. The in-container watchdog destroys "
                "it 30 min after the heartbeat stops, and at its 12 h cap. If no "
                "broker is coming back, destroy it now: "
                ".venv/bin/python vastctl/vastctl.py destroy %s",
                self.fleet.instance_id, keep, self.fleet.instance_id,
            )
        else:
            try:
                self.fleet.teardown("broker shutdown")
            except Exception as exc:
                log.error("teardown on shutdown failed — CHECK FOR ORPHANS: %s",
                          remote.diagnose(exc))
        self.db.close()


broker = Broker()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # FIRST, before adopt_or_reap, before any fleet call, before the port bind.
    # uvicorn runs lifespan startup ahead of `loop.create_server`, so this hook
    # is the earliest point that covers every way of launching the ASGI app.
    #
    # Deliberately outside the try/finally: if the lock is taken we raise before
    # broker.start(), so broker.stop() — and therefore the destroy-on-exit path
    # — is never reached at all.
    LOCK.acquire()

    # Here rather than at import: asyncio's default handler logs to the
    # `asyncio` logger, which has no destination configured, so an exception on
    # the event loop — every HTTP handler runs there — left no trace at all.
    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().set_exception_handler(
            diagnostics.loop_exception_handler)
    # And again here for the signal names: uvicorn takes SIGTERM/SIGINT for
    # itself inside `Server.serve`, which runs *before* lifespan startup, so
    # this is the first point at which the naming wrapper can sit on top of
    # uvicorn's handler rather than under it.
    diagnostics.install("lifespan")

    broker.start()
    try:
        yield
    finally:
        broker.stop()


app = FastAPI(title="vast-render broker", lifespan=lifespan)


# --- API ------------------------------------------------------------------


@app.post("/jobs")
async def submit(request: Request):
    body = await request.json()
    spec = body.get("spec") or {}
    agent = str(body.get("agent") or "anon")[:64]
    prio = int(body.get("prio") or 100)

    missing = REQUIRED_SPEC - spec.keys()
    if missing:
        raise HTTPException(400, f"incomplete spec, missing: {sorted(missing)}")

    # Validated here so a bad scene is a 400 at submit rather than a failed job
    # discovered minutes later, and rejected rather than clamped: this string
    # becomes a filesystem path on this machine and on the instance.
    try:
        scene = scenes.resolve_scene(body.get("scene"))
        # Refused here, at submit, for the same reason the path is validated
        # here: a scene that links libraries the broker will not upload renders
        # EMPTY on the instance and comes back marked done. `UnresolvedLibraries`
        # is a `SceneError`, so it becomes the same 400 — a verdict about the
        # reference, decided before a GPU exists rather than after a 4K frame
        # that looks finished.
        scenes.require_resolvable_libraries(scene)
    except scenes.SceneError as exc:
        raise HTTPException(400, str(exc)) from None
    # Stored resolved and absolute. The queued row must not depend on a relative
    # path still meaning the same thing whenever it is finally dispatched.
    scene_key = str(scene)

    if broker.db.depth() >= config.MAX_QUEUE_DEPTH:
        eta = (broker.db.mean_render_sec() or 240) * broker.db.depth()
        return JSONResponse(
            {"error": "queue full", "depth": broker.db.depth(), "eta_sec": round(eta)},
            status_code=429, headers={"Retry-After": "60"},
        )
    if broker.db.queued_for_agent(agent) >= config.MAX_PER_AGENT_QUEUED:
        return JSONResponse(
            {"error": f"agent {agent} has too many queued jobs",
             "limit": config.MAX_PER_AGENT_QUEUED},
            status_code=429, headers={"Retry-After": "30"},
        )

    # Record what this still is a render OF, not just how long it took. The
    # hash is memoised on (mtime, size), so after the first submit for a given
    # revision this costs a stat. It is what lets one deliberate test frame at
    # delivery settings become the basis of a batch cost projection instead of
    # the batch being projected from whatever previews happen to be on record.
    try:
        still_hash = seq.spec_hash(spec, remote.scene_hash(scene))
    except Exception as exc:
        still_hash = None
        log.warning("could not hash %s; this render will not be usable as a cost "
                    "projection basis: %s", scenes.label(scene), remote.diagnose(exc))

    job_id = broker.db.submit(spec, agent=agent, prio=prio, scene=scene_key,
                              spec_hash=still_hash)
    return {"job_id": job_id, "state": "queued", "depth": broker.db.depth(),
            "scene": scenes.label(scene), "spec_hash": still_hash}


@app.post("/sequences")
async def submit_range(request: Request):
    """Submit one contiguous frame range as a single job.

    Everything expensive is decided here, before a GPU exists: the range is
    validated, the sequence name is validated (it becomes a directory), the
    resume set is computed from frames already on disk, and a spec that would
    put a seam in an existing sequence is refused with 409 rather than
    discovered three hours into the render.
    """
    body = await request.json()
    spec = dict(body.get("spec") or {})
    agent = str(body.get("agent") or "anon")[:64]
    prio = int(body.get("prio") or 100)

    missing = REQUIRED_SPEC - spec.keys()
    if missing:
        raise HTTPException(400, f"incomplete spec, missing: {sorted(missing)}")

    try:
        name = seq.valid_name(body.get("name"))
        # Comma forms included: `1-40,57,90-93`. `summarise()` prints missing
        # frames in exactly this syntax, so what `rq seq status` reports is what
        # this accepts back — the resume of three scattered holes is one job.
        frames = seq.parse_frames(body.get("frames") or "")
        first, last, step = seq.bounds(frames)
    except seq.SeqError as exc:
        raise HTTPException(400, str(exc)) from None
    try:
        scene = scenes.resolve_scene(body.get("scene"))
        # A sequence is where this defect is worst: one unresolved library
        # returns five hundred plausible, empty frames that pass every per-frame
        # check and are only questioned when somebody watches the shot.
        scenes.require_resolvable_libraries(scene)
    except scenes.SceneError as exc:
        raise HTTPException(400, str(exc)) from None

    if len(frames) > config.MAX_FRAMES_PER_JOB:
        raise HTTPException(
            400,
            f"{len(frames)} frames in one job is over the {config.MAX_FRAMES_PER_JOB} "
            f"limit — split the shot into several ranges under the same --name, "
            f"which share one resume record and one output directory",
        )

    # Hashing a 288 MB scene is ~1 s and it is what ties these frames to the
    # exact .blend that produced them. Done at submit so a mismatch is a 409
    # now rather than a stopped render later.
    digest = remote.scene_hash(scene)
    want_hash = seq.spec_hash(spec, digest)
    plan = seq.plan_range(broker.db, name, frames, want_hash)
    if plan.conflict:
        summary = broker.db.seq_summary(name)
        raise HTTPException(
            409,
            f"sequence {name!r} already holds {len(plan.conflict)} frame(s) rendered "
            f"from a different spec or a different .blend "
            f"({seq.summarise(plan.conflict)}; existing spec hashes "
            f"{summary['spec_hashes']}, this one {want_hash}). Continuing would put "
            f"an invisible seam in the middle of the shot. Use a new name, or remove "
            f"those frames deliberately.",
        )

    if broker.db.depth() >= config.MAX_QUEUE_DEPTH:
        return JSONResponse({"error": "queue full", "depth": broker.db.depth()},
                            status_code=429, headers={"Retry-After": "60"})

    job_id = broker.db.submit_range(
        spec, seq=name, first=first, last=last, step=step,
        frames_total=len(frames), spec_hash=want_hash,
        agent=agent, prio=prio, scene=str(scene),
        # Stored only when the three columns above do NOT describe the request.
        # A plain range keeps costing no JSON, and a row that predates this
        # column still means exactly what it always meant.
        frame_list=(None if seq.is_run(frames, first, last, step) else frames),
    )
    # Best estimator first, and the ranking is by WHAT WAS MEASURED, not by how
    # many measurements there are. One frame of this exact spec and .blend beats
    # a thousand frames of something else: the thing that dominates render time
    # here is the scene and the resolution, so a mean over the wrong ones is
    # precise and wrong. Never a model — only measurements, and if there are
    # none the estimate says so rather than inventing a number.
    mean, n, basis = broker.db.mean_sec_for_spec(want_hash)
    exact = mean is not None
    if mean is None:
        mean, n, basis = broker.db.mean_frame_sec(name), 0, f"frames of sequence {name!r}"
    if mean is None:
        mean, n, basis = broker.db.mean_frame_sec(), 0, "frames of OTHER sequences"
    if mean is None:
        mean, n, basis = broker.db.mean_render_sec(), 0, "single frames of OTHER scenes/specs"
    return {
        "job_id": job_id, "state": "queued", "seq": name,
        "frames": {"first": first, "last": last, "step": step,
                   "count": len(frames),
                   # What was actually requested, in the same syntax the status
                   # command prints. A caller that submitted `1-40,57` must be
                   # able to see that the broker did not read it as `1-57`.
                   "spec": seq.summarise(frames),
                   "contiguous": seq.is_run(frames, first, last, step)},
        "already_done": len(plan.have),
        "to_render": len(plan.todo),
        "stale": seq.summarise(plan.stale) if plan.stale else "",
        "spec_hash": want_hash, "scene": scenes.label(scene),
        "scene_hash": digest,
        "dir": str(seq.seq_dir(name)),
        "depth": broker.db.depth(),
        # A projection, plainly labelled as one. The caller decides whether to
        # spend it; the broker's job is to make sure the number is in front of
        # them before the GPU exists rather than after the invoice.
        "estimate": broker.cost_estimate(len(plan.todo), mean, basis=basis,
                                         samples=n, exact=exact),
        # Where the frames LAND. The instance's disk is guarded and is not the
        # one that fills — each frame is deleted there as soon as its fetch
        # verifies — so the only disk a 2,978-frame master can exhaust is this
        # one, and until now nothing measured it. See seq.local_space.
        "local_disk": seq.local_space(name, len(plan.todo),
                                      broker.db.mean_bytes_for_spec(want_hash)),
    }


@app.get("/sequences")
async def list_sequences():
    return {"sequences": [broker.db.seq_summary(n) for n in broker.db.seq_names()]}


@app.get("/sequences/{name}")
async def sequence_status(name: str, deep: bool = Query(False),
                          frames: str = Query(""),
                          remeasure: Optional[bool] = Query(None)):
    """What is actually on disk for this sequence, re-checked now.

    Never answers from the database alone. A row says a frame was delivered;
    only the file says it still is, and the difference is the whole reason this
    endpoint exists.

    `remeasure` re-decodes every frame and re-classifies it rather than trusting
    the measurement recorded at delivery. It follows `deep` unless stated,
    because `rq seq verify` is the command whose whole job is to not take the
    database's word for anything — and because it is the only thing that can
    find a blank frame delivered before this check existed. It is not free: a 4K
    frame costs ~0.5 s to decode, so a 3,000-frame shot is ~25 minutes.
    """
    try:
        name = seq.valid_name(name)
        wanted = seq.parse_frames(frames) if frames else None
    except seq.SeqError as exc:
        raise HTTPException(400, str(exc)) from None
    report = await asyncio.to_thread(seq.audit, broker.db, name, deep, wanted,
                                     remeasure)
    report["summary"] = broker.db.seq_summary(name)
    return report


@app.get("/sequences/{name}/stats")
async def sequence_stats(name: str):
    """Every frame's image measurements, in frame order.

    Nothing aggregated. Over 2,978 frames this is how a human actually finds the
    one that is wrong: dump it, sort by standard deviation, look at the top of
    the list. The outlier list is computed over the same rows so the answer and
    the evidence for it arrive together.
    """
    try:
        name = seq.valid_name(name)
    except seq.SeqError as exc:
        raise HTTPException(400, str(exc)) from None
    rows = await asyncio.to_thread(broker.db.frame_stats, name)
    measured = [{"frame": r["frame"], "mean": r["lum_mean"], "sd": r["lum_sd"],
                 "verdict": r["blank"]}
                for r in rows
                if r["lum_mean"] is not None and r["lum_sd"] is not None]
    return {"seq": name, "frames": rows,
            "measured": len(measured),
            "unmeasured": sum(1 for r in rows if r["lum_mean"] is None),
            "outliers": imgstat.outliers(measured),
            "thresholds": {"blank_sd": config.BLANK_SD_MAX,
                           "black_mean": config.BLACK_MEAN_MAX,
                           "suspect_sd": config.SUSPECT_SD_MAX,
                           "outlier_z": config.SEQ_OUTLIER_Z,
                           "outlier_window": config.SEQ_OUTLIER_WINDOW}}


@app.get("/budget")
async def budget():
    return broker.budget_report()


@app.post("/budget")
async def set_budget(request: Request):
    """Raise or lower the cumulative spend cap without restarting the broker.

    A restart is not a free operation here — it is the moment an instance can be
    orphaned or destroyed — so "the batch needs a bigger ceiling" must not
    require one. Persisted, so it survives the restarts that do happen.
    """
    body = await request.json()
    try:
        usd = float(body.get("usd"))
    except (TypeError, ValueError):
        raise HTTPException(400, "usd must be a number") from None
    if usd < 0:
        raise HTTPException(400, "usd must be >= 0")
    was = broker.spend_cap()
    broker.db.set_meta("max_batch_usd", usd)
    log.warning("spend cap set to $%.2f (was $%.2f); spent so far $%.2f",
                usd, was, broker.total_spend())
    if broker.paused and "cap" in (broker.paused or ""):
        broker.resume()
    return broker.budget_report()


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, wait: float = Query(0, ge=0, le=300)):
    """Long-poll when `wait` is set. Fifty agents polling in a loop would be
    pure overhead; blocking here costs nothing and returns the instant a job
    reaches a terminal state."""
    deadline = time.time() + wait
    while True:
        job = broker.db.get(job_id)
        if job is None:
            raise HTTPException(404, f"no such job: {job_id}")
        if job["state"] in TERMINAL or time.time() >= deadline:
            job.pop("spec", None)
            # Stored as a JSON string so the column stays one column; returned
            # as an object so `rq` does not have to know that.
            if job.get("stats"):
                with contextlib.suppress(TypeError, ValueError):
                    job["stats"] = json.loads(job["stats"])
            return job
        await asyncio.sleep(1.0)


@app.get("/jobs/{job_id}/result")
async def get_result(job_id: str):
    job = broker.db.get(job_id)
    if job is None:
        raise HTTPException(404, f"no such job: {job_id}")
    if job.get("seq"):
        # A sequence's result is a directory of thousands of files, not a body
        # this endpoint can return. Say so instead of trying to open a directory
        # as an image.
        raise HTTPException(
            409,
            f"job {job_id} rendered the frame sequence {job['seq']!r} into "
            f"{job.get('result_path')} — there is no single file to download. "
            f"Use `rq seq status {job['seq']}` to check it, and read the frames "
            f"from that directory.",
        )
    if job["state"] != "done":
        raise HTTPException(409, f"job is {job['state']}, not done")
    path = Path(job["result_path"] or "")
    if not path.exists():
        raise HTTPException(410, "result file is gone")
    return FileResponse(path, media_type="image/png", filename=path.name)


@app.delete("/jobs/{job_id}")
async def cancel(job_id: str):
    if not broker.db.get(job_id):
        raise HTTPException(404, f"no such job: {job_id}")
    return {"canceled": broker.db.cancel(job_id)}


@app.get("/queue")
async def queue():
    return {
        "counts": broker.db.counts(),
        "depth": broker.db.depth(),
        "mean_render_sec": broker.db.mean_render_sec(),
        "paused": broker.paused,
        "idle_sec": round(time.time() - broker.last_work, 1),
        "fleet": broker.fleet.snapshot(),
        "scene": str(config.SCENE),
        "scene_root": str(config.SCENE_ROOT),
        # Which .blend the worker actually has loaded right now, and how much
        # work is waiting per scene — the two things you need to reason about a
        # queue that batches by scene.
        "loaded_scene": (scenes.label(broker.fleet.scene_path)
                         if broker.fleet.scene_path else None),
        "scene_batch": broker.scene_batch,
        "scene_depth": {
            (scenes.label(Path(k)) if k else "(default)"): n
            for k, n in broker.db.depth_by_scene().items()
        },
        "recent": [
            {k: j.get(k) for k in (
                "id", "agent", "state", "render_sec", "err", "scene",
                # Which dispatcher owns this row, and how long the box spent on
                # it if it was a build. Without `kind` an exec failure in
                # `rq status -v` is indistinguishable from a render failure, and
                # they are diagnosed in completely different places.
                "kind", "exec_sec", "bundle",
                # Live render progress, so `rq status` can say something about a
                # running job instead of only naming it.
                "prog_sample", "prog_total", "prog_tile", "prog_tiles",
                "prog_pct", "prog_elapsed", "prog_remaining", "prog_phase",
                # WHEN that progress was last seen and last actually moved, on
                # the broker's clock. Without these the counters above are
                # indistinguishable from a frozen file: on 2026-08-04 the
                # instance's container was restarted mid-frame, progress.json
                # stopped being written, and `rq status` reprinted "sample
                # 304/512, 1m41s elapsed" identically for five minutes while
                # the GPU sat at 0%. The staleness was already recorded here
                # and simply never left the broker.
                "prog_seen", "prog_advanced",
                # Sequence progress. A multi-hour range needs both numbers: the
                # frame counter proves the batch is moving, the sample counter
                # proves the current frame is.
                "seq", "frame_first", "frame_last", "frame_step",
                "frames_total", "frames_done", "frames_failed", "frame_current",
            )}
            for j in broker.db.recent(15)
        ],
        "budget": broker.budget_report(),
        "exec": broker.execsvc.snapshot(),
    }


@app.post("/exec")
async def submit_exec(request: Request):
    """Queue one `blender -b -P <entry>` run on the rented box's CPUs.

    Everything expensive and everything refusable happens here, before an
    instance exists: the bundle root is contained, the inputs are globbed and
    content-addressed, the worker's own required set is checked, and admission
    control applies. A caller learns its spec is wrong in milliseconds instead
    of after a rental.

    The hashing runs OFF the event loop. It is a few megabytes today, but the
    loop is what answers `rq status`, and a handler that reads files on it is
    how a busy broker starts looking dead.
    """
    body = await request.json()
    spec = dict(body.get("spec") or {})
    agent = str(body.get("agent") or "anon")[:64]
    prio = int(body.get("prio") or 100)

    # The worker's set, enforced here too, so a missing field is a 400 rather
    # than a job that is dispatched, rejected on the box, and retried twice.
    missing = execservice.CALLER_REQUIRED - spec.keys()
    if missing:
        raise HTTPException(400, f"incomplete exec spec, missing: {sorted(missing)}")
    for field in ("bundle_root", "bundle_patterns"):
        if field not in body:
            raise HTTPException(
                400, f"{field} is required — an exec job's input is a code tree, "
                     f"content-addressed at submit so the build cannot silently "
                     f"come from different code than was asked for")

    if broker.db.depth() >= config.MAX_QUEUE_DEPTH:
        return JSONResponse({"error": "queue full", "depth": broker.db.depth()},
                            status_code=429, headers={"Retry-After": "60"})
    if broker.db.queued_for_agent(agent) >= config.MAX_PER_AGENT_QUEUED:
        return JSONResponse(
            {"error": f"agent {agent} has too many queued jobs",
             "limit": config.MAX_PER_AGENT_QUEUED},
            status_code=429, headers={"Retry-After": "30"})

    try:
        bundle = await asyncio.to_thread(
            execservice.plan_bundle, body["bundle_root"], body["bundle_patterns"])
    except execservice.ExecError as exc:
        raise HTTPException(400, str(exc)) from None

    # An optional input scene, hashed HERE so what travels is a digest. See
    # execservice.resolve_scene: a build that asks for a blend BY NAME and gets
    # whichever file currently answers to it is the trap the 0.1449 m travel
    # guard exists because of, and inside exec the caller cannot even see which
    # file it got.
    row_spec = dict(spec)
    if body.get("scene"):
        try:
            digest, name, size = await asyncio.to_thread(
                execservice.resolve_scene, body["scene"])
        except execservice.ExecError as exc:
            raise HTTPException(400, str(exc)) from None
        row_spec["scene_digest"] = digest
        row_spec["scene_name"] = name
        row_spec["scene_bytes"] = size
        row_spec["scene_path"] = str(Path(body["scene"]).expanduser().resolve())
    row_spec["bundle"] = bundle.digest
    row_spec["bundle_root"] = str(bundle.root)
    row_spec["bundle_patterns"] = list(body["bundle_patterns"])
    job_id = broker.db.submit(row_spec, agent=agent, prio=prio, kind="exec",
                              bundle=bundle.digest)
    return {"job_id": job_id, "state": "queued", "kind": "exec",
            "bundle": bundle.digest, "bundle_files": len(bundle.members),
            "bundle_bytes": bundle.bytes, "depth": broker.db.depth()}


@app.get("/health")
async def health():
    return {"ok": True, "paused": broker.paused, "fleet": broker.fleet.snapshot()}


@app.post("/resume")
async def resume():
    broker.resume()
    return {"paused": None}


@app.post("/teardown")
async def teardown():
    """Destroy the instance now, without stopping the broker. Queued jobs will
    rent a fresh one when dispatch resumes.

    Off the event loop, because it is minutes of blocking SSH and vast API calls
    behind the fleet lock. Run inline it froze *every* HTTP handler for the whole
    teardown — `rq status` included — which reads to a client exactly like the
    broker having died, the symptom this whole investigation started from.
    """
    await asyncio.to_thread(broker.fleet.teardown, "api request")
    snap = broker.fleet.snapshot()
    # NAME THE CARDS THIS TEARDOWN DID NOT TOUCH. Label scoping is what stops
    # two brokers reaping each other, and its unavoidable cost is that neither
    # can see the other's spend — so the obvious action reports success while
    # half the money keeps running. Reported, never destroyed: a sibling
    # broker's box may have a frame in flight, and cross-broker destruction is
    # the precise bug the label split exists to prevent.
    try:
        others = await asyncio.to_thread(vastctl.other_instances, broker.fleet.client)
    except Exception as exc:                                   # pragma: no cover
        snap["others_error"] = remote.diagnose(exc)
        log.warning("teardown: could not check for other instances: %s",
                    remote.diagnose(exc))
        return snap
    if others:
        snap["still_billing"] = [
            {"id": i.id, "label": i.label, "dph": round(i.dph, 4)} for i in others
        ]
        total = sum(i.dph for i in others)
        log.warning(
            "TEARDOWN IS PARTIAL — %d other instance(s) on this account are "
            "STILL BILLING at $%.4f/hr combined: %s. They carry a different "
            "label, so this broker can neither see nor destroy them. Tear each "
            "one down through its own broker.",
            len(others), total,
            ", ".join(f"{i.id} ({i.label}) ${i.dph:.4f}/hr" for i in others))
    return snap


def main() -> None:
    import uvicorn

    diagnostics.install("main")
    if os.environ.get("VASTRENDER_SUPERVISED") == "1":
        # Started by scripts/brokerd.sh. Tie our life to the supervisor's so a
        # dead supervisor can never leave an unsupervised broker holding the
        # singleton lock and refusing every later start.
        diagnostics.parent_death_signal()

    # Two independent guards against a second broker, because this one cost
    # real money twice.
    #
    # 1. The singleton lock. Authoritative, and released by the kernel however
    #    this process dies.
    try:
        LOCK.acquire()
    except BrokerAlreadyRunning as exc:
        log.error("%s", exc)
        raise SystemExit(3) from None

    cfg = uvicorn.Config(app, host=config.BROKER_HOST, port=config.BROKER_PORT,
                         log_level="warning")

    # 2. Bind the listening socket *before* handing control to uvicorn, so a
    #    port clash fails here — harmlessly, with nothing adopted and no
    #    dispatcher running — instead of inside lifespan startup after
    #    adopt_or_reap has already taken ownership of a live instance.
    #    bind_socket() exits non-zero on EADDRINUSE by itself.
    sock = cfg.bind_socket()
    uvicorn.Server(cfg).run(sockets=[sock])


if __name__ == "__main__":
    main()
