#!/usr/bin/env python3
"""Job store. SQLite in WAL mode *is* the queue — one file is simultaneously
the durability layer, the status table, and the dispatch order.

Write rate here is a few rows per minute, so WAL's single-writer limit is
irrelevant while its unlimited concurrent readers mean a 50-agent status poll
never blocks a dispatch. It also means committed jobs survive a broker crash
for free, with no AOF tuning or second daemon to supervise.

Claiming uses **leases, not locks**: a crashed broker leaves rows in `running`
with an expiring lease, and the next startup re-claims them. A lock would leave
them stranded.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config

# How long a job has EFFECTIVELY been waiting: its real age plus the head start
# its priority buys, clamped both ways. Shared by the two queries that choose
# which scene runs next, so the switch target and the starvation signal can
# never disagree about who is waiting longest — they used to be two hand-copied
# `ORDER BY created ASC` clauses.
#
# Bound-carrying: because the boost is clamped and the age is not, a deferred
# scene always wins eventually. See config.SCENE_PRIO_BOOST_MAX_SEC.
_EFF_AGE = ("((? - created) + MIN(MAX((? - prio) * ?, -?), ?))")


def _eff_args(now: float) -> tuple:
    """The five bind values `_EFF_AGE` needs, in order."""
    return (now, config.DEFAULT_PRIO, config.SCENE_PRIO_BOOST_SEC,
            config.SCENE_PRIO_BOOST_MAX_SEC, config.SCENE_PRIO_BOOST_MAX_SEC)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    agent       TEXT NOT NULL DEFAULT 'anon',
    spec        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'queued',
    prio        INTEGER NOT NULL DEFAULT 100,
    created     REAL NOT NULL,
    started     REAL,
    finished    REAL,
    lease       REAL NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    result_path TEXT,
    render_sec  REAL,
    err         TEXT
);
CREATE INDEX IF NOT EXISTS ix_dispatch ON jobs(state, prio, created);
CREATE INDEX IF NOT EXISTS ix_agent    ON jobs(agent, created);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

-- One row per rendered frame of a named sequence. This table, plus the file it
-- points at, IS the resume record: a re-submitted range renders only the frames
-- that are absent here or whose file no longer verifies.
--
-- Keyed on (seq, frame) rather than on a job id on purpose. A 3,000-frame
-- cinematic will be rendered by many jobs across many rented instances over
-- days, and "has frame 1841 been rendered" must survive every one of those
-- boundaries. Job ids do not.
--
-- `spec_hash` is what stops a resume from silently mixing settings. Two frames
-- of a cut-free video rendered at different sample counts or with different
-- DOF are a seam, and a seam is a delivery-blocking defect that nobody spots by
-- looking at either frame on its own.
CREATE TABLE IF NOT EXISTS frames (
    seq        TEXT    NOT NULL,
    frame      INTEGER NOT NULL,
    state      TEXT    NOT NULL DEFAULT 'done',
    job_id     TEXT,
    path       TEXT,
    bytes      INTEGER,
    width      INTEGER,
    height     INTEGER,
    sha256     TEXT,
    render_sec REAL,
    spec_hash  TEXT,
    finished   REAL,
    err        TEXT,
    PRIMARY KEY (seq, frame)
);
CREATE INDEX IF NOT EXISTS ix_frames_seq ON frames(seq, state);
"""

TERMINAL = ("done", "failed", "canceled")


