#!/usr/bin/env python3
"""Remote side of the EXEC job type: input bundles, the exec server, its tunnel.

Deliberately a separate module from `remote.py` rather than 300 more lines in
it. Everything here is additive — it imports the SSH primitives, the error
types, the disk preflight and the transfer verification that `remote.py`
already got right, and adds nothing to the render path that a render job could
trip over.

THE THING BEING SHIPPED IS CODE, NOT BLENDS
===========================================
An item build's whole input is Python: `world/*.py`, `world/items/*.py`,
`tools/*.py` and one manifest — measured at 7.1 MB raw, 1.5 MB through
`zstd -19`, under two seconds of a 4-5 MB/s uplink, and cached across every job
in a wave. No `.blend` ever goes up. The output blend is *born on the instance*,
which is the entire point: the 553 broker jobs measured against item scenes spent
40,737 s of wall clock to do 7,687 s of rendering, and the 81 % that was not
rendering was overwhelmingly pushing assembled blends up a line that cannot go
faster.

Bundles are content-addressed exactly like scenes, with the `.complete` marker
written LAST, for the same reason: a push that dies halfway leaves a directory
full of plausible Python with one module missing, and the resulting ImportError
would be charged to the module rather than to the transfer.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from . import config, remote
from .remote import (Endpoint, RemoteError, TransferError, probe, run, ssh_base,
                     tail)

BUNDLE_COMPLETE = ".complete"

# Where the exec server listens ON THE INSTANCE, and where its pieces live.
EXEC_PORT = int(os.environ.get("VASTRENDER_EXEC_PORT", "8800"))
EXEC_LOCAL_PORT = int(os.environ.get("VASTRENDER_EXEC_LOCAL_PORT", "8797"))
EXEC_SLOTS = int(os.environ.get("VASTRENDER_EXEC_SLOTS", "12"))
# Free disk the exec server refuses to start a job below. Separate from
# DISK_RESERVE_GB, and larger, because an exec child writes multi-gigabyte
# .blends where a render writes one PNG.
EXEC_MIN_FREE_GB = float(os.environ.get("VASTRENDER_EXEC_MIN_FREE_GB", "4.0"))
# Container memory that must stay available before another build is admitted.
# The cgroup cap on the box running this campaign is 90.5 GiB — not the 515 GB
# the plan assumed, and not the 188 GB `free` reports, which is the HOST's. The
# render worker holds a multi-gigabyte scene resident in the same cgroup and is
# what a cgroup OOM would pick first, so this floor protects somebody else's 4K
# render, not this job.
EXEC_MIN_FREE_MEM_GB = float(os.environ.get("VASTRENDER_EXEC_MIN_FREE_MEM_GB", "20.0"))
# How many input bundles to keep on the instance. They are ~7 MB each, so this
# is hygiene rather than a budget; without it a long campaign accumulates one
# per edit of any module in the tree.
EXEC_BUNDLE_KEEP = int(os.environ.get("VASTRENDER_EXEC_BUNDLE_KEEP", "8"))

LAUNCH_TIMEOUT = 60.0


def exec_root() -> str:
    return f"{config.REMOTE_ROOT}/exec"


def bundles_root() -> str:
    return f"{config.REMOTE_ROOT}/bundles"


def bundle_dir(digest: str) -> str:
    if not _is_digest(digest):
        raise ValueError(f"unsafe bundle digest {digest!r}")
    return f"{bundles_root()}/{digest}"


def _is_digest(digest: str) -> bool:
    return bool(digest) and len(digest) >= 8 and all(c in "0123456789abcdef" for c in digest)


# --- what goes in a bundle -------------------------------------------------


@dataclass(frozen=True)
class Bundle:
    """A resolved set of local files and the digest that names them remotely."""

    root: Path
    members: tuple[Path, ...]       # absolute, all under root
    digest: str
    bytes: int

    @property
    def rel(self) -> list[str]:
        return [str(p.relative_to(self.root)) for p in self.members]

    def describe(self) -> str:
        return (f"bundle {self.digest} — {len(self.members)} file(s), "
                f"{self.bytes / 1e6:.1f} MB from {self.root}")


def collect(root: Path, patterns: Iterable[str]) -> list[Path]:
    """Expand globs under `root`, refusing anything that escapes it.

    Resolve first, then contain — the same rule `scenes.resolve_scene` applies,
    for the same reason. A bundle spec arrives from a caller and becomes a path
    on this machine and a directory on the instance.
    """
    real_root = root.resolve()
    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        if os.path.isabs(pattern):
            raise ValueError(f"bundle pattern {pattern!r} must be relative to {real_root}")
        for path in sorted(real_root.glob(pattern)):
            real = path.resolve()
            if real_root not in real.parents:
                raise ValueError(
                    f"bundle pattern {pattern!r} matched {real}, which is outside "
                    f"{real_root}"
                )
            if not real.is_file() or real in seen:
                continue
            seen.add(real)
            found.append(real)
    if not found:
        raise ValueError(f"bundle patterns {list(patterns)} matched no files under {real_root}")
    return found


def digest_of(root: Path, members: list[Path]) -> tuple[str, int]:
    """Content address over relative path AND content, of every member.

    Path is hashed alongside content because the same bytes under a different
    name is a different bundle: `world/items/x.py` and `world/items/y.py` with
    identical content import differently.
    """
    h = hashlib.sha256()
    total = 0
    for path in sorted(members, key=lambda p: str(p.relative_to(root))):
        rel = str(path.relative_to(root))
        h.update(rel.encode())
        h.update(b"\0")
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
                total += len(chunk)
        h.update(b"\0")
    return h.hexdigest()[:16], total


def prepare(root: Path, patterns: Iterable[str]) -> Bundle:
    members = collect(root, patterns)
    digest, total = digest_of(root.resolve(), members)
    return Bundle(root=root.resolve(), members=tuple(members), digest=digest,
                  bytes=total)


# --- staging on the instance ----------------------------------------------


def bundle_cached(ep: Endpoint, digest: str) -> bool:
    """Complete means the marker exists, and the marker is written last."""
    marker = f"{bundle_dir(digest)}/{BUNDLE_COMPLETE}"
    ran = probe(ep, f"test -f {shlex.quote(marker)} && echo YES || echo NO", timeout=60)
    return ran.ok and ran.out.split()[-1:] == ["YES"]


def push_bundle(ep: Endpoint, bundle: Bundle, *, keep_scenes: Optional[set[str]] = None,
                force: bool = False) -> dict:
    """Stage a bundle on the instance: one streamed tar, verified, marked last.

    The disk preflight is NOT bypassed. A bundle is small, but "small" is a
    property of this bundle rather than of the disk it is landing on, and the
    30 GB volume already carries an 8 GB scene cache and whatever an exec child
    is writing. So the disk is measured; if the bundle plus the reserve does not
    fit, the same LRU scene eviction the scene path uses is run first, and a disk
    that cannot be measured refuses the upload rather than proceeding blind.
    """
    if not force and bundle_cached(ep, bundle.digest):
        return {"cached": True, "digest": bundle.digest, "bytes": bundle.bytes,
                "seconds": 0.0}

    reserve = int(config.DISK_RESERVE_GB * 1e9)
    state = remote.disk_state(ep)
    if not state.ok:
        raise remote.DiskFull(
            f"refusing to stage {bundle.describe()} on {ep}: the disk could not be "
            f"measured, and an unmeasured disk is not a verified-empty one. "
            f"{state.detail}"
        )
    if state.free < bundle.bytes + reserve:
        remote.evict_to_fit(
            ep, keep=set(keep_scenes or ()), incoming=bundle.bytes,
            budget=remote.cache_budget(state, reserve),
            reserve=reserve, state=state,
        )

    base = bundle_dir(bundle.digest)
    started = time.time()
    # Wiped first: a half-pushed bundle from a previous attempt has no marker,
    # so it would never be *used*, but its files would confuse the count check
    # below into passing on the wrong content.
    run(ep, f"rm -rf {shlex.quote(base)} && mkdir -p {shlex.quote(base)}", timeout=120)

    want_files = len(bundle.members)
    want_bytes = bundle.bytes
    rel = [str(p.relative_to(bundle.root)) for p in bundle.members]

    tar = subprocess.Popen(
        ["tar", "-C", str(bundle.root), "-cf", "-", "--", *rel],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    comp = subprocess.Popen(
        ["zstd", "-19", "-T4", "-c"],
        stdin=tar.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if tar.stdout:
        tar.stdout.close()
    ssh = subprocess.Popen(
        ssh_base(ep) + [f"zstd -d -c | tar -x -C {shlex.quote(base)}"],
        stdin=comp.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if comp.stdout:
        comp.stdout.close()
    try:
        _, err = ssh.communicate(timeout=1800)
    except subprocess.TimeoutExpired:
        for proc in (ssh, comp, tar):
            proc.kill()
        raise TransferError("bundle push", str(ep), "no exit after 1800s",
                            time.time() - started, expected=want_bytes) from None
    comp.wait()
    tar.wait()
    if ssh.returncode or tar.returncode or comp.returncode:
        detail = tail(err.decode(errors="replace")) or "no stderr from ssh"
        for label, proc in (("tar", tar), ("zstd", comp)):
            if proc.returncode:
                stream = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                detail += (f" | local {label} exit {proc.returncode}: "
                           f"{tail(stream) or 'no stderr'}")
        raise TransferError(f"bundle push {bundle.digest}", str(ep), detail,
                            time.time() - started, expected=want_bytes)

    counted = probe(
        ep, f"find {shlex.quote(base)} -type f -printf '%s\\n' 2>/dev/null | "
            f"awk '{{n++; b+=$1}} END {{print n+0, b+0}}'",
        timeout=300,
    )
    if not counted.ok or len(counted.out.split()) < 2:
        raise TransferError(f"verifying bundle {bundle.digest}", str(ep),
                            counted.describe(), time.time() - started,
                            expected=want_bytes)
    got_files, got_bytes = (int(x) for x in counted.out.split()[:2])
    if got_files != want_files or got_bytes != want_bytes:
        raise TransferError(
            f"bundle push {bundle.digest}", str(ep),
            f"instance holds {got_files} files / {got_bytes} bytes, expected "
            f"{want_files} / {want_bytes} — an incomplete bundle does not fail a "
            f"build, it fails an import, and the blame lands on the module",
            time.time() - started, sent=got_bytes, expected=want_bytes,
        )

    # LAST. Everything above may fail; nothing above makes the bundle usable.
    run(ep, f"touch {shlex.quote(base)}/{BUNDLE_COMPLETE}", timeout=60)
    # A new bundle per edit of any module in the tree adds up over a campaign.
    # Dropped here rather than on a timer so it is tied to the thing that
    # creates them, and never touches the one just pushed.
    dropped = gc_bundles(ep, keep={bundle.digest})
    return {"cached": False, "digest": bundle.digest, "bytes": want_bytes,
            "files": want_files, "seconds": round(time.time() - started, 2),
            "dropped": dropped}


def gc_bundles(ep: Endpoint, keep: set[str], limit: int = EXEC_BUNDLE_KEEP) -> list[str]:
    """Drop all but the `limit` most recently modified bundles. Never `keep`."""
    ran = probe(ep, f"for d in {bundles_root()}/*/; do [ -d \"$d\" ] && "
                    f"printf '%s %s\\n' \"$(stat -c %Y \"$d\")\" \"$(basename \"$d\")\"; "
                    f"done; echo END", timeout=120)
    if not ran.ok or "END" not in ran.out:
        return []
    rows = []
    for line in ran.out.splitlines():
        parts = line.split()
        if len(parts) == 2 and _is_digest(parts[1]):
            rows.append((float(parts[0]), parts[1]))
    rows.sort(reverse=True)
    doomed = [d for _, d in rows[limit:] if d not in keep]
    if doomed:
        run(ep, "; ".join(f"rm -rf {shlex.quote(bundle_dir(d))}" for d in doomed),
            timeout=300, check=False)
    return doomed


# --- the exec server process ----------------------------------------------

# Same shape and the same reasoning as `remote.WORKER_PIDS`, and the same
# hard-won trap: `pgrep -f` also matches the `bash -c` sshd spawned to run it,
# because sshd puts the whole command string in that shell's argv. The pattern
# must match the REMOTE command line — the exec server runs as
# `-P /workspace/exec_server.py`, so a pattern of `worker/exec_server.py`
# matches nothing and every kill silently succeeds against no process. That is
# the failure that leaves a stale server holding the port while the broker talks
# to it believing it is the new one.
EXEC_PIDS = (
    'for p in $(pgrep -f {pattern} 2>/dev/null); do '
    'case "$(cat /proc/$p/comm 2>/dev/null)" in blender*) printf "%s " "$p";; esac; '
    'done'
)


def exec_launch_cmd(root: str, port: int, slots: int, blender: str = "",
                    min_free_gb: float = EXEC_MIN_FREE_GB,
                    min_free_mem_gb: float = EXEC_MIN_FREE_MEM_GB) -> str:
    """The remote command that starts the exec server, fully detached from SSH.

    Its exact shape is load-bearing and copied deliberately from
    `remote.worker_launch_cmd`, including the `setsid --fork` with no `&`: the
    old `A && B && C &` form backgrounds the whole AND-list as one subshell that
    keeps sshd's stdout pipe open, so ssh never returns and the launch "times
    out" against a process that started perfectly.

    Launched under Blender's Python because `/usr/bin/python3` on this image has
    no numpy and every item module imports it at module scope. Measured on the
    instance, not assumed.
    """
    exe = blender or f"{root}/blender/blender"
    args = (f"{exe} -b --factory-startup -P {root}/exec_server.py "
            f"-- --port {port} --root {root}/exec --bundles {root}/bundles "
            f"--slots {slots} --blender {exe} --min-free-gb {min_free_gb} "
            f"--min-free-mem-gb {min_free_mem_gb}")
    env = f"env OPTIX_CACHE_PATH={root}/optixcache TMPDIR={root}/tmp"
    io = f"< /dev/null > {root}/exec.log 2>&1"
    return (
        f"mkdir -p {root}/tmp {root}/exec {root}/bundles; cd {root}; "
        f"if command -v setsid >/dev/null 2>&1 && setsid --fork true 2>/dev/null; then "
        f"setsid --fork {env} {args} {io}; "
        f"else "
        f"nohup {env} {args} {io} & "
        f"fi"
    )


def exec_server_running(ep: Endpoint) -> Optional[bool]:
    """True / False / None when the instance could not be asked."""
    pids = EXEC_PIDS.format(pattern=shlex.quote(f"{config.REMOTE_ROOT}/exec_server.py"))
    ran = probe(ep, f'echo "PIDS:$({pids})"', timeout=60)
    if not ran.ok:
        return None
    marker = [ln for ln in ran.out.splitlines() if ln.startswith("PIDS:")]
    if not marker:
        return None
    return bool(marker[-1].split(":", 1)[1].split())


def stop_exec_server(ep: Endpoint) -> None:
    """Kill by pid, verify death, verify the port is free.

    The render worker's incident applies here unchanged: if the old process
    survives, the new one dies binding an in-use port and every subsequent
    request goes to the STALE server — which still holds the previous bundle
    cache and the previous slot count — while the broker records the new one as
    current. Silent wrong results, no error anywhere.
    """
    root = config.REMOTE_ROOT
    pids = EXEC_PIDS.format(pattern=shlex.quote(f"{root}/exec_server.py"))
    killed = probe(
        ep,
        f'p="$({pids})"; [ -n "$p" ] && kill $p 2>/dev/null; '
        f'for i in $(seq 1 15); do p="$({pids})"; [ -z "$p" ] && break; sleep 1; done; '
        f'p="$({pids})"; [ -n "$p" ] && kill -9 $p 2>/dev/null; sleep 1; '
        f'echo "SURVIVORS:$({pids})"',
        timeout=120,
    )
    if not killed.ok:
        raise remote.SshError(killed)
    marker = [ln for ln in killed.out.splitlines() if ln.startswith("SURVIVORS:")]
    if not marker:
        raise RemoteError(f"could not confirm the exec server died on {ep} — no marker "
                          f"in output ({killed.describe()})")
    survivors = marker[-1].split(":", 1)[1].split()
    if survivors:
        raise RemoteError(f"could not kill the exec server on {ep}: pids "
                          f"{' '.join(survivors)} still alive after SIGKILL")

    # `ss`, `netstat` and `lsof` are all absent from the CUDA base image —
    # verified on a live instance, which is why the render path reads
    # /proc/net/tcp directly. Column 2 is HEX_IP:HEX_PORT, column 4 is state,
    # 0A is LISTEN.
    port_hex = f"{EXEC_PORT:04X}"
    listening = probe(
        ep, f"cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | "
            f"awk '$4==\"0A\" && $2 ~ /:{port_hex}$/ {{n++}} END {{print n+0}}'",
        timeout=60)
    if not listening.ok or not listening.out.split():
        raise RemoteError(f"could not determine whether port {EXEC_PORT} is free on "
                          f"{ep} — refusing to start a second exec server blind "
                          f"({listening.describe()})")
    if listening.out.split()[-1] != "0":
        raise RemoteError(
            f"port {EXEC_PORT} still bound on {ep} after killing the exec server "
            f"({listening.out.split()[-1]} listener(s) in /proc/net/tcp) — a new "
            f"server would die on bind and the stale one would keep serving with the "
            f"old bundle cache")


def start_exec_server(ep: Endpoint, slots: int = EXEC_SLOTS) -> None:
    """Push exec_server.py, replace any running one, confirm it came up."""
    root = config.REMOTE_ROOT
    src = Path(__file__).resolve().parent.parent / "worker" / "exec_server.py"
    remote.push_file(ep, src, f"{root}/exec_server.py")
    stop_exec_server(ep)
    launched = probe(ep, exec_launch_cmd(root, EXEC_PORT, slots), timeout=LAUNCH_TIMEOUT)
    if not launched.ok:
        raise RemoteError(f"exec server launch on {ep} — {launched.describe()}")

    # Forking successfully is not running successfully: setsid returns 0 even if
    # blender dies on exec.
    for attempt in (1, 2, 3):
        alive = probe(ep, "sleep 3; echo READY", timeout=60)
        if alive.ok:
            break
        if attempt < 3:
            time.sleep(3 * attempt)
    running = exec_server_running(ep)
    if running is False:
        log_tail = probe(ep, f"tail -40 {root}/exec.log", timeout=60)
        raise RemoteError(
            f"exec server on {ep} exited immediately after launch. exec.log: "
            f"{tail(log_tail.out) or log_tail.describe()}")


def open_exec_tunnel(ep: Endpoint, local_port: int = EXEC_LOCAL_PORT) -> subprocess.Popen:
    """A forward of its own, on a connection of its own.

    Not the render tunnel, and not the shared ControlMaster. Sharing the master
    makes the forward hostage to a multiplexed socket that ControlPersist can
    reap, and a dead forward is indistinguishable from a dead server — which is
    what triggered a pointless full redeploy on the render side.
    """
    started = time.time()
    proc = subprocess.Popen(
        remote.ssh_nomux(ep) + [
            "-N", "-o", "ExitOnForwardFailure=yes",
            "-L", f"{local_port}:127.0.0.1:{EXEC_PORT}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    last = ""
    for _ in range(30):
        if proc.poll() is not None:
            err = tail(proc.stderr.read().decode(errors="replace")) if proc.stderr else ""
            raise RemoteError(
                f"exec tunnel to {ep} exited {proc.returncode} after "
                f"{time.time() - started:.1f}s forwarding 127.0.0.1:{local_port} -> "
                f":{EXEC_PORT}: {err or 'ssh exited with no stderr'}")
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=2):
                return proc
        except OSError as exc:
            last = remote.diagnose(exc)
            time.sleep(1)
    proc.terminate()
    raise RemoteError(
        f"exec tunnel to {ep} never bound 127.0.0.1:{local_port} after "
        f"{time.time() - started:.1f}s: {last or 'no connection error recorded'}")


def reap_stale_exec_tunnels(local_port: int = EXEC_LOCAL_PORT) -> int:
    """Kill orphaned `ssh -L <local_port>:127.0.0.1:<EXEC_PORT>` forwards.

    `kill -9` is the only sanctioned way to restart this broker, so a restarted
    broker cannot have cleaned up its own tunnel. The orphan holds the local
    port and the next forward dies with `Address already in use`.
    """
    pattern = f"{local_port}:127.0.0.1:{EXEC_PORT}"
    killed = 0
    try:
        out = subprocess.run(["pgrep", "-af", "ssh"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return 0
    for line in out.splitlines():
        pid_text, _, cmd = line.partition(" ")
        if pattern not in cmd or "-L" not in cmd:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 9)
            killed += 1
        except OSError:
            pass
    if killed:
        time.sleep(1)
    return killed


# Longer than any job's `timeout_s` ceiling plus the server's bounded slot wait,
# with slack. The socket must outlive the work: a client that gives up on a
# reply loses a result that was produced and paid for, and the child keeps
# running regardless.
EXEC_CALL_TIMEOUT = 7200.0
EXEC_CALL_SLACK = 900.0


def exec_call(payload: dict, port: int = EXEC_LOCAL_PORT,
              timeout: float = EXEC_CALL_TIMEOUT) -> dict:
    """One request, one reply, over the exec tunnel. Reuses the render path's
    socket handling so a dropped forward is reported as a dropped forward and
    never as a dead server."""
    return remote.worker_call(payload, port, timeout=timeout)


def wait_exec_server(port: int = EXEC_LOCAL_PORT, timeout: float = 300.0) -> dict:
    """Block until the exec server answers a ping, or raise saying why not."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            reply = exec_call({"cmd": "ping"}, port, timeout=30)
            if reply.get("ok"):
                return reply
            last = str(reply.get("error") or reply)
        except Exception as exc:
            last = remote.diagnose(exc)
        time.sleep(2)
    raise RemoteError(f"exec server never answered a ping on 127.0.0.1:{port} within "
                      f"{timeout:.0f}s: {last or 'no error recorded'}")
