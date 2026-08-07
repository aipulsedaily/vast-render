#!/usr/bin/env python3
"""THE FLEET, DECLARED ONCE. There is no argument for addressing the wrong one.

    from farm.brokers import broker, fleet, live

    broker(3).url          -> "http://127.0.0.1:8762"
    broker(3).state        -> Path(".../state3")
    broker(3).label        -> "fleet03"
    broker(3).rq_env()     -> every VASTRENDER_* var, consistent by construction

    python3 farm/brokers.py --selftest
    python3 farm/brokers.py --show 8

WHY THIS IS ITS OWN MODULE
--------------------------
`rq` addresses a broker by URL and NOTHING ELSE. `VASTRENDER_BROKER`,
`BROKER_STATE`, `--broker 2` — none of them exist; a bare `rq status` reads
`VASTRENDER_URL` or falls back to 8760, silently, and reports broker 1. On
R2-979 that is exactly what happened: one broker's status was read twice and
reported as two brokers. Nothing was wrong with the output. The output was
correct *for the broker that was actually asked*, and the caller had no way to
see which one that was, because the answer does not carry its own address.

Worse, `rq` **silently re-routes**: `anim` and `seq` go to `VASTRENDER_BULK_URL`
(8761) unless `VASTRENDER_URL` is set. A fleet submission written the obvious
way — `rq anim --frames 1-300` eight times with eight different `--seq` names —
sends all eight to broker 2 and nothing to the fleet. It prints a note to
stderr. Nobody reads stderr on the seventh of eight loop iterations.

THE DESIGN RULE, borrowed from `f1-round2/tools/live_campath.py`
----------------------------------------------------------------
A stamp that every caller must remember to check will be forgotten. So this
module does not offer a URL to check — it offers a BROKER, and the URL, the
state directory, the label, the ports and the output directory all come off the
same frozen object. You cannot hold broker 3's URL and broker 1's state dir,
because there is no call that returns one without the other.

Every fleet command prints the identity it used — `#3 http://127.0.0.1:8762
state3 fleet03` — so a report that names a broker carries the evidence of which
broker answered.

And `verify()` closes the last gap: it reads `/proc/<pid>/environ` of whatever
process is actually listening on the port and asserts `VASTRENDER_DB` matches
the declaration. That is not a claim by the broker about itself; it is the
kernel's record of how the process was started. A broker started by hand on the
wrong port with the wrong state directory is caught before a single job is sent.

PREFIX DISJOINTNESS IS A CORRECTNESS PROPERTY, NOT A NAMING PREFERENCE
---------------------------------------------------------------------
`vastctl.our_instances` selects instances with `label.startswith(LABEL_PREFIX)`
and `Fleet.adopt_or_reap` **DESTROYS every instance it returns bar the one it
adopts.** So if one broker's prefix is a prefix of another's, the shorter one
reaps the longer one's card at its next restart, mid-frame.

The obvious scheme fails: `fleet1` is a prefix of `fleet10`. So fleet labels are
**fixed width** (`fleet03`, `fleet10`), which makes prefix-disjointness follow
from distinctness, and `_assert_disjoint()` checks every pair at import time
anyway — including against the two live literals, which are not generated.

stdlib only. `rq` is stdlib-only so that any agent can call it without the
virtualenv; a module whose job is to route `rq` correctly must be too.
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent

# How wide the fleet may be addressed. Not a claim that eight is right — see
# docs/multi-gpu.md and the fleet report — just the range these formulae are
# checked over. Ports stay inside 8760-8799 up to here.
MAX_BROKERS = 12

# --- the two that are already alive, declared as literals ------------------
#
# These are NOT generated, because they were named before this module existed
# and their labels are on live rented cards. Renaming either one would orphan a
# billing instance: the broker would stop recognising its own card as "mine",
# and the watchdog is the only thing that would eventually kill it. They are
# declared here so the fleet can SEE them (to avoid their ports, and to name
# them in a whole-farm cost report) and marked protected so it can never act on
# them.
_LIVE = {
    1: ("renderbroker", "stills / general queue — 8760 is `rq`'s default"),
    2: ("ladderbroker", "bulk sequence work — 8761 is `rq`'s BULK_URL"),
}


def _label(n: int) -> str:
    if n in _LIVE:
        return _LIVE[n][0]
    # Fixed width. See the module docstring: `fleet1` is a prefix of `fleet10`
    # and that is a card-destroying bug, not a cosmetic one.
    return f"fleet{n:02d}"


@dataclass(frozen=True)
class Broker:
    """One broker process: its address, its state, its label, its money.

    Frozen, because the whole point is that these five facts travel together.
    A caller that mutates one of them off a copy has re-created the bug.
    """

    n: int
    label: str
    port: int
    tunnel_port: int
    exec_port: int
    state: Path
    out: Path
    protected: bool
    role: str

    # --- addresses ---------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def db(self) -> Path:
        return self.state / "broker.db"

    @property
    def log(self) -> Path:
        return self.state / "broker.log"

    @property
    def lock(self) -> Path:
        return self.state / "broker.lock"

    @property
    def brokerd_lock(self) -> Path:
        return self.state / "brokerd.lock"

    @property
    def seq(self) -> Path:
        return self.out / "seq"

    @property
    def ident(self) -> str:
        """The line every fleet command prints before saying anything else.

        R2-979 was not a wrong number, it was an unattributed one. A status
        line that does not name its source cannot be checked by the person
        reading it.
        """
        return f"#{self.n} {self.url} {self.state.name} {self.label}"

    # --- how to start it, and how to talk to it ----------------------------

    def env(self, *, disk_gb: int = 80, hibernate: int = 14400,
            max_batch_usd: float = 20.0,
            scene: Optional[str] = None) -> dict:
        """The complete environment for `brokerd.sh`. Nothing derived twice.

        `scripts/broker2.sh` set nine of these by hand. Nine hand-set variables
        is nine chances to give broker 5 broker 4's tunnel port, which does not
        fail to bind — `reap_stale_tunnels` SIGKILLs the sibling's forward
        mid-frame and the sibling reads it as bad hardware.
        """
        e = {
            "VASTRENDER_LABEL": self.label,
            "VASTRENDER_PORT": str(self.port),
            "VASTRENDER_DB": str(self.db),
            "VASTRENDER_LOCK": str(self.lock),
            "VASTRENDER_OUT": str(self.out),
            "VASTRENDER_SEQ_DIR": str(self.seq),
            "VASTRENDER_TUNNEL_LOCAL_PORT": str(self.tunnel_port),
            "VASTRENDER_EXEC_LOCAL_PORT": str(self.exec_port),
            "VASTRENDER_DISK_GB": str(disk_gb),
            "VASTRENDER_HIBERNATE": str(hibernate),
            "VASTRENDER_MAX_BATCH_USD": f"{max_batch_usd:g}",
            "VASTRENDER_LOG": str(self.log),
            "VASTRENDER_BROKERD_LOCK": str(self.brokerd_lock),
        }
        if scene:
            e["VASTRENDER_SCENE"] = scene
        return e

    def rq_env(self, base: Optional[dict] = None) -> dict:
        """Environment for an `rq` invocation aimed at THIS broker.

        `VASTRENDER_URL` is set unconditionally and that is the load-bearing
        part: it is the only thing that defeats `rq`'s silent bulk re-route of
        `anim` and `seq` to 8761. An `rq anim` issued for the fleet without it
        lands on the client's live beat-1 proxy card.
        """
        e = dict(os.environ if base is None else base)
        e["VASTRENDER_URL"] = self.url
        e.pop("VASTRENDER_BULK_URL", None)
        e["VASTRENDER_GET_DIR"] = str(self.out)
        e["VASTRENDER_AGENT"] = e.get("VASTRENDER_AGENT") or f"fleet{self.n}"
        return e

    # --- is it there, and is it who it says it is --------------------------

    def listening(self, timeout: float = 1.5) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout):
                return True
        except OSError:
            return False

    def pid(self) -> Optional[int]:
        """The pid actually listening on this broker's port, from the kernel."""
        return _pid_on_port(self.port)

    def verify(self) -> tuple[bool, str]:
        """Assert the process on this port was started with THIS state dir.

        Reads `/proc/<pid>/environ`, which is the kernel's record of how the
        process was started, not a claim the process makes about itself. A
        broker launched by hand with the wrong `VASTRENDER_DB` — the shape of
        every "I read one broker twice" incident — is caught here, before work
        is sent to it.
        """
        if not self.listening():
            return False, "not listening"
        pid = self.pid()
        if pid is None:
            return False, (f"something is listening on {self.port} but no pid "
                           f"could be attributed to it")
        env = _proc_env(pid)
        if env is None:
            return False, f"pid {pid} environ unreadable"
        got_db = env.get("VASTRENDER_DB") or str(ROOT / "state" / "broker.db")
        got_port = env.get("VASTRENDER_PORT") or "8760"
        got_label = env.get("VASTRENDER_LABEL") or "renderbroker"
        if Path(got_db) != self.db:
            return False, (f"pid {pid} on port {self.port} has "
                           f"VASTRENDER_DB={got_db}, declared {self.db} — "
                           f"THIS IS NOT BROKER {self.n}")
        if int(got_port) != self.port:
            return False, (f"pid {pid} has VASTRENDER_PORT={got_port} but is "
                           f"listening on {self.port}")
        if got_label != self.label:
            return False, (f"pid {pid} has VASTRENDER_LABEL={got_label}, "
                           f"declared {self.label} — its rented card is "
                           f"labelled wrong and adopt_or_reap scopes on it")
        return True, f"pid {pid} {self.label} {self.state.name}"


