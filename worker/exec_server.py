#!/usr/bin/env python3
"""Remote EXEC supervisor. Runs *inside* Blender, but never touches bpy:

    blender -b --factory-startup -P worker/exec_server.py -- \
        --port 8800 --root /workspace/exec --bundles /workspace/bundles --slots 12

WHY THIS IS A SECOND PROCESS AND NOT PART OF server.py
======================================================
`worker/server.py` opens with two laws, and the first one is *never thread*:
Blender's own docs say Python threads crash it during Cycles renders, so that
server accepts, renders and replies strictly serially on the main thread. That
law is right for GPU work — concurrent renders timeslice rather than add
throughput — and it is exactly wrong for this job type. An item BUILD is
single-threaded CPU-bound Python; the whole reason to move it to the rented box
is to run twelve at once. Putting builds inside the render worker would
serialise every build behind every render *and* hand a build process the ability
to corrupt the warm scene it holds resident.

So: a different process, a different port, the same newline-JSON-over-TCP
protocol, the same `ssh -L` tunnel. The render worker's code is untouched.

WHY THIS ONE MAY THREAD
-----------------------
It never imports bpy and never calls into Blender. Everything it does is
`subprocess`, `os` and `socket` — CPython, not Blender's embedded interpreter's
scene state. The Blender process here is nothing but a Python 3 runtime that
happens to be the only one on the box with the modules the child scripts need.

WHY IT RUNS UNDER BLENDER AT ALL
--------------------------------
Measured on this instance rather than assumed: `/usr/bin/python3` exists (3.12.3
in the CUDA base image, contrary to what the plan predicted) but `import numpy`
fails — and every item module in the campaign imports numpy at module scope. The
apt list on this image has no python3-numpy and no pip. Blender ships its own
CPython with numpy, so launching under Blender needs zero provisioning changes
and cannot drift from the interpreter the children themselves run in.

WHAT A JOB IS
-------------
One `blender -b --factory-startup -P <entry> -- <argv>` child, run inside a
directory this server owns, against a *copy* of a content-addressed input
bundle, producing a declared list of output files that the broker then fetches.

    /workspace/exec/<job_id>/bundle/     copy of /workspace/bundles/<digest>/
    /workspace/exec/<job_id>/out/        everything the caller declared
    /workspace/exec/<job_id>/tmp/        TMPDIR and HOME for the child
    /workspace/exec/<job_id>/job.log     the child's stdout+stderr

The bundle is COPIED, not symlinked or shared: a build that writes beside its
own module — which several of these modules do — must not be able to corrupt the
cache every other job is reading. It is ~7 MB of Python; the copy costs
milliseconds and removes a whole class of cross-job contamination.

NO DEFAULTS, EVER
-----------------
`EXEC_REQUIRED` mirrors `server.py`'s `REQUIRED` in shape and in reason. This is
a resident process serving many jobs, so an omitted field would not be "unset",
it would be *whatever the previous job used*. That is not a hypothetical here:
`timeout_s`, `cpu_slots` and `blender_args` all differ between a 40 s interface
dump and a 25-minute paving build. A spec missing any field is rejected with the
field named.

CONTAINMENT, AND WHAT IT DOES NOT PROMISE
-----------------------------------------
EXEC runs caller-supplied Python. That is the entire point of the job type, so
this is not a sandbox and nothing here pretends otherwise. What it does enforce,
because these were live path-traversal vectors in the render path already:

  * `job_id` matches `[A-Za-z0-9_-]{1,64}` — it becomes a directory name.
  * `bundle` matches `[0-9a-f]{8,64}` — it becomes a directory name.
  * `entry` is resolved with realpath FIRST and then required to sit inside the
    job's own bundle copy. Resolving first is the whole trick: a prefix test
    applied before resolution passes `bundle/../../../etc/shadow` happily.
  * every `outputs` element resolves inside the job's `out/`.
  * every token of `argv` and `blender_args` that looks like a path (contains a
    `/`) resolves inside the job directory. So `--save out/x.blend` is fine and
    `--save /home/zany/f1-round2/world/items/x.py` is refused. A script can of
    course still build a path in code; cwd, TMPDIR and HOME all point into the
    job directory so that the *accidental* case — a module writing beside itself
    or into `~` — lands where it should.
  * the child is spawned from an argv LIST, never through a shell.

WHAT LEAVES, AND WHEN
---------------------
Nothing is fetched that was not declared. After the child exits, everything in
the job directory except `out/` and `job.log` is deleted immediately — the
bundle copy, the child's TMPDIR, and any stray file it wrote. A 30 GB disk with
twelve concurrent builds saving multi-gigabyte .blends does not survive keeping
scratch around. The rest of the directory goes when the broker sends `release`
after it has fetched and verified the outputs.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from typing import Optional

# Deliberately NOT imported. This process must stay a plain Python supervisor;
# the moment it touches bpy it inherits server.py's never-thread law and can no
# longer run twelve children at once.
#   import bpy

# Every field a caller must supply. There are deliberately no defaults, for the
# same reason server.py has none: this is a warm process, so a missing key would
# be served by whatever the previous job left behind rather than being "unset".
EXEC_REQUIRED = frozenset({
    "job_id",        # broker-minted; re-validated here as [A-Za-z0-9_-]{1,64}
    "bundle",        # digest of a pushed input bundle; must already be complete
    "entry",         # path RELATIVE to the bundle root
    "argv",          # list[str], passed after `--`; never a shell string
    "outputs",       # list[str] relative to out/; explicit, never a glob
    "timeout_s",     # int; hard kill of the whole process group
    "blender_args",  # list[str], e.g. ["-b", "--factory-startup"]
    "cpu_slots",     # int; how many of this server's slots the job occupies
})

# Fields a caller MAY supply. Separate from EXEC_REQUIRED because "no defaults"
# is a rule about fields that must be present on every job, and these are not:
# a build that opens an existing assembly needs a scene, a build whose blend is
# born here does not. The broker reads BOTH sets, so an optional field still
# travels rather than being silently dropped from the payload.
EXEC_OPTIONAL = frozenset({
    "scene_digest",  # digest of a scene already in this instance's scene cache
    "scene_name",    # bare filename INSIDE that digest's directory
    "gpu",           # bool; DECLARE that this job wants the card. See below.
})

JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
DIGEST_RE = re.compile(r"[0-9a-f]{8,64}")

# A job may not ask for more wall clock than this. The in-container watchdog
# retires the instance at 12 h regardless of what it is doing, so a job longer
# than an hour is a job that will be killed by hardware rotation instead of by
# anything that can requeue it politely.
MAX_TIMEOUT_S = 3600

# How long a job may sit waiting for a slot or for memory before it is answered
# with a refusal. Bounded separately from `timeout_s`, and deliberately much
# smaller: the broker dispatches at most `slots` jobs at once, so a long wait
# here means the broker and this server disagree about the slot count — and a
# job that spends its whole 50-minute budget in a queue nobody can see, then
# times out on the socket and is requeued as a transport failure, is a loop.
# Answering "no slot in 10 minutes" is a fact the broker can act on.
SLOT_WAIT_MAX_S = 600

MAX_REQUEST_BYTES = 1 << 20
LOG_TAIL_CHARS = 4000
BUNDLE_COMPLETE = ".complete"
# The SAME name, for the scene cache the render path fills, and spelled out
# separately because it is read from a directory this server never writes.
# `broker/remote.py` writes it (`SCENE_COMPLETE`) and mirrors this phrase as
# `NOT_STAGED_MARK` so a refusal from here is recognisable on that side as a
# path disagreement rather than a failed build. Keep the three in step; there is
# a test on both sides that reads the other's source and fails if they drift.
SCENE_COMPLETE = ".complete"

# Blender flags that would give the spec a second way to execute code. `entry`
# is the one sanctioned entry point precisely so there is one place to
# containment-check, and a `-P` hidden in blender_args would route around it.
BANNED_BLENDER_ARGS = (
    "-P", "--python", "--python-text", "--python-expr", "--python-console",
    "--python-exit-code", "--addons", "--enable-autoexec",
)

_PRINT_LOCK = threading.Lock()


def log(msg: str) -> None:
    with _PRINT_LOCK:
        print(f"[exec] {msg}", flush=True)


def self_digest() -> str:
    """sha256 of THIS FILE, so a deployed copy can be compared to the tree.

    The broker pushes this file and starts it, and the two sides then live
    independently — `ensure_ready` will not replace a running exec server. So
    the only honest answer to "which version is on the box?" is one the box
    gives, and it is 12 hex characters in the startup log and on every ping.
    """
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:                                          # pragma: no cover
        return "unknown"


def self_mtime() -> float:
    try:
        return os.path.getmtime(os.path.abspath(__file__))
    except OSError:                                          # pragma: no cover
        return 0.0


# --- memory ---------------------------------------------------------------
#
# MEASURED ON THE INSTANCE, not taken from the rental description. The plan this
# implements sized twelve slots against "515 GB RAM"; the box actually running
# the campaign reports
#
#     /sys/fs/cgroup/memory.max        97169440768   (90.5 GiB)
#     /sys/fs/cgroup/cpu.max           2304000 100000 -> 23.04 CPUs
#     nproc                            96
#     /proc/meminfo MemTotal           188 GB
#     /proc/loadavg                    99.5
#
# `nproc`, `free` and `loadavg` are all the HOST's — none of those files are
# namespaced — so every one of them overstates what this container may use, and
# the load average is mostly other tenants. The cgroup files are the only honest
# numbers on the box, and 90 GiB across twelve concurrent item builds is 7.5 GiB
# each against measured peaks well above that.
#
# The consequence if this is ignored is not a slow job. A cgroup OOM picks its
# victim by RSS, and the largest RSS on this box is the *render worker* holding
# the warm scene — so an over-committed exec batch would kill somebody else's
# 4K render and leave the broker to diagnose a "dead worker".

MEM_MAX = "/sys/fs/cgroup/memory.max"
MEM_CURRENT = "/sys/fs/cgroup/memory.current"


class ResourceWait(RuntimeError):
    """The box could not afford this job right now. THE WORK NEVER STARTED.

    Its own class so the fact survives the wire. `await_memory` already calls
    itself "a WAIT rather than a rejection — the work is fine, the moment is
    not", and that was true right up to the point where the reply was
    serialised: every failure leaves here as `{"ok": false, "error": "..."}`,
    the broker's `run_one` turns any non-ok reply into a plain `RuntimeError`,
    and a plain `RuntimeError` is the broker's word for "the caller's script is
    broken" — so it spends an attempt and, three times, fails the job.

    Measured on instance 47040457 on 2026-08-07, 03:43:14 and 03:43:29, on two
    jobs at once:

      RuntimeError: 88de1f4d5faf waited 602s for 20.0G of free memory and only
      3.7G was ever available — the container cap is memory.max, not `free`

    Both were charged an attempt for a memory shortage caused by the render
    worker and eleven sibling builds. Neither had executed a line of its own
    code: `await_memory` sits in front of `stage` and `run_child`, so at the
    moment this is raised there is not even a child process.

    A classification that cannot cross the wire is not a classification. This
    one crosses as `wait: true` on the reply, and the broker turns it back into
    the wait it always was.
    """


class Canceled(RuntimeError):
    """Somebody asked for this job to stop. IT IS NOT A FAILURE OF THE BUILD.

    Its own class for the same reason `ResourceWait` has one: the distinction
    has to survive the wire, or the broker's `run_one` reads the non-ok reply as
    "the caller's script is broken" and spends an attempt retrying work that was
    deliberately stopped. It crosses as `canceled: true` on the reply.

    The defect this exists for, measured on instance 47040457 on 2026-08-07:
    `rq cancel a39bd71095f9` returned `{"canceled": true}` at 03:46 and the
    remote Blender child kept running until its own `--timeout` expired at
    04:44, holding 6 of 12 exec slots and about 8 GB of an assembly the whole
    time. In the same window two of another agent's jobs were refused by the
    memory gate (`only 10.8G was ever available`, `only 3.7G was ever
    available`) and one was OOM-killed at `Read blend`. The broker's cancel
    flipped a SQLite row and there was no path from it to the process. There is
    one now.
    """


class GpuContended(RuntimeError):
    """This job asked for the card and somebody else is holding it. TERMINAL.

    THE DEFECT. This server is designed around a CPU-only assumption — every
    sizing decision in this file is about a 23-CPU cgroup quota and a 90 GiB
    memory cap, and `deprioritise_for_oom` exists specifically to keep an exec
    child from being the thing that survives a squeeze the render worker loses.
    Nothing enforced the assumption. A caller-supplied script is caller-supplied
    Python: it can set `scene.cycles.device = 'GPU'` on line one, and until this
    class existed the exec server would run it.

    Measured on 2026-08-07: an exec job set `cycles.device = GPU` and put a
    second 8 GB film scene on the same 32 GB card as an already-warm render
    worker holding its own scene. Another agent's `carhero` render died with
    `Out of memory in CUDA queue enqueue` TWICE, the second time terminally.
    Cancelling the exec job fixed the victim within seconds; the re-run was
    CPU-only and fine. VRAM is the one resource on this box with no cgroup, no
    gate and no OOM score to bias — the card either fits both scenes or it kills
    one of them, and which one it kills is not a decision anybody made.

    WHY TERMINAL AND NOT A `ResourceWait`. A `ResourceWait` says "the moment is
    not right", and it is right for memory because sibling builds end. The
    render worker does not end: it is a WARM process holding a resident scene
    for the whole campaign, by design and as the entire reason it exists. A wait
    on that clears when the fleet tears the instance down, which is not a wait,
    it is a queue that never drains — the job would sit through
    SLOT_WAIT_MAX_S, be requeued without spending an attempt, and come back
    forever. So this crosses the wire as `gpu_refused: true` and the broker
    fails it terminally, on the FIRST refusal, the way `SceneStagingMismatch`
    is failed.

    WHY A REFUSAL AND NOT A DOWNGRADE. A downgrade to CPU is what
    `CUDA_VISIBLE_DEVICES=""` already does for every job that did not declare
    `gpu` (see `run_child`), and that clamp is the real protection because it
    cannot be argued with. But a job that DECLARED `gpu: true` said something,
    and silently doing the opposite of what it said is how this defect stayed
    invisible in the first place. A declaration that is quietly ignored teaches
    nobody anything; a refusal that names the scene and the card teaches the
    caller exactly what it collided with.
    """


# --- the GPU --------------------------------------------------------------
#
# There is no cgroup for VRAM. `memory.max` bounds this container's RAM and
# `await_memory` gates on it; the card has nothing of the kind, so two processes
# that each fit alone will both be admitted and one of them will die inside
# CUDA. Every function below therefore answers the only question that matters —
# "is somebody else on the card right now" — by looking, and answers it in the
# same THREE states `memory_available` and `remote.Activity` use: yes, no, and
# "I could not tell". "I could not tell" is never rendered as "no".

NVIDIA_SMI_TIMEOUT_S = 20.0

# Source patterns that select a GPU device in Cycles. Advisory ONLY — used to
# WARN, never to refuse. A regex over caller-supplied Python is trivially
# defeated (`getattr(cy, "dev" + "ice")`) and just as trivially false-positived
# (the string in a comment, a docstring, a branch behind a flag that is off), so
# it is worth exactly one log line and one field on the reply. The enforcement
# is `CUDA_VISIBLE_DEVICES` in `run_child`, which does not care what the source
# looks like.
GPU_SOURCE_PATTERNS = (
    re.compile(rb"""cycles\s*\.\s*device\s*=\s*['"]GPU['"]"""),
    re.compile(rb"""\.device\s*=\s*['"]GPU['"]"""),
    re.compile(rb"""compute_device_type\s*=\s*['"](CUDA|OPTIX|HIP|ONEAPI|METAL)['"]"""),
    re.compile(rb"""get_devices_for_type\s*\("""),
    re.compile(rb"""cycles_compute_device"""),
)

