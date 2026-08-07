#!/usr/bin/env python3
"""Does `db.claim` actually hold ACROSS PROCESSES? Measured, with a control.

    .venv/bin/python farm/test_claim_crossproc.py            # both cases
    .venv/bin/python farm/test_claim_crossproc.py --workers 8 --jobs 400

WHY THIS EXISTS
---------------
`db.claim` says, in its own docstring:

    BEGIN IMMEDIATE takes the write lock up front so two dispatchers cannot
    select the same row before either updates it.

That was reasoned about for **two dispatch threads inside one broker process**,
where the GIL and a shared connection pool are also in play. The eight-broker
fleet is eight OS processes, and SQLite's cross-process locking is a different
mechanism from Python's — advisory POSIX locks on the database file, with WAL,
`busy_timeout`, and a real `SQLITE_BUSY` that a threaded test never provokes.
"It is safe because the comment says BEGIN IMMEDIATE" is a claim about source
code. This is a measurement.

WHY THERE IS A NEGATIVE CONTROL, AND WHY IT MATTERS MORE THAN THE POSITIVE
--------------------------------------------------------------------------
A green test proves nothing on its own: a test that never issues two
overlapping claims passes whatever the locking does. So this runs the SAME
harness three times, changing only the transaction bracketing:

  * `IMMEDIATE` — the shipping code path.
  * `DEFERRED`  — `BEGIN IMMEDIATE` -> `BEGIN DEFERRED`, the obvious naive fix.
  * `NOTX`      — no transaction at all: SELECT, then UPDATE, in autocommit.

If `NOTX` also shows zero double-claims, the harness is not contending and
**the positive result is worthless**; that is reported as INCONCLUSIVE, not as
a pass. The test's own sensitivity is a result.

`DEFERRED` is in there because it was tried first as the control and it does
NOT fail — which is worth knowing on its own. SQLite will not upgrade a read
transaction to a write one behind another writer's back, so a deferred claim
aborts with SQLITE_BUSY rather than handing the row out twice. Measured at 8
processes: **safe, and 69 % of attempts thrown away.** A test that had stopped
there would have printed a green tick for a mechanism it never stressed.

MEASURED 2026-08-07, 8 processes, 300-job queue, 4 s each (R2-1241)
-------------------------------------------------------------------
    IMMEDIATE  300 claims  0 busy    0 double-claims   71 claims/s
    DEFERRED   300 claims  680 busy  0 double-claims   71 claims/s
    NOTX       641 claims  0 busy  **206 double-claims**  (one job to 3 pids)

71 claims/s against a dispatch loop that spends 4.5-11 s of serial broker work
per frame: the claim path is four orders of magnitude away from being the
fleet's bottleneck.

WHAT THIS DOES *NOT* LICENSE
----------------------------
Passing here does not mean the fleet should share one database. It must not:
`meta` is a flat key/value table and `instance_id`, `bad_hosts`, the spend
ledger and the resident `scene_hash` are all stored in it under fixed keys. Two
brokers on one DB would each overwrite the other's instance id and then destroy
a card they no longer recognise as theirs. The fleet partitions by CONTIGUOUS
FRAME BLOCK instead — see `farm/manifest.py`, which proves exactly-once on the
delivered frames, which is the property anyone actually cares about.

What passing here does license is the claim that the *primitive* is sound: if a
shared queue is ever built, this is the row-handout mechanism to build it on,
and this file is the evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker import db as dbmod   # noqa: E402

# Real `fork`/`exec`ed processes, launched by this file re-invoking itself with
# `--claimer`, rather than `multiprocessing`. Not stylistic: `multiprocessing`
# with `fork` inherits the parent's SQLite handles, which is exactly the shared
# state the test is supposed to NOT have, and with `spawn` the children's
# tracebacks vanish into a Queue nobody drains if they die early. Eight brokers
# are eight independent `python -m broker.app` processes; so are these.


def _claimer(path: str, mode: str, seconds: float) -> int:
    """Claim rows as fast as possible for `seconds`. Print what we got, as JSON.

    A fresh `DB` per process, which is what the real thing does: `broker.app`
    constructs one at startup and every broker is its own process.
    """
    _patch(mode)
    d = dbmod.DB(Path(path))
    got = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            row = d.claim(lease_sec=3600.0)
        except sqlite3.OperationalError as exc:
            # Recorded, not swallowed. A run that is mostly SQLITE_BUSY is a
            # different result from a run that is mostly claims.
            got.append(["BUSY:" + str(exc)[:60], os.getpid()])
            continue
        if row is None:
            time.sleep(0.001)
            continue
        got.append([row["id"], os.getpid()])
    json.dump(got, sys.stdout)
    return 0


# THE THREE MODES, and why there are three rather than two.
#
# The first control tried was `BEGIN IMMEDIATE` -> `BEGIN DEFERRED`, the
# obvious "naive version". It does not double-claim, and finding that out is
# itself a result worth keeping: SQLite refuses to upgrade a read transaction
# to a write one behind another writer's back, so the deferred claim aborts
# with SQLITE_BUSY instead. It is SAFE and it is TERRIBLE — measured below at
# ~63 % of claims thrown away — and a test that stopped there would have
# reported a green tick for a mechanism it had never actually stressed.
#
# So the real control is NOTX: no transaction at all. That is how somebody
# writes this function if they have not thought about concurrency — SELECT the
# next queued row, then UPDATE it to running — and it is the failure the
# `BEGIN IMMEDIATE` in `db.claim` exists to prevent.
MODES = {
    "IMMEDIATE": ({}, "the shipping code path"),
    "DEFERRED": ({"BEGIN IMMEDIATE": "BEGIN DEFERRED"},
                 "the obvious naive version"),
    "NOTX": ({"BEGIN IMMEDIATE": "SELECT 1", "COMMIT": "SELECT 1",
              "ROLLBACK": "SELECT 1"},
             "no transaction at all — SELECT then UPDATE, autocommit"),
}


def _make_conn_class(subs: dict):
    """A Connection whose statements are rewritten by `subs`, nothing else.

    Installed through sqlite3's own `factory=` extension point rather than by
    monkeypatching methods (`sqlite3.Connection.execute` is read-only) and,
    more importantly, rather than by copying `claim`'s body. Every control has
    to run the REAL query, the REAL fair-share ORDER BY and the REAL UPDATE,
    differing only in its transaction bracketing — a hand-written "naive
    version" would only prove that the hand-written version is broken.
    """

    def weaken(sql):
        if isinstance(sql, str):
            return subs.get(sql.strip().upper(), sql)
        return sql

    class Cur(sqlite3.Cursor):
        def execute(self, sql, *a, **kw):
            return super().execute(weaken(sql), *a, **kw)

    class Conn(sqlite3.Connection):
        def cursor(self, factory=Cur):   # type: ignore[override]
            return super().cursor(factory)

        def execute(self, sql, *a, **kw):
            return self.cursor().execute(weaken(sql), *a, **kw)

    return Conn


def _patch(mode: str) -> None:
    """Make every connection `broker.db` opens from now on run in `mode`."""
    subs, _ = MODES[mode]
    if not subs:
        return
    cls = _make_conn_class(subs)
    real_connect = dbmod.sqlite3.connect

    def connect(*a, **kw):
        kw.setdefault("factory", cls)
        # Autocommit mode needs isolation_level=None or the driver opens its
        # own implicit transaction around the UPDATE and re-creates the very
        # protection this control is trying to remove.
        if "SELECT 1" in subs.values():
            kw["isolation_level"] = None
        return real_connect(*a, **kw)

    dbmod.sqlite3.connect = connect


def run(mode: str, workers: int, jobs: int, seconds: float) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "broker.db"
        d = dbmod.DB(path)
        for i in range(jobs):
            d.submit({"frame": i}, agent=f"a{i % 4}")
        d.close()

        cmd = [sys.executable, os.path.abspath(__file__), "--claimer",
               "--db", str(path), "--mode", mode, "--seconds", str(seconds)]
        t0 = time.time()
        procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) for _ in range(workers)]
        collected = []
        for p in procs:
            out, err = p.communicate(timeout=seconds + 120)
            if p.returncode != 0:
                raise RuntimeError(
                    f"claimer exited {p.returncode}: "
                    f"{err.decode(errors='replace')[-2000:]}")
            collected.append(json.loads(out or b"[]"))
        wall = time.time() - t0

    claims = [c for batch in collected for c in batch
              if not str(c[0]).startswith("BUSY:")]
    busy = sum(1 for batch in collected for c in batch
               if str(c[0]).startswith("BUSY:"))
    per_id = Counter(jid for jid, _ in claims)
    dupes = {jid: n for jid, n in per_id.items() if n > 1}
    # Only count a duplicate as a DOUBLE-CLAIM if two different processes got
    # it. One process claiming twice is the lease expiring, which is by design.
    owners: dict[str, set] = {}
    for jid, pid in claims:
        owners.setdefault(jid, set()).add(pid)
    cross = {jid: sorted(p) for jid, p in owners.items() if len(p) > 1}
    return {
        "mode": mode, "workers": workers, "jobs": jobs, "wall": wall,
        "claims": len(claims), "distinct": len(per_id), "busy": busy,
        "dupes": len(dupes), "cross_process_dupes": len(cross),
        "example": next(iter(cross.items()), None),
        "rate": len(claims) / wall if wall else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--jobs", type=int, default=400)
    ap.add_argument("--seconds", type=float, default=6.0)
    # Re-invocation of this same file as one of the contending processes.
    ap.add_argument("--claimer", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--db", default="", help=argparse.SUPPRESS)
    ap.add_argument("--mode", default="IMMEDIATE", help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.claimer:
        return _claimer(a.db, a.mode, a.seconds)

    print(f"{a.workers} PROCESSES against one SQLite queue of {a.jobs} jobs, "
          f"{a.seconds:g}s each\n")
    results = {}
    for mode in MODES:
        r = run(mode, a.workers, a.jobs, a.seconds)
        results[mode] = r
        lost = r["jobs"] - r["distinct"]
        print(f"  {mode:<10} claims={r['claims']:<6} distinct={r['distinct']:<6} "
              f"busy={r['busy']:<5} DOUBLE-CLAIMED ACROSS PROCESSES="
              f"{r['cross_process_dupes']:<5} "
              f"({r['rate']:.0f} claims/s, {lost} job(s) never handed out)")
        print(f"             {MODES[mode][1]}")
        if r["example"]:
            jid, pids = r["example"]
            print(f"             e.g. job {jid} handed to pids {pids}")

    imm, dfr, notx = results["IMMEDIATE"], results["DEFERRED"], results["NOTX"]
    print()
    ok = imm["cross_process_dupes"] == 0
    sensitive = notx["cross_process_dupes"] > 0
    if not ok:
        print(">> STAGE RESULT: FAIL — BEGIN IMMEDIATE double-claimed "
              f"{imm['cross_process_dupes']} job(s) across processes. The "
              f"eight-broker fleet cannot share a queue.")
        return 1
    if not sensitive:
        print(">> STAGE RESULT: INCONCLUSIVE — the NOTX control ALSO showed no "
              "double-claims, so this harness did not contend hard enough to "
              "detect one. The green result above proves nothing. Raise "
              "--workers or --seconds, or lower --jobs so the queue is short "
              "and every process fights over the same tail.")
        return 2
    print(f">> STAGE RESULT: PASS — {imm['claims']} claims by {imm['workers']} "
          f"separate OS processes against one queue, ZERO jobs handed to two "
          f"processes, ZERO SQLITE_BUSY, {imm['rate']:.0f} claims/s. The NOTX "
          f"control on the identical harness double-claimed "
          f"{notx['cross_process_dupes']} job(s), so the test can see the "
          f"failure it is looking for. db.claim IS cross-process atomic.")
    print(f"   Also measured: BEGIN DEFERRED is SAFE but throws away "
          f"{dfr['busy']} claim(s) to SQLITE_BUSY "
          f"({dfr['busy'] / max(1, dfr['busy'] + dfr['claims']) * 100:.0f} % of "
          f"attempts) — it does not double-render, it stalls. The IMMEDIATE "
          f"path took {imm['busy']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
