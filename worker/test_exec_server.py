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


if __name__ == "__main__":
    raise SystemExit(main())