MAX_SCAN_BYTES = 4 << 20          # per file; a .py bigger than this is not code


def scan_for_gpu(root: str, limit: int = 4000) -> list[str]:
    """Every .py under `root` that looks like it selects a GPU device.

    Returns paths relative to `root`. Bounded by `limit` files so a bundle that
    accidentally includes a site-packages tree cannot turn admission into a
    filesystem walk of unknown length. Never raises: this is a warning, and a
    warning that can break a build is worse than no warning.
    """
    hits: list[str] = []
    seen = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                seen += 1
                if seen > limit:
                    return hits
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(full) > MAX_SCAN_BYTES:
                        continue
                    with open(full, "rb") as fh:
                        blob = fh.read()
                except OSError:
                    continue
                if any(p.search(blob) for p in GPU_SOURCE_PATTERNS):
                    hits.append(os.path.relpath(full, root))
    except OSError:
        pass
    return hits


def gpu_compute_apps() -> Optional[list[dict]]:
    """Every process on the card and how much VRAM it holds, or None.

    None means UNKNOWABLE, not empty: no `nvidia-smi` on PATH, a driver that
    will not answer, a timeout. A container can also be unable to see other
    namespaces' pids while still seeing their memory, in which case pid comes
    back as 0 — that is still a holder, and it is still reported.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-compute-apps=pid,used_gpu_memory,process_name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    apps: list[dict] = []
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            pid = 0
        try:
            mib = int(parts[1])
        except ValueError:
            mib = 0
        apps.append({"pid": pid, "vram_mib": mib,
                     "name": parts[2] if len(parts) > 2 else ""})
    return apps


def gpu_names() -> list[str]:
    """The card(s) this box has, for naming what is being protected."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                              "--format=csv,noheader"],
                             capture_output=True, text=True,
                             timeout=NVIDIA_SMI_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def proc_cmdline(pid: int) -> list[str]:
    """This pid's argv, or [] if it is gone or not ours to read."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def render_worker(workspace: str) -> Optional[dict]:
    """The render worker's pid and the scene it holds, or None if it is not up.

    IDENTIFIED THE SAME WAY `reap_orphans` identifies what it must NOT touch:
    by the exact thing `remote.worker_launch_cmd` builds, which is

        <root>/blender/blender -b <blend> -P <root>/server.py -- --port ...

    So the signal is the literal `-P <workspace>/server.py` token — not the name
    "blender", which on this box also matches this server, twelve exec children
    and any orphan of either. `pkill -f blender` is in this project's defect log
    twice over and this file will not add a third entry.

    The scene comes free: `-b <blend>` is on the same argv, so "the worker holds
    <scene>" is READ, not inferred and not tracked in a state file somebody has
    to remember to update. Nothing is signalled, opened, connected to or asked —
    the render worker is strictly serial and a ping to it blocks for the length
    of a 4K frame, which is precisely the moment this answer is needed.
    """
    marker = os.path.join(workspace, "server.py")
    try:
        entries = os.listdir("/proc")
    except OSError:                                      # pragma: no cover
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        argv = proc_cmdline(pid)
        if not argv:
            continue
        try:
            i = argv.index("-P")
        except ValueError:
            continue
        if i + 1 >= len(argv) or os.path.realpath(argv[i + 1]) != os.path.realpath(marker):
            continue
        if not proc_alive(pid):
            continue
        # The blend is positional after `-b`, but it is positional, so the
        # honest read is "the argv token that is a .blend" with the position
        # after `-b` as the preferred one. A worker whose scene cannot be named
        # is still a worker; `scene` being None must not make this return None.
        scene = None
        if "-b" in argv:
            j = argv.index("-b")
            if j + 1 < len(argv) and argv[j + 1].endswith(".blend"):
                scene = argv[j + 1]
        if scene is None:
            scene = next((a for a in argv if a.endswith(".blend")), None)
        return {"pid": pid, "scene": scene, "rss_bytes": proc_rss(pid)}
    return None


class _Child:
    """One admitted job's kill handle, and the only thing `cancel` targets.

    NOT a name pattern. `pkill -f blender` on this box kills the render worker
    holding a multi-gigabyte warm scene, and killing by name is also how a stale
    worker silently served the previous scene — both already in the project's
    defect log. `reap_orphans` picks its victims by cwd because "the honest
    signal is the working directory"; a cancel can do better still, because it
    knows the job id and therefore the exact `Popen` and the exact process group
    that `start_new_session=True` created for it. Nothing else can be signalled
    through this object even in principle.

    `proc` is None between admission and the fork. That window is real — memory
    waits there for up to ten minutes — so `canceled` is a flag as well as a
    kill, and `event` wakes the waiter instead of making it sit out its poll.
    """

    __slots__ = ("proc", "canceled", "event", "started")

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.canceled = False
        self.event = threading.Event()
        self.started = time.time()


def memory_available() -> Optional[int]:
    """Bytes this container may still allocate, or None if it cannot be known.

    None is deliberately not zero and deliberately not infinity: a caller that
    cannot measure memory must decide what to do about that rather than be
    handed a number that looks like a measurement.
    """
    try:
        with open(MEM_MAX) as fh:
            raw = fh.read().strip()
        if raw != "max":
            with open(MEM_CURRENT) as fh:
                current = int(fh.read().strip())
            return max(0, int(raw) - current)
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


# --- processes ------------------------------------------------------------
#
# Everything below reads /proc and never guesses. Each function answers exactly
# one question about one pid, and every one of them treats "the process went
# away while I was looking at it" as a normal answer rather than an error — a
# reaper races with the thing it is reaping by definition.


def proc_alive(pid: int) -> bool:
    """Is this pid still running — and NOT a zombie?

    A zombie answers `kill(pid, 0)` and appears in /proc, but it has already
    exited: its memory is freed and the entry survives only until someone waits
    on it. Counting one as alive makes the reaper wait out its full SIGKILL
    grace and then report the kill as having FAILED, which is the opposite of
    what happened. That matters because the surviving-orphan warning is meant
    to be believed.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
    except OSError:
        return False
    # The comm field is parenthesised and may itself contain spaces and
    # parentheses, so split after the LAST ')' — the usual /proc/stat trap.
    tail = raw.rpartition(b")")[2].split()
    if not tail:
        return True                          # unparseable: assume alive
    return tail[0] not in (b"Z", b"X", b"x")


