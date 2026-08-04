#!/usr/bin/env python3
"""Everything that touches the rented instance: SSH, provisioning, scene push,
worker start, and the job tunnel.

Two transport decisions are load-bearing:

**ControlMaster multiplexing.** The broker issues many small commands to one
warm host. Without a shared master socket each one pays a full TCP + auth
handshake, which at 900 jobs is minutes of pure latency.

**zstd whole-file, not rsync delta.** Measured on two real revisions of the
scene, rsync matched only 14.7% — Blender rewrites the entire file on save and
embeds pointer addresses that shift every run, so there is nothing stable to
delta against. `zstd -19` sends 285 MB as 62.9 MB and beats rsync's own
delta+compress by 34%, because the rolling-checksum pass fragments the stream
and starves the compressor's context window.

**Every failure here is reported with an exit code, a stderr tail, an elapsed
time and the endpoint it happened on.** This module is the one place where the
broker touches a machine it cannot see, so a failure it cannot describe is a
failure nobody can fix. The log once carried the line

    ERROR broker  job dc1b162d8d85 requeued: blender push failed:

— a 481 MB upload that died twice with the reason discarded, after which the
broker destroyed a healthy GPU and rented another. Two habits caused it and both
are banned below: returning only `proc.stdout` from a command whose failure
lives on `stderr`, and interpolating a value into a message with no fallback for
when it is empty. `Ran.describe()` and `diagnose()` exist so no call site has to
remember.
"""

from __future__ import annotations

import contextlib
import hashlib
from concurrent import futures
import json
import logging
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import config

# This module had no logger at all, while `push_parallel` called `log.info` on
# its resume path — so the first transfer that actually had parts to resume
# would raise `NameError: name 'log' is not defined` inside the push, be
# swallowed by `push_blender`'s `except Exception`, and be reported as one more
# failed transfer attempt. The feature could not fire once without breaking the
# thing it was added to fix. "fleet" rather than "remote" because every other
# line this file emits already goes there, and the log is read by prefix.
log = logging.getLogger("fleet")

# How much of a subprocess's stderr survives into the log. Enough to carry an
# ssh banner exchange or an apt error, short enough to keep one line per failure.
STDERR_TAIL_LINES = 8
STDERR_TAIL_CHARS = 900

# A bulk chunk that has moved no bytes for this long is treated as dead. SSH's
# own keepalives only notice a *broken* connection; a TCP window that has been
# open and empty for minutes looks perfectly healthy to them, and `stdin.write`
# on a stalled pipe blocks with no timeout of its own. Observed: a chunk upload
# wedged for 12+ minutes with the connection nominally up.
STALL_SEC = 150.0

# What "the far end hung up on us" looks like in ssh's stderr, as opposed to a
# stall, a local error or a timeout. These are the three ways one dropped
# connection surfaces: the write side gets EPIPE, the read side gets the RST,
# and ssh's own client loop reports the disconnect it could not send.
RESET_MARKERS = (
    "Connection reset by peer",
    "Broken pipe",
    "client_loop: send disconnect",
)


@dataclass
class Endpoint:
    host: str
    port: int
    instance_id: int
    direct: bool = True

    @property
    def ctl_path(self) -> str:
        return f"/tmp/vastrender-cm-{self.instance_id}"

    def __str__(self) -> str:
        return f"{self.host}:{self.port} (instance {self.instance_id}" + (
            ")" if self.direct else ", VIA PROXY RELAY)"
        )


# --- diagnostics ----------------------------------------------------------


def tail(text: str, lines: int = STDERR_TAIL_LINES, chars: int = STDERR_TAIL_CHARS) -> str:
    """The last few meaningful lines of a subprocess stream, on one line.

    Returns "" for genuinely empty input — callers must supply the fallback,
    which is the whole point of `Ran.describe` and `diagnose` below.
    """
    if not text:
        return ""
    kept = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not kept:
        return ""
    return " | ".join(kept[-lines:])[-chars:]


@dataclass
class Ran:
    """The full result of one remote command — never just its stdout.

    `run(check=False)` used to hand back `proc.stdout` alone, so a command that
    failed at the SSH layer (exit 255, diagnosis on stderr, stdout empty) was
    indistinguishable from one that succeeded and printed nothing. Two log lines
    were produced entirely by that hole: `blender push failed:` and
    `disk state unusable after resume ()`.
    """

    cmd: str
    rc: int
    out: str
    err: str
    elapsed: float
    where: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def transport_failed(self) -> bool:
        """255 is ssh's own "I could not run your command at all"."""
        return self.rc == 255

    @property
    def auth_rejected(self) -> bool:
        """sshd completed the handshake and refused our key.

        This is a *different fact* from `transport_failed`, even though ssh
        reports both as exit 255, and conflating them wedged the broker for
        sixteen minutes on instance 46118513 while a rented 5090 billed.

        A transport failure means we never reached sshd: the packet was
        dropped, the port was refused, the link flapped. Retrying is right,
        because nothing about the host has been established.

        An auth rejection means the exact opposite. We completed the TCP
        connect, the key exchange and the userauth request; the container is
        up, sshd is serving, and it has looked at our key and said no. Nothing
        about that answer changes by asking again — vast.ai injects the
        authorised key at container start, so if it is absent four minutes in,
        the injection did not happen and never will.

        Verbatim from the wedged instance, on both the direct port and the
        proxy relay:

            debug1: Offering public key: /root/.ssh/id_vast_render ED25519 ...
            debug1: Authentications that can continue: publickey
            root@192.0.2.11: Permission denied (publickey).

        Vast's control plane reported that very key attached to that very
        instance, and detaching and re-attaching it did not repair the
        container. The host had simply failed to write authorized_keys.
        """
        if self.rc != 255:
            return False
        err = self.err.lower()
        return (
            "permission denied (publickey" in err
            or "permission denied (public key" in err
            or "too many authentication failures" in err
            or "no supported authentication methods" in err
        )

    def describe(self) -> str:
        """One non-empty line naming exit code, elapsed, endpoint and output."""
        detail = tail(self.err) or tail(self.out) or "no output on stdout or stderr"
        return (
            f"exit {self.rc} after {self.elapsed:.1f}s on {self.where} "
            f"[{self.cmd.strip().splitlines()[0][:100] if self.cmd.strip() else '?'}]: {detail}"
        )


class RemoteError(RuntimeError):
    """Base for everything this module raises. Always carries a description."""


class SshError(RemoteError):
    def __init__(self, ran: Ran) -> None:
        super().__init__(f"ssh: {ran.describe()}")
        self.ran = ran


class WorkerBusy(RemoteError):
    """The worker is mid-render and must not be restarted.

    Deliberately its own type so the fleet's deploy-retry path can re-raise it
    untouched. Counted as an ordinary deploy failure it would exhaust the retry
    budget and then be classified host-level — and the "replace the hardware"
    branch would **destroy a GPU that is 40 minutes into an 8K frame**. Being
    busy is not a failure at all.

    **It carries the job id it is busy with, and callers must use that field
    rather than asking again.** This exception is only ever raised off a
    *successful* read of progress.json, so at the moment of raising, the
    identity of the running render is known for certain. The first version of
    this guard formatted that identity into the message and threw the structured
    value away; the handler then re-queried over an SSH endpoint that was
    flapping, got nothing, and concluded the worker was not rendering the very
    job the exception had just named. Job 54ed3b8bd22f was marked `failed` at
    sample 6896/8192 that way, and finished on the instance with nobody waiting
    for it.
    """

    def __init__(self, message: str, job_id: Optional[str] = None,
                 progress: Optional[dict] = None) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.progress = progress or {}


class ConnectionDropped(RemoteError):
    """The job socket died mid-call. Says NOTHING about the worker.

    This distinction is the entire bug behind "worker closed connection without
    replying". The worker renders on its main thread and serves strictly
    serially, so for the whole duration of a frame it cannot answer anything —
    and when the SSH tunnel carrying that socket dies, the broker reads EOF,
    concludes the worker died, and "redeploys", which kills a perfectly healthy
    worker mid-render and restarts a 40-minute frame from zero.

    Measured on instance 45907537: the worker was alive, listening on 8799 and
    rendering at sample 832/8192 while the broker declared it not ready after
    599 pings — and the very log line announcing that failure quoted the
    worker's own `[worker] ready on 127.0.0.1:8799`.
    """


class ForeignBroker(RemoteError):
    """Another live broker on this machine already owns the running instance.

    Raised instead of adopting it, and the dispatcher pauses rather than renting
    around it — because both ways of continuing cost money: adopting destroys
    the other broker's GPU at the next shutdown, and renting bills two instances
    at once. Stopping is the same answer the singleton lock gives, for the same
    reason. See `foreign_broker`.
    """


class SshNeverReady(RemoteError):
    """sshd on a freshly booted instance never accepted a command.

    Distinct from `SshError` because the two want opposite responses. A
    transient `exit 255` is transport and must be retried on the same GPU. A
    container that has not started sshd after minutes is **the host**, and
    retrying it three times per round for three rounds bills an hour to learn
    nothing — so this one is not transport, and it does not get retried within
    the round either.

    Carries the last attempt so callers can tell *how* it failed. The two
    causes need different handling and the old message asserted the wrong one
    for both: it said "the container behind it is not serving" even when the
    container had served us a banner and rejected our key, which sent a live
    debugging session looking for an sshd that had never been down.
    """

    def __init__(self, message: str, ran: Optional[Ran] = None) -> None:
        super().__init__(message)
        self.ran = ran

    @property
    def auth(self) -> bool:
        """True when the container is serving sshd and refusing our key."""
        return bool(self.ran and self.ran.auth_rejected)


class FleetUnavailable(RemoteError):
    """There was no usable worker to render on. Says nothing about the frame.

    Its own type so a frame-range job can tell "this frame is bad" from "there is
    no GPU right now". Without the distinction, a market that hands back three
    unbootable offers in a row is charged to five consecutive *frames*, the
    consecutive-failure guard trips, and a sequence that has nothing wrong with
    it stops — while every one of those frames is recorded `failed` in the
    resume record for a reason that was never about them.
    """


class WorkerUnreachable(RemoteError):
    """The worker never answered — carrying WHY, so the fleet can classify it.

    This used to be a bare `RuntimeError` raised at three call sites, and its
    message said *"this is a transport failure, not a worker failure"* in plain
    English while its **type** said the opposite. `is_transport()` matches on
    type, so the fleet read a dead SSH forward — or a local port conflict on
    this machine — as the remote host being broken.

    That misclassification was harmless only for as long as a second bug hid
    it: the activity probe returned `unknown` for every never-rendered
    instance, and `unknown` blocked the destroy. Fixing the probe removed the
    mask and the fleet immediately destroyed two healthy GPUs and blacklisted a
    good machine, on a local `Address already in use`. A message is not a
    classification; only the type is.
    """

    def __init__(self, message: str, tunnel_died: bool = False,
                 local: bool = False) -> None:
        super().__init__(message)
        self.tunnel_died = tunnel_died
        self.local = local


class TransferError(RemoteError):
    """A bulk transfer failed. Distinguished from SshError on purpose: a dropped
    upload is a transport problem, and the fleet must retry it rather than
    destroy the GPU underneath it."""

    def __init__(self, op: str, where: str, detail: str, elapsed: float,
                 sent: int = -1, expected: int = -1,
                 streams: int = 0, reset_all: bool = False,
                 chronic: bool = False) -> None:
        if expected >= 0 and sent >= 0:
            moved = f", {sent}/{expected} bytes ({100.0 * sent / max(expected, 1):.1f}%)"
        elif expected >= 0:
            moved = f", {expected} bytes to move"
        else:
            moved = ""
        super().__init__(
            f"{op} failed after {elapsed:.1f}s on {where}{moved}: "
            f"{detail or 'no diagnostic output from the transfer'}"
        )
        self.op = op
        self.where = where
        self.detail = detail
        self.elapsed = elapsed
        self.sent = sent
        self.expected = expected
        # How many concurrent SSH connections this transfer was using.
        self.streams = streams
        # EVERY stream died, and died with the far end closing the connection
        # (RST / EPIPE) rather than with a timeout, a stall or a local error.
        # Streams dying independently at random points is a flaky link; streams
        # dying *together* is the other end hanging up on all of them at once,
        # which is a completely different verdict and needs to survive the trip
        # up to the fleet to be acted on.
        self.reset_all = reset_all
        # The same thing happened with a SINGLE connection. That is the fact
        # that clears us: one connection cannot trip a concurrency limit, so a
        # lone stream being reset the same way says the host resets whatever it
        # is given and no amount of retrying here will change it.
        self.chronic = chronic


class DiskFull(RemoteError):
    """The instance cannot hold what we are about to send it.

    Its own type, and deliberately NOT a TransferError, because the three
    reflexes this broker has for a failed push are all wrong here. It is not
    transport, so retrying the upload cannot help. It is not host-level, so
    destroying the GPU and renting another one buys the same disk and the same
    verdict — at the price of a rental, a Blender push and a scene push. And it
    is not something the queue should keep re-attempting: a scene that does not
    fit beside the loaded one will not fit on the next pass either.

    So this fails the JOB, loudly, naming every number that produced the
    verdict, and leaves the instance alone. The alternative — uploading into a
    disk that is about to fill — is the failure this whole preflight exists to
    prevent: Blender does not fail cleanly on ENOSPC, it writes a short PNG.
    """


# The vast.ai SDK puts the API key in the query string of every request, so any
# HTTPError it raises carries the key in its message — and this broker writes
# exception messages into a log file that gets tailed, grepped and pasted
# around. Observed verbatim in broker.log:
#
#     HTTPError: 400 Client Error: Bad Request for url:
#     https://console.vast.ai/api/v0/asks/43687899/?api_key=942dc099...
#
# That key IS the account: it can rent, destroy and spend. `diagnose` is the one
# function every logged failure passes through, so it is the one place this can
# be fixed once rather than at fifty call sites.
_SECRET_RE = re.compile(r"(api_key=)[^&\s\"'&]+", re.I)


def redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1<redacted>", text)


