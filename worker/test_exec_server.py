#!/usr/bin/env python3
"""Correctness checks for the EXEC supervisor. No GPU, no network, no money:

    .venv/bin/python worker/test_exec_server.py

Everything here is a failure this system has already paid for once, in the
render path, transposed onto the new job type:

  * an omitted field silently inherited from the previous job (server.py:689)
  * a caller-supplied name used as a file path (db.py mints ids because a
    client-supplied one was a traversal into a read-only project)
  * a job that writes outside the directory it was given
  * a "finished" result that was never produced
  * a stale process still holding a port

Two of the checks are the ones the project's own defect log says matter most:
**a new check is run against an artefact already known to be bad, and the
artefact is looked at rather than only the number.** So the traversal tests do
not merely assert an exception — they assert that the file the attack aimed at
still does not exist afterwards, and the timeout test asserts the child process
is actually gone.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import exec_server as X                                            # noqa: E402

PASS, FAIL = "ok", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((PASS if condition else FAIL, name, detail))


# A stand-in for Blender: understands the same `<flags> -P script -- argv`
# shape and runs the script under this interpreter. The point of the tests is
# the supervisor's decisions, not Blender.
FAKE_BLENDER = r"""#!/bin/sh
script=""
while [ $# -gt 0 ]; do
  case "$1" in
    -P) script="$2"; shift 2 ;;
    --) shift; break ;;
    *) shift ;;
  esac
