#!/usr/bin/env python3
"""What each running broker is ACTUALLY EXECUTING, against what is in the tree.

    scripts/drift.py                # every broker on this box
    scripts/drift.py --all          # every tracked file, not only the drifted
    scripts/drift.py --pid 677451   # one process
    scripts/drift.py --json         # for something else to read

Exit status is 0 when nothing has drifted and 1 when something has, so this is
usable as a pre-flight from a script and not only by eye.

WHY THIS EXISTS
===============
On 2026-08-07 the exec staging path pushed an 8 GB blend to the legacy default
`/workspace/scene.blend` and the exec server refused it — the exact condition
`SceneStagingMismatch` was written to make terminal on first sight. The refusal
never fired. The reason was not in the code:

    broker process started      05:51
    broker/execservice.py fixed 07:45

The running process predated its own fix by nearly two hours. It was importing
bytecode compiled from a file that no longer existed in that form, and every
reader of the tree — including the agent debugging the incident — was reading a
version nothing was executing.

**A fix in the tree and not on the box is a fix that does not exist.** That is a
sentence, and a sentence is not a mechanism. This is the mechanism.

WHAT IT CAN AND CANNOT KNOW, STATED PRECISELY
=============================================
It cannot read the source out of a running interpreter — the text is gone after
compilation. So it triangulates from three facts that are all recorded by the
kernel or by CPython, none of which requires touching the process:

  * **Process start time**, from /proc/<pid>/stat field 22 plus /proc/stat's
    btime. Not `ps` etime rounded to seconds, and not the mtime of /proc/<pid>,
    which is the time you looked at it and is the same for every process on the
    box — a trap this file's first draft fell into.
  * **Source mtime and sha256**, from the tree the process's own cwd names.
  * **The .pyc header**, which under PEP 552's default timestamp scheme records
    the source's mtime and size AS SEEN AT IMPORT. That is the strongest single
    piece of evidence available: if the .pyc was written before the process
    started and its embedded mtime matches the file on disk now, the process
    imported the bytes that are there now. If the embedded mtime is older, the
    process imported something else.

The verdict is deliberately three-valued, for the same reason every other
tri-state in this project is:

    STALE    the source is newer than the process. It is running old code.
    ok       the source has not been touched since the process started.
    ?        it could not be determined (no .pyc, a hash-based .pyc with no
             timestamp, an unreadable file). NEVER reported as ok.

WHAT IT DELIBERATELY DOES NOT DO
================================
It never signals, restarts, connects to or writes anything. It reads /proc and
it reads files. A drift checker that could fix drift would be a drift checker
that could restart a broker mid-render, and the whole family of defects this
project keeps paying for is operations whose default scope is "whatever is
there" rather than "what is mine".

It also does not report on `worker/*.py`. Those files do not run here — they are
pushed to the rented instance and started there by `remote.ensure_ready`, so
their drift is between this tree and a box, on a schedule this process cannot
see. `--remote` is the place to add that; it is not built, because asking would
mean opening an SSH session to an instance another agent is rendering on.
`worker/` files are listed under a separate heading with their mtimes so the
question is at least visible.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

# What a broker process actually imports. Kept as globs against the tree rather
# than as a list of names, so a module added later is covered without anybody
# remembering to add it here — the failure mode of a hand-maintained list is
# that the one new file is the one that drifted.
LOCAL_GLOBS = ("broker/*.py", "vastctl/*.py", "scripts/*.sh", "rq", "fleetctl")
# Deployed to the instance, not run here. Reported separately and never as a
# verdict, because this process cannot see the box.
REMOTE_GLOBS = ("worker/*.py",)


def boot_time() -> float:
    """Seconds since the epoch at which this kernel booted."""
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("btime "):
                return float(line.split()[1])
    raise RuntimeError("/proc/stat has no btime — cannot date any process")


def start_time(pid: int) -> float | None:
    """When this pid started, as an epoch. None if it is gone.

    Field 22 of /proc/<pid>/stat, in clock ticks since boot. The comm field is
    parenthesised and may itself contain spaces and parentheses, so the split
    is after the LAST ')' — the usual /proc/stat trap, and the same one
    `proc_alive` in worker/exec_server.py documents.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    tail = raw.rpartition(b")")[2].split()
    if len(tail) < 20:
        return None
    ticks = float(tail[19])                       # field 22 is index 19 here
    return boot_time() + ticks / os.sysconf("SC_CLK_TCK")


def cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def proc_env(pid: int) -> dict:
    """The process's environment, or {} if it is not ours to read.

    Only used to name the broker (VASTRENDER_LABEL, VASTRENDER_PORT) so two
    brokers on one tree are distinguishable in the report. A broker whose
    environ cannot be read is still reported, with less colour.
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    out = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, _, v = item.partition(b"=")
            out[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return out


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_tag_of(python: str) -> str | None:
    """The `cpython-3NN` tag of ANOTHER interpreter.

    THIS IS NOT A DETAIL. `importlib.util.cache_from_source` uses the tag of the
    interpreter that CALLS it, and this tree has bytecode from two: the brokers
    run `.venv/bin/python` (3.13, `cpython-313`) and `rq` runs on `/usr/bin/env
    python3` (3.14, `cpython-314`). The first version of this file read
    `broker/__pycache__/app.cpython-314.pyc` — leftover bytecode from some
    unrelated 3.14 invocation — and reported seven files as STALE on a broker
    that had none. A drift checker that cries wolf is worse than no drift
    checker, because the next real one is the one nobody reads.

    So the tag is taken from the broker's OWN interpreter, named on its argv.
    """
    try:
        out = subprocess.run(
            [python, "-c", "import sys; print(sys.implementation.cache_tag)"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    tag = out.stdout.strip()
    return tag if out.returncode == 0 and tag else None


def pyc_evidence(src: Path, tag: str | None) -> dict:
    """What the cached bytecode says about the source it was compiled from.

    PEP 552: the .pyc header is magic(4) + flags(4) + 8 bytes whose meaning
    depends on flags bit 0 — clear means (source mtime, source size), set means
    a source hash. Only the timestamp form can be compared against a file on
    disk without recompiling, and it is CPython's default, so the hash form is
    reported honestly as "unknown" rather than guessed at.

    `pyc_mtime` matters as much as the embedded values: bytecode written AFTER
    the process started was written by some other interpreter and says nothing
    about what this one imported.

    `tag` is the BROKER's cache tag, not this script's. See `cache_tag_of`.
    """
    if tag:
        cache = str(src.parent / "__pycache__" / f"{src.stem}.{tag}.pyc")
    else:
        cache = importlib.util.cache_from_source(str(src))
    out: dict = {"pyc": cache, "exists": False, "src_mtime": None,
                 "src_size": None, "pyc_mtime": None, "hash_based": False}
    try:
        with open(cache, "rb") as fh:
            head = fh.read(16)
        out["pyc_mtime"] = os.path.getmtime(cache)
    except OSError:
        return out
    if len(head) < 16:
        return out
    out["exists"] = True
    flags = struct.unpack("<I", head[4:8])[0]
    if flags & 0x1:
        out["hash_based"] = True
        return out
    mtime, size = struct.unpack("<II", head[8:16])
    out["src_mtime"] = float(mtime)
    out["src_size"] = int(size)
    return out


def git_state(root: Path) -> dict:
    """HEAD and whether the tree is dirty. Context, never a verdict.

    A dirty tree is not drift — it is somebody working. It is reported because
    "the tree is ahead of the process" reads very differently when the tree is
    also ahead of the last commit.
    """
    def run(*args) -> str:
        try:
            out = subprocess.run(["git", "-C", str(root), *args],
                                 capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""
    head = run("rev-parse", "--short", "HEAD")
    dirty = run("status", "--porcelain")
    return {"head": head or "(no git)",
            "dirty": [line[3:] for line in dirty.splitlines()] if dirty else []}


def brokers(only_pid: int | None = None) -> list[dict]:
    """Every broker process on this box, and the supervisor above it.

    Identified by `-m broker.app` on the argv — the module the process is
    actually running — rather than by the string "broker", which on this box
    also matches dbus-broker, dbus-broker-launch and a Chromium helper. This
    project has lost two multi-hour batches to a pattern that matched more than
    it meant; this file will not add a third.
    """
    found = []
    for entry in sorted(os.listdir("/proc")):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if only_pid is not None and pid != only_pid:
            continue
        argv = cmdline(pid)
        if not ("-m" in argv and "broker.app" in argv):
            continue
        env = proc_env(pid)
        found.append({
            "pid": pid,
            "started": start_time(pid),
            "cwd": proc_cwd(pid),
            "argv": argv,
            # argv[0] is the interpreter this broker runs, and its cache tag is
            # the only one whose .pyc files say anything about what it imported.
            "python": argv[0],
            "cache_tag": cache_tag_of(argv[0]),
            "label": env.get("VASTRENDER_LABEL") or "(default)",
            "port": env.get("VASTRENDER_PORT") or "(default)",
            "db": env.get("VASTRENDER_DB") or "(default)",
            "log": env.get("VASTRENDER_LOG") or "(default)",
        })
    return found


def audit(root: Path, started: float, globs, tag: str | None = None) -> list[dict]:
    rows = []
    for pattern in globs:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            st = path.stat()
            ev = pyc_evidence(path, tag) if path.suffix == ".py" else {"exists": False}
            # THE VERDICT. Newer than the process is unambiguous: those bytes
            # were not what was imported. Everything else needs the .pyc to say
            # anything at all, and when it cannot, this says so.
            if st.st_mtime > started:
                verdict = "STALE"
                why = (f"source is {(st.st_mtime - started) / 60:.0f} min newer "
                       f"than the process")
            elif ev.get("hash_based"):
                verdict = "?"
                why = "hash-based .pyc — no timestamp to compare"
            elif ev.get("src_mtime") is None:
                verdict = "?"
                why = "no .pyc: cannot tell what was imported, only that the file is older"
            elif int(ev["src_mtime"]) != int(st.st_mtime) or ev["src_size"] != st.st_size:
                verdict = "STALE"
                why = (f".pyc was compiled from mtime {int(ev['src_mtime'])} "
                       f"size {ev['src_size']}, on disk now is mtime "
                       f"{int(st.st_mtime)} size {st.st_size}")
            elif ev.get("pyc_mtime") and ev["pyc_mtime"] > started:
                verdict = "?"
                why = ("the .pyc was written after this process started — it "
                       "matches the tree, but another interpreter wrote it")
            else:
                verdict = "ok"
                why = "untouched since the process started, and the .pyc agrees"
            rows.append({
                "path": str(path.relative_to(root)),
                "mtime": st.st_mtime,
                "bytes": st.st_size,
                "sha256": sha256_of(path)[:12],
                "verdict": verdict,
                "why": why,
            })
    return rows


def stamp(t: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "?"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="deployed-vs-tree drift for every running broker")
    ap.add_argument("--all", action="store_true",
                    help="print every tracked file, not only the drifted ones")
    ap.add_argument("--pid", type=int, help="only this broker")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    procs = brokers(a.pid)
    if not procs:
        print("no broker process is running (looked for `-m broker.app` in /proc)")
        return 0

    report = []
    drifted_total = 0
    for proc in procs:
        root = Path(proc["cwd"] or ".").resolve()
        started = proc["started"] or 0.0
        tag = proc.get("cache_tag")
        local = audit(root, started, LOCAL_GLOBS, tag)
        remote = audit(root, started, REMOTE_GLOBS, tag)
        stale = [r for r in local if r["verdict"] == "STALE"]
        unknown = [r for r in local if r["verdict"] == "?"]
        drifted_total += len(stale)
        report.append({"proc": proc, "root": str(root), "git": git_state(root),
                       "local": local, "remote": remote,
                       "stale": len(stale), "unknown": len(unknown)})

    if a.json:
        print(json.dumps(report, indent=2, default=str))
        return 1 if drifted_total else 0

    for item in report:
        proc, git = item["proc"], item["git"]
        print(f"\n=== broker pid {proc['pid']}  label={proc['label']}  "
              f"port={proc['port']} ===")
        print(f"    started   {stamp(proc['started'])}"
              f"   ({(time.time() - (proc['started'] or 0)) / 3600:.1f} h ago)")
        print(f"    tree      {item['root']}   git {git['head']}"
              + (f"   {len(git['dirty'])} uncommitted file(s)" if git["dirty"] else ""))
        print(f"    python    {proc['python']}   bytecode tag "
              f"{proc['cache_tag'] or '(unknown — .pyc evidence unavailable)'}")
        print(f"    db        {proc['db']}")
        rows = item["local"] if a.all else [r for r in item["local"]
                                            if r["verdict"] != "ok"]
        if not rows:
            print("    NO DRIFT — every tracked file predates this process and "
                  "its bytecode agrees")
        for r in sorted(rows, key=lambda r: (r["verdict"] != "STALE", r["path"])):
            print(f"    {r['verdict']:5s} {r['path']:28s} {stamp(r['mtime'])} "
                  f"{r['sha256']}  {r['why']}")
        if item["stale"]:
            print(f"    >> {item['stale']} FILE(S) IN THE TREE ARE NEWER THAN THIS "
                  f"PROCESS. It is running the old ones. Nothing here restarts "
                  f"anything — that is a human decision, because a restart "
                  f"re-claims jobs mid-flight.")
        print("    worker/ (runs on the INSTANCE, not here — pushed by "
              "ensure_ready at worker start, so this is tree state only):")
        for r in item["remote"]:
            print(f"          {r['path']:28s} {stamp(r['mtime'])} {r['sha256']}")

    print(f"\n{drifted_total} drifted file(s) across {len(report)} broker(s)")
    return 1 if drifted_total else 0


if __name__ == "__main__":
    sys.exit(main())