def proc_rss(pid: int) -> int:
    """Resident bytes, or 0 if it cannot be read.

    Reported only so the log can say what the reap gave back — the whole reason
    orphans matter here is the memory they hold against the cgroup gate.
    """
    try:
        with open(f"/proc/{pid}/statm") as fh:
            pages = int(fh.read().split()[1])
    except (OSError, ValueError, IndexError):
        return 0
    return pages * os.sysconf("SC_PAGE_SIZE")


def proc_cwd_in(pid: int, root: str) -> Optional[str]:
    """This pid's cwd if it sits inside `root`, else None.

    A DELETED cwd still identifies its process: the kernel renders the link as
    "<path> (deleted)". The suffix is stripped before the containment test
    precisely so a job directory that `sweep_stale` already removed does not
    hide the process that was using it — that is the state the seven hand-reaped
    orphans were found in.

    The suffix is stripped only when what remains is an absolute path, because
    a directory legitimately named "x (deleted)" is a path a caller controls and
    this must not become a way to point the reaper at an arbitrary process.
    """
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None                          # gone, or another user's process
    probe = cwd
    if probe.endswith(" (deleted)"):
        stripped = probe[: -len(" (deleted)")]
        if stripped.startswith("/"):
            probe = stripped
    root = os.path.realpath(root)
    if probe != root and not probe.startswith(root + os.sep):
        return None
    return cwd


# --- paths ----------------------------------------------------------------


def contained(path: str, root: str) -> str:
    """Absolute realpath of `path`, guaranteed to sit inside `root`.

    Resolve FIRST, then test. A prefix comparison applied before resolution is
    defeated by `..` and by a symlink, which is precisely how a client-supplied
    job id became a traversal into a read-only project earlier in this system's
    life. `os.path.realpath` resolves components that do not exist yet, so this
    works for an output file the child has not written.
    """
    real_root = os.path.realpath(root)
    real = os.path.realpath(os.path.join(real_root, path))
    if real != real_root and not real.startswith(real_root + os.sep):
        raise ValueError(
            f"{path!r} resolves to {real}, which is outside {real_root}"
        )
    return real


def check_tokens(tokens: list, root: str, what: str) -> None:
    """Refuse any argv token that names a path outside the job directory.

    Heuristic by design and documented as one: a token is treated as a path when
    it contains a separator. That catches every realistic accident — an absolute
    `--save /home/zany/...`, a `../../` escape — while leaving plain values and
    flags alone.
    """
    for tok in tokens:
        if not isinstance(tok, str):
            raise ValueError(f"{what} must be a list of strings, got {type(tok).__name__}")
        if "\x00" in tok:
            raise ValueError(f"{what} token contains a null byte")
        if "/" not in tok:
            continue
        # `--save=out/x.blend` as one token: check the value half.
        value = tok.split("=", 1)[1] if tok.startswith("-") and "=" in tok else tok
        if "/" not in value:
            continue
        try:
            contained(value, root)
        except ValueError as exc:
            raise ValueError(f"{what} token {tok!r}: {exc}") from None