def diagnose(exc: BaseException) -> str:
    """A never-empty, secret-free description of any exception, for logging.

    `str(exc)` is empty for more exception types than is comfortable — a bare
    `RuntimeError()`, `KeyError()` with no args, anything raised as `raise Cls()`
    — and `log.warning("... %s", exc)` on one of those is precisely how a
    failure reaches the log with its cause erased. Prefixing the class name
    makes an empty message impossible by construction.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        cmd = exc.cmd if isinstance(exc.cmd, str) else " ".join(map(str, exc.cmd or []))
        return redact(f"TimeoutExpired: no exit after {exc.timeout}s: {cmd[:160]}")
    text = str(exc).strip()
    return redact(f"{type(exc).__name__}: {text}" if text
                  else f"{type(exc).__name__} (no message)")


def ssh_nomux(ep: Endpoint) -> list[str]:
    """SSH with multiplexing explicitly OFF, for parallel bulk transfer.

    ControlMaster is right for many small commands and wrong for parallel
    streams: multiplexed channels share one TCP connection, so N "parallel"
    uploads would contend for a single congestion window and gain nothing. Each
    chunk needs its own TCP connection to get its own window.
    """
    return [
        "ssh",
        "-p", str(ep.port),
        "-i", str(config.SSH_KEY),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-o", "Compression=no",
        # A chunk that cannot even connect must fail in seconds, not hang for
        # the whole transfer timeout while the other seven streams wait on it.
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
        f"root@{ep.host}",
    ]


def ssh_base(ep: Endpoint) -> list[str]:
    return [
        "ssh",
        "-p", str(ep.port),
        "-i", str(config.SSH_KEY),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={ep.ctl_path}",
        "-o", "ControlPersist=10m",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
        # The payload is already zstd; SSH's zlib would burn CPU on
        # incompressible bytes for no gain.
        "-o", "Compression=no",
        f"root@{ep.host}",
    ]


def probe(ep: Endpoint, command: str, timeout: float = 600,
          mux: bool = True) -> Ran:
    """Run a command and return everything about how it went.

    This is the primitive; `run` is the convenience wrapper over it. Anything
    that makes a *decision* from remote output — "is Blender installed?", "did
    the disk survive?" — must use this, because with `run` a command that never
    executed and a command that printed nothing are the same empty string, and
    the broker then pushes half a gigabyte to a host it cannot reach.

    `mux=False` gives this one command its own TCP connection instead of a
    channel on the shared ControlMaster. For almost everything the master is
    right — many small commands, one handshake. It is wrong for anything whose
    failure is *interpreted*, because a channel on a dead master fails in a way
    that is indistinguishable from the command failing, and the master is shared
    with bulk work that can take it down.
    """
    started = time.time()
    try:
        proc = subprocess.run(
            (ssh_base(ep) if mux else ssh_nomux(ep)) + [command],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - started
        out = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return Ran(command, -1, out, (err + f"\nno exit after {timeout:.0f}s").strip(),
                   elapsed, str(ep))
    return Ran(command, proc.returncode, proc.stdout, proc.stderr,
               time.time() - started, str(ep))


def run(ep: Endpoint, command: str, timeout: float = 600, check: bool = True) -> str:
    """Stdout of a remote command.

    `check=False` still swallows the exit code, so it is only correct where the
    caller genuinely does not care whether the command ran — `rm -f`, a
    heartbeat touch. To *test* something on the instance, use `probe`.
    """
    ran = probe(ep, command, timeout=timeout)
    if check and not ran.ok:
        raise SshError(ran)
    return ran.out


WAIT_SSH_SEC = 240.0


def wait_ssh(ep: Endpoint, timeout: float = WAIT_SSH_SEC) -> Ran:
    """Block until the instance actually runs a command for us.

    **A TCP connect is not SSH readiness, and this cost an evening.** `wait_ready`
    declares an instance up when something accepts a connection on the forwarded
    port — but vast injects the authorised key as part of container startup, so
    for the first minute or so the port is open and every command comes straight
    back as `exit 255 after 0.4s` with nothing on stderr. Observed repeatedly:

        instance 46077186 reachable at 192.0.2.15:10366 in 51s
        deploy attempt 1/3 failed: ssh: exit 255 after 0.4s [test -x .../blender]
        deploy attempt 2/3 failed: ssh: exit 255 after 0.4s
        deploy attempt 3/3 failed: ssh: exit 255 after 0.4s

    Three attempts five seconds apart is not a wait, it is a formality. The
    deploy budget was then spent on a host that was merely *young*, the round
    counter climbed, and a perfectly good GPU was on its way to being replaced —
    twice, on two different instances of the same machine.

    So the deploy path waits here first, for minutes rather than seconds, on the
    cheapest possible command. Failing this is a real failure; failing a deploy
    because sshd had not finished starting is not.
    """
    started = time.time()
    deadline = started + timeout
    attempt = 0
    ran = probe(ep, "true", timeout=30)
    while not ran.ok and time.time() < deadline:
        attempt += 1
        time.sleep(min(15.0, 2.0 + attempt))
        ran = probe(ep, "true", timeout=30)
    if ran.ok and attempt:
        log_ssh_wait(ep, time.time() - started, attempt)
    return ran


def log_ssh_wait(ep: Endpoint, elapsed: float, attempts: int) -> None:
    import logging

    logging.getLogger("fleet").info(
        "sshd on %s accepted a command after %.0fs and %d retries — a young "
        "container answers TCP well before it accepts our key",
        ep, elapsed, attempts,
    )


def close_master(ep: Endpoint) -> None:
    subprocess.run(ssh_base(ep) + ["-O", "exit"], capture_output=True, text=True)


# --- provisioning ---------------------------------------------------------

PROVISION = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
mkdir -p {root}/out {root}/bin

if ! command -v zstd >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
      zstd curl xz-utils ca-certificates \
      libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libgl1 libsm6 \
      libegl1 libxkbcommon0 libdbus-1-3 >/dev/null
fi

# Blender must match the local build that assembled the scene, and must carry
# sm_120 kernels — a CUDA toolkit below 12.8 ships no Blackwell cubin and the
# render silently falls back to CPU.
if [ ! -x {root}/blender/blender ]; then
  mkdir -p {root}/blender
  if [ -f {root}/blender.tar.zst ]; then
    # Served from the broker machine. Preferred: download.blender.org measured
    # 0.42 MB/s locally and ~1-2.7 MB/s to the instance, so it is the slow party
    # regardless of who fetches. Pushing from here also guarantees the remote
    # build is byte-identical to the one that assembled the scene.
    zstd -d -c {root}/blender.tar.zst | tar -x -C {root}/blender --strip-components=1
  else
    # -C - resumes a partial download, so a broker restart mid-pull does not
    # throw away what already landed.
    curl -fsSL -C - '{url}' -o {root}/blender.tar.xz
    tar -xJf {root}/blender.tar.xz -C {root}/blender --strip-components=1
  fi
fi

# Persist the OptiX JIT cache across worker restarts: it costs ~9 s to rebuild.
mkdir -p {root}/optixcache
{root}/blender/blender --version | head -2
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
"""


def provision(ep: Endpoint) -> str:
    return run(
        ep,
        PROVISION.format(root=config.REMOTE_ROOT, url=config.BLENDER_URL),
        timeout=1800,
    )


# --- scene ----------------------------------------------------------------


_HASH_CACHE: dict[str, tuple[float, int, str]] = {}
_HASH_LOCK = threading.Lock()

_FULL_HASH_CACHE: dict[str, tuple[float, int, str]] = {}


def file_sha256(path: Path) -> str:
    """Full hex sha256, for comparing against the instance's `sha256sum`.

    Separate from `scene_hash`, which truncates to 16 characters for use as a
    directory name — comparing that to a remote digest would never match.

    Memoised on (mtime, size) like `scene_hash`: the Blender bundle is 481 MB
    and a retrying push would otherwise re-read it once per attempt.
    """
    stat = path.stat()
    key = str(path)
    with _HASH_LOCK:
        cached = _FULL_HASH_CACHE.get(key)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    digest = h.hexdigest()

    with _HASH_LOCK:
        fresh = path.stat()
        _FULL_HASH_CACHE[key] = (fresh.st_mtime, fresh.st_size, digest)
    return digest


def scene_hash(path: Path) -> str:
    """Content hash of the assembled scene. Jobs are rendered against a known
    revision, and a new assembly invalidates the remote copy cleanly — which is
    what stops a render silently coming from stale geometry.

    Memoised on (mtime, size), because a frame sequence asks this question a lot
    more often than a still does. `ensure_ready` re-hashes to decide whether the
    loaded scene is still the right one, and it runs **once per frame** — over a
    3,000-frame shot that is 3,000 full reads of a 288 MB file, roughly half an
    hour of pure I/O spent re-learning something that cannot have changed.

    Keyed on the file's own mtime and size rather than on time, so a reassembled
    .blend still invalidates immediately. Two different revisions with identical
    mtime *and* identical byte count would collide, which requires deliberate
    effort: Blender rewrites the whole file on save and stamps the mtime.
    """
    stat = path.stat()
    key = str(path)
    with _HASH_LOCK:
        cached = _HASH_CACHE.get(key)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()[:16]

    with _HASH_LOCK:
        # Re-stat rather than trusting the one above: the file may have been
        # rewritten while it was being read, and caching a hash of a half-old
        # file under the new mtime would pin a wrong answer indefinitely.
        now = path.stat()
        if (now.st_mtime, now.st_size) == (stat.st_mtime, stat.st_size):
            _HASH_CACHE[key] = (stat.st_mtime, stat.st_size, digest)
    return digest


def scene_dir(digest: str) -> str:
    """The directory one cached scene owns on the instance.

    A directory rather than a bare `<digest>.blend`, and the change is not
    cosmetic. Blender resolves every `//` reference — physics caches above all —
    against the directory holding the .blend, and against its *filename* for
    `//blendcache_<name>/`. Storing the scene as `<hash>.blend` in a shared
    directory therefore broke both: the cache Blender looked for was
    `blendcache_<hash>` and it was not there, so instead of failing, Blender
    re-simulated. Giving each scene its own directory and keeping its original
    filename makes relative references resolve on the instance exactly as they
    do here.
    """
    return f"{config.REMOTE_ROOT}/scenes/{digest}"


def scene_cache_path(digest: str, name: str) -> str:
    """Where a scene's .blend lives on the instance, addressed by content."""
    if "/" in name or name in ("", ".", ".."):
        raise ValueError(f"unsafe scene filename {name!r}")
    return f"{scene_dir(digest)}/{name}"


SCENE_COMPLETE = ".complete"


def scene_cached(ep: Endpoint, digest: str, size: int, name: str) -> bool:
    """Is this exact scene already on the instance, complete *with its caches*?

    Size-verified, not merely present: a push interrupted partway leaves a short
    file, and loading that would either fail or — worse — render something
    truncated. Content addressing means a matching hash and a matching size is
    proof, so re-selecting a scene costs nothing instead of a 22 s upload.

    The marker file is the second half of that proof and it exists because the
    .blend is no longer the whole scene. Sim caches are pushed after it, file by
    file, and a push that dies between them leaves a perfectly valid .blend
    beside a half-copied cache tree — which Blender treats as no cache at all
    and quietly simulates. The marker is written last, so "cached" means the
    whole tree landed, not just the file that happens to be checked.
    """
    path = scene_cache_path(digest, name)
    marker = f"{scene_dir(digest)}/{SCENE_COMPLETE}"
    ran = probe(ep, f"test -f {shlex.quote(marker)} && "
                    f"stat -c %s {shlex.quote(path)} 2>/dev/null || echo 0", timeout=60)
    if not ran.ok or not ran.out.split():
        return False
    try:
        return int(ran.out.split()[-1]) == size
    except ValueError:
        return False


def mark_scene_complete(ep: Endpoint, digest: str) -> None:
    """Written last, once the .blend and every sibling directory have landed."""
    run(ep, f"touch {shlex.quote(scene_dir(digest))}/{SCENE_COMPLETE}", timeout=60)


def touch_scene(ep: Endpoint, digest: str) -> None:
    """Mark a cached scene as just-used, so eviction sees a real LRU order."""
    run(ep, f"touch {shlex.quote(scene_dir(digest))}", timeout=60, check=False)


