#!/usr/bin/env python3
"""Make the next death diagnosable, whatever kills the process.

Two brokers died mid-batch leaving *nothing*: `broker.log` ended mid-wait, no
traceback, no shutdown line, and the rented instance still running. That last
detail is the one that identified the killer, because it is only consistent with
one thing.

    KEEP_ON_EXIT is false, so **every** in-process exit path — an exception out
    of `main()`, `SystemExit`, uvicorn's SIGTERM handler, a clean shutdown —
    runs `broker.stop()` and destroys the instance. The instance survived.
    Therefore no Python code ran on the way out.

Combined with: no `oom_kill` in dmesg, and `systemd-coredump` (active on this
host, and holding five Blender SIGSEGVs from the day before) recording no python
core at all — which rules out every core-dumping signal — the only remaining
causes are SIGKILL and an unhandled non-core signal. The process was killed from
outside. See `docs/incidents.md`.

None of that was knowable from the log at the time, and that is the defect this
module fixes. Every way a Python process can end now writes one line naming
itself first:

| how it ends | what appears in broker.log |
|---|---|
| unhandled exception, main thread | `FATAL: unhandled exception on the main thread` + traceback |
| unhandled exception, any other thread | `FATAL: unhandled exception in thread <name>` + traceback |
| unhandled exception on the asyncio loop | `FATAL: unhandled exception on the event loop` + context |
| SIGSEGV / SIGABRT / SIGBUS / SIGFPE | `faulthandler` dumps every thread's C-level stack |
| SIGTERM / SIGINT / SIGHUP / SIGQUIT | `SIGNAL: received SIGxxx` **before** any handler acts |
| any `sys.exit` / falling off the end | `process exiting normally (atexit)` |
| **none of the above** | nothing — and that absence now *means* SIGKILL |

The last row is the point. Silence used to be uninformative because every case
produced it; now silence is itself the diagnosis.

`kill -USR1 <pid>` dumps every thread's Python stack to the log without
disturbing the process — the tool that was missing while a broker sat for an
hour inside a reattach loop nobody could see into.
"""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import signal
import sys
import threading
import traceback
from typing import Optional

log = logging.getLogger("broker")

# Signals worth naming. Deliberately not SIGKILL or SIGSTOP: those cannot be
# caught, which is exactly why "no line at all" has to be a diagnosis of its own.
_NAMED = ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR2", "SIGXCPU", "SIGXFSZ")

_installed = False
# Signals currently being narrated, so a nested wrapper stays quiet.
_narrating: set = set()


def _stderr():
    """The real stream, not a logging handler.

    `faulthandler` writes to a file descriptor from a signal handler, so it
    needs the raw stream. stdout and stderr are both the log file when the
    broker is started the documented way, so this lands in the same place as
    everything else.
    """
    return sys.stderr


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        log.warning("KeyboardInterrupt on the main thread")
        return
    log.critical("FATAL: unhandled exception on the main thread — %s: %s\n%s",
                 exc_type.__name__, exc,
                 "".join(traceback.format_exception(exc_type, exc, tb)))


def _threadhook(args) -> None:
    """An exception out of a thread does not end the process — it ends the
    *thread*, silently, and the broker then runs on with no dispatcher, or no
    heartbeat, and nothing says so. Naming it is half the fix; `Broker`
    restarts the thread for the other half."""
    if args.exc_type is SystemExit:
        return
    log.critical(
        "FATAL: unhandled exception in thread %r — %s: %s\n%s",
        getattr(args.thread, "name", "?"), args.exc_type.__name__, args.exc_value,
        "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                           args.exc_traceback)),
    )


def _unraisablehook(args) -> None:
    log.error("unraisable exception in %r: %s: %s", args.object,
              getattr(args.exc_type, "__name__", args.exc_type), args.exc_value)