def sha256_of(path: str) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def tail_file(path: str, chars: int = LOG_TAIL_CHARS) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            fh.seek(max(0, end - chars))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def rmtree_quiet(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


# --- slots ----------------------------------------------------------------


class Slots:
    """A counted semaphore that can hand out more than one permit at a time.

    `threading.Semaphore` can, but it cannot do it atomically against other
    waiters, so two 6-slot jobs against 8 slots can each take 4 and deadlock.
    One condition variable and one integer cannot.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.free = total
        self.cond = threading.Condition()
        self.inflight: dict[str, int] = {}

    def acquire(self, job_id: str, n: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        with self.cond:
            while self.free < n:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self.cond.wait(remaining)
            self.free -= n
            self.inflight[job_id] = n
            return True

    def release(self, job_id: str, n: int) -> None:
        with self.cond:
            self.free = min(self.total, self.free + n)
            self.inflight.pop(job_id, None)
            self.cond.notify_all()

    def snapshot(self) -> dict:
        with self.cond:
            return {"total": self.total, "free": self.free,
                    "inflight": dict(self.inflight)}


# --- the server -----------------------------------------------------------


class ExecServer:
    def __init__(self, root: str, bundles: str, slots: int, blender: str,
                 min_free_gb: float, min_free_mem_gb: float = 0.0) -> None:
        self.root = os.path.realpath(root)
        # WHERE THE SCENE CACHE LIVES, which is NOT under this server's root.
        # `--root` is the EXEC root (/workspace/exec); the render path fills
        # /workspace/scenes, a sibling. Deriving it from the parent rather than
        # joining onto self.root, because the first version did the latter,
        # looked for /workspace/exec/scenes/<digest>/.complete, and reported a
        # scene that was demonstrably resident as "not completely staged" —
        # a wrong path wearing the error message of a corrupt push.
        self.scenes = os.path.join(os.path.dirname(self.root), "scenes")
        self.bundles = os.path.realpath(bundles)
        self.blender = blender
        self.min_free = int(min_free_gb * 1e9)
        self.min_free_mem = int(min_free_mem_gb * 1e9)
        # Serialises the gap between "memory looked available" and "the child
        # has started allocating". Without it twelve threads all read the same
        # comfortable number in the same millisecond and all admit.
        self.admit_lock = threading.Lock()
        # job_id -> _Child, for every job between admission and reply. This is
        # the whole of `cancel`'s reach: a job id that is not in here cannot be
        # signalled, so no request can name a process this server did not start.
        self.children: dict[str, _Child] = {}
        self.child_lock = threading.Lock()
        # job_id -> when it was cancelled, for cancels that arrived BEFORE the
        # job did. See `handle_cancel`: the broker considers a job dispatched
        # from the moment it hands it to a thread, and that thread may spend
        # minutes pushing a bundle and an 8 GB scene before this server ever
        # hears the job id. A cancel in that window has nothing to signal, and
        # without a record of it the child starts afterwards — which is the
        # original defect with a smaller window rather than a fix.
        self.canceled_ids: dict[str, float] = {}
        self.slots = Slots(slots)
        self.started = time.time()
        self.jobs = 0
        self.failures = 0
        # Filled by serve(); reported by ping() so the broker can see that a
        # restart leaked, without anyone having to be logged into the instance
        # at the moment it happened.
        self.orphans_reaped: dict = {"count": 0, "rss_bytes": 0, "pids": []}
        # job_id -> admitted-at, for every job that DECLARED `gpu: true` and was
        # let through. Reported by `ping` so the broker can answer the reverse
        # question — "may I start a render worker on this card right now?" —
        # without having to guess from VRAM totals. See `gpu_state`.
        self.gpu_jobs: dict[str, float] = {}
        self._count_lock = threading.Lock()
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.bundles, exist_ok=True)

    # --- lifecycle ------------------------------------------------------

    def reap_orphans(self) -> dict:
        """Kill the CHILDREN a previous exec server left running. Nothing else did.

        `sweep_stale` deletes the job *directories*. It never looked at the
        processes still using them, and an exec server that is restarted —
        `shutdown`, SIGKILL, a redeploy, the supervisor replacing it — does not
        take its children with it. `run_child` starts every child with
        `start_new_session=True` precisely so a timeout can kill a whole tree,
        and that same call is what makes the tree survive its parent: the child
        is its own session leader, so it is re-parented to init and keeps
        running with nobody waiting on it.

        Measured on the live box: **seven orphaned Blender processes holding
        ~35 GB** had to be reaped by hand. They are not merely wasteful. They
        are counted in `memory.current`, which is the number `memory_available`
        subtracts from `memory.max` — so the orphans are *invisible* to the
        memory gate as a cause while being fully visible to it as pressure. The
        gate then holds legitimate work at the door, forever, waiting for
        memory that nothing will ever release. A defect that silently converts
        the safety mechanism into the outage is worth more than the 35 GB.

        HOW AN ORPHAN IS IDENTIFIED, and why not by name. `pkill -f blender`
        would kill the render worker holding a multi-gigabyte warm scene — the
        exact process this file's OOM comment exists to protect, and the same
        kill-by-pattern class as the stale-worker bug already logged in the
        broker. The honest signal is the working directory: `run_child` runs
        every child with `cwd=<exec root>/<job_id>`, and no other process on
        the box has a cwd inside the exec root — checked, not assumed: both
        `remote.worker_launch_cmd` and `execremote.exec_launch_cmd` open with
        `cd {root}`, so the render worker and this server itself both sit at
        /workspace while the exec root is /workspace/exec. The render worker is
        therefore out of reach of this function by construction, not by
        carefulness. That test is unambiguous
        whether or not the directory still exists, because a deleted cwd still
        reads as its old path with " (deleted)" appended — which is why this
        runs BEFORE `sweep_stale` rather than after: intact paths first, and
        the deleted form only as the fallback it should be.

        A fresh server owns no children, so everything this finds is an orphan
        by construction — the same argument `sweep_stale` makes about
        directories, applied to the processes that were using them.
        """
        me = os.getpid()
        try:
            my_pgid = os.getpgid(0)
        except OSError:                                     # pragma: no cover
            my_pgid = None
        victims: list[tuple[int, Optional[int], int, str]] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == me:
                continue
            cwd = proc_cwd_in(pid, self.root)
            if cwd is None:
                continue
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = None
            # NEVER killpg our own group. If a child somehow shares it, signal
            # the pid alone: a group kill here would take out this server, and
            # a reaper whose failure mode is suicide is worse than the leak.
            if pgid is not None and my_pgid is not None and pgid == my_pgid:
                pgid = None
            victims.append((pid, pgid, proc_rss(pid), cwd))

        if not victims:
            return {"count": 0, "rss_bytes": 0, "pids": []}

        rss = sum(v[2] for v in victims)
        for pid, pgid, bytes_, cwd in victims:
            log(f"orphan from a previous exec server: pid {pid} "
                f"rss {bytes_ / 1e9:.2f}G cwd {cwd} — killing")
        for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            alive = [v for v in victims if proc_alive(v[0])]
            if not alive:
                break
            for pid, pgid, _, _ in alive:
                try:
                    if pgid is not None:
                        os.killpg(pgid, sig)
                    else:
                        os.kill(pid, sig)
                except OSError as exc:
                    if exc.errno != errno.ESRCH:
                        log(f"reaping {pid}: kill {sig} failed: {exc}")
            deadline = time.time() + grace
            while time.time() < deadline and any(proc_alive(v[0]) for v in alive):
                time.sleep(0.2)

        survived = [v[0] for v in victims if proc_alive(v[0])]
        if survived:
            # Say it rather than swallow it: unkillable orphans mean the memory
            # they hold is not coming back, and the gate below will refuse work
            # for a reason this line is the only record of.
            log(f"WARNING {len(survived)} orphan(s) survived SIGKILL: {survived} — "
                f"their memory stays charged to this cgroup and the memory gate "
                f"will keep holding jobs at the door because of it")
        return {"count": len(victims), "rss_bytes": rss,
                "pids": [v[0] for v in victims], "survived": survived}

    def sweep_stale(self) -> int:
        """Delete every job directory left by a previous exec server.

        A fresh process is by definition executing nothing, so every directory
        under the exec root is scratch from a run that no longer exists — the
        same reasoning the broker applies when it requeues every `running` row
        at startup. Leaving them costs disk on a 30 GB volume and, worse, lets a
        `release` for a recycled job id delete something live.
        """
        removed = 0
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name)
            if os.path.isdir(path):
                rmtree_quiet(path)
                removed += 1
        return removed

    def job_dir(self, job_id: str) -> str:
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError(f"unsafe job_id {job_id!r}")
        return os.path.join(self.root, job_id)

    # --- bundles --------------------------------------------------------

    def bundle_dir(self, digest: str) -> str:
        if not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"unsafe bundle digest {digest!r}")
        return os.path.join(self.bundles, digest)

    def bundle_ready(self, digest: str) -> bool:
        """Complete means the marker is there, and the marker is written LAST.

        Same rule as the scene cache, for the same reason: a push that dies
        halfway leaves a directory full of plausible Python with one module
        missing, and the resulting `ImportError` would be charged to the module
        rather than to the transfer.
        """
        return os.path.isfile(os.path.join(self.bundle_dir(digest), BUNDLE_COMPLETE))

    # --- the GPU ---------------------------------------------------------

    @property
    def workspace(self) -> str:
        """The instance root, /workspace — the PARENT of the exec root.

        Same derivation and same reason as `self.scenes`: `--root` is
        /workspace/exec, and both the render worker and its server.py live one
        level up. Joining onto self.root is the mistake that made a resident
        scene read as "not completely staged"; it would make a live render
        worker read as absent here, which is the more expensive direction.
        """
        return os.path.dirname(self.root)

    def gpu_state(self) -> dict:
        """Who is on the card, in three states. Never raises, never signals.

        `holder` is the answer this exists for and it has three values:

          * a dict          — somebody else is demonstrably on the card
          * None            — demonstrably nobody is
          * the "unknown" key is set when it COULD NOT BE DETERMINED, and then
            `holder` is None only if the render worker is also absent

        The render worker is the authoritative signal and it is read from /proc,
        so it is available even when `nvidia-smi` is not — which is the case
        that matters, because the container may not see other namespaces' pids
        while the card is very much occupied.
        """
        worker = render_worker(self.workspace)
        apps = gpu_compute_apps()
        mine = set()
        with self.child_lock:
            for child in self.children.values():
                if child.proc is not None and child.proc.pid:
                    mine.add(child.proc.pid)
        foreign = None
        if apps is not None:
            foreign = [a for a in apps
                       if a["pid"] not in mine and a["pid"] != os.getpid()]
        state: dict = {
            "cards": gpu_names(),
            "worker": worker,
            "compute_apps": apps,
            "foreign_apps": foreign,
            "unknown": apps is None,
            "gpu_jobs": sorted(self.gpu_jobs),
        }
        if worker is not None:
            state["holder"] = {"what": "render worker", **worker}
        elif foreign:
            biggest = max(foreign, key=lambda a: a["vram_mib"])
            state["holder"] = {"what": "another process on the card", **biggest}
        else:
            state["holder"] = None
        return state

    def refuse_if_gpu_contended(self, job_id: str) -> None:
        """Raise `GpuContended` if this job may not have the card. Named refusal.

        Called from `validate`, so it lands BEFORE the slot, before the memory
        gate and before anything is staged: a refused job costs the box a /proc
        walk and one `nvidia-smi`, not a slot and not an 8 GB scene push.

        Deliberately refuses when the holder is unknown-but-a-worker-exists, and
        deliberately does NOT refuse when nothing at all can be measured with no
        worker present — the clamp in `run_child` is what makes the second case
        safe, and refusing every GPU job on a box whose driver will not answer
        would make this gate worse than not having one, exactly as
        `await_memory` reasons about an unreadable memory.max.
        """
        state = self.gpu_state()
        holder = state.get("holder")
        if holder is None:
            return
        card = ", ".join(state["cards"]) or "the GPU"
        if holder["what"] == "render worker":
            raise GpuContended(
                f"refusing {job_id}: the render worker holds "
                f"{holder.get('scene') or 'a scene'} on {card} "
                f"(pid {holder['pid']}, {holder.get('rss_bytes', 0) / 1e9:.1f}G "
                f"resident). This job declared gpu: true, and a second scene on "
                f"the same card is not shared, it is a race for VRAM that has "
                f"no cgroup, no gate and no OOM score to bias — on 2026-08-07 "
                f"it killed another agent's render twice with 'Out of memory in "
                f"CUDA queue enqueue', the second time terminally. Resubmit "
                f"without gpu (exec jobs run CPU-only by default and this one "
                f"will be clamped to CPU anyway), or run it as a render job so "
                f"the fleet serialises it against the worker.")
        raise GpuContended(
            f"refusing {job_id}: pid {holder['pid']} "
            f"({holder.get('name') or 'unknown process'}) already holds "
            f"{holder.get('vram_mib', 0)} MiB on {card}. This job declared "
            f"gpu: true and the card is not free.")

    # --- job execution --------------------------------------------------

    def validate(self, spec: dict) -> dict:
        """Everything that can be decided before a slot is taken."""
        missing = EXEC_REQUIRED - spec.keys()
        if missing:
            raise ValueError(f"incomplete exec spec, missing: {sorted(missing)}")
        unknown = spec.keys() - EXEC_REQUIRED - EXEC_OPTIONAL - {"cmd"}
        if unknown:
            raise ValueError(
                f"unknown exec spec field(s): {sorted(unknown)} — this server holds "
                f"no job policy, so a field it does not understand is a client bug, "
                f"not something to ignore"
            )

        job_id = str(spec["job_id"])
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError(f"unsafe job_id {job_id!r}")

        digest = str(spec["bundle"])
        if not DIGEST_RE.fullmatch(digest):
            raise ValueError(f"unsafe bundle digest {digest!r}")
        if not self.bundle_ready(digest):
            raise ValueError(
                f"bundle {digest} is not staged on this instance (no {BUNDLE_COMPLETE} "
                f"marker at {self.bundle_dir(digest)}) — push it before submitting"
            )

        for name, value in (("argv", spec["argv"]),
                            ("blender_args", spec["blender_args"]),
                            ("outputs", spec["outputs"])):
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ValueError(f"{name} must be a list of strings")
        if not spec["outputs"]:
            raise ValueError(
                "outputs is empty — an exec job that returns nothing cannot be "
                "verified, and a job whose result nobody checks is how this project "
                "shipped a gate that measured nothing"
            )
        for banned in BANNED_BLENDER_ARGS:
            if any(tok == banned or tok.startswith(banned + "=")
                   for tok in spec["blender_args"]):
                raise ValueError(
                    f"blender_args may not contain {banned!r}: `entry` is the single "
                    f"sanctioned entry point so there is one place to containment-check"
                )

        try:
            timeout = int(spec["timeout_s"])
        except (TypeError, ValueError):
            raise ValueError(f"timeout_s must be an integer, got {spec['timeout_s']!r}") from None
        if not 1 <= timeout <= MAX_TIMEOUT_S:
            raise ValueError(
                f"timeout_s must be 1..{MAX_TIMEOUT_S}, got {timeout} — the "
                f"in-container watchdog retires this instance at 12 h regardless, so a "
                f"longer job would be killed by hardware rotation instead"
            )

        try:
            cpu_slots = int(spec["cpu_slots"])
        except (TypeError, ValueError):
            raise ValueError(f"cpu_slots must be an integer, got {spec['cpu_slots']!r}") from None
        if cpu_slots < 1:
            raise ValueError(f"cpu_slots must be >= 1, got {cpu_slots}")
        if cpu_slots > self.slots.total:
            raise ValueError(
                f"cpu_slots {cpu_slots} exceeds this server's {self.slots.total} "
                f"slot(s) — the job could never start"
            )

        # Optional, and validated here rather than in `stage` so a malformed
        # request is refused before any directory is built. Both halves or
        # neither: a digest with no name cannot be resolved to a file, and a
        # name with no digest is exactly the resolve-by-name trap.
        scene_digest = spec.get("scene_digest") or ""
        scene_name = spec.get("scene_name") or ""
        if bool(scene_digest) != bool(scene_name):
            raise ValueError(
                "scene_digest and scene_name must be given together: a digest "
                "with no name cannot be resolved, and a name with no digest "
                "would resolve by name, which is the thing this forbids")
        if scene_digest and not DIGEST_RE.fullmatch(scene_digest):
            raise ValueError(f"scene_digest {scene_digest!r} is not a digest")
        if scene_name and ("/" in scene_name or scene_name in (".", "..")):
            raise ValueError(
                f"scene_name {scene_name!r} must be a bare filename — it is "
                "resolved INSIDE the digest's own directory, never as a path")

        # THE CARD IS A DECLARED RESOURCE NOW, AND ITS DEFAULT IS "NO".
        #
        # This is the whole of the fix for the 2026-08-07 VRAM collision. It has
        # three parts and each one covers a hole the other two leave:
        #
        #   1. A job that says nothing gets NOTHING. `run_child` hands the child
        #      an empty CUDA_VISIBLE_DEVICES, so `cycles.device = 'GPU'` in a
        #      caller's script finds zero devices and Cycles renders on the CPU.
        #      This is the part that cannot be argued with, because it is not a
        #      predicate about the caller's code — the code never sees the card.
        #   2. A job that says `gpu: true` is CHECKED, here, against who is
        #      actually holding the card, and refused by name if anyone is.
        #   3. A job that says nothing but LOOKS like it wants the card is
        #      scanned for and reported — on the log line and on the reply — so
        #      the clamp in (1) is never a silent downgrade. The defect this
        #      file is fixing survived because nothing said anything; a clamp
        #      that also said nothing would have hidden it just as well.
        #
        # `gpu` is in EXEC_OPTIONAL rather than EXEC_REQUIRED because the "no
        # defaults in a warm process" law is about fields whose omission would
        # inherit the PREVIOUS job's value. This one cannot: absent means False,
        # every time, and False is enforced by the environment rather than
        # remembered.
        gpu = spec.get("gpu", False)
        if not isinstance(gpu, bool):
            raise ValueError(
                f"gpu must be a boolean, got {gpu!r} — it is a declaration that "
                f"this job intends to use the card, and a truthy string would "
                f"make the declaration accidental")
        if gpu:
            self.refuse_if_gpu_contended(job_id)
        gpu_hints: list[str] = []
        if not gpu:
            gpu_hints = scan_for_gpu(self.bundle_dir(digest))
            if gpu_hints:
                log(f"job {job_id}: {len(gpu_hints)} file(s) in bundle {digest} "
                    f"select a GPU device but the job did not declare gpu — the "
                    f"child gets CUDA_VISIBLE_DEVICES='' and will render on the "
                    f"CPU: {', '.join(gpu_hints[:6])}")

        return {"job_id": job_id, "digest": digest, "timeout": timeout,
                "cpu_slots": cpu_slots, "entry": str(spec["entry"]),
                "argv": list(spec["argv"]),
                "blender_args": list(spec["blender_args"]),
                "outputs": list(spec["outputs"]),
                "scene_digest": scene_digest, "scene_name": scene_name,
                "gpu": gpu, "gpu_hints": gpu_hints}

    def stage(self, plan: dict) -> dict:
        """Build the job directory and resolve every path inside it."""
        job = self.job_dir(plan["job_id"])
        # A job id is minted per job, so an existing directory means a retry of
        # the same id. Start clean rather than run against half of a previous
        # attempt's output — a stale file that satisfies an `outputs` entry
        # would be fetched and filed as this run's result.
        rmtree_quiet(job)
        bundle = os.path.join(job, "bundle")
        out = os.path.join(job, "out")
        tmp = os.path.join(job, "tmp")
        shutil.copytree(self.bundle_dir(plan["digest"]), bundle, symlinks=False)
        os.makedirs(out, exist_ok=True)
        os.makedirs(tmp, exist_ok=True)

        # OPTIONAL INPUT SCENE, RESOLVED BY CONTENT.
        #
        # Exec was "code in, blend born on the box", and every build that was
        # ever too big to run locally is the other shape: it OPENS an existing
        # assembly. The blend is already here — content-addressed in the scene
        # cache the render path fills — so this links it into the job directory
        # instead of moving 8 GB across a wire that cannot carry it.
        #
        # It is a LINK, not a copy: an 8 GB copy per job would exhaust the disk
        # and take longer than the build. Hard link first so the child sees a
        # plain file, falling back to a symlink across filesystems.
        #
        # BY DIGEST, NEVER BY NAME. The name is used only to find the file
        # INSIDE the digest's own directory; the digest is the identity. Asking
        # for a scene by name and getting whichever blend currently answers to
        # it is precisely the trap that cost the 0.1449 m travel guard, where
        # two `breach_film.npz` files with the same name held different tables.
        # A digest that is not present is an error, never a near-match.
        if plan.get("scene_digest"):
            digest = plan["scene_digest"]
            if not DIGEST_RE.fullmatch(digest):
                raise ValueError(f"scene_digest {digest!r} is not a digest")
            # The name selects a file INSIDE the digest's directory and nothing
            # else. It arrives from the broker, but the broker got it from a
            # caller, and it is about to become a path — the same reason job ids
            # are broker-minted. `basename` is not enough on its own because
            # `..` survives it, so both are checked.
            sname = plan["scene_name"]
            if (not sname or os.path.basename(sname) != sname
                    or sname in (".", "..")):
                raise ValueError(f"scene_name {sname!r} is not a plain filename")
            src = os.path.join(self.scenes, digest, sname)
            marker = os.path.join(self.scenes, digest, SCENE_COMPLETE)
            if not os.path.isfile(marker):
                raise ValueError(
                    f"scene {digest} is not completely staged on this instance "
                    f"(no {SCENE_COMPLETE} marker) — refusing to open a "
                    f"half-pushed blend, which reads as valid and renders as "
                    f"wrong. Looked in {os.path.join(self.scenes, digest)}")
            if not os.path.isfile(src):
                raise ValueError(
                    f"scene {digest} is staged but holds no {plan['scene_name']!r}")
            dst = os.path.join(job, "scene.blend")
            try:
                os.link(src, dst)
            except OSError:
                os.symlink(src, dst)

        entry = contained(plan["entry"], bundle)
        if not os.path.isfile(entry):
            raise ValueError(
                f"entry {plan['entry']!r} does not exist in bundle {plan['digest']}"
            )
        outputs = [contained(name, out) for name in plan["outputs"]]

        # Checked against the JOB root, not against out/: a build legitimately
        # reads from bundle/ and writes to out/, and both are inside the job.
        check_tokens(plan["argv"], job, "argv")
        check_tokens(plan["blender_args"], job, "blender_args")

        return {"job": job, "bundle": bundle, "out": out, "tmp": tmp,
                "entry": entry, "outputs": outputs,
                "log": os.path.join(job, "job.log")}

    def run_child(self, plan: dict, paths: dict,
                  child: Optional[_Child] = None) -> dict:
        """Spawn, wait, and hard-kill the whole process group on timeout or cancel."""
        cmd = [self.blender, *plan["blender_args"], "-P", paths["entry"]]
        if plan["argv"]:
            cmd += ["--", *plan["argv"]]

        env = dict(os.environ)
        # Everything Blender or a careless module might write to `~`, TMPDIR or
        # the CWD lands inside the job directory instead of on the shared disk.
        env["TMPDIR"] = paths["tmp"]
        env["HOME"] = paths["tmp"]
        env["BLENDER_USER_RESOURCES"] = paths["tmp"]
        env["OPTIX_CACHE_PATH"] = os.path.join(self.root, ".optixcache")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # These children are CPU-bound Python plus numpy. Letting numpy's BLAS
        # open 96 threads inside each of twelve concurrent jobs is 1,152 threads
        # fighting over a 23-CPU cgroup quota.
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            env[var] = "1"

        # THE CPU-ONLY ASSUMPTION, ENFORCED RATHER THAN ASSUMED.
        #
        # Everything about this server's sizing is a CPU-and-RAM argument, and
        # `deprioritise_for_oom` twenty lines below exists so that an exec child
        # loses a memory squeeze the render worker would otherwise lose. None of
        # that reaches VRAM: the card has no cgroup, so the only way an exec
        # child cannot take memory from the resident render scene is for the
        # child not to be able to SEE the card.
        #
        # A guard in the spec cannot do this. `entry` is caller-supplied Python
        # — that is the entire point of the job type — so any predicate about
        # what the script "intends" is a guess about a file that can compute its
        # intent at runtime. An empty CUDA_VISIBLE_DEVICES is not a guess.
        # Cycles enumerates zero devices, `scene.cycles.device = 'GPU'` still
        # assigns cleanly, and the render falls to the CPU, which is what every
        # exec job on this box was already assumed to be doing.
        #
        # HIP too, because the clamp should not depend on which vendor's card
        # the fleet happened to rent this week.
        if not plan.get("gpu"):
            env["CUDA_VISIBLE_DEVICES"] = ""
            env["HIP_VISIBLE_DEVICES"] = ""
        else:
            # Declared, checked against the holder in `validate`, and recorded
            # so `ping` can answer the reverse question: the broker must not
            # start a render worker on a card an exec job is holding either.
            with self.child_lock:
                self.gpu_jobs[plan["job_id"]] = time.time()
            log(f"job {plan['job_id']} runs WITH THE GPU — declared gpu: true "
                f"and the card was free at admission")

        def deprioritise_for_oom() -> None:
            """Make this child the cgroup OOM killer's first choice.

            It picks by RSS, and the largest RSS on this instance is the render
            worker holding a multi-gigabyte scene resident. Without this, an
            exec batch that over-commits memory kills somebody else's 4K render
            and the broker gets to diagnose a "dead worker" that was nothing of
            the kind. Best effort: the file is absent on some kernels and a
            failure here must not stop the job.
            """
            try:
                with open("/proc/self/oom_score_adj", "w") as fh:
                    fh.write("800")
            except OSError:
                pass

        started = time.time()
        with open(paths["log"], "wb") as logfh:
            logfh.write((" ".join(cmd) + "\n").encode())
            logfh.flush()
            # FORK AND REGISTER UNDER ONE LOCK. Without it there is a window in
            # which the child exists and `child.proc` is still None, and a
            # cancel landing in that window would be told "admitted, not yet
            # started" about a process that had in fact just been started —
            # exactly the orphan this whole change exists to prevent, with a
            # reply claiming success. `Popen` here is a fork/exec of a small
            # supervisor, not of the 8 GB child, so the lock is held for
            # milliseconds.
            with self.child_lock:
                if child is not None and child.canceled:
                    raise Canceled(
                        f"{plan['job_id']} was canceled before its child was "
                        f"started — nothing was spawned")
                proc = subprocess.Popen(
                    cmd, cwd=paths["job"], env=env, preexec_fn=deprioritise_for_oom,
                    stdin=subprocess.DEVNULL, stdout=logfh, stderr=subprocess.STDOUT,
                    # Its own process group, so a timeout OR A CANCEL can kill
                    # the child AND anything it spawned. Blender forks helpers;
                    # signalling only the pid leaves them holding memory and CPU
                    # on a box where the whole point is that twelve of these run
                    # at once.
                    start_new_session=True,
                )
                if child is not None:
                    child.proc = proc
            timed_out = False
            try:
                rc = proc.wait(timeout=plan["timeout"])
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_group(proc)
                rc = proc.wait()
        return {"rc": rc, "timed_out": timed_out,
                "canceled": bool(child is not None and child.canceled),
                "elapsed": time.time() - started}

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        # NEVER killpg our own group — the same rule `reap_orphans` states, and
        # for the same reason: a signal to this server's own group takes out the
        # exec server and every other job's child with it. `start_new_session`
        # makes that impossible by construction; this is the assertion of it,
        # because "impossible by construction" is what the render worker was
        # also said to be before `pkill -f` found it.
        try:
            if pgid is not None and pgid == os.getpgid(0):
                pgid = None
        except OSError:                                     # pragma: no cover
            pass
        for sig, grace in ((signal.SIGTERM, 10.0), (signal.SIGKILL, 5.0)):
            if proc.poll() is not None:
                return
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    proc.send_signal(sig)
            except OSError as exc:
                if exc.errno != errno.ESRCH:
                    log(f"kill {sig} failed: {exc}")
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def scrub(paths: dict) -> int:
        """Delete everything in the job directory except out/ and job.log.

        Immediately, not at release time. Twelve concurrent builds each saving a
        multi-gigabyte .blend into a 30 GB volume is the sizing constraint here,
        and the bundle copy plus the child's TMPDIR plus whatever it dropped in
        its CWD are all dead the moment it exits.
        """
        freed = 0
        keep = {"out", "job.log"}
        for name in os.listdir(paths["job"]):
            if name in keep:
                continue
            target = os.path.join(paths["job"], name)
            try:
                if os.path.isdir(target) and not os.path.islink(target):
                    for base, _dirs, files in os.walk(target):
                        for f in files:
                            with_ = os.path.join(base, f)
                            try:
                                freed += os.lstat(with_).st_size
                            except OSError:
                                pass
                    rmtree_quiet(target)
                else:
                    freed += os.lstat(target).st_size
                    os.unlink(target)
            except OSError:
                pass
        return freed

    def handle_exec(self, spec: dict) -> dict:
        plan = self.validate(spec)
        job_id = plan["job_id"]

        # BEFORE THE SLOT, THE DISK CHECK AND THE MEMORY GATE. A job whose
        # cancel overtook it must not consume any of the three, and above all
        # must not fork. See `self.canceled_ids`.
        with self.child_lock:
            self._prune_cancels()
            if job_id in self.canceled_ids:
                self.canceled_ids.pop(job_id, None)
                log(f"job {job_id} REFUSED on arrival: it was canceled while in "
                    f"flight to this server")
                return {"ok": False, "canceled": True, "job_id": job_id,
                        "rc": None, "timed_out": False, "outputs": [],
                        "missing": [], "exec_sec": 0.0, "log": "",
                        "error": f"{job_id} was canceled before it reached this "
                                 f"exec server — no slot was taken and no child "
                                 f"was started"}

        free = shutil.disk_usage(self.root).free
        if free < self.min_free:
            raise RuntimeError(
                f"refusing to start {job_id}: {free / 1e9:.2f}G free on {self.root}, "
                f"below the {self.min_free / 1e9:.2f}G floor. Blender does not fail "
                f"cleanly on ENOSPC — it writes a short file that passes a size check "
                f"against itself"
            )

        wait_budget = min(SLOT_WAIT_MAX_S, plan["timeout"])
        if not self.slots.acquire(job_id, plan["cpu_slots"], wait_budget):
            raise RuntimeError(
                f"no slot for {job_id} within {wait_budget}s "
                f"({plan['cpu_slots']} of {self.slots.total} wanted, "
                f"{self.slots.snapshot()['free']} free) — the broker dispatches at "
                f"most `slots` jobs at once, so this means the two disagree about "
                f"how many that is"
            )
        # REGISTERED BEFORE ANY WAIT, NOT BEFORE THE FORK. Admission is where a
        # job starts costing the box something a cancel would want back: the
        # slot is taken from this line on, and `await_memory` can hold here for
        # ten minutes. A registry that only covered running children would leave
        # `cancel` answering "not running" about a job that is demonstrably
        # occupying capacity.
        child = _Child()
        with self.child_lock:
            # Re-read the tombstone under the SAME lock the registration takes.
            # The check above ran before `slots.acquire`, which blocks: a cancel
            # arriving during that wait would find no child to signal, plant a
            # tombstone, and be overtaken by this job. Consuming it here means
            # there is no instant at which a cancel can be lost — before this
            # line it is a tombstone, after it the child object carries the flag.
            if self.canceled_ids.pop(job_id, None) is not None:
                child.canceled = True
            self.children[job_id] = child
        try:
            waited = self.await_memory(job_id, wait_budget, child)
            paths = self.stage(plan)
            log(f"job {job_id} start entry={plan['entry']} slots={plan['cpu_slots']} "
                f"timeout={plan['timeout']}s"
                + (f" (waited {waited:.0f}s for memory)" if waited else ""))
            result = self.run_child(plan, paths, child)
            freed = self.scrub(paths)

            produced = []
            missing = []
            for declared, path in zip(plan["outputs"], paths["outputs"]):
                if not os.path.isfile(path):
                    missing.append(declared)
                    continue
                size, digest = sha256_of(path)
                produced.append({"name": declared, "bytes": size, "sha256": digest,
                                 "path": path})

            canceled = bool(result.get("canceled"))
            ok = (result["rc"] == 0 and not result["timed_out"]
                  and not canceled and not missing)
            with self._count_lock:
                self.jobs += 1
                # A cancel is NOT a failure of this server or of the caller's
                # code, and counting it as one would make `ping`'s failure count
                # — the number a human reads to decide whether the box is sick —
                # go up every time somebody changed their mind.
                if not ok and not canceled:
                    self.failures += 1

            reply = {
                "ok": ok,
                "job_id": job_id,
                "rc": result["rc"],
                "timed_out": result["timed_out"],
                "canceled": canceled,
                "exec_sec": round(result["elapsed"], 3),
                "outputs": produced,
                "missing": missing,
                "scrubbed_bytes": freed,
                "out_dir": paths["out"],
                "log": tail_file(paths["log"]),
                # SO THE CLAMP IS NEVER SILENT. `gpu` is what the job asked
                # for; `gpu_clamped` is what it got; `gpu_hints` names the files
                # that made the difference visible. A downgrade nobody can see
                # in `rq status -v` is a downgrade that hides its own cause,
                # which is how the collision this guard exists for went
                # unnoticed until it killed somebody else's render.
                "gpu": bool(plan.get("gpu")),
                "gpu_clamped": not plan.get("gpu"),
                "gpu_hints": list(plan.get("gpu_hints") or []),
            }
            if not ok:
                if canceled:
                    reply["error"] = (
                        f"canceled by request after {result['elapsed']:.1f}s — the "
                        f"child's process group was signalled and is gone")
                elif result["timed_out"]:
                    reply["error"] = (f"killed after {plan['timeout']}s "
                                      f"(timeout_s), process group signalled")
                elif result["rc"] != 0:
                    reply["error"] = f"child exited {result['rc']}"
                else:
                    reply["error"] = (f"declared output(s) not produced: {missing} — "
                                      f"the child exited 0 without writing them")
            log(f"job {job_id} {'CANCELED' if canceled else 'ok' if ok else 'FAILED'} "
                f"rc={result['rc']} {result['elapsed']:.1f}s "
                f"outputs={len(produced)}/{len(plan['outputs'])}")
            return reply
        except Canceled as exc:
            # Cancelled between admission and the fork, so there is no child, no
            # rc and no log — but there IS a reply, and it carries the same
            # marker as the other branch. The broker must not have to tell the
            # two apart to know that nothing is still running out there.
            with self._count_lock:
                self.jobs += 1
            log(f"job {job_id} CANCELED before its child was started")
            return {"ok": False, "canceled": True, "job_id": job_id,
                    "rc": None, "timed_out": False, "outputs": [], "missing": [],
                    "exec_sec": round(time.time() - child.started, 3),
                    "error": str(exc), "log": ""}
        finally:
            # BOTH, ALWAYS, AND IN THIS ORDER. The slot release is what makes a
            # cancel worth doing at all — a kill that leaks its slots just moves
            # the bug, and the orphan on 47040457 held 6 of 12 for an hour. It
            # is in a `finally` rather than on each path precisely so that a
            # kill, a timeout, a crash in `scrub` and a clean exit all release
            # exactly once. The registry entry goes first so nothing can be
            # signalled after the process it names is already reaped.
            with self.child_lock:
                self.children.pop(job_id, None)
                # Same `finally`, same reason: a GPU claim that outlives its
                # child would make `ping` report the card as busy forever and
                # the reverse guard would refuse every render after it.
                self.gpu_jobs.pop(job_id, None)
            self.slots.release(job_id, plan["cpu_slots"])

    def await_memory(self, job_id: str, timeout: float,
                     child: Optional[_Child] = None) -> float:
        """Hold a job at the door until the container can afford to start it.

        A slot is a CPU promise, not a memory one, and on this box those two
        run out at very different points: 23 CPUs of quota against 90 GiB of
        memory, with item builds measured well past 7 GiB of peak RSS each. So
        admission is gated on both, and the memory half is a WAIT rather than a
        rejection — the work is fine, the moment is not.

        Only one job may pass this gate at a time, and it holds the lock for a
        beat afterwards, because the child's allocation is not instantaneous:
        without that pause twelve threads read the same comfortable number
        before any of them has grown.
        """
        # THE CANCEL CHECK IS OUTSIDE THE `min_free_mem` SHORT-CIRCUIT. With the
        # gate disabled this method used to return before reading anything, so a
        # job cancelled between admission and here would have gone on to fork.
        if child is not None and child.canceled:
            raise Canceled(f"{job_id} was canceled while waiting to be admitted")
        if self.min_free_mem <= 0:
            return 0.0
        deadline = time.time() + timeout
        began = time.time()
        warned = False
        with self.admit_lock:
            while True:
                if child is not None and child.canceled:
                    raise Canceled(
                        f"{job_id} was canceled after {time.time() - began:.0f}s "
                        f"waiting for memory — no child was ever started")
                available = memory_available()
                if available is None:
                    # Unmeasurable. Proceed — refusing every job on a kernel
                    # that does not publish cgroup memory would make the gate
                    # worse than not having one — but say so once.
                    if not warned:
                        log(f"job {job_id}: memory could not be measured "
                            f"({MEM_MAX} unreadable); admitting without the gate")
                    break
                if available >= self.min_free_mem:
                    break
                if time.time() > deadline:
                    # ResourceWait, not RuntimeError. See the class: this is
                    # raised BEFORE `stage` and `run_child`, so there is not
                    # even a child process yet — nothing about the caller's code
                    # has been tried, let alone found wanting.
                    raise ResourceWait(
                        f"{job_id} waited {time.time() - began:.0f}s for "
                        f"{self.min_free_mem / 1e9:.1f}G of free memory and only "
                        f"{available / 1e9:.1f}G was ever available — the container "
                        f"cap is {MEM_MAX}, not the host's `free`. The build was "
                        f"never started, so this is a WAIT: the render worker and "
                        f"the sibling builds have to give the memory back first"
                    )
                if not warned:
                    warned = True
                    log(f"job {job_id} waiting for memory: {available / 1e9:.1f}G "
                        f"available, floor {self.min_free_mem / 1e9:.1f}G")
                # WAIT ON THE EVENT, NOT ON THE CLOCK. This gate holds
                # `admit_lock`, so a job sleeping out a five-second poll here is
                # also holding every sibling's admission behind it; a cancel
                # that took five seconds to be noticed would be five seconds of
                # the whole queue stopped for a job nobody wants any more.
                if child is not None:
                    child.event.wait(5.0)
                else:
                    time.sleep(5.0)
            # Let the admitted child actually grow before the next one reads.
            time.sleep(1.0)
        return time.time() - began

    # How long a cancel for a job that has not arrived yet is remembered. The
    # window it has to cover is the broker's dispatch-to-admission gap: a bundle
    # push plus, at worst, an 8 GB scene push. An hour is far more than that and
    # still bounded, which is the point — an unbounded set of ids on a server
    # that never restarts is a leak, however small each entry is.
    CANCEL_MEMORY_S = 3600.0

    def _prune_cancels(self) -> None:
        """Called under `child_lock` only."""
        cutoff = time.time() - self.CANCEL_MEMORY_S
        for job_id in [k for k, t in self.canceled_ids.items() if t < cutoff]:
            self.canceled_ids.pop(job_id, None)

    def handle_cancel(self, spec: dict) -> dict:
        """Stop ONE job, by job id, by killing its own process group.

        THE MISSING HALF OF `rq cancel`. The broker's cancel path updated a
        SQLite row and stopped; there was no request that reached the process.
        Observed on instance 47040457 on 2026-08-07: `{"canceled": true}` at
        03:46 and a Blender child still running at 04:44, holding 6 of 12 slots
        and ~8 GB while two of another agent's jobs were refused by the memory
        gate and a third was OOM-killed at `Read blend`.

        BY JOB ID, NEVER BY NAME. `self.children` is populated only by
        `handle_exec`, keyed by the id the broker minted, and holds the actual
        `Popen`. The process group comes from `os.getpgid` on that object's own
        pid — not from a pattern, not from `pgrep`, not from anything a request
        could steer. A job id this server does not know signals nothing at all
        and says so. That is strictly narrower than `reap_orphans`'s cwd test,
        which is already the standard here and which cannot be used for this
        anyway: every live exec child has a cwd inside the exec root, so it
        identifies the whole population rather than one member.

        IDEMPOTENT, AND SILENCE IS NOT AN ERROR. A job that already finished, or
        that was cancelled while still queued in the broker and so was never
        dispatched, is `canceled: false, running: false` and `ok: true` — the
        broker cancels the row either way, and turning "there was nothing to
        kill" into a failure would make the common case look broken.
        """
        job_id = str(spec.get("job_id", ""))
        if not JOB_ID_RE.fullmatch(job_id):
            raise ValueError(f"unsafe job_id {job_id!r}")

        with self.child_lock:
            self._prune_cancels()
            child = self.children.get(job_id)
            if child is None:
                # A TOMBSTONE, NOT A SHRUG. Job ids are broker-minted and never
                # reused, so remembering one costs a dict entry and closes the
                # window described at `self.canceled_ids`. It is equally correct
                # for a job that already finished: nothing will ever present
                # that id again, and `_prune_cancels` collects it.
                self.canceled_ids[job_id] = time.time()
                return {"ok": True, "job_id": job_id, "canceled": True,
                        "running": False, "pid": None, "pgid": None,
                        "detail": "no job of that id is admitted here — nothing "
                                  "was signalled, and the id is recorded so that "
                                  "a job still in flight to this server is "
                                  "refused on arrival rather than started"}
            child.canceled = True
            child.event.set()
            proc = child.proc

        if proc is None:
            # Admitted, holding a slot, waiting for memory — and now flagged.
            # `await_memory` raises `Canceled` on the next pass, which the event
            # above has already woken, and `handle_exec`'s `finally` gives the
            # slot back. Nothing was forked, so there is nothing to kill.
            log(f"job {job_id} cancel: admitted but not yet forked — flagged, "
                f"it will not start")
            return {"ok": True, "job_id": job_id, "canceled": True,
                    "running": False, "pid": None, "pgid": None,
                    "detail": "the job was waiting to be admitted and will not "
                              "start; its slot is released by the job thread"}

        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        log(f"job {job_id} cancel: signalling process group {pgid} (pid {proc.pid})")
        # OUTSIDE `child_lock`. This blocks for up to fifteen seconds between
        # SIGTERM and SIGKILL, and the job's own thread needs that lock to
        # deregister itself when the child dies.
        self._kill_group(proc)
        alive = proc.poll() is None
        if alive:
            log(f"WARNING job {job_id} survived SIGKILL — pid {proc.pid} is still "
                f"alive and its memory stays charged to this cgroup")
        else:
            log(f"job {job_id} cancel: child is gone (rc {proc.returncode})")
        return {"ok": True, "job_id": job_id, "canceled": True, "running": True,
                "pid": proc.pid, "pgid": pgid, "killed": not alive,
                "rc": proc.returncode,
                "detail": ("the child's process group was signalled and is gone"
                           if not alive else
                           "the child SURVIVED SIGKILL — its slots and memory "
                           "are still held")}

    def handle_release(self, spec: dict) -> dict:
        job_id = str(spec.get("job_id", ""))
        path = self.job_dir(job_id)
        if job_id in self.slots.snapshot()["inflight"]:
            raise RuntimeError(f"refusing to release {job_id}: it is still running")
        existed = os.path.isdir(path)
        rmtree_quiet(path)
        return {"ok": True, "job_id": job_id, "released": existed}

    def handle_purge(self, spec: dict) -> dict:
        """Sweep job directories older than `older_than_s`, never in-flight ones."""
        try:
            age = float(spec["older_than_s"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("purge requires older_than_s (seconds), an int or float") from None
        cutoff = time.time() - age
        inflight = set(self.slots.snapshot()["inflight"])
        removed = []
        for name in sorted(os.listdir(self.root)):
            path = os.path.join(self.root, name)
            if not os.path.isdir(path) or name in inflight or name.startswith("."):
                continue
            try:
                if os.stat(path).st_mtime < cutoff:
                    rmtree_quiet(path)
                    removed.append(name)
            except OSError:
                pass
        return {"ok": True, "removed": removed}

    def handle_bundles(self, _spec: dict) -> dict:
        staged = []
        for name in sorted(os.listdir(self.bundles)):
            path = os.path.join(self.bundles, name)
            if os.path.isdir(path):
                staged.append({"digest": name, "complete": self.bundle_ready(name)})
        return {"ok": True, "bundles": staged}

    def ping(self) -> dict:
        usage = shutil.disk_usage(self.root)
        return {
            "ok": True,
            "started_at": self.started,
            "uptime_sec": round(time.time() - self.started, 1),
            "jobs": self.jobs,
            "failures": self.failures,
            "slots": self.slots.snapshot(),
            "disk": {"total": usage.total, "free": usage.free},
            "mem_available": memory_available(),
            "mem_floor": self.min_free_mem,
            "orphans_reaped": self.orphans_reaped,
            "blender": self.blender,
            "root": self.root,
            # DEPLOYED-VS-TREE, ANSWERED BY THE BOX. This server and
            # broker/execservice.py deploy separately, and every disagreement
            # between them so far has been diagnosed by argument rather than by
            # comparison. The broker can hash the file it would push and see in
            # one line whether that is what is running.
            "code_sha256": self_digest(),
            "code_mtime": self_mtime(),
            # THE REVERSE DIRECTION OF THE GPU GUARD, made answerable.
            #
            # `refuse_if_gpu_contended` stops an exec job from climbing onto a
            # card the render worker holds. The other order is just as real —
            # the broker relaunching the render worker while an exec job that
            # declared `gpu: true` is mid-render — and the broker cannot see
            # inside this server to know. Now it can: `gpu_jobs` is the list of
            # admitted GPU jobs and `holder` is who is on the card, both read at
            # the moment of the ping. `remote.ensure_ready` already pings this
            # server before it will touch the worker, so the fact is available
            # exactly where the decision is made.
            "gpu": self.gpu_state(),
        }

    # --- protocol -------------------------------------------------------

    def serve_conn(self, conn: socket.socket) -> None:
        """One request, one reply. Contained exactly like server.py's loop: a
        client that stalls, vanishes mid-build or gives up before the reply must
        cost that connection and nothing else."""
        try:
            # Generous, because the reply does not come until the child is done
            # and a build legitimately runs for tens of minutes. The read below
            # is of the request only, so this is the idle-client bound.
            conn.settimeout(MAX_TIMEOUT_S + SLOT_WAIT_MAX_S + 300)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_REQUEST_BYTES:
                    raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
            if not buf.strip():
                return
            try:
                spec = json.loads(buf)
            except json.JSONDecodeError as exc:
                conn.sendall(json.dumps({"ok": False, "error": f"bad json: {exc}"}).encode() + b"\n")
                return
            if not isinstance(spec, dict):
                conn.sendall(json.dumps({"ok": False, "error": "request must be a JSON object"}).encode() + b"\n")
                return

            cmd = spec.get("cmd")
            try:
                if cmd == "ping":
                    reply = self.ping()
                elif cmd == "cancel":
                    reply = self.handle_cancel(spec)
                elif cmd == "release":
                    reply = self.handle_release(spec)
                elif cmd == "purge":
                    reply = self.handle_purge(spec)
                elif cmd == "bundles":
                    reply = self.handle_bundles(spec)
                elif cmd == "shutdown":
                    conn.sendall(json.dumps({"ok": True, "bye": True}).encode() + b"\n")
                    log("shutdown requested")
                    os._exit(0)
                elif cmd is None:
                    reply = self.handle_exec(spec)
                else:
                    reply = {"ok": False, "error": f"unknown cmd {cmd!r}"}
            except Exception as exc:
                traceback.print_exc()
                reply = {"ok": False, "job_id": spec.get("job_id"),
                         "error": f"{type(exc).__name__}: {exc}"}
                # THE ONE BIT THAT MAKES A WAIT SURVIVE SERIALISATION. Without
                # it every refusal looks identical to a broken script on the
                # broker's side, and the broker's only reasonable reading of
                # "your job did not run" is to spend an attempt on it.
                if isinstance(exc, ResourceWait):
                    reply["wait"] = True
                # AND THE ONE BIT THAT MAKES A REFUSAL SURVIVE IT. Same
                # argument, opposite verdict: `wait` means "come back", this
                # means "do not come back, and do not spend two more attempts
                # rediscovering that the render worker is still warm". The
                # broker fails it terminally on sight, as it does a
                # SceneStagingMismatch.
                if isinstance(exc, GpuContended):
                    reply["gpu_refused"] = True
            conn.sendall(json.dumps(reply).encode() + b"\n")
        except Exception as exc:
            log(f"connection dropped: {type(exc).__name__}: {exc}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def serve(self, port: int, host: str = "127.0.0.1") -> None:
        # PROCESSES BEFORE DIRECTORIES. Both are stale for the same reason, but
        # the reap identifies its victims by a cwd inside the exec root, and
        # sweeping first turns every one of those into the "(deleted)" form.
        # It still works — proc_cwd_in handles it — but the intact path is the
        # stronger evidence, so take it while it is there.
        orphans = self.reap_orphans()
        self.orphans_reaped = orphans
        if orphans["count"]:
            log(f"reaped {orphans['count']} orphaned child process(es) holding "
                f"{orphans['rss_bytes'] / 1e9:.2f}G from a previous exec server — "
                f"that memory was charged to this cgroup and the memory gate was "
                f"refusing work because of it")
        swept = self.sweep_stale()
        if swept:
            log(f"swept {swept} stale job director(ies) — a fresh exec server is by "
                f"definition executing nothing")
        # WHAT THIS PROCESS IS, IN ITS OWN LOG, BEFORE IT SERVES ANYTHING.
        #
        # This file and `broker/execservice.py` deploy separately and they are
        # each other's most likely source of disagreement — `SceneStagingMismatch`
        # names "a stale worker/exec_server.py on the box" as its first
        # hypothesis, and there was never a way to check it without an SSH
        # session and a guess. The digest below is the check: `rq status -v`
        # already shows this server's ping, and the broker knows the hash of the
        # file it WOULD have pushed, so "the box is running something else" is
        # now one comparison rather than an argument.
        log(f"this exec_server.py is sha256:{self_digest()} "
            f"mtime={time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self_mtime()))} "
            f"pid={os.getpid()} python={sys.version.split()[0]}")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(64)
        log(f"ready on {host}:{port} — {self.slots.total} slot(s), blender={self.blender}, "
            f"root={self.root}, bundles={self.bundles}")
        while True:
            try:
                conn, _ = srv.accept()
            except OSError as exc:
                log(f"accept failed: {exc}")
                continue
            threading.Thread(target=self.serve_conn, args=(conn,),
                             daemon=True).start()


def cli_argv(argv: list[str]) -> list[str]:
    """Our arguments, whether Blender is hosting us or CPython is.

    Under `blender ... -P exec_server.py -- --port 8800` the whole Blender
    command line is in sys.argv and only what follows `--` is ours. Run directly
    as `python3 exec_server.py --port 8899` — which is how the test suite
    exercises it, and the only way to test it without renting a GPU — there is
    no `--` and argv[0] is this file. Getting this wrong is not a crash: argparse
    simply sees no arguments and every default applies, so the server comes up
    on the wrong port with the wrong slot count and says so in its banner while
    the caller waits on a socket nothing is listening to.
    """
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    if argv and os.path.basename(argv[0]) == os.path.basename(__file__):
        return argv[1:]
    return []


def main() -> None:
    argv = cli_argv(sys.argv)
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8800)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--root", default="/workspace/exec")
    p.add_argument("--bundles", default="/workspace/bundles")
    p.add_argument("--slots", type=int, default=12)
    p.add_argument("--blender", default="/workspace/blender/blender")
    p.add_argument("--min-free-gb", type=float, default=3.0,
                   help="refuse to start a job below this much free disk; "
                        "Blender writes a short file rather than failing on ENOSPC")
    p.add_argument("--min-free-mem-gb", type=float, default=0.0,
                   help="hold a job at the door until the CONTAINER (not the "
                        "host) has this much memory available; 0 disables the "
                        "gate. Measured from cgroup memory.max/current, because "
                        "/proc/meminfo in a container is the host's")
    args = p.parse_args(argv)
    server = ExecServer(args.root, args.bundles, args.slots, args.blender,
                        args.min_free_gb, args.min_free_mem_gb)
    server.serve(args.port, args.host)


if __name__ == "__main__":
    main()