def push_scene_siblings(ep: Endpoint, digest: str, parent: Path,
                        dirs: list[Path]) -> tuple[int, int]:
    """Copy directories that must sit *beside* the .blend on the instance.

    Physics caches, and anything else the blend references relatively. Pushed
    into the scene's own directory under their original names, which is the only
    arrangement in which `//cache/...` and `//blendcache_shot/` resolve remotely.

    **One streamed tar, not one scp per file.** A rigid-body destruction bake is
    one `.bphys` per frame — three thousand small files for a two-minute shot,
    more once debris and particles are in it. At the 69 ms round trip measured to
    these hosts, scp-per-file is pure latency: the 48-file test fixture alone
    would spend ~15 s of handshakes moving half a megabyte. tar over one SSH
    connection pays that once, and zstd on the way keeps the wire cheap.

    Verified by counting files and bytes on the far side, because a cache tree
    that arrives incomplete does not fail a render — it makes Blender simulate.

    Returns (files, bytes). Read-only on the instance by intent: nothing on the
    box ever writes into these, and the worker never bakes.
    """
    names = [d.name for d in dirs if d.is_dir()]
    if not names:
        return 0, 0

    want_files = 0
    want_bytes = 0
    for d in dirs:
        for p in d.rglob("*"):
            if p.is_file():
                want_files += 1
                want_bytes += p.stat().st_size
    if not want_files:
        return 0, 0

    base = scene_dir(digest)
    started = time.time()
    tar = subprocess.Popen(
        ["tar", "-C", str(parent), "-cf", "-", *names],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    comp = subprocess.Popen(
        ["zstd", "-3", "-T4", "-c"],
        stdin=tar.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if tar.stdout:
        tar.stdout.close()
    ssh = subprocess.Popen(
        # Its own connection, for the same reason as `push_scene` above: this is
        # a bulk stream, a sim-cache tree is routinely gigabytes, and on the
        # multiplexed master anything that takes over 180 s is killed by the
        # starved-keepalive teardown rather than by anything wrong with the link.
        ssh_nomux(ep) + [f"mkdir -p {shlex.quote(base)} && zstd -d -c | "
                         f"tar -x -C {shlex.quote(base)}"],
        stdin=comp.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if comp.stdout:
        comp.stdout.close()
    try:
        _, err = ssh.communicate(timeout=3600)
    except subprocess.TimeoutExpired:
        for proc in (ssh, comp, tar):
            proc.kill()
        raise TransferError("cache push", str(ep), "no exit after 3600s",
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
        raise TransferError(f"cache push ({', '.join(names)})", str(ep), detail,
                            time.time() - started, expected=want_bytes)

    # Count what landed. A short cache tree is indistinguishable from a correct
    # one at render time: Blender simply simulates the frames it cannot read.
    counted = probe(
        ep,
        "{ " + "; ".join(
            f"find {shlex.quote(base + '/' + n)} -type f -printf '%s\\n' 2>/dev/null"
            for n in names
        ) + "; } | awk '{n++; b+=$1} END {print n+0, b+0}'",
        timeout=600,
    )
    if not counted.ok or len(counted.out.split()) < 2:
        raise TransferError(f"verifying cache push ({', '.join(names)})", str(ep),
                            counted.describe(), time.time() - started,
                            expected=want_bytes)
    got_files, got_bytes = (int(x) for x in counted.out.split()[:2])
    if got_files != want_files or got_bytes != want_bytes:
        raise TransferError(
            f"cache push ({', '.join(names)})", str(ep),
            f"instance holds {got_files} files / {got_bytes} bytes, expected "
            f"{want_files} / {want_bytes} — an incomplete physics cache does not "
            f"fail a render, it makes Blender simulate instead",
            time.time() - started, sent=got_bytes, expected=want_bytes,
        )
    return want_files, want_bytes


@dataclass(frozen=True)
class SceneEntry:
    """One cached scene directory on the instance."""

    digest: str
    bytes: int
    used_at: float          # mtime of the directory == last time a job touched it


@dataclass
class DiskState:
    """What the instance's disk actually looks like, right now.

    `ok` is the whole point. Every field below is meaningless unless the
    measurement itself succeeded, and this project has already shipped two gates
    that printed a green verdict while measuring nothing (f1-round2 R2-018:
    "0 clusters, 0 environment objects -> COLLISION_CLEAN"). A DiskState that
    could not be read says so and carries `detail`; it never reports zero bytes
    used and lets a caller read that as "plenty of room".
    """

    ok: bool
    detail: str = ""
    total: int = 0
    used: int = 0
    free: int = 0
    scenes: tuple[SceneEntry, ...] = ()
    measured_at: float = 0.0

    @property
    def cache_bytes(self) -> int:
        return sum(s.bytes for s in self.scenes)

    @property
    def scene_count(self) -> int:
        return len(self.scenes)

    @property
    def other_bytes(self) -> int:
        """Everything on the disk that is NOT the scene cache — the container
        image, Blender, the OptiX cache, worker logs, frames not yet fetched."""
        return max(0, self.used - self.cache_bytes)

    def describe(self) -> str:
        if not self.ok:
            return f"disk state UNKNOWN: {self.detail or 'no reason recorded'}"
        pct = 100.0 * self.used / max(self.total, 1)
        return (f"{self.used / 1e9:.1f}G used of {self.total / 1e9:.1f}G ({pct:.0f}%), "
                f"{self.free / 1e9:.1f}G free; scene cache {self.cache_bytes / 1e9:.2f}G "
                f"in {self.scene_count} scene(s)")


# One command, one round trip: `df` for the filesystem and a per-scene `du` for
# the cache. Measured at 0.38 s over 42 cached scenes and 9.0 GB, so it is cheap
# enough to ride the heartbeat thread every few minutes.
#
# `df -kP` is the POSIX single-line form: without -P a long device name wraps
# onto two lines and NR==2 parses a device instead of a size. Availability is
# taken from df's own "Available" column, which already excludes the 5% root
# reserve — conservative in the safe direction, since we run as root.
_DISK_CMD = r"""
mkdir -p {root}/scenes 2>/dev/null
df -kP {root} | awk 'NR==2 {{printf "DF %s %s %s\n", $2, $3, $4}}'
for d in {root}/scenes/*/; do
  [ -d "$d" ] || continue
  printf 'S %s %s %s\n' "$(stat -c %Y "$d")" \
    "$(du -sb "$d" 2>/dev/null | cut -f1)" "$(basename "$d")"
done
echo END
"""


def disk_state(ep: Endpoint, timeout: float = 300) -> DiskState:
    """Measure the instance's disk and its scene cache. Never guesses.

    Returns `ok=False` with a reason whenever the numbers could not be read —
    ssh failed, df printed nothing parseable, the trailing marker never arrived
    (a truncated read). A caller may then refuse to proceed; what it may not do
    is treat an unmeasured disk as an empty one.
    """
    ran = probe(ep, _DISK_CMD.format(root=config.REMOTE_ROOT), timeout=timeout)
    now = time.time()
    if not ran.ok:
        return DiskState(ok=False, detail=ran.describe(), measured_at=now)

    total = used = free = -1
    scenes_seen: list[SceneEntry] = []
    complete = False
    for line in ran.out.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "DF" and len(parts) == 4:
            try:
                total, used, free = (int(x) * 1024 for x in parts[1:4])
            except ValueError:
                pass
        elif parts[0] == "S":
            # A `du` that failed leaves its field EMPTY, which collapses the
            # line to three fields rather than producing a bad number — so the
            # arity check is the one that matters, and skipping such a line
            # would make the cache look smaller than it is. That is the
            # direction that fills a disk, so refuse the whole measurement.
            try:
                if len(parts) != 4:
                    raise ValueError("wrong field count")
                scenes_seen.append(SceneEntry(digest=parts[3], bytes=int(parts[2]),
                                              used_at=float(parts[1])))
            except ValueError:
                return DiskState(ok=False, measured_at=now,
                                 detail=f"unparseable scene line {line!r} from {ep} — "
                                        f"a scene whose size could not be read is not "
                                        f"a scene of size zero")
        elif parts[0] == "END":
            complete = True

    if not complete:
        return DiskState(ok=False, measured_at=now,
                         detail=f"output from {ep} ended before the END marker — "
                                f"a truncated read, not an empty disk "
                                f"({tail(ran.out) or 'no output'})")
    if total <= 0 or free < 0:
        return DiskState(ok=False, measured_at=now,
                         detail=f"df on {ep} produced no usable numbers "
                                f"({tail(ran.out) or 'no output'})")
    return DiskState(ok=True, total=total, used=used, free=free,
                     scenes=tuple(scenes_seen), measured_at=now)


@dataclass
class Eviction:
    """What one preflight did, in numbers a human can check against `du`."""

    before: DiskState
    after: DiskState
    evicted: tuple[SceneEntry, ...]
    budget: int
    incoming: int
    reserve: int

    @property
    def freed(self) -> int:
        return sum(s.bytes for s in self.evicted)

    def describe(self) -> str:
        if not self.evicted:
            return (f"nothing to evict — {self.before.describe()}; "
                    f"incoming {self.incoming / 1e9:.2f}G fits")
        return (f"evicted {len(self.evicted)} least-recently-used scene(s), "
                f"{self.freed / 1e9:.2f}G: "
                f"{', '.join(s.digest[:8] for s in self.evicted)} — cache "
                f"{self.before.cache_bytes / 1e9:.2f}G -> "
                f"{self.after.cache_bytes / 1e9:.2f}G, free "
                f"{self.before.free / 1e9:.2f}G -> {self.after.free / 1e9:.2f}G "
                f"(budget {self.budget / 1e9:.2f}G, incoming "
                f"{self.incoming / 1e9:.2f}G, reserve {self.reserve / 1e9:.2f}G)")


def effective_budget(state: DiskState, configured: int, reserve: int) -> int:
    """How large the scene cache may actually be on THIS disk.

    The configured cap is a policy ceiling written for a 30 GB volume. The disk
    is physics, and the user is moving to a 16 GB one. So the budget is the
    smaller of the two: whatever is left after everything that is not the cache
    (the container image, Blender, the OptiX cache, frames in flight) and after
    the free-space reserve.

    Derived from a measurement rather than from `DISK_GB`, because the config
    value is what we asked vast.ai for and the filesystem is what we got.
    """
    room = state.total - state.other_bytes - reserve
    return max(0, min(configured, room))


def cache_room(state: DiskState, reserve: int) -> int:
    """Bytes available to the scene cache on this disk, cache itself excluded.

    Split out because both the derivation and the ceiling need it, and because
    the exclusion is the load-bearing part: `other_bytes` subtracts the cache,
    so this does NOT shrink as the cache fills. A budget derived from free
    space would chase its own tail — every upload would lower the ceiling that
    permitted it — and settle wherever the feedback happened to stop.
    """
    return max(0, state.total - state.other_bytes - reserve)


def derived_cache_bytes(state: DiskState, reserve: int) -> int:
    """A scene-cache ceiling sized from the disk in front of us.

    Replaces a hardcoded 8 GB that was correct for the 16 GB volume it was
    written for and wrong for the 32 GB volume it was applied to. See
    config.SCENE_CACHE_GB for the measurement that retired the constant.

    The floor may not conjure disk it does not have, so it is clamped to the
    room that exists; a disk too small for the floor gets all of its room and
    the eviction path deals with the consequences honestly.
    """
    room = cache_room(state, reserve)
    floor = min(int(config.SCENE_CACHE_FLOOR_GB * 1e9), room)
    return max(floor, int(config.SCENE_CACHE_FRACTION * room))


def cache_budget(state: DiskState, reserve: int) -> int:
    """The scene-cache budget for THIS disk: derived unless explicitly pinned.

    One place that answers the question, so `rq status`, the eviction preflight
    and the exec path cannot disagree about it — they read the same number or
    none of them do.
    """
    pinned = float(getattr(config, "SCENE_CACHE_GB", 0.0) or 0.0)
    if pinned > 0:
        return effective_budget(state, int(pinned * 1e9), reserve)
    return effective_budget(state, derived_cache_bytes(state, reserve), reserve)


def describe_cache_budget(state: DiskState, reserve: int) -> str:
    """One line explaining where the budget came from, for the deploy log.

    A derived number that is never printed is a magic number with extra steps.
    """
    budget = cache_budget(state, reserve)
    pinned = float(getattr(config, "SCENE_CACHE_GB", 0.0) or 0.0)
    if pinned > 0:
        how = f"pinned by SCENE_CACHE_GB={pinned:g}"
    else:
        how = (f"derived: {config.SCENE_CACHE_FRACTION:.0%} of "
               f"{cache_room(state, reserve) / 1e9:.1f}G room "
               f"(floor {config.SCENE_CACHE_FLOOR_GB:g}G)")
    return (f"scene cache budget {budget / 1e9:.2f}G — {how}; disk "
            f"{state.total / 1e9:.1f}G total, {state.other_bytes / 1e9:.1f}G "
            f"non-cache, {reserve / 1e9:.1f}G reserve")


def evict_to_fit(ep: Endpoint, keep: set[str], *, incoming: int, budget: int,
                 reserve: int, state: Optional[DiskState] = None,
                 defer: "Optional[Callable[[], set[str]]]" = None) -> Eviction:
    """Make room for `incoming` bytes of scene, LRU-first. Verified, not assumed.

    Two constraints, and they are deliberately not the same kind of thing:

      * **free space is physics** — after the upload there must still be
        `reserve` bytes free. Unsatisfiable means `DiskFull`, because writing
        anyway is how a disk fills mid-batch. Blender does not fail cleanly on
        ENOSPC; it writes a short PNG that passes a size check against itself.
      * **the cache budget is policy** — a ceiling so 435 uploads of 270 MB do
        not become 117 GB. Best-effort: if every remaining scene is pinned, the
        budget is exceeded and logged, not turned into a failed job.

    `keep` is never evicted. It holds the scene the warm worker has open and any
    scene with a job in flight; evicting either would delete the file Blender
    has mapped, and the render that follows is silently wrong rather than
    loudly broken.

    "Least recently USED" is the directory's mtime, which `touch_scene` stamps
    every time a job selects that scene — not creation time. A scene uploaded
    once and used all day must outlive one uploaded an hour ago and never
    touched since.

    `defer` is evicted LAST — scenes with jobs still queued against them. LRU
    alone gets this exactly backwards: a scene's stamp is written when it is
    *selected*, so one with work merely waiting still carries the oldest
    possible timestamp and sorts ahead of an idle scene that finished an hour
    ago. That is how a 4.5 GB scene holding sixteen queued jobs became the
    first thing thrown away. Deferring is an ordering, not a pin: if evicting
    every idle scene is not enough, these still go, and free space still
    outranks every preference expressed here.

    The eviction is re-measured afterwards. `rm -rf` runs with check=False (a
    missing directory is not an error), so "we sent the removals" is not
    evidence the bytes are gone — and the whole point of this function is to be
    the thing that knows.
    """
    # `state` lets the caller hand in the measurement it already took to size the
    # budget, so the preflight is one round trip rather than two — and, more to
    # the point, so the budget and the eviction are computed from the SAME
    # numbers rather than from two reads a few seconds apart.
    before = state if state is not None else disk_state(ep)
    if not before.ok:
        raise DiskFull(
            f"refusing to upload {incoming / 1e9:.2f}G to {ep}: the disk could not "
            f"be measured, and an unmeasured disk is not a verified-empty one. "
            f"{before.detail}"
        )

    # What the EXISTING cache must shrink to, so that adding `incoming` still
    # leaves both constraints satisfied.
    want_cache = max(0, budget - incoming)
    need_policy = max(0, before.cache_bytes - want_cache)
    need_space = max(0, incoming + reserve - before.free)
    need = max(need_policy, need_space)

    candidates = [s for s in before.scenes if s.digest not in keep]
    # Asked for only when something is actually going to be deleted. Answering
    # it means content-hashing every scene the queue still wants, and a cold
    # memo over the live queue measured 31.3 s for 9.67 GB — a real tax to pay
    # on the dispatch thread for an ordering that, most passes, orders nothing.
    if need > 0 and candidates and defer is not None:
        wanted = defer()
        if wanted:
            candidates.sort(key=lambda s: (s.digest in wanted, s.used_at))
        else:
            candidates.sort(key=lambda s: s.used_at)
    else:
        candidates.sort(key=lambda s: s.used_at)
    evicted: list[SceneEntry] = []
    freed = 0
    for entry in candidates:
        if freed >= need:
            break
        freed += entry.bytes
        evicted.append(entry)

    # One SSH command per batch, not one per scene. Measured against a live
    # instance: 26 directories removed one connection at a time took 67.6 s,
    # almost all of it TCP setup and key exchange at 69 ms RTT — the same
    # per-round-trip cost that made scp-per-cache-file untenable. Chunked so a
    # cache of hundreds cannot build a command line longer than the far side
    # will accept.
    for i in range(0, len(evicted), 50):
        batch = evicted[i:i + 50]
        run(ep, "; ".join(f"rm -rf {shlex.quote(scene_dir(s.digest))}" for s in batch),
            timeout=600, check=False)

    after = disk_state(ep) if evicted else before
    if not after.ok:
        raise DiskFull(
            f"evicted {len(evicted)} scene(s) from {ep} but could not re-measure the "
            f"disk to prove it worked, so the upload is refused rather than "
            f"attempted blind. {after.detail}"
        )

    report = Eviction(before=before, after=after, evicted=tuple(evicted),
                      budget=budget, incoming=incoming, reserve=reserve)

    if after.free < incoming + reserve:
        pinned = sorted(keep)
        biggest = sorted(before.scenes, key=lambda s: -s.bytes)[:3]
        raise DiskFull(
            f"NOT ENOUGH DISK on {ep} for {incoming / 1e9:.2f}G of scene. "
            f"After evicting {len(evicted)} scene(s) ({freed / 1e9:.2f}G) the disk "
            f"holds {after.used / 1e9:.1f}G of {after.total / 1e9:.1f}G with "
            f"{after.free / 1e9:.2f}G free; the upload needs {incoming / 1e9:.2f}G "
            f"plus a {reserve / 1e9:.2f}G reserve, i.e. "
            f"{(incoming + reserve - after.free) / 1e9:.2f}G more than exists. "
            f"Scene cache is {after.cache_bytes / 1e9:.2f}G in {after.scene_count} "
            f"scene(s); non-cache use (image, Blender, output) is "
            f"{after.other_bytes / 1e9:.2f}G. Unevictable (loaded or in flight): "
            f"{', '.join(d[:8] for d in pinned) or 'none'}. Largest cached: "
            f"{', '.join(f'{s.digest[:8]}={s.bytes / 1e9:.2f}G' for s in biggest) or 'none'}. "
            f"Rent a larger disk (VASTRENDER_DISK_GB) or render a smaller assembly — "
            f"retrying cannot create space."
        )
    return report


def drop_blender_bundle(ep: Endpoint) -> int:
    """Delete the pushed Blender tarball once the install is proven good.

    460 MB of pure dead weight: the bundle is extracted into
    `{root}/blender/` and nothing ever reads it again. Both paths that could
    want it test for the *installed* build, not the archive —
    `Fleet._deploy` skips the push on `test -x {root}/blender/blender`, and
    `PROVISION` re-extracts only when that same test fails — so a resume or a
    redeploy over a working install never touches it, and one that meets a
    missing install re-pushes it from the broker. Verified by reading both call
    sites before deleting anything.

    Proven good means Blender RUNS, not that a file exists. A truncated extract
    leaves an executable at the right path; the version banner is what
    distinguishes it from a working install, and it is the difference between
    freeing 460 MB and throwing away the only copy of a broken one.

    Returns the bytes freed — 0 when there was nothing to free, which is the
    normal steady state.
    """
    root = config.REMOTE_ROOT
    ran = probe(
        ep,
        f"test -f {root}/blender.tar.zst || {{ echo NOBUNDLE; exit 0; }}; "
        f"stat -c %s {root}/blender.tar.zst; "
        f"{root}/blender/blender --version 2>/dev/null | head -1",
        timeout=300,
    )
    if not ran.ok:
        return 0
    lines = [ln.strip() for ln in ran.out.splitlines() if ln.strip()]
    if not lines or lines[0] == "NOBUNDLE":
        return 0
    try:
        size = int(lines[0])
    except ValueError:
        return 0
    if not any(ln.lower().startswith("blender") for ln in lines[1:]):
        import logging

        logging.getLogger("fleet").warning(
            "keeping %s/blender.tar.zst (%.0f MB): %s/blender/blender did not "
            "report a version, so the installed build is not proven good and the "
            "archive is the only copy on the box", root, size / 1e6, root)
        return 0
    gone = probe(ep, f"rm -f {root}/blender.tar.zst && "
                     f"test ! -f {root}/blender.tar.zst && echo GONE", timeout=120)
    return size if gone.ok and gone.out.strip() == "GONE" else 0


def scene_zstd_level(scene: Path) -> tuple[int, str]:
    """Which zstd level to push this scene at, and why — for the log.

    A push costs compression plus wire, and the level trades one for the other.
    The old fixed 19 optimised the wire alone, which is the wrong half on a
    link this fast relative to this CPU (see config.SCENE_ZSTD_LEVEL for the
    measurements). Worse, it optimised the wire even when there was no wire
    saving available at all.

    Two things are decided here:

    **Is the payload already compressed?** A .blend saved with Blender's
    "Compress File" preference IS a zstd frame, and packed EXR/PNG textures are
    incompressible whatever the container. Re-compressing either is pure CPU
    for nothing — measured at 59 s for a 1.06x ratio on a 602 MB blend. Caught
    two ways: the magic number, which is definitive and free, and failing that
    a probe, which catches incompressible content inside an uncompressed
    container.

    **Otherwise, what level?** config.SCENE_ZSTD_LEVEL, which is the measured
    optimum for this farm's CPU-to-link ratio rather than the best ratio
    available at any price.

    Never raises. Every failure here — an unreadable head, a missing zstd, a
    short file — falls back to the configured default, because a level is a
    performance choice and no performance choice is worth failing an upload
    over.
    """
    default = int(config.SCENE_ZSTD_LEVEL)
    cheap = int(config.SCENE_ZSTD_LEVEL_PRECOMPRESSED)
    try:
        size = scene.stat().st_size
        with open(scene, "rb") as fh:
            magic = fh.read(4)
            # zstd frame (Blender 3.0+ "Compress File") or gzip (older Blender).
            if magic[:4] == b"\x28\xb5\x2f\xfd" or magic[:2] == b"\x1f\x8b":
                kind = "zstd" if magic[:4] == b"\x28\xb5\x2f\xfd" else "gzip"
                blame = (" — saved with Blender's 'Compress File' on"
                         if scene.suffix == ".blend" else "")
                return cheap, (f"already a {kind} stream{blame}; "
                               f"level {cheap} (framing only)")
            if size < config.SCENE_ZSTD_PROBE_MIN_MB * 1e6:
                return default, f"level {default}"

            # Probe the MIDDLE, not the head: a .blend opens with a short
            # header and a run of pointer tables that compress like nothing
            # else in the file, so the first bytes are the least representative
            # sample available.
            span = int(config.SCENE_ZSTD_PROBE_MB * 1e6)
            fh.seek(max(0, (size - span) // 2))
            sample = fh.read(span)
        if len(sample) < 1 << 20:
            return default, f"level {default}"
        probed = subprocess.run(["zstd", "-3", "-T6", "-c"], input=sample,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=120)
        if probed.returncode != 0 or not probed.stdout:
            return default, f"level {default}"
        ratio = len(sample) / len(probed.stdout)
        if ratio < config.SCENE_ZSTD_MIN_RATIO:
            return cheap, (f"probes at {ratio:.2f}x — incompressible; "
                           f"level {cheap} instead of {default}")
        return default, f"probes at {ratio:.2f}x, level {default}"
    except Exception as exc:                       # noqa: BLE001 - see docstring
        log.debug("zstd level probe for %s failed (%s) — using level %d",
                  scene.name, exc, default)
        return default, f"level {default}"


def push_scene(ep: Endpoint, scene: Path, remote_path: str = "",
               level: Optional[int] = None) -> float:
    """Stream the scene through zstd into place. No temp file either end.

    `level` defaults to whatever `scene_zstd_level` measures for this file
    rather than to a constant; pass one explicitly to override.
    """
    remote_path = remote_path or f"{config.REMOTE_ROOT}/scene.blend"
    if level is None:
        level, why = scene_zstd_level(scene)
        log.info("compressing %s at zstd -%d (%s)", scene.name, level, why)
    started = time.time()
    zstd = subprocess.Popen(
        ["zstd", f"-{level}", "-T6", "-c", str(scene)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    ssh = subprocess.Popen(
        # NOT the multiplexed master. Measured 2026-08-04 on instance 46712525:
        # two consecutive 5.2 GB scene pushes died at 181.4 s and 168.7 s with
        # `ssh exit 255, no stderr | local zstd exit -13` (SIGPIPE), and both
        # retries — on a freshly built master — completed the same bytes in 94 s
        # and 118 s at 44-55 MB/s. The link was never the problem.
        #
        # `ssh_base` sets ServerAliveInterval=30 with ServerAliveCountMax=6, and
        # 30 x 6 = 180 s is the number both failures landed on. A bulk stream
        # saturating one channel of a multiplexed connection head-of-line-blocks
        # the master's own keepalive replies; six go unanswered and ssh tears
        # down the WHOLE master, which is why the transfer dies with no stderr
        # (LogLevel=ERROR eats "Timeout, server not responding") and zstd takes
        # a SIGPIPE. So this is not a flaky link that retrying fixes — it is a
        # hard 180 s ceiling on the mux path, and every scene big or slow enough
        # to need longer than that would fail forever, burn the deploy attempts
        # and start condemning perfectly good GPUs.
        #
        # A dedicated connection has nothing to be starved behind. This is the
        # same reasoning that already puts `push_parallel` and the frame fetch
        # on `ssh_nomux`, and the same reasoning as the job tunnel's own
        # connection; the scene push was the last bulk path still on the master.
        ssh_nomux(ep) + [f"zstd -d -o {shlex.quote(remote_path)} -f"],
        stdin=zstd.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if zstd.stdout:
        zstd.stdout.close()
    try:
        _, err = ssh.communicate(timeout=3600)
    except subprocess.TimeoutExpired:
        ssh.kill()
        zstd.kill()
        raise TransferError("scene push", str(ep), "no exit after 3600s", time.time() - started,
                            expected=scene.stat().st_size) from None
    zstd.wait()
    if ssh.returncode != 0:
        # Both halves matter: a local zstd that died mid-stream looks, from the
        # ssh side, exactly like a network drop.
        zerr = tail((zstd.stderr.read().decode(errors="replace") if zstd.stderr else ""))
        detail = tail(err.decode(errors="replace")) or f"ssh exit {ssh.returncode}, no stderr"
        if zstd.returncode:
            detail += f" | local zstd exit {zstd.returncode}: {zerr or 'no stderr'}"
        raise TransferError("scene push", str(ep), detail, time.time() - started,
                            expected=scene.stat().st_size)
    return time.time() - started


def push_parallel(ep: Endpoint, local: Path, remote_path: str, streams: int = 8,
                  concurrency: Optional[int] = None) -> float:
    """Upload one file over N concurrent SSH connections, joined remotely.

    A single SSH stream is capped by OpenSSH's fixed internal buffer divided by
    the round-trip time — at the 69 ms RTT measured to these hosts that is a few
    MB/s no matter how much bandwidth the link has. ESnet's guidance is to avoid
    scp/sftp/rsync entirely above 10 ms RTT for exactly this reason. Splitting
    the file across independent TCP connections sidesteps the per-connection
    window, which is the same trick Globus and scp-chunk use.

    Each chunk lands in its own `.partN` file and is verified by size before the
    parts are concatenated, so a torn chunk fails loudly rather than producing a
    corrupt archive.

    Every chunk reports its own exit code, stderr tail, bytes sent and elapsed
    time, and they are all carried into the raised `TransferError`. A stream
    that stops moving bytes is killed by `STALL_SEC` rather than blocking
    forever in `stdin.write`, which has no timeout of its own — an observed
    failure mode that left an orphan ssh wedged for over twelve minutes and gave
    the broker nothing at all to log.

    **Resumable.** Every part that survived a previous attempt is kept and only
    its missing tail is sent. This is not an optimisation, it is the difference
    between a flaky link finishing and a flaky link never finishing. `push_blender`
    has always documented itself as resuming "from the byte offset the remote
    already holds" — but it delegates here, and this function used to open by
    deleting every `.partN` it found, so each of its four attempts restarted from
    zero. Measured 2026-08-02 against machine 55313: attempts reached 20.9%, 6.8%,
    7.0%, 6.8% of a 481 MB bundle and then threw all of it away, so a link that
    could move 80 MB before dropping could never deliver 481 MB no matter how many
    times it tried. Twelve attempts across two instances moved roughly 600 MB and
    delivered nothing.

    Resume is keyed on a signature of (size, stream count) left on the instance,
    so partials from a different bundle — or from a run that split it differently
    — are discarded rather than spliced into a plausible-looking corrupt archive.
    The joined result is verified by sha256 against the local file, not by size
    alone: appending to a stale part is exactly the class of mistake that
    produces a file of precisely the right length and the wrong contents.

    `streams` is how the FILE IS SPLIT; `concurrency` is how many of those parts
    are in flight at once. They are separate on purpose. Resume is keyed on
    `(size, streams)`, so lowering the split count invalidates every part
    already on the instance — which would make the single-connection fallback
    throw away exactly the bytes it is trying to rescue. Keeping the split at 8
    and sending one part at a time asks the same question ("is our concurrency
    what the far end objects to?") while every byte already delivered stays
    valid and resumable.
    """
    started = time.time()
    total = local.stat().st_size
    streams = max(1, min(streams, 8))          # sshd MaxStartups is 10 by default
    concurrency = max(1, min(concurrency or streams, streams))
    chunk = -(-total // streams)               # ceil

    q = shlex.quote(remote_path)
    sig = f"{total}:{streams}"
    sig_path = f"{remote_path}.parts"

    # What, if anything, survived the last attempt? One round trip, and it must
    # be a `probe`: an ssh that never ran returns the same empty output as "no
    # parts here", and treating an unreachable host as an empty one is how a
    # resume silently becomes a restart.
    have: dict[int, int] = {}
    state = probe(ep, f"cat {shlex.quote(sig_path)} 2>/dev/null; echo ---; "
                      f"stat -c '%n %s' {q}.part[0-9] 2>/dev/null || true", timeout=120)
    if not state.ok:
        # Could not READ the resume state. That is not the same as "there is
        # nothing to resume", and conflating the two undoes the whole feature:
        # every branch below treats an empty `have` as an empty instance, and
        # the `else` branch then deletes every part that survived. The command
        # cannot fail on its own — it is `cat ... 2>/dev/null; echo ---; stat
        # ... || true`, which exits 0 whatever is on the disk — so a non-ok
        # answer is ssh, and ssh being down is precisely when the bytes already
        # on the far end are most worth keeping. Fail the attempt instead; the
        # next one re-reads the state and appends to what is there.
        raise SshError(state)

    head, _, listing = state.out.partition("---")
    if head.strip() == sig:
        for line in listing.splitlines():
            name, _, size = line.strip().rpartition(" ")
            if not name.startswith(f"{remote_path}.part"):
                continue
            try:
                index, count = int(name.rsplit("part", 1)[1]), int(size)
            except ValueError:
                continue
            expected = max(0, min(chunk, total - index * chunk))
            # Longer than it should be means a torn or foreign part. There is
            # no safe prefix to keep, so keep nothing.
            if 0 < count <= expected:
                have[index] = count

    if have:
        log.info("resuming push of %s: %d/%d parts hold %.0f/%.0f MB already",
                 local.name, len(have), streams, sum(have.values()) / 1e6, total / 1e6)
    else:
        # Nothing usable. Clear the ground, including any earlier joined file,
        # and stamp the signature this run's parts belong to.
        run(ep, f"rm -f {q}.part* {q}", check=False)
    run(ep, f"printf '%s\\n' {shlex.quote(sig)} > {shlex.quote(sig_path)}", check=False)

    @dataclass
    class Chunk:
        index: int
        expected: int = 0
        sent: int = 0
        rc: int = 0
        err: str = ""
        elapsed: float = 0.0
        stalled: bool = False

        @property
        def ok(self) -> bool:
            return self.rc == 0 and not self.stalled and self.sent == self.expected

        def describe(self) -> str:
            why = "stalled" if self.stalled else f"exit {self.rc}"
            return (f"part{self.index}: {why} after {self.elapsed:.1f}s, "
                    f"{self.sent}/{self.expected} bytes"
                    f"{' — ' + self.err if self.err else ''}")

    def send(index: int) -> Chunk:
        offset = index * chunk
        length = max(0, min(chunk, total - offset))
        result = Chunk(index=index, expected=length)
        if length <= 0:
            return result

        # Bytes this part already holds from an earlier attempt. Counted as
        # `sent` so `ok` and the percentage in the error still mean "how much of
        # this part is on the far end", which is the question either one is
        # asked.
        already = have.get(index, 0)
        result.sent = already
        if already >= length:
            return result
        remaining_total = length - already

        began = time.time()
        part = f"{remote_path}.part{index}"
        # Append to what survived; truncate only when starting this part fresh.
        redirect = ">>" if already else ">"
        proc = subprocess.Popen(
            ssh_nomux(ep) + [f"head -c {remaining_total} {redirect} {shlex.quote(part)}"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

        # Progress watchdog. `stdin.write` on a wedged connection blocks with no
        # timeout, and ssh's keepalives cannot tell an idle-but-open TCP window
        # from a healthy one, so nothing else here would ever notice.
        progress = [time.time()]
        done = threading.Event()

        def watch() -> None:
            while not done.wait(5.0):
                if time.time() - progress[0] > STALL_SEC:
                    result.stalled = True
                    proc.kill()               # unblocks the write with EPIPE
                    return

        watcher = threading.Thread(target=watch, name=f"stall-{index}", daemon=True)
        watcher.start()
        try:
            with open(local, "rb") as fh:
                fh.seek(offset + already)
                remaining = remaining_total
                try:
                    while remaining > 0:
                        buf = fh.read(min(1 << 20, remaining))
                        if not buf:
                            result.err = "local file ended early"
                            break
                        proc.stdin.write(buf)      # type: ignore[union-attr]
                        remaining -= len(buf)
                        result.sent += len(buf)
                        progress[0] = time.time()
                    proc.stdin.close()             # type: ignore[union-attr]
                except (BrokenPipeError, OSError) as exc:
                    result.err = diagnose(exc)
        finally:
            done.set()

        stderr_text = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        try:
            proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            result.stalled = True
        result.rc = proc.returncode if proc.returncode is not None else -1
        result.elapsed = time.time() - began
        chunk_err = tail(stderr_text)
        if chunk_err:
            result.err = f"{result.err} | {chunk_err}" if result.err else chunk_err
        if result.stalled and not result.err:
            result.err = f"no bytes moved for {STALL_SEC:.0f}s"
        return result

    with futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(send, range(streams)))

    sent_total = sum(c.sent for c in results)
    bad = [c for c in results if not c.ok]
    if bad:
        # Did every stream die, and die because the far end hung up? Both
        # halves matter. A subset failing, or failures that are stalls and
        # local errors, is a link behaving badly and is worth retrying as-is.
        # All of them going down together on a peer reset is the far end
        # closing connections, and the fleet needs to be able to tell those
        # apart without re-parsing this message.
        reset_all = len(bad) == streams and all(
            any(m in c.err for m in RESET_MARKERS) for c in bad
        )
        raise TransferError(
            f"parallel push of {local.name} ({len(bad)}/{streams} parts, "
            f"{concurrency} at a time)",
            str(ep), "; ".join(c.describe() for c in bad), time.time() - started,
            sent=sent_total, expected=total,
            streams=streams, reset_all=reset_all,
        )

    parts = " ".join(f"{shlex.quote(remote_path)}.part{i}" for i in range(streams))
    joined = probe(ep, f"cat {parts} > {shlex.quote(remote_path)} && rm -f {remote_path}.part*",
                   timeout=600)
    if not joined.ok:
        raise TransferError(f"joining {streams} parts of {local.name}", str(ep),
                            joined.describe(), time.time() - started,
                            sent=sent_total, expected=total)

    stat = probe(ep, f"stat -c %s {shlex.quote(remote_path)}", timeout=120)
    remote_size = -1
    if stat.ok and stat.out.split():
        try:
            remote_size = int(stat.out.split()[-1])
        except ValueError:
            remote_size = -1
    if remote_size != total:
        raise TransferError(
            f"size check of {local.name}", str(ep),
            f"remote reports {remote_size if remote_size >= 0 else 'unreadable'} "
            f"vs {total} local ({stat.describe()})",
            time.time() - started, sent=max(remote_size, 0), expected=total,
        )

    # Content, not just length. Resume splices bytes from several attempts into
    # one file, so "the right number of bytes arrived" stops being evidence that
    # the right bytes arrived — and the way this fails is a Blender tarball that
    # is exactly 481485662 bytes and does not extract. Cheap next to the upload:
    # a few seconds of local hashing and a couple on the instance.
    digest = probe(ep, f"sha256sum {shlex.quote(remote_path)}", timeout=600)
    remote_sum = digest.out.split()[0] if digest.ok and digest.out.split() else ""
    local_sum = file_sha256(local)
    if remote_sum != local_sum:
        # Nothing here is salvageable and leaving it invites the next attempt to
        # "resume" onto poisoned bytes.
        run(ep, f"rm -f {q} {q}.part*", check=False)
        raise TransferError(
            f"sha256 check of {local.name}", str(ep),
            f"remote {remote_sum or 'unreadable'} vs local {local_sum} "
            f"({digest.describe()}) — the transferred file is the right size and "
            f"the wrong contents; every part has been discarded",
            time.time() - started, sent=0, expected=total,
        )
    return time.time() - started


def push_blender(ep: Endpoint, bundle: Path, attempts: int = 4) -> float:
    """Serve the Blender build from this machine instead of blender.org.

    Two reasons beyond speed: the remote build ends up byte-identical to the one
    that assembled the scene, and it removes a third-party download from the
    critical path of every cold start.

    Resumable by design. Half a gigabyte over a single SSH stream takes minutes
    and does drop in practice; restarting from zero each time is how a transient
    hiccup turns into an unbounded retry loop. rsync is not available yet at
    this point (provisioning installs it afterwards), so resume is done by
    appending from the byte offset the remote already holds.

    **Falls back to ONE CONNECTION AT A TIME when the far end keeps hanging
    up.** The eight streams exist for robustness, not speed — measured 4.02 MB/s
    parallel against 4.68 MB/s single — so serialising them costs nothing worth
    having, and it answers a question no amount of retrying at eight can: a lone
    connection cannot trip a concurrency or connection-rate limit, so if it is
    reset the same way, the host resets whatever it is given. That verdict
    travels up as `chronic` and is what licenses the fleet to stop retrying
    here. Only `reset_all` failures count toward the fallback: a stall or a
    subset of streams failing is an ordinary bad link, and answering that by
    halving our own throughput would make a recoverable transfer worse.

    The file is still split eight ways while it is sent one part at a time —
    resume is keyed on the split, so narrowing that would discard every byte
    already on the instance to run the experiment.

    Reports the high-water mark of bytes the INSTANCE holds as `sent`, not the
    bytes this attempt pushed. With resume, those diverge, and the first is the
    only one that answers "is this transfer getting anywhere?" — which is the
    question the deploy-retry policy is built on.
    """
    started = time.time()
    remote_path = f"{config.REMOTE_ROOT}/blender.tar.zst"
    total = bundle.stat().st_size
    failures: list[str] = []
    streams = config.PUSH_STREAMS
    concurrency = streams   # how many parts are in flight; the split never changes
    resets = 0            # consecutive all-streams-reset failures at this width
    delivered = 0         # high-water mark of bytes the instance holds
    chronic = False

    for attempt in range(1, attempts + 1):
        stat = probe(ep, f"stat -c %s {shlex.quote(remote_path)} 2>/dev/null || echo 0",
                     timeout=120)
        if stat.transport_failed:
            # The host is not answering at all. Pushing 481 MB at it is not the
            # next thing to try, and reporting "push failed" for it would name
            # the wrong problem entirely.
            raise SshError(stat)
        already = 0
        if stat.ok and stat.out.split():
            try:
                already = int(stat.out.split()[-1])
            except ValueError:
                already = 0
        if already == total:
            return time.time() - started
        delivered = max(delivered, already)

        try:
            push_parallel(ep, bundle, remote_path, streams=streams,
                          concurrency=concurrency)
            return time.time() - started
        except Exception as exc:
            # A dropped chunk is a transport problem, not a broken host. Retry
            # the transfer rather than escalating to replacing the GPU — but
            # keep every attempt's reason, because "it failed four times" is
            # only useful alongside *how* it failed each time.
            failures.append(f"attempt {attempt}/{attempts} at {concurrency} "
                            f"connection(s) at a time: {diagnose(exc)}")
            delivered = max(delivered, getattr(exc, "sent", 0) or 0)
            if getattr(exc, "reset_all", False):
                resets += 1
                if concurrency > 1 and resets >= config.PUSH_SERIAL_AFTER:
                    log.warning(
                        "%s: all %d streams have now been reset by the far end "
                        "%d time(s) in a row. Falling back to ONE CONNECTION AT "
                        "A TIME — parallel buys robustness, not throughput, and "
                        "one connection cannot be tripping a connection-rate or "
                        "MaxStartups limit. If this is reset too, the host is "
                        "the problem and not our concurrency.",
                        ep, streams, resets,
                    )
                    concurrency, resets = 1, 0
                elif concurrency == 1:
                    chronic = True
            else:
                resets = 0
            if attempt < attempts:
                time.sleep(3 * attempt)

    raise TransferError(
        f"blender bundle push ({total / 1e6:.0f} MB)", str(ep),
        " || ".join(failures) or "all attempts failed without raising",
        time.time() - started, sent=delivered, expected=total,
        streams=concurrency, chronic=chronic,
    )


def push_file(ep: Endpoint, local: Path, remote: str) -> None:
    started = time.time()
    cmd = ["scp", "-P", str(ep.port), "-i", str(config.SSH_KEY),
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=20",
           "-o", f"ControlPath={ep.ctl_path}",
           str(local), f"root@{ep.host}:{remote}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise TransferError(f"scp {local.name} -> {remote}", str(ep),
                            "no exit after 600s", time.time() - started,
                            expected=local.stat().st_size) from None
    if proc.returncode != 0:
        raise TransferError(
            f"scp {local.name} -> {remote}", str(ep),
            tail(proc.stderr) or f"scp exit {proc.returncode} with no stderr",
            time.time() - started, expected=local.stat().st_size,
        )


def push_assets(ep: Endpoint, dirs: list[Path]) -> tuple[int, int]:
    """Mirror local asset directories to the instance at identical ABSOLUTE paths.

    Blender resolves an unpacked external reference against the path stored in
    the blend, so the only way a remote render can match a local one is for that
    exact path to exist on the instance. Returns (files, bytes) copied.
    """
    files = 0
    total = 0
    for d in dirs:
        if not d.is_dir():
            continue
        members = sorted(p for p in d.rglob("*") if p.is_file())
        if not members:
            continue
        wanted = {str(p.parent) for p in members}
        mk = probe(ep, " && ".join(f"mkdir -p {shlex.quote(w)}" for w in sorted(wanted)),
                   timeout=120)
        if not mk.ok:
            raise SshError(mk)
        for src in members:
            # scp, size-verified by push_file's own error path; these are small
            # (a few hundred KB of EXR) so parallel chunking would be overhead.
            push_file(ep, src, str(src))
            files += 1
            total += src.stat().st_size
    return files, total


def read_progress(ep: Endpoint) -> Optional[dict]:
    """The worker's current render progress, or None if unreadable.

    Polled over SSH rather than asked over the job socket, because the worker is
    strictly serial: during a render the main thread is inside
    `bpy.ops.render.render()` and cannot answer even a ping. A 35-minute 8K
    frame was therefore indistinguishable from a wedged one.

    Returns None rather than raising — progress is a nicety, and a failed poll
    must never be able to fail a render that is going fine. **For the same
    reason nothing may decide anything from this None**: it means "no progress
    to display", and cannot tell an idle worker from an unreachable one. Code
    that acts on the answer takes `activity()`.
    """
    return activity(ep, attempts=1).progress


def finished_png_info(ep: Endpoint, job_id: str) -> Optional[dict]:
    """A finished render already sitting on the instance: its size and sha256,
    or None when there is no *stable, complete* file to collect.

    The worker writes its PNG to disk independently of the socket that asked for
    it, so a frame survives the broker losing interest in it entirely — which is
    not hypothetical: job 54ed3b8bd22f was marked `failed` at sample 6896/8192
    and rendered on to completion with nobody waiting for it. Asking this before
    re-rendering turns that from 40 wasted GPU-minutes into one probe.

    Two things beyond a bare `stat`, both because a bare `stat` lied:

      * **Stability.** A single size check races the worker: a PNG still being
        written stats as "finished", gets fetched short, and the fetch failure
        is then blamed on the network. The size is read, the file hashed, and
        the size read again — one round trip — and any disagreement means the
        file is in flight, which is answered with None, not with a number.
      * **The digest.** The normal reply carries `png.sha256` computed by the
        worker; a reply rebuilt from the filesystem used to carry nothing, so a
        recovered frame — precisely the fetch most likely to happen over a
        flapping link — was the one frame whose transfer could not be verified.
        Hashing it on the instance restores that check for the recovery path.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        return None
    out = f"{config.REMOTE_ROOT}/out/{job_id}.png"
    q = shlex.quote(out)
    ran = probe(
        ep,
        f"a=$(stat -c %s {q} 2>/dev/null || echo 0); "
        f"h=$(sha256sum {q} 2>/dev/null | cut -d' ' -f1); "
        f"b=$(stat -c %s {q} 2>/dev/null || echo 0); "
        f'echo "$a $b $h"',
        timeout=120,
    )
    if not ran.ok:
        return None
    parts = ran.out.split()
    if len(parts) < 2:
        return None
    try:
        before, after = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if before <= 0 or before != after:
        return None            # absent, or growing under the worker's pen
    sha = parts[2] if len(parts) > 2 and re.fullmatch(r"[0-9a-f]{64}", parts[2]) else ""
    return {"bytes": before, "sha256": sha, "path": out}


def finished_png(ep: Endpoint, job_id: str) -> int:
    """Size of a finished render on the instance, or 0. Compatibility wrapper —
    prefer `finished_png_info`, which also proves the file is stable and returns
    the digest that makes the fetch verifiable."""
    info = finished_png_info(ep, job_id)
    return info["bytes"] if info else 0


# Every way Blender says "I could not find that file", as printed by Blender
# 5.2 and captured verbatim from a real load on 2026-08-04.
#
# This list is a list because the version of this check that shipped was ONE
# STRING — `Image file <path> does not exist` — and its own docstring named the
# failure it could not see: "the broker returns a subtly wrong frame and logs
# nothing". A missing *image* prints that. A missing *library* prints none of
# it; it prints `Cannot find lib '<path>'`, and so a scene that linked its
# entire set of grandstands out of another .blend rendered a strip of sky over
# pure black in 0.83 s, passed the blank gate, and was reported done.
#
# The shape of that defect is a check that names the right class of failure and
# then tests for one instance of it. So each pattern below is paired with the
# observation that produced it, and `test_missing_asset_patterns_cover_blender`
# fails if a captured line stops matching.
#
#   Warning: Unable to open '<path>': No such file or directory   library, first
#   Info: Cannot find lib '<path>'                                library, named
#   Info: LIB: Collection: 'X' missing from '<path>'              per datablock
#   Warning: N libraries and M linked data-blocks are missing     summary
#   Warning: Image file <path> does not exist                     image
ASSET_MISS_PATTERNS = (
    r"Image file [^ ]+ does not exist",
    r"Cannot find lib '[^']+'",
    r"Unable to open '[^']+': No such file or directory",
    r"LIB: [A-Za-z]+: '[^']*' missing from '[^']+'",
    r"[0-9]+ libraries and [0-9]+ linked data-blocks are missing",
)


def missing_assets(ep: Endpoint) -> list[str]:
    """Lines where Blender said it could not resolve an external file.

    Returned as the matched TEXT, not as bare paths. The previous version
    stripped its one pattern down to a path, which read well in a log line and
    threw away the only thing that distinguishes a missing HDRI — cosmetic, the
    frame may still be what was wanted — from a missing linked .blend, which
    means the geometry is not in the render at all.
    """
    pattern = "|".join(ASSET_MISS_PATTERNS)
    ran = probe(
        ep,
        f"grep -aoE {shlex.quote(pattern)} {config.REMOTE_ROOT}/worker.log "
        f"2>/dev/null | sort -u || true",
        timeout=60,
    )
    return [ln.strip() for ln in ran.out.splitlines() if ln.strip()] if ran.ok else []


# Substrings that mean "a linked .blend was not found", as opposed to a missing
# texture. Kept separate from the patterns above because the two demand
# different responses: a missing image is a warning, a missing library is a
# scene that cannot be rendered.
LIBRARY_MISS_MARKERS = ("Cannot find lib '", "libraries and", "missing from '",
                        "Unable to open '")


def missing_libraries(ep: Endpoint) -> list[str]:
    """The subset of `missing_assets` that means linked geometry is absent.

    Also matches the worker's own `MISSING LIBRARY:` line, which is the
    authoritative one: it comes from `bpy.data.libraries[...].is_missing` rather
    than from parsing Blender's console output, and it is printed at load,
    before any render.
    """
    found = [ln for ln in missing_assets(ep)
             if any(m in ln for m in LIBRARY_MISS_MARKERS)]
    ran = probe(
        ep,
        f"grep -a 'MISSING LIBRARY:' {config.REMOTE_ROOT}/worker.log 2>/dev/null "
        f"| sort -u || true",
        timeout=60,
    )
    if ran.ok:
        found += [ln.strip() for ln in ran.out.splitlines() if ln.strip()]
    return sorted(set(found))


# Parallel byte-range fetching was tried here and MEASURED SLOWER. Eight
# `tail -c +N | head -c LEN` streams pulled 8 MB in 20.0 s against 10.6 s for a
# single `cat` over the shared master, and 25 MB at 0.67 MB/s. The idea came
# from a micro-benchmark that used `dd bs=1`, which is pathologically slow and
# made the single-stream baseline look like 0.19 MB/s when it is really ~0.8.
#
# It is the same answer this project already recorded for parallel *upload*:
# no gain. Kept as a comment so nobody re-derives it from the RTT alone —
# 90 ms genuinely does suggest window limiting, and the suggestion is wrong
# here.


def fetch_file(ep: Endpoint, remote: str, local: Path, attempts: int = 4) -> int:
    """Fetch a result, atomically and size-verified.

    Both guards are here because of an observed failure: scp reported an error
    partway through a 1.9 MB PNG but still left a 783 KB truncated file at the
    destination. A silently corrupt render is worse than a missing one — it
    looks like a finished frame. So download to a temp path, require the byte
    count to match the source exactly, and only then move it into place.

    Retried because these drops are transient: the render itself took 2.7
    seconds and re-rendering it is far cheaper than losing it, but re-fetching
    is cheaper still.
    """
    started = time.time()
    local.parent.mkdir(parents=True, exist_ok=True)
    stat = probe(ep, f"stat -c %s {shlex.quote(remote)}", timeout=120)
    if not stat.ok or not stat.out.split():
        raise TransferError(f"sizing {remote}", str(ep), stat.describe(),
                            time.time() - started)
    try:
        expect = int(stat.out.split()[-1])
    except ValueError:
        raise TransferError(f"sizing {remote}", str(ep),
                            f"unparseable stat output: {stat.describe()}",
                            time.time() - started) from None

    tmp = local.with_suffix(local.suffix + ".part")
    failures: list[str] = []

    # Attempt 0: stream the bytes over the ControlMaster with `cat`, no scp.
    #
    # scp is a protocol on top of SSH — it starts a remote `scp -f`, exchanges
    # file metadata, then transfers. Measured against a live instance on a
    # 120 KB file: 7.3 s with its own connection, 4.6 s multiplexed, **1 s**
    # like this. The difference is all protocol and handshake; the payload is
    # noise at these sizes.
    #
    # Safe precisely because nothing here trusts the transfer: the size is
    # compared against the source and the caller compares a sha256 the worker
    # computed on the file it wrote. A short or corrupt read fails those checks
    # and falls through to scp, which is kept as the fallback rather than
    # removed — it has years of edge cases handled that a pipe does not.
    began = time.time()
    tmp.unlink(missing_ok=True)
    try:
        with open(tmp, "wb") as fh:
            proc = subprocess.run(
                ssh_base(ep) + [f"cat {shlex.quote(remote)}"],
                stdout=fh, stderr=subprocess.PIPE, timeout=900,
            )
        if proc.returncode == 0 and tmp.stat().st_size == expect:
            tmp.replace(local)
            return expect
        failures.append(
            f"stream: ssh exit {proc.returncode} after {time.time() - began:.1f}s, "
            f"{tmp.stat().st_size if tmp.exists() else 0}/{expect} bytes — "
            f"{tail(proc.stderr.decode(errors='replace')) or 'no stderr'}"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        failures.append(f"stream: {diagnose(exc)}")

    for attempt in range(1, attempts + 1):
        began = time.time()
        tmp.unlink(missing_ok=True)
        # First attempt rides the shared ControlMaster; retries get their own
        # connection.
        #
        # Measured on a live instance: fetching a 120 KB file over a fresh
        # connection took **7.3 s**, essentially all of it TCP setup, key
        # exchange and auth — the file itself is nothing. Over a 3,000-frame
        # sequence that is five hours of wall clock and about $1.70 of GPU
        # sitting idle waiting for handshakes. Every other small command in this
        # module already reuses the master; there was no reason for this one not
        # to, beyond it having been copied from `push_parallel`, where a private
        # connection genuinely matters because eight of them run at once and
        # must not share a congestion window.
        #
        # The retry still gets a private connection, so a reaped or wedged
        # master costs one slow fetch rather than the frame.
        mux = (["-o", f"ControlPath={ep.ctl_path}", "-o", "ControlMaster=auto"]
               if attempt == 1 else
               ["-o", "ControlMaster=no", "-o", "ControlPath=none"])
        try:
            proc = subprocess.run(
                ["scp", "-P", str(ep.port), "-i", str(config.SSH_KEY),
                 "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "LogLevel=ERROR", *mux,
                 "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
                 "-o", "ServerAliveCountMax=4",
                 f"root@{ep.host}:{remote}", str(tmp)],
                capture_output=True, text=True, timeout=900,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"attempt {attempt}/{attempts}: no exit after 900s")
            continue
        if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size == expect:
            tmp.replace(local)
            return expect
        got = tmp.stat().st_size if tmp.exists() else 0
        failures.append(
            f"attempt {attempt}/{attempts}: scp exit {proc.returncode} after "
            f"{time.time() - began:.1f}s, {got}/{expect} bytes"
            f" — {tail(proc.stderr) or 'no stderr'}"
        )
        if attempt < attempts:
            time.sleep(2 * attempt)

    got = tmp.stat().st_size if tmp.exists() else 0
    tmp.unlink(missing_ok=True)
    raise TransferError(f"fetch {remote}", str(ep), " || ".join(failures),
                        time.time() - started, sent=got, expected=expect)


# --- worker ---------------------------------------------------------------

# Shell snippet listing ONLY real worker processes, by pid, space separated.
#
# `pgrep -f '<root>/server.py'` cannot be used on its own, and this was verified
# against a live instance rather than reasoned about: the pattern also matches
# the `bash -c` that sshd spawned to run the pgrep, because sshd puts the whole
# command string in that shell's argv. Measured there — 4 matches for one
# worker: the blender process, a wedged subshell, and the checking shell itself.
#
# Two consequences, both of which were live bugs. The count was never 0, so the
# "did the old worker die?" check could never legitimately pass; and
# `pkill -9 -f '<root>/server.py'` SIGKILLed the very shell running the check,
# which is the only reason the check *appeared* to pass — the shell died before
# printing, and empty stdout read as "no survivors".
#
# `comm` disambiguates: the worker is `blender`, every false positive is `bash`.
WORKER_PIDS = (
    'for p in $(pgrep -f {pattern} 2>/dev/null); do '
    'case "$(cat /proc/$p/comm 2>/dev/null)" in blender*) printf "%s " "$p";; esac; '
    'done'
)

# The launch forks and returns; anything approaching a minute means it is wedged.
# The old value was 600 s and it was being hit in full, every time.
LAUNCH_TIMEOUT = 60.0


def worker_launch_cmd(root: str, port: int, scene: str = "", blender: str = "") -> str:
    """The remote command that starts the worker, fully detached from SSH.

    Its exact shape is load-bearing, so it is built here where it can be tested
    without an instance. `blender` overrides the executable, for those tests.
    """
    exe = blender or f"{root}/blender/blender"
    # The scene is explicit because the worker holds exactly one loaded scene
    # and switching means relaunching it against a different .blend.
    blend = scene or f"{root}/scene.blend"
    args = (f"{exe} -b {shlex.quote(blend)} -P {root}/server.py "
            f"-- --port {port} --out-dir {root}/out "
            f"--progress {root}/progress.json")
    env = f"env OPTIX_CACHE_PATH={root}/optixcache TMPDIR={root}/tmp"
    io = f"< /dev/null > {root}/worker.log 2>&1"
    # Note the `;` before the fallback's `&`: it makes the `&` bind to that one
    # command instead of to a whole `&&` chain, which is the entire bug.
    return (
        f"mkdir -p {root}/tmp; cd {root}; "
        f"if command -v setsid >/dev/null 2>&1 && setsid --fork true 2>/dev/null; then "
        f"setsid --fork {env} {args} {io}; "
        f"else "
        f"nohup {env} {args} {io} & "
        f"fi"
    )


# How stale progress.json may be while still describing a live render. The
# worker rewrites it at every adaptive-sampling checkpoint, and one measured
# batch covered 191 samples in a single 22 s step, so this is generous.
PROGRESS_MAX_AGE = 300.0


@dataclass
class Activity:
    """What the instance says the worker is doing — in THREE states, not two.

    `reachable` is the field that matters. **"I could not ask" is not an answer
    to "is it rendering?"**, and every place this project collapsed those two
    into one boolean has cost a frame or a GPU:

      * the idle timer read its own failed poll as an idle GPU and stopped an
        instance that was at 99% and 420 W on an 8K frame;
      * the reattach loop read one unreadable poll as "the instance is not
        rendering this job either, so the worker really is gone" and requeued a
        render that was 15% done — twice, in the log for job 54ed3b8bd22f;
      * the deploy path read it as "nothing running here", walked into
        `start_worker`, and was saved only by the WorkerBusy guard — which then
        failed the job the guard had just protected.

    So `rendering`, `idle` and `unknown` are three distinct answers, and only
    `idle` — reachable, parsed, and definitely not rendering — licenses anything
    destructive.
    """

    reachable: bool
    progress: Optional[dict] = None
    age: Optional[float] = None
    detail: str = ""

    @property
    def state(self) -> Optional[str]:
        return (self.progress or {}).get("state")

    @property
    def job_id(self) -> Optional[str]:
        return (self.progress or {}).get("job_id")

    @property
    def fresh(self) -> bool:
        return self.age is not None and self.age <= PROGRESS_MAX_AGE

    @property
    def rendering(self) -> bool:
        """Definitely rendering right now. Never true when merely unreachable."""
        return self.reachable and self.state == "rendering" and self.fresh

    @property
    def idle(self) -> bool:
        """Definitely NOT rendering. The only state that may authorise a kill,
        a redeploy or a hibernate."""
        return self.reachable and not self.rendering

    @property
    def unknown(self) -> bool:
        return not self.reachable

    def renders(self, job_id: str) -> bool:
        return self.rendering and self.job_id == job_id

    def describe(self) -> str:
        if not self.reachable:
            return f"could not ask the instance what it is doing: {self.detail}"
        if self.rendering:
            return (f"rendering job {self.job_id} at sample "
                    f"{(self.progress or {}).get('sample')}/"
                    f"{(self.progress or {}).get('total')} "
                    f"(tile {(self.progress or {}).get('tile')}/"
                    f"{(self.progress or {}).get('tiles')}), "
                    f"{((self.progress or {}).get('elapsed_sec') or 0) / 60:.1f} min in")
        if self.progress is None:
            return "reachable, and publishing no progress at all"
        return (f"reachable and not rendering (state {self.state!r}, "
                f"last job {self.job_id}, file {self.age:.0f}s old)"
                if self.age is not None else
                f"reachable and not rendering (state {self.state!r})")


def activity(ep: Endpoint, attempts: int = 3,
             max_age: float = PROGRESS_MAX_AGE) -> Activity:
    """Ask the instance what the worker is doing. Never guesses.

    Read from progress.json over the SSH command channel, which keeps working
    when the forwarded job port does not — that asymmetry is the whole point.
    A serial worker *cannot* answer a ping mid-render, so the socket is useless
    as a liveness test exactly when liveness matters most.

    Retried, because this instance's SSH endpoint flaps: `connect to host ...
    port ...: Connection refused` three times in one hour while the GPU rendered
    without interruption. And when every attempt fails it says **unknown**, not
    "idle" — see `Activity`.

    Freshness is measured entirely on the instance (file mtime against the
    instance's own clock) because the rented box has been observed running
    minutes away from this machine's.
    """
    ran = None
    for attempt in range(1, max(1, attempts) + 1):
        ran = probe(
            ep,
            f"stat -c %Y {config.REMOTE_ROOT}/progress.json 2>/dev/null; date +%s; "
            f"cat {config.REMOTE_ROOT}/progress.json 2>/dev/null",
            timeout=45,
        )
        if ran.ok:
            break
        # A non-zero exit is NOT necessarily a failed probe, and reading it as
        # one made every never-rendered instance permanently `unknown`.
        #
        # The compound command's exit status is the status of its LAST element,
        # `cat progress.json`. On an instance that has not rendered yet there is
        # no progress.json, so `cat` exits 1 and the whole probe was scored as
        # unreachable — even though ssh had just run our command and `date`
        # had printed the answer on stdout:
        #
        #     exit 1 after 0.6s on 192.0.2.13:23972 [stat -c %Y ...]: 1785254527
        #
        # That is a complete, healthy answer meaning "nothing is rendering". The
        # branch below says exactly that ("the file is absent ... it is not
        # rendering") and was unreachable in the absent case, because this early
        # return fired first. So a fresh instance could never be called idle,
        # the deploy gate could never license replacing one, and any host that
        # broke after sshd came up wedged the broker until the watchdog reaped
        # it. Observed on instance 46121112, whose Blender died of SIGBUS.
        #
        # What actually means "we could not ask" is ssh failing to run the
        # command at all (rc 255) or coming back with nothing on stdout.
        if not ran.transport_failed and ran.out.strip():
            break
        if attempt < attempts:
            time.sleep(2 * attempt)
    if ran is None or ran.transport_failed or not ran.out.strip():
        return Activity(reachable=False,
                        detail=ran.describe() if ran else "no probe attempted")

    lines = ran.out.strip().splitlines()
    if len(lines) < 3:
        # The command ran and the file is absent or empty: that IS an answer —
        # the worker has published nothing, so it is not rendering.
        return Activity(reachable=True, detail="progress.json absent or empty")
    try:
        age = float(int(lines[1]) - int(lines[0]))
    except ValueError:
        return Activity(reachable=True, detail=f"unparseable clock/mtime: {lines[:2]}")
    try:
        data = json.loads("\n".join(lines[2:]))
    except json.JSONDecodeError as exc:
        # A half-written file is a torn read, not a statement about the GPU.
        # Treat it as unknown so nobody kills a render over a partial write.
        return Activity(reachable=False, age=age,
                        detail=f"progress.json did not parse: {diagnose(exc)}")
    if not isinstance(data, dict):
        return Activity(reachable=True, age=age, detail="progress.json is not an object")
    act = Activity(reachable=True, progress=data, age=age)
    if act.state == "rendering" and age > max_age:
        act.detail = (f"progress.json says rendering job {act.job_id} but is {age:.0f}s "
                      f"old (> {max_age:.0f}s) — treating that worker as long gone")
    return act


def rendering_now(ep: Endpoint, max_age: float = PROGRESS_MAX_AGE) -> Optional[dict]:
    """The job the worker is actively rendering, or None.

    Thin compatibility wrapper over `activity()`. **Prefer `activity()` in new
    code**: this signature cannot distinguish "not rendering" from "could not
    ask", which is the exact conflation that has cost this project three 8K
    frames and one stopped GPU. Every caller that makes a decision now takes the
    Activity.
    """
    act = activity(ep, max_age=max_age)
    return act.progress if act.rendering else None


def start_worker(ep: Endpoint, scene: str = "", force: bool = False) -> None:
    """Replace the running worker, verifying the old one actually died.

    Refuses to kill a worker that is actively rendering unless `force` is set.
    That guard exists because the broker spent this session killing healthy
    workers: a dropped tunnel was read as a dead worker, the "repair" was a
    redeploy, and the redeploy SIGKILLed a process that was 33 s into a
    40-minute 8K frame — three times in a row, until the job ran out of
    attempts. Restarting a worker is cheap; throwing away someone's render is
    not.

    `scene` is the .blend the new worker loads. Switching scenes is a relaunch
    rather than an in-process `open_mainfile`, because the dominant cost is the
    per-camera OptiX prewarm either way, and this path is already hardened:
    pid-verified kill, a real port-free check, and `setsid --fork` detachment.

    Getting this wrong is silently catastrophic rather than loud. If the old
    process survives, the new one dies binding an in-use port, `wait_worker`
    then gets a healthy ping *from the stale worker still holding the previous
    scene*, and every subsequent render returns the old geometry while the
    broker records the new scene hash as current. So: kill by pid, escalate to
    SIGKILL, and refuse to continue until the port is free.
    """
    root = config.REMOTE_ROOT
    port = config.WORKER_PORT
    pids = WORKER_PIDS.format(pattern=shlex.quote(f"{root}/server.py"))

    busy_before: Optional[Activity] = None
    if not force:
        busy_before = activity(ep)
        if busy_before.rendering:
            raise WorkerBusy(
                f"refusing to restart the worker on {ep}: it is actively rendering "
                f"job {busy_before.job_id} at sample {(busy_before.progress or {}).get('sample')}/"
                f"{(busy_before.progress or {}).get('total')} "
                f"(tile {(busy_before.progress or {}).get('tile')}/"
                f"{(busy_before.progress or {}).get('tiles')}), "
                f"{((busy_before.progress or {}).get('elapsed_sec') or 0) / 60:.1f} min in. "
                f"Killing it would discard that work — which is exactly how a 40-minute 8K "
                f"frame was lost three times. Pass force=True only when the render is "
                f"genuinely unwanted.",
                job_id=busy_before.job_id,
                progress=busy_before.progress,
            )

    # The guard above is a separate SSH round trip from the kill below, and on a
    # host whose SSH flaps those two can disagree: three failed probes report
    # "unknown", the flap ends, and the kill lands on a live render. So the kill
    # command re-checks progress.json **itself**, on the instance, in the same
    # invocation — there is no window between this check and the signal.
    #
    # `force` skips it, because a forced restart is an explicit decision to
    # discard the frame.
    guard = "" if force else (
        f'__mt=$(stat -c %Y {root}/progress.json 2>/dev/null || echo 0); __now=$(date +%s); '
        f'if [ "$__mt" -gt 0 ] && [ $((__now - __mt)) -le {int(PROGRESS_MAX_AGE)} ] && '
        f'grep -q \'"state": *"rendering"\' {root}/progress.json 2>/dev/null; then '
        f'echo "BUSY:$(sed -n \'s/.*"job_id": *"\\([^"]*\\)".*/\\1/p\' '
        f'{root}/progress.json 2>/dev/null | head -1)"; exit 0; fi; '
    )

    # Kill by pid, never `pkill -f` — see WORKER_PIDS. SIGTERM first so the
    # worker can close its socket, then SIGKILL what is left.
    killed = probe(
        ep,
        guard +
        f'p="$({pids})"; [ -n "$p" ] && kill $p 2>/dev/null; '
        f'for i in $(seq 1 20); do '
        f'  p="$({pids})"; [ -z "$p" ] && break; sleep 1; '
        f'done; '
        f'p="$({pids})"; [ -n "$p" ] && kill -9 $p 2>/dev/null; sleep 1; '
        f'echo "SURVIVORS:$({pids})"',
        timeout=120,
    )
    # An unanswered probe is not "zero survivors" — treating it as one is how a
    # stale worker keeps serving the previous scene while the broker records the
    # new hash as current.
    if not killed.ok:
        raise SshError(killed)
    still_busy = [ln for ln in killed.out.splitlines() if ln.startswith("BUSY:")]
    if still_busy:
        # The instance itself refused, atomically, with nothing signalled.
        running_id = still_busy[-1].split(":", 1)[1].strip() or None
        raise WorkerBusy(
            f"refusing to restart the worker on {ep}: the instance reported job "
            f"{running_id} still rendering at the moment of the kill, so nothing was "
            f"signalled. The pre-check said {busy_before.describe() if busy_before else 'nothing'} "
            f"— a flapping SSH endpoint can make those disagree, which is why the kill "
            f"re-checks on the box instead of trusting a separate round trip.",
            job_id=running_id,
            progress=(busy_before.progress if busy_before and busy_before.job_id == running_id
                      else None),
        )
    marker = [ln for ln in killed.out.splitlines() if ln.startswith("SURVIVORS:")]
    if not marker:
        raise RemoteError(
            f"could not confirm the previous worker died on {ep} — no marker in "
            f"output ({killed.describe()})"
        )
    survivors = marker[-1].split(":", 1)[1].split()
    if survivors:
        raise RemoteError(
            f"could not kill the previous worker on {ep}: pids {' '.join(survivors)} "
            f"still alive after SIGKILL"
        )

    # Verified on a live instance: `ss`, `netstat` AND `lsof` are all absent from
    # nvidia/cuda:12.8.0-base-ubuntu24.04. The old check,
    # `(ss -ltn || netstat -ltn) | grep -c ':8799 '`, therefore printed 0 every
    # single time and never tested anything at all — while the skill's rule is
    # explicitly "verify the process is dead AND the port is free", because a new
    # worker that dies binding an in-use port leaves the *stale* worker answering
    # pings with the previous scene loaded.
    #
    # /proc/net/tcp is always present. Column 2 is the local address as
    # HEX_IP:HEX_PORT, column 4 is the connection state, and 0A is LISTEN.
    port_hex = f"{port:04X}"
    listening = probe(
        ep,
        f"cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | "
        f"awk '$4==\"0A\" && $2 ~ /:{port_hex}$/ {{n++}} END {{print n+0}}'",
        timeout=60,
    )
    if not listening.ok or not listening.out.split():
        raise RemoteError(
            f"could not determine whether port {port} is free on {ep} — refusing to "
            f"start a second worker blind ({listening.describe()})"
        )
    bound = listening.out.split()[-1]
    if bound != "0":
        raise RemoteError(
            f"port {port} still bound on {ep} after killing the worker "
            f"({bound} listener(s) in /proc/net/tcp) — a new worker would die on "
            f"bind and the stale one would keep serving the old scene"
        )

    # Launch fully detached from the SSH session.
    #
    # The old form was `mkdir -p X && cd Y && VARS nohup blender ... > log 2>&1 &`
    # and it blocked the SSH command for the full 600 s timeout on a worker that
    # had in fact started perfectly — scene loaded, all four cameras prewarmed,
    # `ready on 127.0.0.1:8799` in worker.log. Every one of those timeouts was
    # the broker killing a healthy worker and redeploying over it.
    #
    # The cause is shell operator precedence, not stdin. `&` binds *looser* than
    # `&&`, so `A && B && C &` backgrounds the entire AND-list as one async
    # subshell. That subshell then runs blender in the foreground and sits in
    # wait(), and — critically — the `> log 2>&1` redirect applies only to the
    # blender command inside the list, not to the subshell, which therefore
    # keeps sshd's stdout/stderr pipes open. sshd never sees EOF, so ssh never
    # returns. Confirmed on the instance: the subshell was in `do_wait` with
    # fd 1 and 2 on pipes while blender's were on worker.log.
    #
    # Adding `< /dev/null` does nothing here: bash already redirects the stdin
    # of an async list to /dev/null when job control is off, which is exactly
    # what the instance showed (subshell fd 0 -> /dev/null). Measured locally,
    # both `< /dev/null` and `setsid` still hang while the `&&`-plus-`&` shape
    # remains.
    #
    # `setsid --fork` with no `&` at all: it forks, the parent exits
    # immediately, the shell exits, the pipes close, ssh returns in
    # milliseconds. It also puts the worker in its own session, so it cannot be
    # taken down with the SSH connection or the reaped ControlMaster. `env` is
    # required because setsid execs its argument directly and would otherwise
    # treat `VAR=value` as the program name. The `&` fallback keeps `;` before
    # it so the `&` binds to that one command only.
    launched = probe(ep, worker_launch_cmd(root, port, scene), timeout=LAUNCH_TIMEOUT)
    if not launched.ok:
        raise RemoteError(f"worker launch on {ep} — {launched.describe()}")

    # Forking successfully is not running successfully: setsid returns 0 even if
    # blender dies on exec. Catch that here, in seconds, rather than letting
    # wait_worker spin for its full 30 minutes against a process that never was.
    # Retried, and non-fatal when only the transport fails. This is a
    # nice-to-have check for "blender died on exec"; on a host whose SSH flaps
    # it must not be the thing that fails a job. Making it fatal turned a
    # transient `ssh exit 255` into a failed 8K render on its first live run.
    alive = None
    for attempt in (1, 2, 3):
        alive = probe(ep, f'sleep 3; echo "ALIVE:$({pids})"', timeout=60)
        if alive.ok:
            break
        if attempt < 3:
            time.sleep(3 * attempt)
    if alive is None or not alive.ok:
        # Could not ask. The launch itself returned cleanly and wait_worker will
        # catch a worker that never comes up, so proceed rather than condemn a
        # launch we merely failed to confirm.
        return
    running = [ln for ln in alive.out.splitlines() if ln.startswith("ALIVE:")]
    if not running or not running[-1].split(":", 1)[1].split():
        log_tail = probe(ep, f"tail -30 {root}/worker.log", timeout=60)
        raise RemoteError(
            f"worker on {ep} exited immediately after launch. worker.log: "
            f"{tail(log_tail.out) or log_tail.describe()}"
        )


def reap_stale_tunnels(local_port: int) -> int:
    """Kill orphaned `ssh -L <local_port>:...` processes left by a dead broker.

    `kill -9` is the ONLY sanctioned way to restart this broker — SIGTERM runs
    the shutdown path, which destroys the instance — so a killed broker cannot
    clean up after itself by construction. Its tunnel survives, holds the local
    port, and the next broker's forward dies with `Address already in use`.
    That is not an edge case; it is the documented restart procedure's
    guaranteed side effect, and it was being blamed on the rented hardware.

    Matches on the forward spec rather than on "ssh", so nothing else is
    touched — and never kills this process's own children, which are tracked
    and terminated properly.
    """
    pattern = f"{local_port}:127.0.0.1:{config.WORKER_PORT}"
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
        with contextlib.suppress(Exception):
            os.kill(pid, signal.SIGKILL)
            killed += 1
    if killed:
        time.sleep(1)      # let the kernel release the listener
    return killed


def open_tunnel(ep: Endpoint, local_port: int) -> subprocess.Popen:
    """Forward the worker's port to localhost so job dispatch is a plain socket
    write, with no per-job SSH process.

    Deliberately on its own connection, not the shared ControlMaster. Every job
    rides this forward, so tying its lifetime to a multiplexed socket that
    ControlPersist can reap — or that any other command can tear down — makes
    the whole queue fail with "connection refused" while the instance is
    perfectly healthy. That is what happened: the master was reused, the tunnel
    process exited, and the broker concluded the worker had died and redeployed.

    ExitOnForwardFailure turns a silently-unbound port into a process that exits
    immediately, so a failure is visible now rather than as a 30-minute
    wait_worker spin.
    """
    started = time.time()
    proc = subprocess.Popen(
        ssh_nomux(ep) + [
            "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-L", f"{local_port}:127.0.0.1:{config.WORKER_PORT}",
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    # Fail fast if the forward could not be established at all.
    last_os_error = ""
    for _ in range(30):
        if proc.poll() is not None:
            err = tail(proc.stderr.read().decode(errors="replace")) if proc.stderr else ""
            raise RemoteError(
                f"tunnel to {ep} exited {proc.returncode} after "
                f"{time.time() - started:.1f}s while forwarding "
                f"127.0.0.1:{local_port} -> :{config.WORKER_PORT}: "
                f"{err or 'ssh exited with no stderr'}"
            )
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=2):
                return proc
        except OSError as exc:
            last_os_error = diagnose(exc)
            time.sleep(1)

    # Returning a tunnel that never bound is worse than failing: every job then
    # gets "connection refused" from a healthy instance, and wait_worker spins
    # for its full 30-minute timeout before anyone finds out.
    proc.terminate()
    raise RemoteError(
        f"tunnel to {ep} never bound 127.0.0.1:{local_port} after "
        f"{time.time() - started:.1f}s (ssh still alive, forward to "
        f":{config.WORKER_PORT}): {last_os_error or 'no connection error recorded'}"
    )


def worker_call(payload: dict, port: int, timeout: float = 7200) -> dict:
    """Send one request and read one reply.

    A socket that dies raises `ConnectionDropped`, never a bare RuntimeError.
    The old message — "worker closed connection without replying" — asserted
    something this function cannot know: EOF on a forwarded port means the
    *forward* ended, and the worker at the far end may be rendering happily.
    Callers must recover the result instead of assuming the render was lost.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=60) as sock:
            sock.settimeout(timeout)
            sock.sendall(json.dumps(payload).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as exc:
        raise ConnectionDropped(
            f"job socket on 127.0.0.1:{port} failed: {diagnose(exc)} — this is the "
            f"tunnel, not necessarily the worker"
        ) from None
    if not buf.strip():
        raise ConnectionDropped(
            f"job socket on 127.0.0.1:{port} reached EOF with no reply — the SSH "
            f"forward ended mid-call; the worker may still be rendering"
        )
    try:
        return json.loads(buf)
    except json.JSONDecodeError as exc:
        raise ConnectionDropped(
            f"job socket on 127.0.0.1:{port} returned unparseable data "
            f"({len(buf)} bytes): {diagnose(exc)}"
        ) from None


@dataclass
class WaitResult:
    """Truthy on success, and carrying why not when it is not.

    `wait_worker` used to return a bare bool, so its caller could say only
    "worker never became ready" — with no elapsed time, no attempt count, and
    none of the connection errors it had been swallowing for up to 30 minutes.
    """

    ready: bool
    elapsed: float
    attempts: int
    last_error: str = ""
    tunnel_died: bool = False

    def __bool__(self) -> bool:
        return self.ready

    @property
    def local_bind_failed(self) -> bool:
        """The forward could not bind on THIS machine — our fault, not the host's.

        `kill -9` is the only sanctioned way to restart this broker, so an
        orphaned `ssh -L` from the previous process holding the local port is a
        routine consequence of the documented procedure, not an exotic race.
        The instance on the far end is blameless and must not be destroyed or
        blacklisted for it. Observed verbatim, and it cost two healthy GPUs and
        a wrongly condemned machine:

            bind [127.0.0.1]:8798: Address already in use
            channel_setup_fwd_listener_tcpip: cannot listen to port: 8798
            Could not request local forwarding.
        """
        err = self.last_error.lower()
        return ("address already in use" in err
                or "cannot listen to port" in err
                or "could not request local forwarding" in err)

    def describe(self) -> str:
        return (f"not ready after {self.elapsed:.0f}s and {self.attempts} pings: "
                f"{self.last_error or 'no error recorded — the port simply never answered ok'}")


def wait_worker(port: int, timeout: float = 1200,
                tunnel: Optional[subprocess.Popen] = None,
                on_wait: Optional[Callable[[float, float], None]] = None,
                report_every: float = 120.0) -> WaitResult:
    """`ping` only answers once the scene is loaded and pre-warm has run, so
    this is genuine readiness rather than 'the port is open'.

    Pass `tunnel` and this gives up the moment the forward dies. Without it,
    every ping goes to a local port nothing is listening on: measured on a live
    instance as **599 pings over 1800 s**, while the worker sat ready and
    listening the whole time. Thirty minutes of a rented GPU to discover a dead
    ssh process this can detect in one poll.

    `on_wait(elapsed, timeout)` is called every `report_every` seconds. On a big
    scene this wait is legitimately tens of minutes, and without it the broker
    log simply stops — which is how a healthy instance loading a 4.17 GB circuit
    was read as a hang. The callback is where the caller goes and asks the
    instance what it is actually doing. Its failures are swallowed: reporting
    must never be able to fail a wait.
    """
    started = time.time()
    deadline = started + timeout
    attempts = 0
    last = ""
    next_report = started + report_every
    while time.time() < deadline:
        attempts += 1
        if on_wait is not None and time.time() >= next_report:
            next_report = time.time() + report_every
            with contextlib.suppress(Exception):
                on_wait(time.time() - started, timeout)
        if tunnel is not None and tunnel.poll() is not None:
            err = ""
            if tunnel.stderr is not None:
                with contextlib.suppress(Exception):
                    err = tail(tunnel.stderr.read().decode(errors="replace"))
            return WaitResult(
                False, time.time() - started, attempts,
                f"the SSH tunnel exited {tunnel.returncode} after "
                f"{time.time() - started:.0f}s, so these pings went nowhere — "
                f"this is a transport failure, not a worker failure: "
                f"{err or 'ssh wrote no stderr'}",
                tunnel_died=True,
            )
        try:
            reply = worker_call({"cmd": "ping"}, port, timeout=30)
            if reply.get("ok"):
                return WaitResult(True, time.time() - started, attempts)
            last = f"ping replied not-ok: {str(reply)[:200]}"
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            last = diagnose(exc)
        time.sleep(3)
    return WaitResult(False, time.time() - started, attempts, last)


def owner_token() -> str:
    """Who is beating. Written into the heartbeat so the *instance* can say
    which broker owns it — see `foreign_broker`."""
    return f"{socket.gethostname()} {os.getpid()}"


def heartbeat(ep: Endpoint) -> None:
    """Refresh the file the in-container watchdog watches. If this stops — the
    broker crashed, the network died, this machine lost power — the instance
    destroys itself rather than billing indefinitely.

    Now also *signs* it. The watchdog reads only the mtime (`stat -c %Y`), so
    the contents are free real estate, and they carry the one fact no amount of
    local bookkeeping can supply: which process currently believes it owns this
    GPU. See `foreign_broker`.

    Raises on failure so the caller logs it. A silently dropped heartbeat is a
    countdown to the watchdog destroying a healthy instance.

    **On its own connection, for the same reason the job tunnel is.** It used
    to ride the shared ControlMaster, which makes the one signal that keeps the
    instance alive hostage to every other thing that touches that socket — and
    the bulk pushes are exactly such a thing. Observed 2026-08-02 while a 481 MB
    bundle push was in flight:

        heartbeat ... failed: mux_client_request_session: read from master
        failed: Broken pipe

    That is not the instance refusing a heartbeat. That is the heartbeat never
    reaching the instance, because the multiplexed socket it was queued on had
    been torn down — and it starts the watchdog's 30-minute clock on a box that
    might be perfectly capable of answering. Liveness signalling must not share
    fate with the work whose liveness it reports; the beat is one small command
    a minute, so its own handshake costs a few hundred milliseconds of an
    otherwise idle interval.

    One extra connection is affordable against sshd's default MaxStartups of
    10:30:100: PUSH_STREAMS is 8, this is the 9th, and the streams authenticate
    immediately rather than loitering in the unauthenticated window.
    """
    ran = probe(ep, f"printf '%s\\n' {shlex.quote(owner_token())} > "
                    f"{config.REMOTE_ROOT}/.broker_heartbeat", timeout=30,
                mux=False)
    if not ran.ok:
        raise SshError(ran)


def foreign_broker(ep: Endpoint, stale_sec: float = 300.0) -> Optional[str]:
    """Another *live* broker on this machine that is beating this instance.

    The singleton flock guarantees one broker per **state directory**, but
    "which instances are ours" is answered by the **vast.ai account**. Those are
    not the same key, and the gap is reachable: point `VASTRENDER_LOCK` at a
    different path — which is exactly what testing against a scratch state
    directory does — and a second broker sails past the lock, calls
    `adopt_or_reap`, and takes ownership of the instance the first one is
    rendering on. Its next shutdown then destroys that GPU. Observed live while
    fixing this, one clean shutdown away from killing an 8K frame at 2%.

    Freshness alone cannot decide it: a legitimate restart arrives seconds after
    the previous broker died, with the heartbeat still warm. The *pid* can — a
    broker that is still running is still a process, and one that is gone is
    gone. So this reports a holder only when the signature names a process that
    is (a) on this host, (b) alive, (c) actually a broker, and (d) not us.

    An unsigned or unreadable heartbeat means "no evidence", never "occupied":
    instances predating this, and every failed read, must stay adoptable or a
    restart could never take its own GPU back.
    """
    ran = probe(ep, f"stat -c %Y {config.REMOTE_ROOT}/.broker_heartbeat 2>/dev/null; "
                    f"date +%s; cat {config.REMOTE_ROOT}/.broker_heartbeat 2>/dev/null",
                timeout=45)
    if not ran.ok:
        return None
    lines = ran.out.strip().splitlines()
    if len(lines) < 3:
        return None
    try:
        age = int(lines[1]) - int(lines[0])
    except ValueError:
        return None
    if age > stale_sec:
        return None                      # nobody has beaten it recently

    parts = lines[2].split()
    if len(parts) < 2 or parts[0] != socket.gethostname():
        return None                      # unsigned, or another machine entirely
    try:
        pid = int(parts[1])
    except ValueError:
        return None
    if pid == os.getpid():
        return None                      # our own beat

    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return None                      # that broker is gone: adopt freely
    if not comm.startswith("python") or "broker.app" not in cmdline:
        return None                      # pid reused by something else

    return (f"broker pid {pid} on this machine beat it {age}s ago and is still "
            f"running — that process owns this instance")