def _ports(n: int) -> tuple[int, int, int]:
    """(broker, tunnel, exec) for broker n. The live two fall out of it.

    Broker 1 is 8760/8798/8797 and broker 2 is 8761/8796/8795, and both were
    chosen by hand before there was a formula. Checked in `_selftest`, because
    a formula that does not reproduce the live values is a formula that would
    have to renumber a running card.
    """
    return 8760 + (n - 1), 8800 - 2 * n, 8799 - 2 * n


def broker(n: int) -> Broker:
    """Broker n. The only constructor. There is no `Broker(url=...)`."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError(f"broker index must be an int, got {n!r}")
    if not 1 <= n <= MAX_BROKERS:
        raise ValueError(
            f"broker {n} is outside 1..{MAX_BROKERS}. Ports would leave the "
            f"8760-8799 block and could collide with WORKER_PORT (8799 on the "
            f"instance) or a sibling's tunnel — which does not fail to bind, "
            f"it SIGKILLs the sibling's forward mid-frame.")
    port, tunnel, execp = _ports(n)
    return Broker(
        n=n,
        label=_label(n),
        port=port,
        tunnel_port=tunnel,
        exec_port=execp,
        state=ROOT / ("state" if n == 1 else f"state{n}"),
        out=ROOT / ("out" if n == 1 else f"out{n}"),
        protected=n in _LIVE,
        role=_LIVE[n][1] if n in _LIVE else "fleet worker",
    )


def fleet(width: int) -> list[Broker]:
    """The `width` brokers a fleet run would use — brokers 3..width+2.

    IT NEVER INCLUDES THE LIVE TWO. `fleet(8)` is brokers 3-10, not 1-8. The
    live brokers carry the client's beat-1 proxy and the stills queue; a fleet
    command that could address them could tear one down, and `rq teardown`
    reports success while the other card keeps billing.
    """
    if width < 1:
        raise ValueError("fleet width must be >= 1")
    first = max(_LIVE) + 1
    if first + width - 1 > MAX_BROKERS:
        raise ValueError(
            f"fleet of {width} needs brokers {first}..{first + width - 1}, "
            f"beyond MAX_BROKERS={MAX_BROKERS}")
    return [broker(n) for n in range(first, first + width)]


def all_declared() -> list[Broker]:
    return [broker(n) for n in range(1, MAX_BROKERS + 1)]


def live() -> Iterator[Broker]:
    """Every declared broker that currently has something on its port."""
    for b in all_declared():
        if b.listening():
            yield b


# --- the kernel's opinion, not the process's -------------------------------


def _pid_on_port(port: int) -> Optional[int]:
    """Which pid holds the listening socket on 127.0.0.1:<port>.

    /proc/net/tcp gives the inode; /proc/*/fd gives the owner. `ss`, `netstat`
    and `lsof` are all absent from the container images this project runs on
    (see the incident log) and are not guaranteed here either, so this uses
    only files that always exist.
    """
    want = None
    for path, loopback in (("/proc/net/tcp", "0100007F"), ("/proc/net/tcp6", None)):
        try:
            lines = Path(path).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) < 10 or f[3] != "0A":       # 0A == LISTEN
                continue
            local = f[1]
            try:
                lport = int(local.split(":")[1], 16)
            except (IndexError, ValueError):
                continue
            if lport != port:
                continue
            want = f[9]
            break
        if want:
            break
    if not want:
        return None
    target = f"socket:[{want}]"
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    if os.readlink(fd) == target:
                        return int(proc.name)
                except OSError:
                    continue
        except OSError:
            continue
    return None


def _proc_env(pid: int) -> Optional[dict]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    out = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, _, v = item.partition(b"=")
            out[k.decode(errors="replace")] = v.decode(errors="replace")
    return out


# --- invariants, checked at import ----------------------------------------


def _assert_disjoint() -> None:
    """No label may be a prefix of another. This destroys cards, see above."""
    labels = [broker(n).label for n in range(1, MAX_BROKERS + 1)]
    for i, a in enumerate(labels):
        for j, b in enumerate(labels):
            if i != j and b.startswith(a):
                raise AssertionError(
                    f"label {a!r} (broker {i+1}) is a prefix of {b!r} "
                    f"(broker {j+1}). vastctl.our_instances selects with "
                    f"startswith and Fleet.adopt_or_reap DESTROYS everything "
                    f"it returns bar one — broker {i+1} would reap broker "
                    f"{j+1}'s card mid-frame at its next restart.")


def _assert_ports_unique() -> None:
    seen: dict[int, str] = {}
    for n in range(1, MAX_BROKERS + 1):
        b = broker(n)
        for kind, p in (("broker", b.port), ("tunnel", b.tunnel_port),
                        ("exec", b.exec_port)):
            if p in seen:
                raise AssertionError(
                    f"port {p} is both {seen[p]} and broker {n}'s {kind}. A "
                    f"duplicate tunnel port does not fail to bind: "
                    f"reap_stale_tunnels SIGKILLs the other broker's forward.")
            seen[p] = f"broker {n}'s {kind}"
    # WORKER_PORT is 8799 on the INSTANCE, but reap_stale_tunnels pgreps for
    # `-L <local>:127.0.0.1:8799`, so a local port of 8799 would be ambiguous.
    if 8799 in seen:
        raise AssertionError(f"8799 ({seen[8799]}) collides with WORKER_PORT")


def _assert_dirs_unique() -> None:
    dirs = [(broker(n).state, broker(n).out) for n in range(1, MAX_BROKERS + 1)]
    for kind, idx in (("state", 0), ("out", 1)):
        vals = [d[idx] for d in dirs]
        if len(set(vals)) != len(vals):
            raise AssertionError(f"two brokers share a {kind} directory")


_assert_disjoint()
_assert_ports_unique()
_assert_dirs_unique()


# --- selftest / show -------------------------------------------------------


def _selftest() -> int:
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
        if not cond:
            fails.append(name)

    print("farm/brokers.py selftest")

    b1, b2 = broker(1), broker(2)
    check("broker 1 reproduces the LIVE values",
          (b1.port, b1.tunnel_port, b1.exec_port, b1.label, b1.state.name)
          == (8760, 8798, 8797, "renderbroker", "state"),
          f"{b1.port}/{b1.tunnel_port}/{b1.exec_port} {b1.label} {b1.state.name}")
    check("broker 2 reproduces the LIVE values (scripts/broker2.sh)",
          (b2.port, b2.tunnel_port, b2.exec_port, b2.label, b2.state.name,
           b2.out.name) == (8761, 8796, 8795, "ladderbroker", "state2", "out2"),
          f"{b2.port}/{b2.tunnel_port}/{b2.exec_port} {b2.label} {b2.state.name}")
    check("brokers 1 and 2 are protected", b1.protected and b2.protected)
    check("fleet(8) excludes the live two",
          [b.n for b in fleet(8)] == [3, 4, 5, 6, 7, 8, 9, 10])

    # The failure this scheme exists to prevent, demonstrated rather than
    # asserted: the obvious label scheme really does alias.
    naive = [f"fleet{n}" for n in range(1, 13)]
    aliased = [(a, b) for a in naive for b in naive if a != b and b.startswith(a)]
    check("the NAIVE label scheme aliases (so fixed width is not cosmetic)",
          bool(aliased), f"e.g. {aliased[0]}" if aliased else "")
    real = [broker(n).label for n in range(1, 13)]
    check("the real label scheme does not alias",
          not [(a, b) for a in real for b in real if a != b and b.startswith(a)])

    check("all ports inside 8760-8798",
          all(8760 <= p <= 8798
              for n in range(1, MAX_BROKERS + 1)
              for p in _ports(n)))
    try:
        broker(0)
        check("broker(0) refused", False)
    except ValueError:
        check("broker(0) refused", True)
    try:
        broker(MAX_BROKERS + 1)
        check("broker(MAX+1) refused", False)
    except ValueError:
        check("broker(MAX+1) refused", True)

    # The silent bulk re-route is the live landmine: without VASTRENDER_URL,
    # `rq anim` goes to 8761 — the client's beat-1 proxy card.
    e = broker(5).rq_env({"VASTRENDER_BULK_URL": "http://127.0.0.1:8761"})
    check("rq_env pins VASTRENDER_URL and drops BULK_URL",
          e["VASTRENDER_URL"] == broker(5).url
          and "VASTRENDER_BULK_URL" not in e,
          e["VASTRENDER_URL"])

    # Identity against the live processes, if they are up. This is the one that
    # would have caught R2-979.
    for b in (b1, b2):
        if b.listening():
            ok, why = b.verify()
            check(f"live broker {b.n} identity", ok, why)
        else:
            print(f"  skip  live broker {b.n} not listening")

    print(f"\n{'PASS' if not fails else 'FAIL: ' + ', '.join(fails)}")
    return 1 if fails else 0


def _show(width: int) -> int:
    print(f"{'#':>2}  {'url':<24} {'label':<14} {'state':<9} {'out':<7} "
          f"{'tun':<5} {'exec':<5} role")
    for b in all_declared()[:max(width, 2) + 2]:
        mark = "LIVE-PROTECTED" if b.protected else ""
        print(f"{b.n:>2}  {b.url:<24} {b.label:<14} {b.state.name:<9} "
              f"{b.out.name:<7} {b.tunnel_port:<5} {b.exec_port:<5} "
              f"{b.role} {mark}")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--show" in sys.argv:
        i = sys.argv.index("--show")
        sys.exit(_show(int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 8))
    print(__doc__)
    sys.exit(_show(8))