done
exec "$PYTHON" "$script" -- "$@"
"""


class Harness:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="execsrv-test-"))
        self.root = self.dir / "exec"
        self.bundles = self.dir / "bundles"
        self.outside = self.dir / "OUTSIDE"
        self.outside.mkdir(parents=True)
        blender = self.dir / "fakeblender"
        blender.write_text(FAKE_BLENDER)
        blender.chmod(0o755)
        os.environ["PYTHON"] = sys.executable
        self.blender = str(blender)

        # One bundle, complete, with three entry scripts.
        self.digest = "a1b2c3d4e5f60718"
        bundle = self.bundles / self.digest
        (bundle / "tools").mkdir(parents=True)
        (bundle / "tools" / "ok.py").write_text(
            "import sys, os\n"
            "argv = sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []\n"
            "target = argv[argv.index('--out')+1] if '--out' in argv else 'out/r.txt'\n"
            "os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)\n"
            "open(target, 'w').write('produced ' + ' '.join(argv))\n"
            "open('stray.txt', 'w').write('scratch that must be scrubbed')\n"
            "os.makedirs('tmp', exist_ok=True)\n"
            "open('tmp/big.bin', 'wb').write(b'x' * 100000)\n"
        )
        (bundle / "tools" / "slow.py").write_text(
            "import time, os\n"
            "open(os.environ['SLOWMARK'], 'w').write(str(os.getpid()))\n"
            "time.sleep(600)\n"
        )
        # Writes its own pid AND a grandchild's into out/, then sleeps past any
        # test's patience. The grandchild is the point: it inherits the process
        # group `start_new_session=True` created, so a cancel that signals only
        # the pid leaves it holding memory — which on the real box is Blender's
        # forked helpers holding an 8 GB assembly.
        (bundle / "tools" / "sleeper.py").write_text(
            "import os, subprocess, sys, time\n"
            "os.makedirs('out', exist_ok=True)\n"
            "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
            "open('out/pid.txt', 'w').write('%d %d' % (os.getpid(), kid.pid))\n"
            "time.sleep(600)\n"
        )
        (bundle / "tools" / "silent.py").write_text("pass\n")
        (bundle / "tools" / "boom.py").write_text("raise SystemExit(3)\n")
        (bundle / X.BUNDLE_COMPLETE).touch()

        # A second, INCOMPLETE bundle: files present, marker absent.
        self.half = "ffffffffffffffff"
        (self.bundles / self.half / "tools").mkdir(parents=True)
        (self.bundles / self.half / "tools" / "ok.py").write_text("pass\n")

        self.server = X.ExecServer(str(self.root), str(self.bundles), slots=4,
                                   blender=self.blender, min_free_gb=0.0)

    def spec(self, **over) -> dict:
        base = {
            "job_id": "j" + os.urandom(4).hex(),
            "bundle": self.digest,
            "entry": "tools/ok.py",
            "argv": [],
            "outputs": ["r.txt"],
            "timeout_s": 60,
            "blender_args": ["-b", "--factory-startup"],
            "cpu_slots": 1,
        }
        base.update(over)
        return base

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def main() -> int:
    h = Harness()
    try:
        run_checks(h)
    finally:
        h.cleanup()

    width = max(len(n) for _, n, _ in results)
    failed = 0
    for state, name, detail in results:
        if state == FAIL:
            failed += 1
        print(f"{state:4s} {name:{width}s}  {detail}")
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


def run_checks(h: Harness) -> None:
    # --- the happy path, first, so a failure below is not just "nothing works"
    reply = h.server.handle_exec(h.spec(argv=["--out", "out/r.txt"]))
    check("a well-formed job runs and returns its declared output",
          reply["ok"] and reply["outputs"] and reply["outputs"][0]["name"] == "r.txt",
          f"rc={reply.get('rc')} err={reply.get('error')}")
    out_path = Path(reply["outputs"][0]["path"])
    check("the declared output really exists on disk, with the reported size",
          out_path.is_file() and out_path.stat().st_size == reply["outputs"][0]["bytes"],
          str(out_path))
    import hashlib
    check("the reported sha256 is the sha256 of the file that is there",
          hashlib.sha256(out_path.read_bytes()).hexdigest() == reply["outputs"][0]["sha256"])

    job_dir = out_path.parent.parent
    check("the bundle copy is scrubbed the moment the child exits",
          not (job_dir / "bundle").exists())
    check("the child's scratch is scrubbed too — 30 GB does not survive 12 of these",
          not (job_dir / "stray.txt").exists() and not (job_dir / "tmp").exists(),
          f"scrubbed {reply['scrubbed_bytes']} bytes")
    check("out/ and job.log survive the scrub",
          (job_dir / "out").is_dir() and (job_dir / "job.log").is_file())

    # --- no defaults, ever ------------------------------------------------
    missing_named = []
    for field in sorted(X.EXEC_REQUIRED):
        spec = h.spec()
        spec.pop(field)
        try:
            h.server.validate(spec)
            missing_named.append(f"{field}: ACCEPTED")
        except ValueError as exc:
            if field not in str(exc):
                missing_named.append(f"{field}: rejected but not named")
    check("every required field, omitted, is rejected AND named",
          not missing_named, "; ".join(missing_named) or f"{len(X.EXEC_REQUIRED)} fields")

    try:
        h.server.validate(h.spec(samples=64))
        check("an UNKNOWN field is rejected rather than ignored", False)
    except ValueError as exc:
        check("an UNKNOWN field is rejected rather than ignored", "samples" in str(exc))

    # --- path traversal ---------------------------------------------------
    victim = h.outside / "stolen.txt"
    for name, spec in (
        ("entry escapes the bundle", h.spec(entry="../../../etc/passwd")),
        ("entry escapes via a bundle-relative ..",
         h.spec(entry="tools/../../../tools/ok.py")),
        ("an output escapes out/", h.spec(outputs=[f"../../../{victim.name}"])),
        ("an absolute output escapes out/", h.spec(outputs=[str(victim)])),
        ("an argv token writes outside the job dir",
         h.spec(argv=["--out", str(victim)])),
        ("an argv token escapes with ..",
         h.spec(argv=["--out", "../../../../etc/cron.d/pwn"])),
        ("a --flag=value argv token escapes",
         h.spec(argv=[f"--out={victim}"])),
        ("blender_args names a path outside the job dir",
         h.spec(blender_args=["-b", str(h.outside / "evil.blend")])),
    ):
        try:
            h.server.handle_exec(spec)
            check(f"REFUSED: {name}", False, "accepted")
        except (ValueError, RuntimeError) as exc:
            check(f"REFUSED: {name}", True, str(exc)[:70])
    check("nothing outside the job directory was written by any of those",
          not victim.exists() and not any(h.outside.iterdir()),
          str(sorted(p.name for p in h.outside.iterdir())))

    for name, spec in (
        ("job_id with a slash", h.spec(job_id="../../etc/x")),
        ("job_id with a dot-dot", h.spec(job_id="..")),
        ("empty job_id", h.spec(job_id="")),
        ("bundle digest with a slash", h.spec(bundle="../scenes")),
        ("bundle digest that is not hex", h.spec(bundle="zzzzzzzzzzzzzzzz")),
    ):
        try:
            h.server.validate(spec)
            check(f"REFUSED: {name}", False, "accepted")
        except ValueError as exc:
            check(f"REFUSED: {name}", True, str(exc)[:70])

    # --- a check run against an artefact ALREADY KNOWN TO BE BAD ----------
    # The bundle at `h.half` is exactly a half-landed push: its files are there
    # and its marker is not. If the completeness check does not fail on it, the
    # check is measuring nothing.
    check("the incomplete bundle really does have its files on disk",
          (h.bundles / h.half / "tools" / "ok.py").is_file())
    check("the incomplete bundle really is missing only its marker",
          not (h.bundles / h.half / X.BUNDLE_COMPLETE).exists())
    try:
        h.server.validate(h.spec(bundle=h.half))
        check("REFUSED: a bundle with files but no .complete marker", False, "accepted")
    except ValueError as exc:
        check("REFUSED: a bundle with files but no .complete marker",
              "not staged" in str(exc), str(exc)[:70])
    try:
        h.server.validate(h.spec(bundle="0123456789abcdef"))
        check("REFUSED: a bundle that is not on the instance at all", False, "accepted")
    except ValueError as exc:
        check("REFUSED: a bundle that is not on the instance at all", True, str(exc)[:60])

    # --- code-execution flags in blender_args ----------------------------
    for banned in ("-P", "--python-expr", "--python-console"):
        try:
            h.server.validate(h.spec(blender_args=["-b", banned, "x"]))
            check(f"REFUSED: {banned} in blender_args", False, "accepted")
        except ValueError:
            check(f"REFUSED: {banned} in blender_args", True)

    # --- bounds -----------------------------------------------------------
    for name, spec in (
        ("timeout_s above the 1 h ceiling", h.spec(timeout_s=99999)),
        ("timeout_s of zero", h.spec(timeout_s=0)),
        ("timeout_s that is not a number", h.spec(timeout_s="soon")),
        ("cpu_slots larger than the server has", h.spec(cpu_slots=99)),
        ("cpu_slots of zero", h.spec(cpu_slots=0)),
        ("an empty outputs list", h.spec(outputs=[])),
        ("outputs that is not a list of strings", h.spec(outputs=[{"a": 1}])),
        ("argv that is a shell string rather than a list",
         h.spec(argv="--out out/r.txt; rm -rf /")),
    ):
        try:
            h.server.validate(spec)
            check(f"REFUSED: {name}", False, "accepted")
        except ValueError as exc:
            check(f"REFUSED: {name}", True, str(exc)[:70])

    # --- a job that lies about succeeding --------------------------------
    reply = h.server.handle_exec(h.spec(entry="tools/silent.py", outputs=["r.txt"]))
    check("a child that exits 0 without writing its output FAILS the job",
          not reply["ok"] and reply["missing"] == ["r.txt"] and reply["rc"] == 0,
          reply.get("error", ""))

    reply = h.server.handle_exec(h.spec(entry="tools/boom.py"))
    check("a child that exits non-zero fails the job and reports the code",
          not reply["ok"] and reply["rc"] == 3, reply.get("error", ""))
    check("the failing child's log comes back with the reply",
          isinstance(reply.get("log"), str))

    # --- timeout kills the whole process group ---------------------------
    mark = h.dir / "slowpid.txt"
    os.environ["SLOWMARK"] = str(mark)
    t0 = time.time()
    reply = h.server.handle_exec(h.spec(entry="tools/slow.py", timeout_s=3))
    elapsed = time.time() - t0
    check("a job over timeout_s is killed, not waited on",
          not reply["ok"] and reply["timed_out"] and elapsed < 60,
          f"{elapsed:.1f}s")
    child_pid = int(mark.read_text()) if mark.exists() else 0
    alive = False
    if child_pid:
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
                alive = True
                time.sleep(0.25)
            except OSError:
                alive = False
                break
    check("the child process is actually GONE afterwards, not merely abandoned",
          bool(child_pid) and not alive, f"pid {child_pid}")

    # --- cancel -----------------------------------------------------------
    #
    # THE DEFECT, in one sentence: `rq cancel` on an exec job flipped a SQLite
    # row and nothing else, because there was no cancellation path to a
    # dispatched child at all. Instance 47040457, 2026-08-07: a39bd71095f9 was
    # cancelled at 03:46, reported `{"canceled": true}`, and its Blender child
    # ran until its own timeout at 04:44 — 58 minutes holding 6 of 12 slots and
    # ~8 GB, while two of another agent's jobs were refused by the memory gate
    # for want of that memory and a third was OOM-killed at `Read blend`.
    #
    # So the checks below are about the box, not about the reply: the process
    # tree is inspected, the slot counter is read, and a SIBLING job is run
    # alongside the victim and asserted to survive. That last one is the same
    # assertion the orphan reaper makes for the same reason — `pkill -f blender`
    # would pass every "the child is gone" check on this page and take the
    # render worker's warm scene with it.

    def start_sleeper(job_id: str, cpu_slots: int = 1) -> dict:
        """Launch a job that runs for ten minutes; return a handle to it."""
        box: dict = {"reply": None, "job_id": job_id}

        def run() -> None:
            box["reply"] = h.server.handle_exec(h.spec(
                job_id=job_id, entry="tools/sleeper.py", outputs=["pid.txt"],
                timeout_s=600, cpu_slots=cpu_slots))

        box["thread"] = threading.Thread(target=run, daemon=True)
        box["thread"].start()
        mark = h.root / job_id / "out" / "pid.txt"
        for _ in range(200):
            if mark.is_file() and mark.read_text().strip():
                break
            time.sleep(0.1)
        box["pids"] = [int(p) for p in mark.read_text().split()] if mark.is_file() else []
        return box

    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def gone_within(pids: list, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not any(alive(p) for p in pids):
                return True
            time.sleep(0.1)
        return not any(alive(p) for p in pids)

    free_before = h.server.slots.snapshot()["free"]
    victim = start_sleeper("cancelvictim", cpu_slots=2)
    bystander = start_sleeper("bystanderjob", cpu_slots=1)
    check("the job to be cancelled really is running, with a grandchild, "
          "before anything is cancelled",
          len(victim["pids"]) == 2 and all(alive(p) for p in victim["pids"]),
          str(victim["pids"]))
    check("and it is really holding its slots",
          h.server.slots.snapshot()["free"] == free_before - 3,
          str(h.server.slots.snapshot()))

    t0 = time.time()
    cancel_reply = h.server.handle_cancel({"job_id": "cancelvictim"})
    cancel_sec = time.time() - t0
    check("cancel reports that it signalled a running child, and names the "
          "process group it signalled",
          cancel_reply["ok"] and cancel_reply["canceled"] and cancel_reply["running"]
          and cancel_reply["killed"] and cancel_reply["pgid"] == cancel_reply["pid"],
          f"pid={cancel_reply.get('pid')} pgid={cancel_reply.get('pgid')} "
          f"{cancel_sec:.1f}s")
    check("THE WHOLE PROCESS GROUP IS GONE — child and grandchild, not just the "
          "process the supervisor happened to hold a handle to",
          gone_within(victim["pids"], 20.0),
          str([(p, alive(p)) for p in victim["pids"]]))
    victim["thread"].join(timeout=30)
    check("the cancelled job's own call returns immediately instead of sitting "
          "out its 600 s timeout_s",
          not victim["thread"].is_alive() and cancel_sec < 30, f"{cancel_sec:.1f}s")
    vreply = victim["reply"] or {}
    check("the reply is marked CANCELED rather than merely not-ok, so the "
          "broker cannot read a deliberate stop as a broken script",
          vreply.get("canceled") is True and vreply.get("ok") is False,
          str(vreply.get("error"))[:70])
    check("and it did not sit out timeout_s to get there",
          float(vreply.get("exec_sec") or 9999) < 60, str(vreply.get("exec_sec")))

    # THE HALF THAT MAKES THE CANCEL WORTH DOING. A kill that leaks the slots
    # just moves the bug: the orphan on 47040457 was starving other agents
    # because of the slots and the memory, not because it was running.
    check("the cancelled job's SLOTS are released — a kill that leaks them "
          "leaves the box exactly as starved as before",
          h.server.slots.snapshot()["free"] == free_before - 1,
          str(h.server.slots.snapshot()))
    check("and it is no longer listed as in-flight",
          "cancelvictim" not in h.server.slots.snapshot()["inflight"])

    # THE ONE THAT MATTERS MOST. Targeting by name or by cwd would have killed
    # this too — every exec child has a cwd inside the exec root, and every one
    # of them is Blender.
    check("THE SIBLING JOB IS UNTOUCHED — cancel targets one process group by "
          "job id, never a pattern that matches every child on the box",
          all(alive(p) for p in bystander["pids"]),
          str([(p, alive(p)) for p in bystander["pids"]]))

    h.server.handle_cancel({"job_id": "bystanderjob"})
    bystander["thread"].join(timeout=30)
    check("with both cancelled, every slot is back",
          h.server.slots.snapshot()["free"] == free_before,
          str(h.server.slots.snapshot()))
    check("and the sibling's tree is gone too once it is the one named — the "
          "test leaves no sleeper behind either",
          gone_within(bystander["pids"], 20.0),
          str([(p, alive(p)) for p in bystander["pids"]]))

    # --- cancel is idempotent, and never a path to an arbitrary process ----
    for name, jid in (("a job_id with a slash", "../../etc/x"),
                      ("a job_id with a dot-dot", ".."),
                      ("an empty job_id", "")):
        try:
            h.server.handle_cancel({"job_id": jid})
            check(f"REFUSED: cancel with {name}", False, "accepted")
        except ValueError:
            check(f"REFUSED: cancel with {name}", True)

    again = h.server.handle_cancel({"job_id": "cancelvictim"})
    check("cancelling a job that already stopped is ok, not an error — the "
          "broker cancels the row either way and a second call must not look "
          "like a failure",
          again["ok"] and again["running"] is False, str(again.get("detail"))[:60])

    # --- a cancel that OVERTAKES its job ----------------------------------
    #
    # The broker calls a job "dispatched" from the moment it hands it to a
    # thread, and that thread can spend minutes pushing a bundle and an 8 GB
    # scene before this server has ever heard the id. A cancel in that window
    # has nothing to signal — and if it merely shrugged, the child would start
    # afterwards, which is the original defect with a shorter fuse.
    early = h.server.handle_cancel({"job_id": "notarrivedyet"})
    check("a cancel for a job that has not arrived is recorded rather than "
          "shrugged off", early["ok"] and early["canceled"] and not early["running"])
    free_now = h.server.slots.snapshot()["free"]
    late = h.server.handle_exec(h.spec(job_id="notarrivedyet",
                                       entry="tools/sleeper.py",
                                       outputs=["pid.txt"], timeout_s=600))
    check("and the job is REFUSED ON ARRIVAL, marked canceled",
          late.get("canceled") is True and late.get("ok") is False,
          str(late.get("error"))[:70])
    check("no child was started for it — the job directory has no pid file",
          not (h.root / "notarrivedyet" / "out" / "pid.txt").exists())
    check("and it consumed no slot on its way through",
          h.server.slots.snapshot()["free"] == free_now,
          str(h.server.slots.snapshot()))
    # The record is consumed, not permanent: a resubmission is a new job id, but
    # an id that is refused forever would be a trap if one were ever reused.
    check("the record is spent once used, so it cannot refuse anything twice",
          "notarrivedyet" not in h.server.canceled_ids)

    # --- slots ------------------------------------------------------------
    slots = X.Slots(4)
    check("slots start free", slots.snapshot()["free"] == 4)
    check("a multi-slot acquire takes them all at once",
          slots.acquire("a", 3, 1.0) and slots.snapshot()["free"] == 1)
    check("an over-capacity request waits rather than partially taking",
          not slots.acquire("b", 3, 0.2) and slots.snapshot()["free"] == 1)
    slots.release("a", 3)
    check("release restores capacity", slots.snapshot()["free"] == 4)

    order: list[str] = []

    def grab(name: str, n: int) -> None:
        if slots.acquire(name, n, 5.0):
            order.append(name)
            time.sleep(0.2)
            slots.release(name, n)

    threads = [threading.Thread(target=grab, args=(f"t{i}", 4)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("three 4-slot jobs against 4 slots serialise instead of deadlocking",
          len(order) == 3, str(order))

    # --- concurrency actually happens ------------------------------------
    started = time.time()
    replies: list[dict] = []
    lock = threading.Lock()

    def one() -> None:
        r = h.server.handle_exec(h.spec(argv=["--out", "out/r.txt"]))
        with lock:
            replies.append(r)

    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("four jobs run concurrently and all four produce their output",
          len(replies) == 4 and all(r["ok"] for r in replies),
          f"{time.time() - started:.1f}s for 4")

    # --- release / purge --------------------------------------------------
    spec = h.spec(argv=["--out", "out/r.txt"])
    reply = h.server.handle_exec(spec)
    jd = Path(reply["outputs"][0]["path"]).parent.parent
    check("the job directory survives until it is released", jd.is_dir())
    rel = h.server.handle_release({"job_id": spec["job_id"]})
    check("release removes the job directory", rel["ok"] and not jd.exists())
    try:
        h.server.handle_release({"job_id": "../../etc"})
        check("REFUSED: release with a traversing job_id", False, "accepted")
    except ValueError:
        check("REFUSED: release with a traversing job_id", True)

    # --- startup sweep ----------------------------------------------------
    (h.root / "leftover").mkdir(parents=True, exist_ok=True)
    (h.root / "leftover" / "junk.bin").write_bytes(b"0" * 1000)
    swept = h.server.sweep_stale()
    check("a fresh exec server sweeps job directories a dead one left behind",
          swept >= 1 and not (h.root / "leftover").exists(), f"{swept} removed")

    # --- disk floor -------------------------------------------------------
    tight = X.ExecServer(str(h.root), str(h.bundles), slots=2, blender=h.blender,
                         min_free_gb=10_000_000.0)
    try:
        tight.handle_exec(h.spec())
        check("REFUSED: a job when free disk is below the floor", False, "accepted")
    except RuntimeError as exc:
        check("REFUSED: a job when free disk is below the floor",
              "free" in str(exc), str(exc)[:70])

    # --- orphan reaping ---------------------------------------------------
    #
    # The leak this covers: an exec server that is restarted does NOT take its
    # children with it — `start_new_session=True` makes each one a session
    # leader, so it is re-parented to init and keeps running. Seven of them
    # holding ~35 GB were reaped by hand on the live box. The memory is charged
    # to the same cgroup `memory_available` reads, so the orphans are invisible
    # to the memory gate as a cause and fully visible to it as pressure: the
    # gate then refuses legitimate work forever, waiting on memory that nothing
    # will release.
    #
    # The second check is the important one. `pkill -f blender` would also have
    # cleared the orphans — and taken the render worker holding a warm
    # multi-gigabyte scene with it. Reaping must be decided by cwd, not by name.
    def spawn_at(cwd: Path) -> subprocess.Popen:
        cwd.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            cwd=str(cwd), start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def fresh_server() -> "X.ExecServer":
        return X.ExecServer(str(h.root), str(h.bundles), slots=1,
                            blender=h.blender, min_free_gb=0.0)

    inside = spawn_at(h.root / "orphanjob")
    outside = spawn_at(h.outside / "innocent")
    time.sleep(0.4)
    reaped = fresh_server().reap_orphans()
    inside.poll()
    check("an orphan whose cwd is inside the exec root is reaped",
          inside.poll() is not None and inside.pid in reaped["pids"],
          f"count={reaped['count']} rss={reaped['rss_bytes']}")
    check("a process outside the exec root is left alone",
          outside.poll() is None and outside.pid not in reaped["pids"],
          "kill-by-pattern would have taken the render worker instead")
    check("the reap reports the memory it gave back, and no survivors",
          reaped["rss_bytes"] > 0 and not reaped["survived"],
          f"{reaped['rss_bytes']} bytes")

    # The state the seven were actually found in: the job directory had already
    # been swept, so the cwd link reads "<path> (deleted)". An identification
    # that only matched intact paths would have missed every one of them.
    gone_dir = h.root / "deletedjob"
    gone = spawn_at(gone_dir)
    time.sleep(0.4)
    shutil.rmtree(gone_dir)
    reaped2 = fresh_server().reap_orphans()
    gone.poll()
    check("an orphan whose job directory was already deleted is still found",
          gone.poll() is not None and gone.pid in reaped2["pids"],
          "cwd reads '<path> (deleted)'")
    check("a fresh root with nothing running reaps nothing",
          fresh_server().reap_orphans()["count"] == 0)
    outside.kill()
    outside.wait(timeout=10)

    # --- the wire ---------------------------------------------------------
    port = 8899
    for candidate in range(8899, 8930):
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", candidate))
            probe.close()
            port = candidate
            break
        except OSError:
            continue
    wire = X.ExecServer(str(h.root), str(h.bundles), slots=2, blender=h.blender,
                        min_free_gb=0.0)
    threading.Thread(target=wire.serve, args=(port,), daemon=True).start()
    time.sleep(0.5)

    def send(payload: dict) -> dict:
        with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
            sock.settimeout(120)
            sock.sendall(json.dumps(payload).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf)

    pong = send({"cmd": "ping"})
    check("ping over the socket reports slots and disk",
          pong["ok"] and pong["slots"]["total"] == 2 and pong["disk"]["free"] > 0)
    bad = send({"cmd": "nonsense"})
    check("an unknown command is answered, not dropped",
          not bad["ok"] and "nonsense" in bad["error"])
    incomplete = send({"job_id": "x1", "bundle": h.digest})
    check("an incomplete spec over the socket comes back naming the fields",
          not incomplete["ok"] and "incomplete exec spec" in incomplete["error"],
          incomplete.get("error", "")[:70])
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(b"{not json\n")
        sock.settimeout(20)
        raw = sock.recv(65536)
    check("malformed JSON is answered with an error, not a dead connection",
          b"bad json" in raw, raw[:60].decode(errors="replace"))
    good = send(h.spec(argv=["--out", "out/r.txt"]))
    check("a real job over the socket returns ok with its output",
          good["ok"] and good["outputs"][0]["bytes"] > 0)

    # --- cancel OVER THE WIRE ---------------------------------------------
    #
    # Asserted here and not only as a unit call, because the wire is the entire
    # interface the broker has to this server: a cancel that works in-process
    # and is not routed in `serve_conn` is a cancel the broker cannot reach,
    # which is precisely the state the system was already in.
    wire_reply: dict = {}

    def wire_sleeper() -> None:
        wire_reply.update(send(h.spec(job_id="wirecancel", entry="tools/sleeper.py",
                                      outputs=["pid.txt"], timeout_s=600)))

    wt = threading.Thread(target=wire_sleeper, daemon=True)
    wt.start()
    wire_mark = h.root / "wirecancel" / "out" / "pid.txt"
    for _ in range(200):
        if wire_mark.is_file() and wire_mark.read_text().strip():
            break
        time.sleep(0.1)
    wire_pids = [int(p) for p in wire_mark.read_text().split()] if wire_mark.is_file() else []
    creply = send({"cmd": "cancel", "job_id": "wirecancel"})
    wt.join(timeout=30)
    check("cancel is routed over the socket and kills the child",
          creply.get("ok") and creply.get("killed") and bool(wire_pids)
          and gone_within(wire_pids, 20.0), str(creply.get("detail"))[:60])
    check("and the cancelled job's own reply crosses the wire marked canceled",
          wire_reply.get("canceled") is True and wire_reply.get("ok") is False,
          str(wire_reply.get("error"))[:70])
    check("the wire server's slots are free again after the kill",
          send({"cmd": "ping"})["slots"]["free"] == 2,
          str(send({"cmd": "ping"})["slots"]))
    check("a cancel is not counted as a FAILURE — the number a human reads to "
          "decide the box is sick must not move because somebody changed their mind",
          send({"cmd": "ping"})["failures"] == 0,
          str(send({"cmd": "ping"})["failures"]))

    # --- a refusal to ADMIT must not read as a verdict on the build ------
    #
    # `await_memory` calls itself "a WAIT rather than a rejection", and that was
    # true until the reply was serialised: every failure left as
    # `{"ok": false, "error": ...}`, and the broker's only reading of that is
    # "the caller's script is broken" — an attempt spent, three times, then
    # failed. Two jobs died that way on instance 47040457 on 2026-08-07 at
    # 03:43, for memory the render worker was holding. The gate runs BEFORE
    # `stage` and `run_child`, so at the moment it fires there is not even a
    # child process; there is nothing to have a verdict about.
    #
    # Asserted OVER THE WIRE, because the wire is where the distinction was
    # being lost. A unit test on the exception type would have passed
    # throughout the entire life of the bug.
    starved_port = port + 1
    starved = X.ExecServer(str(h.root), str(h.bundles), slots=2,
                           blender=h.blender, min_free_gb=0.0,
                           min_free_mem_gb=10000.0)
    real_mem = X.memory_available
    X.memory_available = lambda: 1_000_000_000        # 1 GB, deterministically short
    try:
        threading.Thread(target=starved.serve, args=(starved_port,),
                         daemon=True).start()
        time.sleep(0.5)
        with socket.create_connection(("127.0.0.1", starved_port), timeout=30) as sock:
            sock.settimeout(120)
            sock.sendall(json.dumps(
                h.spec(timeout_s=1, argv=["--out", "out/r.txt"])).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        starved_reply = json.loads(buf)
    finally:
        X.memory_available = real_mem
    check("a job the box cannot afford is refused, not run",
          not starved_reply["ok"] and "free memory" in starved_reply.get("error", ""),
          starved_reply.get("error", "")[:70])
    check("and the refusal crosses the wire MARKED as a wait, so the broker "
          "cannot mistake it for a broken script",
          starved_reply.get("wait") is True, str(starved_reply.get("wait")))
    check("a genuine child failure is NOT marked as a wait — the marker must "
          "distinguish, not blanket",
          send(h.spec(entry="tools/boom.py")).get("wait") is None)

    check_the_scene_cache(h)
    check_the_gpu_guard(h)


def check_the_gpu_guard(h: Harness) -> None:
    """The card is a declared resource, and its default is NO.

    THE DEFECT THIS SECTION PINS, 2026-08-07. An agent ran a render through
    `rq exec` and set `cycles.device = GPU`. That put a second 8 GB film scene
    on the same 32 GB card as an already-warm render worker holding its own
    scene. Another agent's `carhero` job died twice with `Out of memory in CUDA
    queue enqueue`, the second time terminally. Cancelling the exec job fixed
    the victim within seconds; the re-run was CPU-only and fine.

    Nothing in this file had an opinion about the card. Every sizing decision in
    it is about CPUs and cgroup memory, `deprioritise_for_oom` exists to keep an
    exec child from being what survives a RAM squeeze the render worker loses,
    and VRAM — the one resource with no cgroup, no gate and no OOM score to bias
    — was assumed rather than enforced.

    THE ARTEFACT IS LOOKED AT, NOT ONLY THE EXCEPTION, per the project's own
    rule: the refusal must NAME the scene it is protecting and the card it is
    protecting it on, because "the job failed" is not something a caller can
    act on. And the clamp is asserted on the CHILD'S ENVIRONMENT, not on the
    fact that a flag was read — a guard that inspects the spec is a guess about
    caller-supplied Python, and an empty CUDA_VISIBLE_DEVICES is not.
    """
    # --- the clamp, which is the half that cannot be argued with -----------
    #
    # An entry script that prints its own view of the card. This is the real
    # test: the supervisor's *decision* is only worth what the child's
    # environment actually says.
    bundle = Path(h.bundles) / h.digest
    (bundle / "tools" / "seesgpu.py").write_text(
        "import os\n"
        "open('out/r.txt', 'w').write(repr(os.environ.get('CUDA_VISIBLE_DEVICES')))\n"
    )
    (bundle / "tools" / "wantsgpu.py").write_text(
        "import bpy\n"
        "bpy.context.scene.cycles.device = 'GPU'\n"
    )

    def ran_with(**over) -> str:
        job = h.server.handle_exec(h.spec(entry="tools/seesgpu.py", **over))
        out = Path(job["out_dir"]) / "r.txt"
        text = out.read_text() if out.is_file() else "(no output)"
        h.server.handle_release({"job_id": over.get("job_id") or job["job_id"]})
        return text

    check("an exec child that declared nothing sees NO CUDA DEVICES — the "
          "CPU-only assumption is enforced in the environment, not assumed "
          "about the caller's Python",
          ran_with() == "''", ran_with())

    # --- the scan, so the clamp is never SILENT ----------------------------
    hits = X.scan_for_gpu(str(bundle))
    check("a bundle whose source selects a GPU device is NAMED, so a clamp is "
          "reported rather than being a downgrade nobody can see",
          "tools/wantsgpu.py" in hits, str(hits))
    plan = h.server.validate(h.spec(entry="tools/ok.py"))
    check("and the hint travels on the plan, which is what puts it on the "
          "reply and in the broker's log",
          "tools/wantsgpu.py" in plan["gpu_hints"], str(plan["gpu_hints"]))
    check("a job that declares gpu is NOT scanned — the scan is advisory and "
          "the declaration is the real signal",
          h.server.validate(h.spec(entry="tools/ok.py", gpu=True))["gpu_hints"] == [])
    (bundle / "tools" / "wantsgpu.py").unlink()

    # --- the refusal -------------------------------------------------------
    #
    # A stand-in render worker: a real process, with the real argv shape
    # `remote.worker_launch_cmd` builds, so `render_worker` is exercised against
    # what it will actually meet rather than against a mock of itself.
    workspace = Path(h.server.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    server_py = workspace / "server.py"
    server_py.write_text("import time\ntime.sleep(600)\n")
    scene = str(workspace / "scenes" / "deadbeef" / "film18.blend")
    fake = subprocess.Popen(
        [sys.executable, str(server_py), "-b", scene, "-P", str(server_py),
         "--", "--port", "8799"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if X.render_worker(str(workspace)):
                break
            time.sleep(0.05)
        found = X.render_worker(str(workspace))
        check("the render worker is found by its `-P <workspace>/server.py` "
              "argv, not by the name 'blender' — which on this box also matches "
              "this server, twelve exec children and every orphan of either",
              bool(found) and found["pid"] == fake.pid, str(found))
        check("and the scene it holds is READ off that same argv, so nothing "
              "has to remember to update a state file",
              bool(found) and found["scene"] == scene, str(found and found["scene"]))

        try:
            h.server.validate(h.spec(gpu=True))
            check("REFUSED: gpu:true while the render worker holds a scene", False,
                  "the job was ADMITTED")
        except X.GpuContended as exc:
            check("REFUSED: gpu:true while the render worker holds a scene",
                  True, str(exc)[:70])
            check("and the refusal NAMES the scene it is protecting, so the "
                  "caller can act on it instead of guessing",
                  "film18.blend" in str(exc), str(exc)[:70])
            check("and it names the pid holding it",
                  str(fake.pid) in str(exc))

        # The refusal must cost NOTHING. It lands in `validate`, before the
        # slot, before the memory gate and before an 8 GB scene push — a guard
        # that spends the resource it is protecting is not a guard.
        check("the refusal takes no slot — it is decided before admission",
              h.server.slots.snapshot()["free"] == h.server.slots.total,
              str(h.server.slots.snapshot()))

        check("a job that did NOT declare gpu is admitted while the worker "
              "holds the card — the clamp is what protects it, so the guard "
              "must not become a general outage",
              bool(h.server.validate(h.spec())))

        state = h.server.gpu_state()
        check("ping's gpu state reports the holder, which is how the REVERSE "
              "order becomes answerable: a worker must not start on a card an "
              "exec job holds either",
              (state["holder"] or {}).get("what") == "render worker", str(state["holder"]))
    finally:
        # By pid, and only the pid this test started. Never a pattern: the
        # whole point of `render_worker` is that matching on 'blender' is what
        # kills the thing being protected.
        fake.kill()
        fake.wait(timeout=10)

    check("with no render worker running, the card reads as free rather than "
          "as unknown — 'I could not tell' is never rendered as 'no', but "
          "'nobody is there' must not be rendered as 'somebody is'",
          X.render_worker(str(workspace)) is None)

    # --- the wire ----------------------------------------------------------
    try:
        h.server.validate(h.spec(gpu="yes"))
        check("REFUSED: a truthy string for gpu", False, "accepted")
    except ValueError as exc:
        check("REFUSED: a truthy string for gpu — a declaration must be made "
              "on purpose, not by accident of JSON typing",
              True, str(exc)[:60])

    check("gpu is in the schema the BROKER reads out of this file, so the "
          "declaration travels instead of being dropped from the payload",
          "gpu" in X.EXEC_OPTIONAL)


def check_the_scene_cache(h: Harness) -> None:
    """The `.complete` marker on the SCENE cache — read here, written elsewhere.

    THE DEFECT THIS SECTION PINS. `rq exec --scene <a blend that only ever
    reached the instance via the EXEC staging path>` was refused by this server,
    every time, for every such scene. Job dea2b1d24914 on instance 47049525,
    2026-08-07: film16_R2851.blend (7.97 GB, digest 8b12a832281eef52) was pushed
    three times in five and a half minutes and refused three times with "is not
    completely staged on this instance". Verified on the box afterwards — the
    blend was sitting at `/workspace/scene.blend`, the legacy default of
    `remote.push_scene`, and `/workspace/scenes/8b12a832281eef52/` did not
    exist. The broker's exec path never called `mark_scene_complete` at all.

    THE READER WAS RIGHT AND STAYS UNCHANGED IN WHAT IT DEMANDS. A blend
    without its marker is refused here for a good reason: sibling cache trees
    are pushed after the blend, and Blender answers a half-copied physics cache
    by simulating rather than by failing — a different image, silently. So the
    fix was to the writer, and these checks exist to make sure the safety
    property survived it: the marker is still what "staged" means, a payload
    without one is still refused, and a marker without its payload is refused
    too.

    The scene cache is deliberately NOT under this server's root — it is
    `dirname(--root)/scenes`, filled by the render path — so it is built here
    exactly where the broker's `remote.scene_dir` puts it.
    """
    scenes = Path(h.server.scenes)
    digest = "8b12a832281eef52"
    name = "film16_R2851.blend"

    def plan_for(dig: str, sname: str = name) -> dict:
        return h.server.validate(h.spec(argv=["--out", "out/r.txt"],
                                        scene_digest=dig, scene_name=sname))

    # 1. A COMPLETE ENTRY — payload then marker, the order the writer uses.
    (scenes / digest).mkdir(parents=True, exist_ok=True)
    (scenes / digest / name).write_bytes(b"BLENDER-v502" + b"\x00" * 2048)
    (scenes / digest / "blendcache_film16_R2851").mkdir(exist_ok=True)
    (scenes / digest / "blendcache_film16_R2851" / "cloth_000620_00.bphys").write_bytes(b"x" * 64)

    # Before the marker: this is EXACTLY what the failing job saw, minus the
    # wrong path. Refused, and that refusal is the safety property.
    try:
        h.server.stage(plan_for(digest))
        check("REFUSED: a payload that is all there but has no marker yet — "
              "the writer may still be pushing cache trees", False, "accepted")
    except ValueError as exc:
        check("REFUSED: a payload that is all there but has no marker yet — "
              "the writer may still be pushing cache trees",
              "not completely staged" in str(exc), str(exc)[:90])

    # And the marker arrives last. Now it must open.
    (scenes / digest / X.SCENE_COMPLETE).touch()
    paths = h.server.stage(plan_for(digest))
    linked = os.path.join(paths["job"], "scene.blend")
    check("a scene whose ONLY push came from the exec staging path opens — the "
          "whole point of the fix",
          os.path.isfile(linked), linked)
    check("and it is the same bytes, linked rather than copied, so an 8 GB "
          "assembly costs no disk per job",
          os.path.samefile(linked, scenes / digest / name))

    # 2. THE MARKER MUST NOT BE ENOUGH ON ITS OWN. A marker beside a missing
    #    blend is a cache entry that lies in the other direction.
    empty = "0011223344556677"
    (scenes / empty).mkdir(parents=True, exist_ok=True)
    (scenes / empty / X.SCENE_COMPLETE).touch()
    try:
        h.server.stage(plan_for(empty))
        check("REFUSED: a marker with no blend beside it", False, "accepted")
    except ValueError as exc:
        check("REFUSED: a marker with no blend beside it",
              "holds no" in str(exc), str(exc)[:90])

    # 3. A DIGEST THAT IS NOT HERE AT ALL is an error, never a near-match on
    #    the name. Two assemblies routinely share a filename.
    try:
        h.server.stage(plan_for("aaaabbbbccccdddd"))
        check("REFUSED: a digest that is not staged at all", False, "accepted")
    except ValueError as exc:
        check("REFUSED: a digest that is not staged at all",
              "not completely staged" in str(exc), str(exc)[:90])

    # 4. THE REFUSAL CARRIES THE DIRECTORY IT LOOKED IN. The failing job's log
    #    said "no .complete marker" and nothing about where, so five and a half
    #    minutes of pushes went by before anyone could see that the reader and
    #    the writer were looking at different paths.
    try:
        h.server.stage(plan_for("aaaabbbbccccdddd"))
    except ValueError as exc:
        check("and the refusal NAMES the directory it looked in, so a path "
              "disagreement is visible in one log line",
              str(scenes / "aaaabbbbccccdddd") in str(exc), str(exc)[-70:])

    # 5. The name selects a file inside the digest's directory and cannot
    #    escape it — it originates with a caller and becomes a path here.
    for bad in ("../../etc/passwd", "sub/dir.blend", "..", ""):
        try:
            h.server.stage({**plan_for(digest), "scene_name": bad})
            check(f"REFUSED: scene_name {bad!r} escaping the digest directory",
                  False, "accepted")
        except ValueError as exc:
            check(f"REFUSED: scene_name {bad!r} escaping the digest directory",
                  True, str(exc)[:60])

    # 6. The constant the broker mirrors. `broker/remote.py` writes
    #    `SCENE_COMPLETE` and matches `NOT_STAGED_MARK` against what is raised
    #    here; both files deploy separately, with no shared imports.
    check("the marker this server reads is the one the broker writes",
          X.SCENE_COMPLETE == ".complete", X.SCENE_COMPLETE)
    try:
        h.server.stage(plan_for("aaaabbbbccccdddd"))
    except ValueError as exc:
        check("and the phrase the broker matches to recognise this refusal — "
              "and stop re-pushing gigabytes — is still in it",
              "is not completely staged on this instance" in str(exc),
              str(exc)[:60])


if __name__ == "__main__":
    raise SystemExit(main())