class DB:
    """One connection per thread.

    Sharing a single connection across the dispatch thread, the heartbeat
    thread and FastAPI's event loop is not merely a locking question:
    transactions belong to the *connection*. With one shared handle, an HTTP
    `submit()` commit lands inside the dispatcher's open `BEGIN IMMEDIATE` and
    ends it early, so `claim()`'s own COMMIT raises while its `state='running'`
    update has already been committed by the interloper — leaving a job marked
    running that nobody is executing until its lease expires an hour later.

    Per-thread connections make each transaction private, and WAL keeps
    concurrent readers non-blocking.
    """

    def __init__(self, path: Path, default_scene: Optional[str] = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._local = threading.local()
        self.conn.executescript(SCHEMA)
        self._migrate(default_scene)
        self.conn.commit()

    # Live render progress, polled off the instance. Added by migration rather
    # than baked into SCHEMA so an existing broker.db picks them up in place —
    # the alternative is dropping a database that holds the queue.
    #
    # `prog_seen` and `prog_advanced` are on the BROKER's clock, never the
    # worker's: the rented instance's clock was measured running ~9 minutes
    # ahead of this machine's, so any stall arithmetic mixing the two would be
    # nonsense. Only `prog_elapsed` comes from the worker, and that is a
    # duration measured entirely within the worker, so skew cannot touch it.
    PROGRESS_COLUMNS = {
        # Which .blend this job renders. NULL means "the broker's default
        # scene", which is what every job submitted before per-job scenes
        # existed means — so old rows keep working without a backfill.
        "scene": "TEXT",
        "prog_sample": "INTEGER",
        "prog_total": "INTEGER",
        # Big frames render in tiles and the sample counter restarts per tile,
        # so the tile pair is part of the position, not decoration.
        "prog_tile": "INTEGER",
        "prog_tiles": "INTEGER",
        "prog_pct": "REAL",
        "prog_elapsed": "REAL",
        "prog_remaining": "REAL",
        "prog_phase": "TEXT",
        "prog_seen": "REAL",
        "prog_advanced": "REAL",
        # --- frame-sequence jobs ---
        # NULL `seq` means a still: one job, one image, exactly as before. A
        # non-NULL one names the sequence this job contributes frames to, which
        # is the key the resume record is written under.
        "seq": "TEXT",
        "frame_first": "INTEGER",
        "frame_last": "INTEGER",
        "frame_step": "INTEGER",
        # The frames this job covers, as a JSON list, when they are NOT the plain
        # arithmetic run frame_first..frame_last x frame_step — `1-40,57,90-93`.
        # NULL means "the run those three columns describe", which is what every
        # row written before comma forms existed means, so nothing is backfilled
        # and the common case still costs no JSON.
        "frame_list": "TEXT",
        "frames_total": "INTEGER",     # frames this job must produce (after resume)
        "frames_done": "INTEGER",
        "frames_failed": "INTEGER",
        "frame_current": "INTEGER",
        "spec_hash": "TEXT",
        # --- what was in the image, not whether the file was intact ---
        # Every other column here describes the FILE. These describe the
        # picture, and they exist because a structurally perfect, sha256-matched,
        # correctly dimensioned 640x480 PNG came back entirely black and was
        # recorded `done`. `blank` holds the verdict from broker/imgstat.py;
        # NULL means the job predates the check, which is NOT the same as OK.
        "blank": "TEXT",
        "lum_mean": "REAL",
        "lum_sd": "REAL",
        "stats": "TEXT",
        # Size of the image this job returned. Frames have carried one all
        # along; stills did not, which meant a deliberate 4K calibration render
        # — the exact thing the docs tell you to do before committing a batch —
        # taught the disk preflight nothing, and it fell back to averaging 720p
        # test frames. A projection that confident and that wrong is worse than
        # none, so the calibration render now counts.
        "bytes": "INTEGER",
    }

    # The same for a sequence's frames, migrated separately because they live in
    # their own table. A frame carries more of it: over 3,000 frames these
    # columns ARE the audit trail, and finding the one bad frame among them
    # should be a query rather than a re-render.
    FRAME_COLUMNS = {
        "blank": "TEXT",
        "lum_mean": "REAL",
        "lum_sd": "REAL",
        "lum_min": "REAL",
        "lum_max": "REAL",
        "lum_levels": "INTEGER",
        "stats": "TEXT",
    }

    # --- job KIND ---------------------------------------------------------
    #
    # `render` is a frame on the GPU; `exec` is a Blender process on the rented
    # box's CPUs, dispatched by a second thread up to EXEC_SLOTS at a time. They
    # share this table because they share everything that matters — fair-share
    # ordering, leases, the retry budget, cancellation, the audit trail — and
    # nothing about a queue row is render-specific.
    #
    # What they must NOT share is the dispatcher. The render dispatcher takes
    # one job at a time because the GPU is one device; the exec dispatcher takes
    # twelve because the box has 23 CPUs. So `claim` is now kind-filtered on
    # BOTH sides: an exec job reaching the render dispatcher would be sent to
    # the warm Blender server as a render spec and rejected for a missing
    # `camera`, and a render job reaching the exec dispatcher would be rejected
    # for a missing `bundle`. Both are silent-ish failures that burn attempts.
    EXEC_COLUMNS = {
        "kind": "TEXT NOT NULL DEFAULT 'render'",
        # The content-addressed input bundle this exec job runs against, kept
        # out of the spec blob so `rq status` and the eviction guard can see it
        # without parsing JSON.
        "bundle": "TEXT",
        # Where the fetched, verified outputs landed locally, as a JSON list.
        "outputs": "TEXT",
        "exec_sec": "REAL",
    }

    def _migrate(self, default_scene: Optional[str] = None) -> None:
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for col, typ in self.PROGRESS_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        for col, typ in self.EXEC_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
        # Every row that predates this column is a render, and the DEFAULT above
        # only applies to rows inserted afterwards — SQLite backfills existing
        # rows with the default on ALTER, but an explicit UPDATE costs nothing
        # and removes the need to remember which SQLite version does what.
        self.conn.execute("UPDATE jobs SET kind='render' WHERE kind IS NULL")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_kind ON jobs(kind, state, created)"
        )
        have_frames = {r["name"] for r in self.conn.execute("PRAGMA table_info(frames)")}
        for col, typ in self.FRAME_COLUMNS.items():
            if col not in have_frames:
                self.conn.execute(f"ALTER TABLE frames ADD COLUMN {col} {typ}")
        # Dispatch now filters by scene on every claim, so it needs an index of
        # its own; ix_dispatch leads with state and cannot serve it.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_scene ON jobs(scene, state, created)"
        )
        # Rows predating per-job scenes carry NULL, which is genuinely ambiguous:
        # `claim(scene=None)` means "any scene", while a NULL row means "the
        # default scene". Backfilling once removes the ambiguity instead of
        # spreading a special case through every query. Their scene *was* the
        # default at the time they were submitted, which is exactly this value.
        if default_scene:
            self.conn.execute(
                "UPDATE jobs SET scene=? WHERE scene IS NULL", (default_scene,)
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
        return conn

    # --- submit ----------------------------------------------------------

    def submit(self, spec: dict, agent: str = "anon", prio: int = 100,
               scene: Optional[str] = None, spec_hash: Optional[str] = None,
               kind: str = "render", bundle: Optional[str] = None) -> str:
        """Every submit creates a distinct row. Identical specs are *not*
        collapsed: a params hash cannot observe scene state, so reusing a prior
        render would silently serve a stale frame after a reassembly.

        `spec_hash` is stored for the same reason a sequence stores one, and it
        is what makes "render one frame first and ask again" a real instruction
        rather than a hint. Without it a still is an anonymous duration, so a
        cost projection for a 4K delivery batch could only average it in with
        every 1080p preview ever rendered — and that number is worse than no
        number, because it looks authoritative.
        """
        # The id is always minted here, never taken from the client. It becomes
        # both the primary key and a filename, so a caller-supplied value is a
        # path-traversal vector (".../opus5-car-render/..." would write into a
        # project this system must never modify) and a way for one agent to
        # collide with another's row and result.
        job_id = uuid.uuid4().hex[:12]
        spec = dict(spec, job_id=job_id)
        if kind not in ("render", "exec"):
            raise ValueError(f"unknown job kind {kind!r}")
        self.conn.execute(
            "INSERT INTO jobs (id, agent, spec, prio, created, scene, spec_hash, "
            "kind, bundle) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, agent, json.dumps(spec), prio, time.time(), scene, spec_hash,
             kind, bundle),
        )
        self.conn.commit()
        return job_id

    # --- dispatch --------------------------------------------------------

    def claim(self, lease_sec: float, scene: Optional[str] = None) -> Optional[dict]:
        """Take the next job under fair-share ordering.

        Priority alone starves: one agent submitting 50 jobs would monopolise
        the GPU. Ordering by each agent's recent service count first means an
        agent that just had ten renders sorts behind one that has had none.

        `scene` restricts the claim to jobs wanting that .blend. Fair-share
        still applies *within* the scene, so batching by scene never lets one
        agent monopolise the batch it happens to share with everyone else.
        Passing None claims from any scene.

        BEGIN IMMEDIATE takes the write lock up front so two dispatchers cannot
        select the same row before either updates it.
        """
        now = time.time()
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            # `scene IS ?` rather than `=`: a NULL scene means the default one,
            # and `NULL = NULL` is NULL in SQL, so `=` would never match a job
            # submitted before per-job scenes existed.
            scene_clause = "AND j.scene IS ?" if scene is not None else ""
            params: tuple = (now - 600, now)
            if scene is not None:
                params = params + (scene,)
            row = cur.execute(
                f"""
                SELECT j.* FROM jobs j
                LEFT JOIN (
                    SELECT agent, COUNT(*) n FROM jobs
                    WHERE state IN ('running','done') AND created > ?
                    GROUP BY agent
                ) s ON s.agent = j.agent
                WHERE (j.state = 'queued'
                       OR (j.state = 'running' AND j.lease < ?))
                  AND j.kind = 'render'
                  {scene_clause}
                ORDER BY COALESCE(s.n, 0) ASC, j.prio ASC, j.created ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                cur.execute("COMMIT")
                return None
            cur.execute(
                "UPDATE jobs SET state='running', started=COALESCE(started,?), "
                "lease=?, attempts=attempts+1 WHERE id=?",
                (now, now + lease_sec, row["id"]),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        # The row was SELECTed before the UPDATE, so its `attempts` is one
        # behind what the database now says. Returning the stale value made
        # every "is this a retry?" comparison in the dispatcher off by one:
        # the collect-a-finished-frame recovery gated on `attempts > 1` did
        # not open until the *third* attempt, and after a refunding requeue it
        # never opened at all — so a frame sitting finished on the instance
        # was re-rendered in full. Report what was actually written.
        job = dict(row)
        job["attempts"] = (row["attempts"] or 0) + 1
        return job

    def claim_exec(self, lease_sec: float) -> Optional[dict]:
        """Take the next EXEC job under the same fair-share ordering.

        A separate method rather than a `kind=` parameter on `claim`, because
        the two dispatchers are not the same shape and reading them side by side
        should make that obvious: `claim` batches by scene to avoid paying a
        worker restart and an OptiX prewarm on every alternation, and an exec
        job has no scene to batch by — its input is a code bundle, and switching
        between bundles costs a `cp -a` of 8 MB.

        Fair-share still applies, and still across BOTH kinds: the subquery
        counts every job an agent has had recently, so an agent that just had
        twelve builds sorts behind one that has had none, and it cannot use the
        exec queue to jump the render queue's fair-share either.
        """
        now = time.time()
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute(
                """
                SELECT j.* FROM jobs j
                LEFT JOIN (
                    SELECT agent, COUNT(*) n FROM jobs
                    WHERE state IN ('running','done') AND created > ?
                    GROUP BY agent
                ) s ON s.agent = j.agent
                WHERE (j.state = 'queued'
                       OR (j.state = 'running' AND j.lease < ?))
                  AND j.kind = 'exec'
                ORDER BY COALESCE(s.n, 0) ASC, j.prio ASC, j.created ASC
                LIMIT 1
                """,
                (now - 600, now),
            ).fetchone()
            if row is None:
                cur.execute("COMMIT")
                return None
            cur.execute(
                "UPDATE jobs SET state='running', started=COALESCE(started,?), "
                "lease=?, attempts=attempts+1 WHERE id=?",
                (now, now + lease_sec, row["id"]),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        job = dict(row)
        job["attempts"] = (row["attempts"] or 0) + 1
        return job

    def exec_inflight(self) -> int:
        """Exec rows this broker believes are executing right now."""
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE kind='exec' AND state='running' "
            "AND lease > ?", (time.time(),)
        ).fetchone()
        return row["n"]

    def exec_waiting(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE kind='exec' AND (state='queued' "
            "OR (state='running' AND lease < ?))", (time.time(),)
        ).fetchone()
        return row["n"]

    def finish_exec(self, job_id: str, outputs: list, exec_sec: float) -> None:
        """Record a delivered exec job: where its verified outputs landed."""
        self.conn.execute(
            "UPDATE jobs SET state='done', finished=?, exec_sec=?, outputs=?, "
            "result_path=?, err=NULL WHERE id=?",
            (time.time(), exec_sec, json.dumps(outputs),
             outputs[0] if outputs else None, job_id),
        )
        self.conn.commit()

    def oldest_waiting_scene(
        self, exclude_scene: Optional[str] = None
    ) -> tuple[Optional[str], float]:
        """The scene of the oldest waiting job, and how long it has waited.

        "Oldest" is EFFECTIVE age — real wait plus the head start `prio` buys.
        Priority used to stop dead at the scene boundary: it ordered jobs
        inside `claim` and did nothing for which scene got loaded, which was
        pure FIFO on `created`. A `prio 10` job on a fresh scene therefore lost
        to a `prio 100` job on an older one for as long as that scene had work
        — measured 2026-08-03, a 13.6 s render queued 41 minutes behind.

        Choosing the largest effective age is what bounds unfairness between
        scenes: however long a batch runs, the scene waiting longest is served
        next, and because the priority head start is CLAMPED while age is not,
        a deferred scene always wins eventually. See
        config.SCENE_PRIO_BOOST_MAX_SEC for the bound that makes that true.

        `exclude_scene` is what makes SCENE_BATCH_MAX a real bound rather than
        a suggestion. Without it a capped batch re-ran this query, got the
        loaded scene back — it still held the oldest job — and reset the batch
        counter, so the cap could be reached indefinitely without ever yielding.
        A scene submitted later than a 60-job batch waited for all 60, whatever
        the cap said. Excluding the loaded scene once the cap is reached turns
        "at most N consecutive jobs while another scene waits" into something
        the dispatcher actually enforces.
        """
        now = time.time()
        if exclude_scene is None:
            row = self.conn.execute(
                f"SELECT scene, created, {_EFF_AGE} eff FROM jobs "
                "WHERE kind='render' AND (state='queued' OR "
                "(state='running' AND lease < ?)) "
                "ORDER BY eff DESC, created ASC LIMIT 1",
                (*_eff_args(now), now),
            ).fetchone()
        else:
            row = self.conn.execute(
                f"SELECT scene, created, {_EFF_AGE} eff FROM jobs "
                "WHERE kind='render' AND (state='queued' OR "
                "(state='running' AND lease < ?)) AND scene IS NOT ? "
                "ORDER BY eff DESC, created ASC LIMIT 1",
                (*_eff_args(now), now, exclude_scene),
            ).fetchone()
        if row is None:
            return None, 0.0
        return row["scene"], max(0.0, now - row["created"])

    def oldest_waiting_age(self, exclude_scene: Optional[str]) -> Optional[float]:
        """EFFECTIVE seconds the oldest job wanting a *different* scene has waited.

        Effective, not raw, and the same expression `oldest_waiting_scene` uses
        — so the decision to switch and the choice of what to switch TO always
        agree about who has waited longest. When they disagreed, a high-priority
        job could win the target query while never clearing the threshold that
        triggers a switch at all, which is `prio` looking like it works and not
        working.
        """
        now = time.time()
        if exclude_scene is None:
            row = self.conn.execute(
                f"SELECT {_EFF_AGE} eff FROM jobs WHERE kind='render' AND "
                "(state='queued' OR (state='running' AND lease < ?)) "
                "AND scene IS NOT NULL ORDER BY eff DESC LIMIT 1",
                (*_eff_args(now), now),
            ).fetchone()
        else:
            row = self.conn.execute(
                f"SELECT {_EFF_AGE} eff FROM jobs WHERE kind='render' AND "
                "(state='queued' OR (state='running' AND lease < ?)) "
                "AND scene IS NOT ? ORDER BY eff DESC LIMIT 1",
                (*_eff_args(now), now, exclude_scene),
            ).fetchone()
        return None if row is None else max(0.0, row["eff"])

    def scene_blank_verdict_history(self, scene: Optional[str]) -> tuple[int, int, Optional[float]]:
        """(times this scene rendered blank, times it rendered fine, when last fine).

        The one fact that separates "this scene is broken" from "the farm is
        broken", and the one nobody had when three agents each reported a black
        4K frame within an hour on 2026-08-04. Answering it by hand took an
        afternoon of SQL; the farm was exonerated by a single observation —
        every scene that ever rendered black had rendered black on 100% of its
        attempts, and no scene had ever produced both. A GPU that had gone bad
        would blacken whatever ran on it next, so a scene with a clean history
        and a sudden blank is a farm question, and a scene that has never once
        produced a picture is a scene question. Cheap enough to run on the
        failure path, which is the only place it is needed.
        """
        if not scene:
            return (0, 0, None)
        row = self.conn.execute(
            "SELECT "
            " SUM(CASE WHEN blank IS NOT NULL AND blank != 'OK' THEN 1 ELSE 0 END) blk,"
            " SUM(CASE WHEN state='done' AND (blank IS NULL OR blank='OK') THEN 1 ELSE 0 END) ok,"
            " MAX(CASE WHEN state='done' AND (blank IS NULL OR blank='OK') THEN finished END) lastok"
            " FROM jobs WHERE scene = ?",
            (scene,),
        ).fetchone()
        if row is None:
            return (0, 0, None)
        return (int(row["blk"] or 0), int(row["ok"] or 0), row["lastok"])

    def depth_by_scene(self) -> dict[str, int]:
        """Waiting jobs per scene, for `rq status`. Key "" is the default scene."""
        now = time.time()
        rows = self.conn.execute(
            "SELECT COALESCE(scene,'') k, COUNT(*) n FROM jobs "
            "WHERE kind='render' AND (state='queued' OR "
            "(state='running' AND lease < ?)) "
            "GROUP BY k ORDER BY n DESC", (now,),
        ).fetchall()
        return {r["k"]: r["n"] for r in rows}

    def renew(self, job_id: str, lease_sec: float) -> None:
        self.conn.execute(
            "UPDATE jobs SET lease=? WHERE id=? AND state='running'",
            (time.time() + lease_sec, job_id),
        )
        self.conn.commit()

    def set_progress(self, job_id: str, sample: Optional[int], total: Optional[int],
                     pct: Optional[float], elapsed: Optional[float],
                     remaining: Optional[float], phase: Optional[str],
                     advanced: bool, tile: Optional[int] = None,
                     tiles: Optional[int] = None) -> None:
        """Record a progress observation for a running job.

        `prog_advanced` only moves when the sample counter actually changed,
        which is what makes it a usable stall signal — bumping it on every poll
        would mean the watchdog watched its own polling, not the render.
        """
        now = time.time()
        if advanced:
            self.conn.execute(
                "UPDATE jobs SET prog_sample=?, prog_total=?, prog_tile=?, prog_tiles=?, "
                "prog_pct=?, prog_elapsed=?, prog_remaining=?, prog_phase=?, "
                "prog_seen=?, prog_advanced=? WHERE id=? AND state='running'",
                (sample, total, tile, tiles, pct, elapsed, remaining, phase,
                 now, now, job_id),
            )
        else:
            self.conn.execute(
                "UPDATE jobs SET prog_sample=?, prog_total=?, prog_tile=?, prog_tiles=?, "
                "prog_pct=?, prog_elapsed=?, prog_remaining=?, prog_phase=?, "
                "prog_seen=?, prog_advanced=COALESCE(prog_advanced, ?) "
                "WHERE id=? AND state='running'",
                (sample, total, tile, tiles, pct, elapsed, remaining, phase,
                 now, now, job_id),
            )
        self.conn.commit()

    def clear_progress(self, job_id: str) -> None:
        """Wipe progress when a job leaves `running`, so a finished or requeued
        job never displays a frozen counter from its last attempt."""
        self.conn.execute(
            "UPDATE jobs SET prog_sample=NULL, prog_total=NULL, prog_tile=NULL, prog_tiles=NULL, prog_pct=NULL, "
            "prog_elapsed=NULL, prog_remaining=NULL, prog_phase=NULL, "
            "prog_seen=NULL, prog_advanced=NULL WHERE id=?",
            (job_id,),
        )
        self.conn.commit()

    def finish(self, job_id: str, path: str, render_sec: float,
               stats: Optional[dict] = None, size: Optional[int] = None) -> None:
        # Only a running job may complete. Without the guard, a job cancelled
        # mid-render is flipped back to 'done' when the render lands, and the
        # cancel silently did nothing.
        self.conn.execute(
            "UPDATE jobs SET state='done', finished=?, result_path=?, render_sec=?, "
            "bytes=COALESCE(?, bytes), err=NULL "
            "WHERE id=? AND state='running'",
            (time.time(), path, render_sec, size, job_id),
        )
        # Written even for a job that failed on being blank, and written
        # separately from the state so the numbers survive whatever verdict the
        # row ends up with. A record of "this is what the pixels were" is the
        # only thing that lets a human argue with the classifier later.
        self.set_image_stats(job_id, stats)
        self.conn.commit()

    def set_image_stats(self, job_id: str, stats: Optional[dict]) -> None:
        if not stats:
            return
        self.conn.execute(
            "UPDATE jobs SET blank=?, lum_mean=?, lum_sd=?, stats=? WHERE id=?",
            (stats.get("verdict"), stats.get("mean"), stats.get("sd"),
             json.dumps(stats, separators=(",", ":")), job_id),
        )
        self.conn.commit()

    def fail(self, job_id: str, err: str, max_attempts: int) -> str:
        """Requeue unless the job has burned its attempts. Keeping failed rows
        (rather than deleting) is what makes failures visible in `status`
        instead of silently vanishing."""
        row = self.conn.execute(
            "SELECT attempts, state FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            return "failed"
        # A cancelled job must stay cancelled. Requeueing it here would
        # resurrect work the caller explicitly stopped.
        if row["state"] in ("canceled", "done"):
            return row["state"]
        attempts = row["attempts"]
        state = "failed" if attempts >= max_attempts else "queued"
        self.conn.execute(
            "UPDATE jobs SET state=?, err=?, finished=? WHERE id=? AND state='running'",
            (state, err[:2000], time.time() if state == "failed" else None, job_id),
        )
        self.conn.commit()
        return state

    def fail_terminal(self, job_id: str, err: str) -> str:
        """Fail a job for good, whatever its attempt count.

        `fail()` retries, which is right for everything that might be transport.
        It is wrong for a render that succeeded and produced an image with
        nothing in it: the second and third attempts render the same empty frame
        for the same money and reach the same verdict. A cancelled or already
        finished job is still left alone.
        """
        self.conn.execute(
            "UPDATE jobs SET state='failed', err=?, finished=? "
            "WHERE id=? AND state='running'",
            (err[:2000], time.time(), job_id),
        )
        self.conn.commit()
        return "failed"

    def requeue(self, job_id: str, err: str = "") -> bool:
        """Put a running job back on the queue *without* counting it as failed.

        `fail()` is the wrong tool when the work is still in flight. It burns an
        attempt, and once the attempts run out it writes `failed` — which is how
        a job that the instance was rendering at sample 6896/8192 ended up in
        the queue as a failure while its PNG was still being written.

        The attempt is given back, because the job did not fail: the broker lost
        track of it. `claim()` increments on the way back in, so this leaves the
        count where it started and the retry budget still means "how many times
        did this job actually go wrong".
        """
        cur = self.conn.execute(
            "UPDATE jobs SET state='queued', lease=0, err=?, "
            "attempts=MAX(0, attempts-1) WHERE id=? AND state='running'",
            (err[:2000], job_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def cancel(self, job_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE jobs SET state='canceled', finished=? WHERE id=? AND state NOT IN "
            "('done','failed','canceled')",
            (time.time(), job_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def requeue_expired(self) -> int:
        """Reclaim jobs whose lease lapsed — the crash-recovery path."""
        # Progress is cleared with the requeue: a retry that inherited the
        # previous attempt's counters would look stalled the instant it was
        # claimed, and the watchdog would cry wolf on a job that just started.
        cur = self.conn.execute(
            "UPDATE jobs SET state='queued', prog_sample=NULL, prog_total=NULL, "
            "prog_pct=NULL, prog_elapsed=NULL, prog_remaining=NULL, prog_phase=NULL, "
            "prog_seen=NULL, prog_advanced=NULL "
            "WHERE state='running' AND lease < ?",
            (time.time(),),
        )
        self.conn.commit()
        return cur.rowcount

    def requeue_all_running(self) -> int:
        """Reclaim every running job. Startup only.

        A freshly started broker is, by definition, not executing anything. Any
        row still marked running was claimed by a process that no longer exists,
        and waiting for its lease to lapse strands the job for up to an hour
        while a GPU sits idle. Lease expiry is the right tool for a *live*
        broker losing a job; process start is unambiguous.
        """
        cur = self.conn.execute(
            "UPDATE jobs SET state='queued', lease=0, prog_sample=NULL, prog_total=NULL, "
            "prog_pct=NULL, prog_elapsed=NULL, prog_remaining=NULL, prog_phase=NULL, "
            "prog_seen=NULL, prog_advanced=NULL WHERE state='running'"
        )
        self.conn.commit()
        return cur.rowcount

    # --- read ------------------------------------------------------------

    def get(self, job_id: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    def depth(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE state IN ('queued','running')"
        ).fetchone()
        return row["n"]

    def queued_for_agent(self, agent: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE agent=? AND state='queued'", (agent,)
        ).fetchone()
        return row["n"]

    def recent(self, limit: int = 50) -> list[dict]:
        """The newest `limit` jobs, plus every running job unconditionally.

        The union is load-bearing, not a nicety. `recent` is what `rq status`
        renders, and a *running* row is the one row status exists to show —
        it is the only evidence that the GPU is working rather than wedged.
        Ordering by `created` alone loses exactly the wrong row: a long job is
        by definition old, so a burst of newer submissions pushes it out of the
        window while it is still running, and status then prints `running: 1`
        in the counts with no line explaining it.

        That is precisely how it failed. A 50-frame sequence job sat mid-batch
        and healthy, delivering a frame every ~76 s, while 19 newer jobs were
        queued behind it — past the 15-row window. Status went silent about it,
        `idle_sec` was sampled near the top of its normal per-frame sawtooth,
        and a working box read as a stalled one holding a dead slot. The next
        step after that reading is cancelling the job, which would have thrown
        away hours of correct frames.

        Running rows are unbounded in principle, but bounded by the dispatcher
        in practice: it runs one job at a time, so this adds one row, not a
        flood.
        """
        rows = self.conn.execute(
            "SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()
        out = {r["id"]: dict(r) for r in rows}
        for r in self.conn.execute(
            "SELECT * FROM jobs WHERE state='running'"
        ).fetchall():
            out.setdefault(r["id"], dict(r))
        return sorted(out.values(), key=lambda j: j["created"], reverse=True)

    def mean_render_sec(self) -> Optional[float]:
        """Mean seconds per *still*.

        `seq IS NULL` is load-bearing. A sequence job's `render_sec` is the
        duration of the whole range — hours — and averaging that in with single
        frames turns the queue ETA and the cost projection into nonsense in the
        pessimistic direction, which is the direction that stops a batch being
        submitted at all.
        """
        row = self.conn.execute(
            "SELECT AVG(render_sec) a FROM jobs "
            "WHERE state='done' AND render_sec IS NOT NULL AND seq IS NULL"
        ).fetchone()
        return row["a"]

    def mean_frame_sec(self, seq: Optional[str] = None) -> Optional[float]:
        """Mean seconds per rendered FRAME, optionally for one sequence.

        This is the number a cost projection needs. Frames of the sequence being
        extended are the best estimator there is — same scene, same settings,
        same hardware class — so a caller asks for those first and falls back to
        every frame ever rendered.
        """
        if seq is not None:
            row = self.conn.execute(
                "SELECT AVG(render_sec) a FROM frames "
                "WHERE state='done' AND render_sec > 0 AND seq=?", (seq,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT AVG(render_sec) a FROM frames "
                "WHERE state='done' AND render_sec > 0"
            ).fetchone()
        return row["a"]

    def mean_sec_for_spec(self, spec_hash: str) -> tuple[Optional[float], int, str]:
        """Mean seconds per image for renders of EXACTLY this spec and .blend.

        Returns `(mean, sample_count, basis)` so the caller can say what the
        projection rests on. `spec_hash` folds in the scene's content hash and
        every image field, so a match means same geometry, same resolution,
        same samples, same engine — the only honest basis for projecting a
        batch that does not exist yet.

        Frames are preferred over stills purely because a frame of a sequence
        has already paid the warm-worker costs a batch will pay; a still of the
        same spec is the same render and is the *point* of this method — it is
        how a 4K delivery batch gets a real number from one deliberate test
        frame instead of an extrapolation from 1080p previews.
        """
        row = self.conn.execute(
            "SELECT AVG(render_sec) a, COUNT(*) n FROM frames "
            "WHERE state='done' AND render_sec > 0 AND spec_hash=?", (spec_hash,)
        ).fetchone()
        if row and row["n"]:
            return row["a"], int(row["n"]), "frames of this exact spec and .blend"
        row = self.conn.execute(
            "SELECT AVG(render_sec) a, COUNT(*) n FROM jobs "
            "WHERE state='done' AND render_sec IS NOT NULL AND seq IS NULL "
            "AND spec_hash=?", (spec_hash,)
        ).fetchone()
        if row and row["n"]:
            return row["a"], int(row["n"]), "single frame(s) rendered at this exact spec and .blend"
        return None, 0, ""

    def mean_bytes_for_spec(self, spec_hash: str) -> Optional[float]:
        """Mean size of a delivered frame of EXACTLY this spec and .blend.

        Same ranking rule as `mean_sec_for_spec`, and for the same reason: what
        a frame weighs is decided by its resolution and its content, so an
        average over 1080p previews of another scene is precise and wrong. Used
        to answer whether a batch fits on this machine's disk, which is a
        question whose wrong answer costs a multi-day render.
        """
        row = self.conn.execute(
            "SELECT AVG(bytes) a FROM frames WHERE state='done' AND bytes > 0 "
            "AND spec_hash=?", (spec_hash,),
        ).fetchone()
        if row and row["a"]:
            return row["a"]
        # A STILL of this exact spec and .blend. This is the case the docs send
        # people to — "render one frame at the batch's own resolution and
        # samples" — and it is the same image at the same size, so it is just as
        # good a basis as a frame is.
        row = self.conn.execute(
            "SELECT AVG(bytes) a FROM jobs WHERE state='done' AND seq IS NULL "
            "AND bytes > 0 AND spec_hash=?", (spec_hash,),
        ).fetchone()
        if row and row["a"]:
            return row["a"]
        # No frame of this exact kind yet. Any delivered frame is a weaker basis
        # and the caller labels it as one, but it is still a measurement — and
        # "no idea" is what let a 101 GB batch be submitted onto a 79 GiB disk.
        row = self.conn.execute(
            "SELECT AVG(bytes) a FROM frames WHERE state='done' AND bytes > 0"
        ).fetchone()
        return row["a"] if row and row["a"] else None

    # --- frame sequences -------------------------------------------------

    def submit_range(self, spec: dict, seq: str, first: int, last: int, step: int,
                     frames_total: int, spec_hash: str, agent: str = "anon",
                     prio: int = 100, scene: Optional[str] = None,
                     frame_list: Optional[list[int]] = None) -> str:
        """A job that renders a contiguous frame range rather than one image.

        The id is minted here for exactly the reason a still's is: it becomes a
        path. `seq` is caller-supplied — it has to be, because it is the key
        that makes a resume days later find the same frames — so it is validated
        against `SEQ_RE` before it is ever joined to a directory, on this machine
        and on the instance.
        """
        job_id = uuid.uuid4().hex[:12]
        spec = dict(spec, job_id=job_id)
        self.conn.execute(
            "INSERT INTO jobs (id, agent, spec, prio, created, scene, seq, "
            "frame_first, frame_last, frame_step, frame_list, frames_total, "
            "frames_done, frames_failed, spec_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)",
            (job_id, agent, json.dumps(spec), prio, time.time(), scene, seq,
             first, last, step,
             json.dumps(frame_list) if frame_list is not None else None,
             frames_total, spec_hash),
        )
        self.conn.commit()
        return job_id

    def frame(self, seq: str, frame: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM frames WHERE seq=? AND frame=?", (seq, frame)
        ).fetchone()
        return dict(row) if row else None

    def frames(self, seq: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM frames WHERE seq=? ORDER BY frame", (seq,)
        ).fetchall()
        return [dict(r) for r in rows]

    def frame_done(self, seq: str, frame: int, job_id: str, path: str, size: int,
                   width: Optional[int], height: Optional[int], sha256: str,
                   render_sec: float, spec_hash: str,
                   stats: Optional[dict] = None) -> None:
        """Record a frame that has been fetched AND verified locally.

        Written only after verification, never after the render: a row here is a
        promise that the file on this machine is complete and correct, and a
        resume trusts it enough to skip the frame. A row written at render time
        would make every fetch failure look like a delivered frame.

        `stats` is what the image measured as. It is stored on the row for the
        same reason the sha256 is: so the cheap resume pass can answer "was this
        frame any good" without re-decoding 3,000 PNGs. A `done` row with
        `blank` set to a blank verdict never gets written by the broker — the
        delivery path fails first — but the column is what `plan_range` reads,
        so a row hand-edited or written by an older broker still cannot poison a
        resume.
        """
        stats = stats or {}
        self.conn.execute(
            "INSERT INTO frames (seq, frame, state, job_id, path, bytes, width, "
            "height, sha256, render_sec, spec_hash, finished, err, "
            "blank, lum_mean, lum_sd, lum_min, lum_max, lum_levels, stats) "
            "VALUES (?,?,'done',?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?) "
            "ON CONFLICT(seq, frame) DO UPDATE SET state='done', job_id=excluded.job_id, "
            "path=excluded.path, bytes=excluded.bytes, width=excluded.width, "
            "height=excluded.height, sha256=excluded.sha256, "
            "render_sec=excluded.render_sec, spec_hash=excluded.spec_hash, "
            "finished=excluded.finished, err=NULL, blank=excluded.blank, "
            "lum_mean=excluded.lum_mean, lum_sd=excluded.lum_sd, "
            "lum_min=excluded.lum_min, lum_max=excluded.lum_max, "
            "lum_levels=excluded.lum_levels, stats=excluded.stats",
            (seq, frame, job_id, path, size, width, height, sha256, render_sec,
             spec_hash, time.time(),
             stats.get("verdict"), stats.get("mean"), stats.get("sd"),
             stats.get("min"), stats.get("max"), stats.get("levels"),
             json.dumps(stats, separators=(",", ":")) if stats else None),
        )
        self.conn.execute(
            "UPDATE jobs SET frames_done=COALESCE(frames_done,0)+1, frame_current=? "
            "WHERE id=?", (frame, job_id),
        )
        self.conn.commit()

    def frame_failed(self, seq: str, frame: int, job_id: str, err: str,
                     spec_hash: str) -> None:
        """Record a frame that could not be produced.

        Kept rather than dropped, so `rq seq status` can name exactly which
        frames are missing and why. A failed row never blocks a resume — the
        resume set is "not done", not "not attempted".
        """
        self.conn.execute(
            "INSERT INTO frames (seq, frame, state, job_id, spec_hash, finished, err) "
            "VALUES (?,?,'failed',?,?,?,?) "
            "ON CONFLICT(seq, frame) DO UPDATE SET state='failed', "
            "job_id=excluded.job_id, spec_hash=excluded.spec_hash, "
            "finished=excluded.finished, err=excluded.err",
            (seq, frame, job_id, spec_hash, time.time(), err[:2000]),
        )
        self.conn.execute(
            "UPDATE jobs SET frames_failed=COALESCE(frames_failed,0)+1 WHERE id=?",
            (job_id,),
        )
        self.conn.commit()

    def frame_forget(self, seq: str, frame: int) -> None:
        """Drop a frame's record — used when its file fails verification, so the
        resume set picks it up again instead of trusting a stale row."""
        self.conn.execute("DELETE FROM frames WHERE seq=? AND frame=?", (seq, frame))
        self.conn.commit()

    def set_frame_current(self, job_id: str, frame: int) -> None:
        self.conn.execute("UPDATE jobs SET frame_current=? WHERE id=?", (frame, job_id))
        self.conn.commit()

    def set_frame_progress(self, job_id: str, total: int, done: int,
                           failed: int = 0) -> None:
        """Reset a sequence job's counters at the start of a dispatch pass.

        `done` is seeded from the frames already delivered, so the displayed
        progress covers the whole range rather than restarting at zero every
        time an instance is replaced under a multi-day render.
        """
        self.conn.execute(
            "UPDATE jobs SET frames_total=?, frames_done=?, frames_failed=? WHERE id=?",
            (total, done, failed, job_id),
        )
        self.conn.commit()

    def seq_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT seq FROM frames WHERE seq IS NOT NULL "
            "UNION SELECT DISTINCT seq FROM jobs WHERE seq IS NOT NULL"
        ).fetchall()
        return sorted(r["seq"] for r in rows if r["seq"])

    def seq_summary(self, seq: str) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) n, SUM(state='done') done, SUM(state='failed') failed, "
            "SUM(bytes) bytes, AVG(render_sec) mean_sec, MIN(frame) lo, MAX(frame) hi "
            "FROM frames WHERE seq=?", (seq,),
        ).fetchone()
        hashes = [r["spec_hash"] for r in self.conn.execute(
            "SELECT DISTINCT spec_hash FROM frames WHERE seq=? AND state='done'", (seq,)
        ).fetchall()]
        # How each delivered frame's IMAGE measured. NULL counts as `unmeasured`
        # and is deliberately not folded into OK: a frame delivered before the
        # blank check existed has not been cleared, it has not been looked at.
        verdicts: dict[str, int] = {}
        for r in self.conn.execute(
            "SELECT COALESCE(blank,'unmeasured') v, COUNT(*) n FROM frames "
            "WHERE seq=? AND state='done' GROUP BY v", (seq,),
        ).fetchall():
            verdicts[r["v"]] = r["n"]
        jobs = self.conn.execute(
            "SELECT id, state, frame_first, frame_last, frame_step, frames_total, "
            "frames_done, frames_failed, frame_current, err, created FROM jobs "
            "WHERE seq=? ORDER BY created", (seq,),
        ).fetchall()
        return {
            "seq": seq,
            "frames_recorded": row["n"] or 0,
            "frames_done": row["done"] or 0,
            "frames_failed": row["failed"] or 0,
            "bytes": row["bytes"] or 0,
            "mean_render_sec": row["mean_sec"],
            "first": row["lo"], "last": row["hi"],
            # More than one hash means frames in this sequence were rendered
            # with different settings. For a single unbroken shot that is a
            # seam, so it is surfaced rather than averaged away.
            "spec_hashes": [h for h in hashes if h],
            "verdicts": verdicts,
            "jobs": [dict(j) for j in jobs],
        }

    def frame_stats(self, seq: str) -> list[dict]:
        """Per-frame image measurements, in frame order.

        This is the dump a human reads over 2,978 frames. Nothing is aggregated
        away: the point is to be able to sort by standard deviation and see the
        one row that does not belong.
        """
        rows = self.conn.execute(
            "SELECT frame, state, bytes, render_sec, blank, lum_mean, lum_sd, "
            "lum_min, lum_max, lum_levels, stats FROM frames WHERE seq=? "
            "ORDER BY frame", (seq,),
        ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            blob = row.pop("stats", None)
            if blob:
                try:
                    row["hist16"] = json.loads(blob).get("hist16")
                except (TypeError, ValueError):
                    row["hist16"] = None
            out.append(row)
        return out

    # --- meta ------------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return json.loads(row["v"]) if row else default

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
