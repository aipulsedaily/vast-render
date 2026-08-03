#!/usr/bin/env python3
"""One broker per state directory, enforced by the kernel.

This exists because starting a second broker destroyed the *first* one's GPU.

uvicorn runs lifespan startup **before** it binds the listening socket
(`Server.startup()` awaits `lifespan.startup()` and only then calls
`loop.create_server`). So a second `python -m broker.app` got all the way
through `Fleet.adopt_or_reap()` — taking ownership of the live instance another
broker was actively rendering on — and only then failed with "address already
in use". Its shutdown path then did what shutdown always does: destroyed the
instance it had just adopted. `timeout 25 .venv/bin/python -m broker.app`, run
only to capture a startup message, killed a rented GPU mid-batch. Twice.

An advisory `flock` is the right primitive here rather than a PID file:

* The kernel drops it when the holder dies **for any reason** — SIGKILL, a
  segfault, the OOM killer, a power cut. There is no stale lock to clean up and
  no PID to validate, which is exactly where PID files fail.
* It is held against an *open file description*, so it survives `fork` into a
  `setsid nohup` background process — the documented way this broker is
  started — while still being released the moment the last fd closes.

The PID written into the file is diagnostics only. It is never consulted to
decide whether the lock is held; that is the kernel's answer alone.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional


class BrokerAlreadyRunning(RuntimeError):
    """Raised instead of adopting, renting, or destroying anything."""

    def __init__(self, path: Path, holder_pid: Optional[int]) -> None:
        who = f"pid {holder_pid}" if holder_pid else "an unknown pid"
        super().__init__(
            f"another broker already holds {path} ({who}) — refusing to start. "
            f"A second broker would adopt the running instance and destroy it on "
            f"exit. Stop the other one first: kill $(pgrep -f 'python -m broker.app')"
        )
        self.path = path
        self.holder_pid = holder_pid


class SingleInstanceLock:
    """Exclusive, non-blocking, held for the lifetime of the process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        """Take the lock or raise `BrokerAlreadyRunning`.

        Idempotent within a process: flock is per open file description, so a
        second `open()` of the same path from this same process would deadlock
        against our own lock. Calling this from both `main()` and the lifespan
        hook is therefore safe and deliberate — `main()` gives a clean message
        for `python -m broker.app`, the lifespan hook covers `uvicorn
        broker.app:app` and anything else that imports the ASGI app directly.
        """
        if self._fd is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Read the holder's pid for the error message only — losing the race
            # is already decided, this just makes the log actionable.
            holder: Optional[int] = None
            try:
                holder = int(os.pread(fd, 32, 0).decode(errors="replace").strip() or 0) or None
            except (OSError, ValueError):
                pass
            os.close(fd)
            raise BrokerAlreadyRunning(self.path, holder) from None

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        # Never closed: the lock lives as long as this process does, and the
        # kernel releases it on exit however that exit happens.
        self._fd = fd

    def release(self) -> None:
        """Only for tests. Production relies on process exit to release it."""
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)