def loop_exception_handler(loop, context: dict) -> None:
    """For `loop.set_exception_handler`.

    asyncio's default handler logs to the `asyncio` logger, which nothing here
    configures a destination for — so an exception on the event loop was
    invisible in `broker.log`. HTTP handlers run here, and so does lifespan
    shutdown.
    """
    exc = context.get("exception")
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) \
        if exc is not None else ""
    log.critical("FATAL: unhandled exception on the event loop — %s\n%s",
                 context.get("message", ""), detail or context)


def _on_exit() -> None:
    # Reached by every *in-process* exit and by none of the kills. Its absence
    # from the tail of the log is therefore evidence, not an omission.
    log.warning("process exiting normally (atexit) — pid %d", os.getpid())


def _log_signal(signum: int, prev) -> None:
    """Name the signal, then let whoever was there before decide what it means.

    Installed *after* uvicorn's own SIGTERM/SIGINT handlers (uvicorn takes them
    inside `Server.serve`, which runs before lifespan startup), and chains to
    them, so shutdown behaviour is unchanged — the broker just says what hit it
    first. Without this the graceful path is indistinguishable in the log from
    the process simply stopping.
    """
    # Only the outermost wrapper narrates. uvicorn replaces our handler and we
    # then wrap *its*, so a chain can be more than one layer deep and the same
    # signal would be reported once per layer.
    first = signum not in _narrating
    if first:
        _narrating.add(signum)
    try:
        if first:
            log.error("SIGNAL: received %s (pid %d, ppid %d) — %s",
                      signal.Signals(signum).name, os.getpid(), os.getppid(),
                      "handing it to the previous handler" if callable(prev)
                      else f"default disposition {prev!r}")
        if callable(prev):
            prev(signum, None)
    finally:
        if first:
            _narrating.discard(signum)


_wrapped: dict = {}


def _install_signal_names() -> None:
    for name in _NAMED:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            prev = signal.getsignal(sig)
        except (ValueError, OSError):
            continue
        # Already ours and still installed: leave it. `install()` runs three
        # times (import, main, lifespan) so that it can sit on top of uvicorn's
        # handlers, and wrapping our own wrapper each time printed the same
        # signal three times and chained through two dead layers to reach it.
        if prev is _wrapped.get(sig):
            continue
        if sig == signal.SIGHUP:
            # Ignored on purpose, which is what `nohup` does and what a daemon
            # should do: the broker outlives whatever shell started it, and a
            # hangup must not be able to take a 40-minute frame with it.
            def hup(signum, frame):
                log.error("SIGNAL: received SIGHUP — IGNORING it; a daemon "
                          "outlives the terminal that started it")
            handler = hup
        else:
            handler = (lambda s=sig, p=prev: (lambda signum, frame: _log_signal(signum, p)))()
        try:
            signal.signal(sig, handler)
            _wrapped[sig] = handler
        except (ValueError, OSError, RuntimeError):
            # Not the main thread, or the platform refuses. Never fatal.
            pass


def install(where: str = "") -> None:
    """Install every hook. Idempotent; safe to call from `main()` and lifespan."""
    global _installed

    if not _installed:
        sys.excepthook = _excepthook
        threading.excepthook = _threadhook
        if hasattr(sys, "unraisablehook"):
            sys.unraisablehook = _unraisablehook
        try:
            faulthandler.enable(file=_stderr(), all_threads=True)
            # On demand: `kill -USR1 <pid>` prints every thread's Python stack.
            # A broker parked in a reattach loop is otherwise a black box, and
            # the last two incidents were both spent inside exactly that loop.
            if hasattr(signal, "SIGUSR1"):
                # chain=False, and it is not a detail. `chain=True` calls the
                # previous handler after dumping, which for SIGUSR1 is SIG_DFL —
                # and the default disposition of SIGUSR1 is *terminate*. The
                # diagnostic tool would kill the broker it was called to inspect.
                # Caught here by `kill -USR1` killing a test broker outright.
                faulthandler.register(signal.SIGUSR1, file=_stderr(),
                                      all_threads=True, chain=False)
        except (RuntimeError, ValueError, AttributeError) as exc:
            log.warning("faulthandler unavailable: %s", exc)
        atexit.register(_on_exit)
        _installed = True

    # Always re-taken: uvicorn installs its own SIGTERM/SIGINT handlers after
    # this module is first imported, so the chaining wrapper has to go on top
    # of *those*, which means running again once uvicorn has had its turn.
    _install_signal_names()
    if where:
        log.info("crash diagnostics installed (%s)", where)


def report_process_identity() -> None:
    """Say who owns this process, loudly, at startup.

    The two lost batches were both brokers `exec`'d as a *background task of the
    agent harness* — `run_in_background: true` with `exec .venv/bin/python -m
    broker.app`, which makes the broker itself that task's process. Whatever
    reaps the task reaps the broker, with a signal it cannot be expected to
    survive, and nothing in the log could have told anyone that was the shape of
    the problem. These four numbers can.

    A detached broker is a session leader: pid == pgid == sid. Anything else
    means some other process group owns its lifetime.
    """
    pid, ppid = os.getpid(), os.getppid()
    try:
        pgid, sid = os.getpgid(0), os.getsid(0)
    except OSError:                                  # pragma: no cover
        pgid = sid = -1

    log.info("broker pid=%d ppid=%d pgid=%d sid=%d", pid, ppid, pgid, sid)

    if os.environ.get("VASTRENDER_SUPERVISED") == "1":
        # Sharing the supervisor's group is the point: that is how brokerd.sh
        # reaps and restarts us, and the supervisor is the detached one.
        log.info("supervised by brokerd pid %d — it will report how this "
                 "process dies and restart it", ppid)
    elif pid != sid or pid != pgid:
        log.warning(
            "THIS BROKER IS NOT DETACHED (pid %d, but pgid %d / sid %d) — its "
            "lifetime belongs to another process group, which can kill it "
            "mid-render with no traceback and no teardown. That is how two "
            "multi-hour batches were lost. Start it with scripts/brokerd.sh, or "
            "at least `setsid nohup ... &` — never `exec` inside a supervised "
            "shell.", pid, pgid, sid,
        )


def parent_death_signal(sig: Optional[int] = None) -> None:
    """Die when the parent does — for the *supervised* child only.

    Only meaningful under `scripts/brokerd.sh`: it guarantees that killing the
    supervisor cannot leave an unsupervised broker holding the singleton lock,
    which would then refuse every later start. Never enabled by default, since a
    normally-started broker must outlive the shell that launched it.

    **SIGKILL, not SIGTERM**, and the difference is a rented GPU.

    `scripts/brokerd.sh stop` signals the supervisor first (so it does not
    faithfully restart the broker it is about to kill) and then `kill -9`s the
    broker, with the explicit comment that -9 "leaves the GPU and the warm
    worker for the next broker to adopt". With SIGTERM here, the supervisor's
    death beat that -9 to it: the kernel delivered SIGTERM to the broker, the
    broker ran its shutdown path, and with KEEP_ON_EXIT off that path DESTROYS
    THE INSTANCE. Observed on a real stop, whose log reads

        WARNING brokerd  supervisor asked to stop — leaving broker 2656354 alone
        ERROR   broker   SIGNAL: received SIGTERM (pid 2656354, ppid 1)

    one line apart. It cost nothing only because no instance was rented at the
    time.

    SIGKILL serves this mechanism's entire purpose — no unsupervised broker may
    hold the singleton lock — while leaving the instance alone, which is the
    intended behaviour for every deliberate restart. An instance left behind is
    not orphaned: the next broker adopts it in seconds, and if none comes back
    the in-container watchdog destroys it 30 minutes after the heartbeat stops.
    """
    if sys.platform != "linux":
        return
    try:
        import ctypes

        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            PR_SET_PDEATHSIG, sig if sig is not None else signal.SIGKILL, 0, 0, 0)
    except Exception as exc:                          # pragma: no cover
        log.debug("PR_SET_PDEATHSIG unavailable: %s", exc)
