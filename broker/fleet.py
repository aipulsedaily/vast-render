#!/usr/bin/env python3
"""Owns the rented GPU: rent it when work appears, tear it down when work stops.

The whole point of a single warm instance is that per-job overhead gets paid
once instead of once per job. Cold-per-job would pay, every time: instance boot
(~2-5 min), a 63 MB scene upload, scene load, BVH build, and the OptiX JIT.
On a 5090 where the render itself may be 3-6 minutes, that overhead would
dominate the thing you are renting the GPU for.

Teardown is `destroy`, never `stop`. Storage bills for as long as an instance
*exists*; stopping ends only the GPU meter.

Two rules govern when hardware gets replaced, and both were written after the
broker got them wrong on a live batch:

  * **A failed transfer must not destroy the instance.** A dropped upload over a
    69 ms link is a transport problem. Condemning the GPU for one turns a
    retryable hiccup into a fresh rental, another image pull and another
    half-gigabyte push — which is exactly what happened when a 481 MB Blender
    bundle failed twice and a healthy 5090 was destroyed for it.
  * **Never re-rent the offer that just failed.** After destroying an instance
    that never came up, the broker searched again, got the same cheapest offer
    back, and rented it a second time five minutes later.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from vastai import VastAI

from . import config, remote, scenes
from .remote import Endpoint

import sys
sys.path.insert(0, str(config.ROOT / "vastctl"))
import vastctl  # noqa: E402

log = logging.getLogger("fleet")

DEPLOY_ATTEMPTS = config.DEPLOY_ATTEMPTS
MAX_TRANSPORT_ROUNDS = config.MAX_TRANSPORT_ROUNDS
MAX_STALLED_ROUNDS = config.MAX_STALLED_ROUNDS
RECONCILE_AFTER_HEARTBEATS = config.RECONCILE_AFTER_HEARTBEATS
# Wake-ups to attempt before abandoning a hibernated instance. Each one
# costs a 490 s timeout, so two is already 16 minutes of a stranded queue.
RESUME_ATTEMPTS = 2

# How long a condemned offer or machine stays condemned.
#
# Not forever: a host's link is a property of the host *today*. Banning machine
# 55313 for all time would slowly eat the market, and the reason it reset every
# connection this morning may be a switch someone fixes this afternoon.
#
# Not per-session either, which is what it used to be, and the reason this
# constant exists. The blacklists lived only in Fleet's memory, so the one event
# guaranteed to clear them was a broker restart — and a broker restart is
# exactly what an operator does when the broker is wedged on a bad host. The
# very first thing the fresh process then did was search the market, find the
# same cheapest offer that had just burned forty minutes, and rent it back.
BAD_HOST_TTL_SEC = float(getattr(config, "BAD_HOST_TTL_SEC", 24 * 3600))

# Where that survives. One small JSON file next to the DB rather than a `meta`
# row, because Fleet holds no db handle and threading one in for this would
# widen its constructor for every caller and every test stub.
BAD_HOSTS_PATH = config.DB_PATH.parent / "bad_hosts.json"


class CondemnedIds(set):
    """A set of offer/machine ids that writes itself to disk when it changes.

    Subclassing `set` rather than adding save calls at each site is deliberate:
    the ids are condemned from seven different places in this file, and a fix
    that depends on every future one of them remembering to persist is a fix
    that lasts until the next branch is added. Here the storage is a property of
    the container, so it cannot be forgotten.

    Entries carry the time they were condemned and expire after
    `BAD_HOST_TTL_SEC`; expiry is applied at load, so a long-lived broker keeps
    its bans for its whole life and a fresh one starts from what is still recent.

    Persistence failures are logged and swallowed. Losing the ban list degrades
    the broker to the old per-session behaviour, which is bad; taking the broker
    down while it holds a rented GPU because a state file was unwritable is
    worse.
    """

    def __init__(self, kind: str, path: Path = BAD_HOSTS_PATH) -> None:
        super().__init__()
        self.kind = kind
        self.path = path
        self.stamps: dict[int, float] = {}
        self._load()

    # --- persistence ---
    def _read_all(self) -> dict:
        try:
            with self.path.open() as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            log.warning("could not read %s (%s) — starting with no condemned "
                        "hosts", self.path, remote.diagnose(exc))
            return {}

    def _load(self) -> None:
        entries = self._read_all().get(self.kind) or {}
        if not isinstance(entries, dict):
            return
        now = time.time()
        for key, when in entries.items():
            try:
                ident, stamp = int(key), float(when)
            except (TypeError, ValueError):
                continue
            if now - stamp < BAD_HOST_TTL_SEC:
                super().add(ident)
                self.stamps[ident] = stamp
        if self:
            log.info("%s condemned earlier and still within the %.0f h ban: %s",
                     self.kind, BAD_HOST_TTL_SEC / 3600,
                     ", ".join(str(i) for i in sorted(self)))

    def _save(self) -> None:
        try:
            data = self._read_all()
            data[self.kind] = {str(i): self.stamps.get(i, time.time()) for i in self}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
            tmp.replace(self.path)          # atomic; a torn ban list is unreadable
        except Exception as exc:
            log.warning("could not persist condemned %s to %s: %s",
                        self.kind, self.path, remote.diagnose(exc))

    # --- mutations ---
    def add(self, ident) -> None:                    # type: ignore[override]
        if not ident or ident in self:
            return
        super().add(ident)
        self.stamps[ident] = time.time()
        self._save()

    def discard(self, ident) -> None:                # type: ignore[override]
        if ident not in self:
            return
        super().discard(ident)
        self.stamps.pop(ident, None)
        self._save()

    def clear(self) -> None:                         # type: ignore[override]
        if not self:
            return
        super().clear()
        self.stamps.clear()
        self._save()


# Printed by `heal_scene_dir_cmd` only when it actually found something wrong,
# so the caller can tell "healed a stray inode" from "nothing to do" without
# parsing `ls` output.
STRAY_MARK = "STRAY"


def heal_scene_dir_cmd(scene_dir: str) -> str:
    """Make `scene_dir` a usable directory, describing anything it displaces.

    `mkdir -p` succeeds on an existing directory and fails only when the path
    is something else, so an EEXIST here is a corrupted entry in the broker's
    own cache rather than a statement about the host — and it was read as one
    anyway, costing instance 46668588 (see the call site).

    Two properties matter and are tested against a real filesystem:

    * a genuine cache directory is never touched, whatever it contains. The
      removal is reachable only on a path that already failed `-d`.
    * the stray inode is DESCRIBED before it is removed. Healing silently would
      guarantee the next recurrence is as unexplained as the last, and the
      evidence stops existing the instant the fix runs, so it cannot be left
      for whoever reads the log later.
    """
    q = shlex.quote(scene_dir)
    # `-e` FOLLOWS symlinks, so it is false on a dangling one — and a dangling
    # symlink is precisely a path that `mkdir -p` refuses with EEXIST while
    # every "does it exist" test says no. Guarding on `-e` alone therefore
    # skipped the heal on one of the few shapes that can actually cause the
    # outage. `-L` catches the link itself, whatever it points at.
    return (
        f"if {{ [ -e {q} ] || [ -L {q} ]; }} && [ ! -d {q} ]; then "
        f"echo {STRAY_MARK}; ls -ld {q}; "
        f"readlink -f {q} 2>/dev/null; "
        f"head -c 160 {q} 2>/dev/null | od -An -c | head -4; "
        f"rm -f {q}; "
        f"fi; mkdir -p {q}"
    )


class _PerInstance:
    """Measurements that belong to ONE rented box, keyed by its instance id.

    Keyed rather than merely cleared, because a reset that only runs on the
    clean path is not a reset. `_forget_vanished` zeroes these at teardown and
    that was believed to be enough; then instance 46705078 died through the
    ERROR path during a DNS outage — its stop call could not resolve
    console.vast.ai, five reap retries failed, and the reset never ran. Its
    3660 s of load survived into the next box's numbers, so `rq status` read
    `load 4152s (52%)` on an instance whose real figures were 492 s load
    against 2923 s render: 14 %, healthy. The exact sum 3660 + 492 = 4152 is
    how the contamination was proved.

    That cost a coordinator and an agent a scene-thrash investigation into a
    farm that was not thrashing. This project has been bitten by the same shape
    before: `assert_levelled` sat inside `if not a.no_rig:` and a rig-less
    build shipped un-relit. A guard on the clean path only guards the clean
    path, so the fix here is structural — a read for the wrong instance cannot
    return the previous instance's value, because the value is not reachable
    without its id matching.
    """

    __slots__ = ("iid", "load_sec", "render_sec", "fetch_samples", "switch_cost")

    def __init__(self, iid: Optional[int]) -> None:
        self.rebind(iid)

    def rebind(self, iid: Optional[int]) -> None:
        self.iid = iid
        self.load_sec: float = 0.0
        self.render_sec: float = 0.0
        self.fetch_samples: list[float] = []
        self.switch_cost: dict[str, float] = {}


class Fleet:
    """One instance, its tunnel, and the money it is spending."""

    def _bound(self) -> "_PerInstance":
        """The accounting for the CURRENT instance, rebinding if it changed.

        Every read and every write of the four per-instance measurements goes
        through here, which is what makes a stale value unreachable rather than
        unlikely: adopting a different instance id — however the previous one
        died, and whether or not any teardown path ran — reaches a zeroed
        record on the very next access.
        """
        # Lazily created rather than assumed: tests build a Fleet with
        # `__new__` to exercise one method without a constructor, and a
        # measurement accessor that raises on those is a worse accessor. Same
        # tolerance for `instance_id`, which those objects also lack.
        acct = getattr(self, "_acct", None)
        if acct is None:
            acct = self._acct = _PerInstance(getattr(self, "instance_id", None))
        iid = getattr(self, "instance_id", None)
        if acct.iid != iid:
            acct.rebind(iid)
        return acct

    @property
    def load_sec(self) -> float:
        return self._bound().load_sec

    @load_sec.setter
    def load_sec(self, value: float) -> None:
        self._bound().load_sec = float(value)

    @property
    def render_sec(self) -> float:
        return self._bound().render_sec

    @render_sec.setter
    def render_sec(self, value: float) -> None:
        self._bound().render_sec = float(value)

    @property
    def fetch_samples(self) -> list:
        return self._bound().fetch_samples

    @fetch_samples.setter
    def fetch_samples(self, value: list) -> None:
        self._bound().fetch_samples = value

    @property
    def switch_cost(self) -> dict:
        return self._bound().switch_cost

    @switch_cost.setter
    def switch_cost(self, value: dict) -> None:
        self._bound().switch_cost = value

    def __init__(self, local_port: int = config.TUNNEL_LOCAL_PORT):
        self.client = VastAI(raw=True, quiet=True)
        self.local_port = local_port
        # Bound to the instance that earned them; see _PerInstance. Created
        # before `instance_id` exists because the properties below read it.
        self._acct = _PerInstance(None)
        # Which instance's derived cache budget has been logged, so the line
        # lands once per box rather than on every heartbeat.
        self._budget_logged: Optional[int] = None
        self.ep: Optional[Endpoint] = None
        self.tunnel: Optional[subprocess.Popen] = None
        self.instance_id: Optional[int] = None
        self.dph: float = 0.0
        # Fraction of the box's GPUs this card is. None until an instance is
        # rented or adopted; < 0.99 means co-tenants. See _log_exclusivity.
        self.gpu_frac: Optional[float] = None
        self.started_at: Optional[float] = None
        self.scene_hash: Optional[str] = None
        # The .blend the worker currently has loaded. The dispatcher batches by
        # scene, so it needs to know which one that is without re-hashing.
        self.scene_path: Optional[Path] = None
        # Asset directories already mirrored onto *this* instance, so switching
        # back and forth between scenes does not re-push them every time.
        self.mirrored_assets: set[Path] = set()
        # switch_cost, load_sec, render_sec and fetch_samples all live in
        # `self._acct` and are reached through the properties below. They are
        # per-instance measurements, so they are KEYED by instance id rather
        # than merely cleared at teardown — see _PerInstance for the incident.
        # Which scenes have work queued against them, injected by the Broker —
        # the fleet owns no queue and must not grow one. Returns scene hashes.
        # Used to order eviction, never to forbid it: see protected_scenes.
        self.scene_demand: Callable[[], set[str]] = lambda: set()
        self.last_ready: bool = False
        self.status: str = "down"
        self.stopped_at: Optional[float] = None
        self.gpu_seconds: float = 0.0   # accumulated across hibernation cycles
        # Offers and machines that failed to come up. Re-renting the offer that
        # just timed out is how the broker paid twice for the same bad host
        # inside five minutes.
        #
        # These OUTLIVE the process (see CondemnedIds). They used to be plain
        # per-session sets, and the failure that produced this comment ran for
        # ninety minutes: machine 55313 / offer 43856614 reset every ssh
        # connection given to it, the broker spent 3 rounds x 3 attempts failing
        # to push Blender, destroyed instance 46579745 at 08:14:25 — and one
        # second later rented offer 43856614 again, because it was still the
        # cheapest thing on the market. Restarting the broker to escape made it
        # worse, not better, since the restart is what emptied the list.
        self.bad_offers: set[int] = CondemnedIds("offers")
        self.bad_machines: set[int] = CondemnedIds("machines")
        # Machines to skip WITHOUT condemning them: they stalled on the way up
        # for a reason we do not blame the hardware for, but re-renting one
        # inside the same recovery costs another full readiness budget for the
        # same answer. Deliberately a plain set, not CondemnedIds — this must
        # never reach bad_hosts.json, because persisting it would turn a
        # control-plane blip into the 24 h ban that a control-plane blip must
        # never earn. Dies with the process; cleared when offers run out.
        self.stalled_machines: set[int] = set()
        # Consecutive failed deploy rounds against the *current* instance. A
        # transport failure is retried on the same hardware until this is
        # exhausted; only then is the GPU assumed to be the problem.
        self.deploy_failures: int = 0
        # The most bytes of the Blender bundle this instance has ever been
        # observed to hold, and how many consecutive transport rounds have
        # failed to raise it.
        #
        # This pair is the exit the retry policy did not have. "Retry a dropped
        # upload rather than condemn the GPU" is right, and it is only bounded
        # by a count of rounds — so a host that resets every connection buys the
        # entire budget at full price. What tells the two apart is not how many
        # times a push failed but whether the failures are getting anywhere:
        # pushes resume, so a flaky link keeps whatever bytes it lands and its
        # high-water mark climbs, while a host that hangs up on everything ends
        # every round exactly where it started. Any progress at all resets
        # `stalled_rounds`, so the transient case keeps its full budget.
        self.transport_bytes: int = 0
        self.stalled_rounds: int = 0
        # Consecutive heartbeat failures, so the beat can escalate to asking
        # vast.ai whether the instance still exists rather than beating at a
        # box that was destroyed out from under us.
        self.heartbeat_failures: int = 0
        # COULD the instance we hold right now be holding a render we cannot
        # see? Deliberately NOT "have we talked to it". The two come apart, and
        # the difference is a permanently wedged broker.
        #
        # This is the one fact that can turn an `unknown` activity probe into a
        # safe destroy, and it exists because the tri-state rule — "only a
        # definitely-idle answer licenses a kill" — has a hole at the bottom.
        # When a host stops answering, every probe is unanswerable forever, so
        # activity() is permanently `unknown`, so the deploy path may never
        # replace the instance. Instance 46118513 sat in exactly that state:
        # three deploy rounds, sixteen minutes, every job failed, and a rented
        # 5090 billed throughout with the broker unable to escape.
        #
        # `unknown` is the right answer to "might this be rendering?" only when
        # there is something that *could* be. A render exists only where a
        # WORKER was started, so this is set at `start_worker` and nowhere else
        # — plus unconditionally on adoption, where a previous broker may have
        # left a frame in flight and nothing here can prove otherwise.
        #
        # The first version set it on any successful ssh command, which sounds
        # equivalent and is not: running `true` on a box cannot start a render,
        # yet it permanently blocked replacement. Instance 46124078 proved it —
        # ssh worked long enough to provision, the 481 MB Blender push then
        # failed at 3.5% on every retry, and the flag insisted a box that had
        # never had Blender on it at all might be mid-frame.
        self.may_hold_render: bool = False
        # Host behind the current instance, so a host-level failure can
        # blacklist the machine rather than the offer alone.
        self.machine_id: int = 0
        # Offer the current instance was rented from, so the deploy-failure
        # path can condemn it and stop the next rent buying it straight back.
        self.offer_id: int = 0
        # Consecutive failed wake-ups of a hibernated instance. vast does
        # not always act on start_instance; looping strands the queue.
        self.resume_failures: int = 0
        # Called just before teardown clears the counters, so cumulative
        # spend survives instance replacement.
        self.on_teardown = None
        # Instances whose destroy was attempted but never CONFIRMED, mapped to
        # when it was last tried. `destroy()` returning False used to be
        # silently ignored inside `_rent`'s cleanup — the broker then rented the
        # next offer while the unconfirmed one billed on, untracked, until the
        # next restart's reconciliation. These are retried from the heartbeat
        # thread until vast confirms they are gone.
        self.doomed: dict[int, float] = {}
        # Scene hashes eviction must never touch, beyond the loaded one. The
        # dispatcher pins the scene of the job it is running for the whole job,
        # so a frame in flight cannot have its .blend deleted underneath it.
        # A set, not a single value, because a pin must survive being taken and
        # released around a job that fails, and because adoption can leave a
        # render running that this broker did not start.
        self.pinned_scenes: set[str] = set()
        # Last measured disk state on the instance, sampled by the heartbeat
        # thread so `rq status` can answer without an SSH call on the event loop.
        self.disk: Optional[remote.DiskState] = None
        self._disk_sampled = 0.0
        # Last time each cached scene was marked used remotely, so the LRU touch
        # can be rate-limited instead of costing an SSH round trip per frame.
        self._touched: dict[str, float] = {}
        # The push in flight, if any: {"what", "bytes", "began"}. Set around a
        # transfer purely so `rq status` can tell slow from wedged — see
        # transfer_report. Never read by anything that makes a decision.
        self.transfer: Optional[dict] = None
        self.lock = threading.Lock()

    # --- accounting ------------------------------------------------------

    @property
    def uptime(self) -> float:
        """Seconds *running*. A hibernated instance accrues none of this."""
        if self.stopped_at or not self.started_at:
            return 0.0
        return time.time() - self.started_at

    @property
    def spend(self) -> float:
        """GPU cost so far, across every run/stop cycle of this instance.
        Disk is billed separately and is ~1.4 cents/hour on a 30 GB volume."""
        return self.dph * ((self.gpu_seconds + self.uptime) / 3600.0)

    @property
    def disk_spend(self) -> float:
        """Storage bills for as long as the instance *exists*, stopped or not —
        which is the whole reason hibernation has a deadline."""
        if not self.started_at and not self.stopped_at:
            return 0.0
        age = self.gpu_seconds + self.uptime + self.hibernated_for
        return config.DISK_GB * 0.0004667 * (age / 3600.0)

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "instance_id": self.instance_id,
            "dph": round(self.dph, 4),
            # Surfaced because the headline price is actively misleading
            # without it: the shared card cost 12 % less and rendered 1.64x
            # slower. None means the instance never reported it.
            "gpu_frac": self.gpu_frac,
            "uptime_sec": round(self.uptime, 1),
            "hibernated_sec": round(self.hibernated_for, 1),
            "gpu_usd": round(self.spend, 4),
            "disk_usd": round(self.disk_spend, 4),
            "spend_usd": round(self.spend + self.disk_spend, 4),
            "scene_hash": self.scene_hash,
            "load_sec": round(self.load_sec, 1),
            "render_sec": round(self.render_sec, 1),
            "fetch_kbps": (round(self.fetch_bps / 1000.0, 1)
                           if self.fetch_bps is not None else None),
            "fetch_samples": len(self.fetch_samples),
            "endpoint": f"{self.ep.host}:{self.ep.port}" if self.ep else None,
            "disk": self.disk_report(),
            "transfer": self.transfer_report(),
        }

    def transfer_report(self) -> Optional[dict]:
        """What is being pushed right now, if anything — for `rq status`.

        Exists because "uploading-scene" on its own is indistinguishable from
        "wedged". A 4.22 GB scene took 510 s to push on 2026-08-02 and for all
        of it the status line said only `uploading-scene`, with the disk figure
        beside it up to 300 s stale (DISK_SAMPLE_SEC). The operator's reasonable
        reading was that the broker had hung, and the actual answer — half a
        gigabyte of blend moving at 8 MB/s, exactly as fast as this link goes —
        was not on offer anywhere.

        Local state only: no SSH, no blocking, nothing that can fail. It reports
        WHAT and HOW BIG and HOW LONG SO FAR, which is enough to tell a slow
        transfer from a dead one. It deliberately does not claim a byte count —
        the bytes are inside a zstd pipe and this would have to guess.
        """
        t = self.transfer
        if not t:
            return None
        elapsed = time.time() - t["began"]
        return {
            "what": t["what"],
            "mb": round(t["bytes"] / 1e6, 1),
            "elapsed_sec": round(elapsed, 1),
            "mb_per_sec": round(t["bytes"] / 1e6 / elapsed, 2) if elapsed > 1 else None,
        }

    def disk_report(self) -> dict:
        """The instance's disk, for `rq status`. Never blocking, never invented.

        Served from the heartbeat thread's last sample. A disk that has not been
        measured reports `measured: false` with a reason rather than zeroes: an
        operator scanning this line must not be able to read "0 used" as room to
        spare — that is the R2-018 failure (a gate that passed while measuring
        nothing) wearing a different hat.
        """
        state = self.disk
        if state is None:
            return {"measured": False,
                    "detail": "not sampled yet" if self.ep else "no instance"}
        if not state.ok:
            return {"measured": False, "detail": state.detail,
                    "age_sec": round(time.time() - state.measured_at, 1)}
        return {
            "measured": True,
            "age_sec": round(time.time() - state.measured_at, 1),
            "total_gb": round(state.total / 1e9, 2),
            "used_gb": round(state.used / 1e9, 2),
            "free_gb": round(state.free / 1e9, 2),
            "pct_used": round(100.0 * state.used / max(state.total, 1), 1),
            "cache_gb": round(state.cache_bytes / 1e9, 2),
            "scene_count": state.scene_count,
            "other_gb": round(state.other_bytes / 1e9, 2),
            "budget_gb": round(remote.cache_budget(
                state, int(config.DISK_RESERVE_GB * 1e9)) / 1e9, 2),
            "reserve_gb": round(config.DISK_RESERVE_GB, 2),
        }

    def sample_disk(self, force: bool = False) -> Optional[remote.DiskState]:
        """Re-measure the instance's disk. Called from the heartbeat thread.

        Rides the heartbeat because that is the only thread still running during
        a multi-hour render, and it runs *after* the beat has been sent, so a
        slow or hanging `du` can never delay the file the in-container watchdog
        is reading. One SSH command; measured at 0.38 s over 42 cached scenes.
        """
        if not self.ep or self.stopped_at:
            return None
        if not force and time.time() - self._disk_sampled < config.DISK_SAMPLE_SEC:
            return self.disk
        self._disk_sampled = time.time()
        state = remote.disk_state(self.ep, timeout=120)
        self.disk = state
        if not state.ok:
            log.warning("could not measure the instance disk: %s", state.detail)
            return state
        reserve = int(config.DISK_RESERVE_GB * 1e9)
        # Once per instance, on the first successful measurement: the budget is
        # derived from this disk now, so the number that governs eviction must
        # be visible next to the disk it was derived from. A derived value that
        # is never printed is a magic number with extra steps.
        if self._budget_logged != self.instance_id:
            self._budget_logged = self.instance_id
            log.info("%s", remote.describe_cache_budget(state, reserve))
        if state.free < reserve:
            log.warning(
                "DISK LOW on instance %s: %s — under the %.1f GB reserve. The next "
                "scene upload will evict harder, and may refuse. Check with: "
                "rq status",
                self.instance_id, state.describe(), config.DISK_RESERVE_GB)
        return state

    # --- scene pins ------------------------------------------------------

    def pin_scene(self, digest: str) -> None:
        """Protect a scene from eviction for as long as a job is using it.

        The loaded scene is protected anyway, but the two facts come apart: a
        job can be dispatched, its scene uploaded, and the worker restarted
        before `scene_hash` is updated — and `scene_hash` is deliberately only
        written once a worker is actually serving that scene.
        """
        if digest:
            self.pinned_scenes.add(digest)

    def unpin_scene(self, digest: str) -> None:
        self.pinned_scenes.discard(digest)

    def protected_scenes(self) -> set[str]:
        """Every scene hash eviction must leave alone."""
        keep = set(self.pinned_scenes)
        if self.scene_hash:
            keep.add(self.scene_hash)
        return keep

    def demanded_scenes(self) -> set[str]:
        """Scene hashes with jobs still waiting on them — evict these LAST.

        Deliberately NOT part of `protected_scenes`. A hard pin is the wrong
        instrument: with five agents queuing against five scenes, "has queued
        work" can cover the whole cache, and an unevictable cache turns a
        policy ceiling into `DiskFull` — a refused job where the old code
        merely paid for a re-push. Physics must always win over intent.

        What was actually wrong is subtler than "it can be evicted": it was
        evicted **first**. Eviction is LRU by last use, and a scene nobody has
        selected yet has by definition never been touched, so a 4.5 GB scene
        with sixteen jobs queued sorted *ahead* of an idle scene finished with
        hours ago. Ordering demand last fixes that without forbidding anything:
        a scene with work waiting is evicted only when evicting everything
        idle was not enough.
        """
        with contextlib.suppress(Exception):
            return set(self.scene_demand())
        return set()

    # --- download throughput --------------------------------------------

    def note_fetch(self, nbytes: int, seconds: float) -> None:
        """Record one real fetch's throughput.

        Fed from the fetches the broker already performs, so the measurement is
        free and is of the traffic that actually matters — returning frames —
        rather than of a synthetic probe that would cost bandwidth and get
        switched off.

        Small transfers are ignored: at 265 ms RTT a 100 KB file is nearly all
        handshake and reports a rate that measures latency, not bandwidth.
        """
        if nbytes < config.FETCH_SAMPLE_MIN_BYTES or seconds <= 0:
            return
        self.fetch_samples.append(nbytes / seconds)
        del self.fetch_samples[:-int(config.FETCH_SAMPLE_WINDOW)]

    @property
    def fetch_bps(self) -> Optional[float]:
        """Median observed download rate, or None until it is worth believing.

        Median rather than mean: one frame fetched while a scene push shares
        the link is not evidence about the link.
        """
        if len(self.fetch_samples) < int(config.FETCH_MIN_SAMPLES):
            return None
        ordered = sorted(self.fetch_samples)
        return ordered[len(ordered) // 2]

    def download_too_slow(self) -> Optional[str]:
        """Why this instance cannot return results, or None if it can.

        The gap this closes: every other transport check counts FAILURES, and a
        link that delivers slowly never fails. It never times out, never stalls
        a round, never spends the transport budget — so it looks healthy
        forever while nothing arrives.
        """
        bps = self.fetch_bps
        if bps is None or bps >= config.FETCH_MIN_KBPS * 1000.0:
            return None
        return (
            f"download is {bps / 1000.0:.1f} KB/s over "
            f"{len(self.fetch_samples)} real fetch(es), under the "
            f"{config.FETCH_MIN_KBPS:.0f} KB/s floor — this instance renders "
            f"fine and cannot return the results. An 8 MB frame would take "
            f"{8e6 / bps / 60.0:.1f} min to fetch."
        )

    def condemn_slow_link(self, why: str) -> None:
        """Blacklist the OFFER for a link that cannot deliver, and destroy.

        The offer, deliberately **not** the machine. A transport wipeout —
        every push dead across three rounds — proves a host's link and earns a
        machine ban. This is one container's measured path over one rental, and
        banning a machine for 24 h on that is heavier than the evidence
        supports. Condemning the offer is enough to move the next rent
        elsewhere, which is the whole objective.
        """
        offer_id = getattr(self, "offer_id", 0)
        if offer_id:
            self.bad_offers.add(offer_id)
            log.error("offer %s blacklisted — %s", offer_id, why)
        log.error("instance %s destroyed for an unusable DOWNLOAD path: %s. "
                  "It passed every other health check; none of them can see "
                  "slow, only failed.", self.instance_id, why)
        self.teardown("download too slow")

    def reload_cost_sec(self, scene: Optional[Path] = None) -> float:
        """Seconds to make `scene` (default: the loaded one) live again.

        Measured where we have measured it, estimated from size where we have
        not — never zero, because a zero here would tell the dispatcher that
        abandoning a 4.5 GB scene is free.
        """
        digest = None
        if scene is None:
            scene = self.scene_path
            # Already known for the loaded scene, and this runs on the dispatch
            # loop: `scene_hash` re-reads the whole file whenever its memo misses
            # on (mtime, size), and re-reading 4.5 GB to price a decision would
            # cost more than the decision.
            digest = self.scene_hash
        if scene is None:
            return 0.0
        with contextlib.suppress(OSError):
            if digest is None:
                digest = remote.scene_hash(scene)
            measured = self.switch_cost.get(digest)
            if measured is not None:
                return measured
            gb = scene.stat().st_size / 1e9
            return config.SCENE_RELOAD_BASE_SEC + config.SCENE_RELOAD_SEC_PER_GB * gb
        return float(config.SCENE_RELOAD_BASE_SEC)

    def mark_scene_used(self, digest: str, min_interval: float = 120.0) -> None:
        """Stamp a cached scene as just-used, so the LRU order is by USE.

        Rate-limited: `ensure_ready` runs once per frame, and a 3,000-frame
        sequence must not buy 3,000 SSH round trips to re-learn that the same
        scene is still the one in use. Two minutes is far finer than the
        eviction it feeds, which only ever compares scenes hours apart.
        """
        if not self.ep or self.stopped_at or not digest:
            return
        now = time.time()
        if now - self._touched.get(digest, 0.0) < min_interval:
            return
        self._touched[digest] = now
        with contextlib.suppress(Exception):
            remote.touch_scene(self.ep, digest)

    # --- endpoints -------------------------------------------------------

    @staticmethod
    def _endpoint(inst) -> Optional[Endpoint]:
        """Endpoint for an instance, flagged with whether it is direct.

        The proxy relay is a valid fallback but a 5.4x slower one, and a cold
        start pushes 481 MB. Recording which one we got means the log can say so
        when a transfer crawls, instead of leaving it a mystery.
        """
        pair = inst.ssh
        if not pair:
            return None
        direct = inst.ssh_direct is not None
        if not direct:
            log.warning(
                "instance %s published no direct port — falling back to the vast "
                "proxy relay at %s:%s (measured 6.9 Mbps vs 37 Mbps direct); "
                "transfers will be slow",
                inst.id, pair[0], pair[1],
            )
        return Endpoint(host=pair[0], port=pair[1], instance_id=inst.id, direct=direct)

    # --- startup reconciliation -----------------------------------------

    def _log_exclusivity(self, inst) -> None:
        """Record whether the card we just took is ours alone. Never silent."""
        frac = inst.gpu_frac
        self.gpu_frac = frac
        if frac is None:
            log.warning("instance %s does not report gpu_frac — exclusivity "
                        "UNKNOWN, treat as shared until measured", inst.id)
        elif frac >= vastctl.EXCLUSIVE_GPU_FRAC:
            log.info("instance %s is EXCLUSIVE (gpu_frac %.3f) — no co-tenant "
                     "can take VRAM or GPU time on this card", inst.id, frac)
        else:
            log.warning(
                "instance %s is SHARED: gpu_frac %.3f — about 1/%.0f of the "
                "box, with co-tenants on our card. This is the R2-382 class "
                "(a co-tenant held 17,737 MiB while Cycles returned "
                "zero-filled buffers) and it measured 1.64x SLOWER PER FRAME "
                "than an exclusive 5090 on identical work. The gpu_frac>=0.99 "
                "filter runs at RENT time only, so adoption cannot fix this — "
                "destroy the instance and let dispatch rent a fresh one.",
                inst.id, frac, 1.0 / frac if frac else 0)

    def adopt_or_reap(self) -> Optional[int]:
        """Take over one healthy instance from a previous run; destroy the rest.

        A blanket reap would make restarting the broker cost a full cold start —
        a new rental, image pull, Blender install and scene upload — and would
        throw away a GPU that agents are actively waiting on. Adopting keeps the
        restart free while still guaranteeing that nothing is left orphaned:
        every instance is either taken over here or destroyed.
        """
        adopted: Optional[int] = None
        for inst in vastctl.our_instances(self.client):
            state = inst.classify()
            if adopted is None and state in ("running", "cold"):
                self.instance_id = inst.id
                self.dph = inst.dph
                # SAY WHAT WE ARE ADOPTING. The gpu_frac>=0.99 filter runs at
                # RENT time and adoption bypasses it forever — see
                # vastctl.Instance.gpu_frac for the measurement. A shared card
                # is not a billing detail: it is the R2-382 co-tenant class,
                # and it measured 1.64x slower per frame than an exclusive
                # 5090 on identical work. Adoption cannot refuse it — there may
                # be a frame in flight on it, and stranding the queue to
                # re-rent is worse — but it must never be SILENT again.
                self._log_exclusivity(inst)
                # Carry the host across adoption, or a broker restart loses the
                # ability to blacklist the machine it is about to condemn — and
                # blacklists are per-session, so a restart is exactly when the
                # list is empty and the bad host is most re-rentable.
                self.machine_id = int(inst.raw.get("machine_id") or 0)
                endpoint = inst.ssh
                if state == "cold":
                    # Stopped. Its stop time is not exposed, so restart the
                    # hibernation clock from now — conservative in the safe
                    # direction only if the deadline still fires, which it does.
                    self.stopped_at = time.time()
                    self.status = "stopped"
                    log.info("adopted stopped instance %s (hibernating)", inst.id)
                elif endpoint:
                    ep = self._endpoint(inst)
                    # Ask the instance whether it already has an owner. The
                    # singleton lock is per state directory; instance ownership
                    # is per vast.ai account, and the two keys come apart the
                    # moment VASTRENDER_LOCK is overridden. The heartbeat is
                    # signed with the beating broker's pid precisely so this
                    # question has an answer. See remote.foreign_broker.
                    held = None
                    if ep is not None:
                        with contextlib.suppress(Exception):
                            held = remote.foreign_broker(ep)
                    if held:
                        self.instance_id = None
                        self.dph = 0.0
                        raise remote.ForeignBroker(
                            f"instance {inst.id} is NOT ours to take: {held}. "
                            f"Adopting it would pull the GPU out from under a "
                            f"running batch and destroy it at this process's "
                            f"next shutdown — the exact bug state/broker.lock "
                            f"exists to prevent, reachable again whenever "
                            f"VASTRENDER_LOCK points somewhere else. Stop this "
                            f"broker; there is already one running."
                        )
                    self.ep = ep
                    self.started_at = time.time()
                    # Adoption assumes contact and assumes the instance may be
                    # busy: a previous broker could have deployed a worker and
                    # left a frame in flight, and the fast-replace path must
                    # never open on a guess.
                    #
                    # The one exception is an instance that REFUSES OUR KEY.
                    # Restarting the broker must not be able to launder an
                    # unusable instance into a protected one — the first version
                    # of this flag did exactly that, and a restart would have
                    # re-wedged the broker onto the same dead host it had just
                    # learned to replace.
                    #
                    # An auth rejection is safe to act on here for a reason the
                    # generic unreachable case does not have: vast.ai writes the
                    # authorised key when the CONTAINER STARTS. A container that
                    # does not have it either never provisioned it, or has been
                    # restarted since — and a restart has already killed any
                    # Blender process that was rendering. Either way there is no
                    # frame left to protect. It also cannot belong to a sibling
                    # broker: ownership is per vast.ai account, this is the
                    # account's only key, so a broker that cannot authenticate
                    # here proves none of ours can.
                    probe = remote.probe(ep, "true", timeout=30) if ep else None
                    if probe is not None and probe.auth_rejected:
                        self.may_hold_render = False
                        log.error(
                            "adopted instance %s REFUSES our ssh key (%s). sshd is "
                            "serving and denied publickey auth, so this container "
                            "never had the key written into it. It holds no frame "
                            "we could reach and none we could have started — it is "
                            "replaceable, not protected.",
                            inst.id, probe.describe(),
                        )
                    else:
                        self.may_hold_render = True
                    self.status = "adopted"
                    log.info("adopted running %s over %s", self.ep,
                             "a direct port" if self.ep and self.ep.direct
                             else "THE PROXY RELAY")
                else:
                    log.warning("instance %s is running but has no SSH (%s) — destroying",
                                inst.id, inst.status_detail)
                    vastctl.destroy(self.client, inst.id)
                    self.instance_id = None
                    continue
                adopted = inst.id
            else:
                log.warning("destroying stray instance %s (%s)", inst.id, state)
                vastctl.destroy(self.client, inst.id)
        return adopted

    # --- bring up --------------------------------------------------------

    def activity(self, attempts: int = 3) -> remote.Activity:
        """What the instance says the worker is doing. Unknown when there is no
        endpoint to ask — never "idle", which would license a kill."""
        if not self.ep:
            return remote.Activity(reachable=False, detail="no endpoint")
        if self.stopped_at:
            # A stopped container runs no processes at all. This is a real
            # answer, and cheap: probing a hibernated instance would otherwise
            # spend three SSH connect timeouts to learn nothing on every resume.
            return remote.Activity(reachable=True, detail="instance is stopped")
        return remote.activity(self.ep, attempts=attempts)

    def reconcile(self, why: str, locked: bool = True) -> str:
        """Ask vast.ai whether the instance we are talking to still exists.

        Returns vast's own classification — running | loading | cold | bad |
        gone | unknown — and, on `gone`, drops the instance locally so the next
        pass rents a fresh one.

        **Nothing used to ask this, and that is a bigger hole than any retry
        policy.** Every liveness signal the broker has runs over SSH to an
        endpoint, so "the instance answers" and "the instance exists" are the
        same question asked the same way, and when the answer is no the broker
        cannot tell a flaky link from a box that is not there any more. Observed
        2026-08-02: instance 46585570 was destroyed out of band and verified
        gone, and the broker went on reporting `waiting-for-ssh instance=
        46585570`, on course to spend a 900 s ssh timeout and then a full deploy
        budget on a machine that no longer existed. It is not a manual-teardown
        curiosity either: a preempted bid instance looks exactly like this from
        here, and bids are the only way some of the cheaper offers are rentable.
        One `show_instances` call turns fifteen minutes of waiting on a ghost
        into an immediate re-rent.

        `unknown` on an API error is deliberate, and it is the safe direction:
        vast being unreachable is not evidence about our instance, and treating
        it as `gone` would drop a live rented GPU on the floor still billing —
        the local record of it is the only thing that will ever destroy it.

        `locked` says whether the caller already holds `self.lock`. The deploy
        path does — `ensure_ready` holds it across the whole deploy, including
        the 481 MB push — so it must not try to take it again, and its mutation
        is already protected. The HEARTBEAT thread holds nothing and must never
        block on that lock: waiting for it would gate liveness signalling on
        "deploy finished", which is the exact coupling the in-container watchdog
        punishes by destroying a healthy instance mid-deploy. So it takes the
        lock only if it is free, and otherwise just reports — a deploy is in
        flight, and that deploy does its own reconcile at the end of the round.
        """
        iid = self.instance_id
        if not iid:
            return "gone"
        try:
            mine = {i.id: i for i in vastctl.our_instances(self.client)}
        except Exception as exc:
            log.warning(
                "could not reconcile instance %s with vast.ai (%s) — assuming it "
                "still exists. Dropping it on an API error would strand a rented "
                "GPU that only this record can destroy.",
                iid, remote.diagnose(exc),
            )
            return "unknown"

        found = mine.get(iid)
        if found is None:
            log.error(
                "instance %s DOES NOT EXIST on vast.ai any more (noticed via %s). "
                "It was destroyed out of band — by hand, by a reap, or by a bid "
                "being preempted. Forgetting it locally and renting fresh; there "
                "is nothing to tear down and nothing to bill.",
                iid, why,
            )
            if locked:
                self._forget_vanished()
            elif self.lock.acquire(blocking=False):
                try:
                    # Re-check under the lock: the dispatcher may have replaced
                    # the instance between the API call and here, and forgetting
                    # a DIFFERENT, live instance would strand a rented GPU.
                    if self.instance_id == iid:
                        self._forget_vanished()
                finally:
                    self.lock.release()
            else:
                log.warning(
                    "instance %s is gone but a deploy holds the fleet lock — "
                    "leaving the cleanup to it rather than stalling the "
                    "heartbeat thread behind a 481 MB push.", iid,
                )
            return "gone"

        state = found.classify()
        if state in ("bad", "cold"):
            log.warning(
                "instance %s still exists but vast.ai reports it %s (%s), noticed "
                "via %s — ssh will not recover on its own.",
                iid, state, found.status_detail(), why,
            )
        return state

    def _forget_vanished(self) -> None:
        """Drop an instance vast.ai says is gone, WITHOUT trying to destroy it.

        Deliberately not `_teardown_locked`: that calls destroy and logs a
        spend line for an instance that no longer exists, and a destroy of an
        unknown id is an API error that would be read as "teardown failed" and
        put the id on the doomed-retry list forever. Spend is still banked —
        the GPU really did run and really was paid for.
        """
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
            self.tunnel = None
        if self.ep:
            remote.close_master(self.ep)
        if self.on_teardown:
            try:
                self.on_teardown()
            except Exception as exc:
                log.warning("could not bank spend: %s", remote.diagnose(exc))
        self.ep = None
        self.instance_id = None
        # Per-instance, like gpu_seconds. Carrying a dead box's exclusivity
        # onto its replacement is exactly the silence this field exists to end.
        self.gpu_frac = None
        self.started_at = None
        self.stopped_at = None
        self.gpu_seconds = 0.0
        self.scene_hash = None
        self.scene_path = None
        # Per-instance, like gpu_seconds: a new box has a cold scene cache, its
        # own load-vs-render story and its own link. Carrying any of it across
        # would hide the cold start an operator is looking for, and would judge
        # a healthy replacement on its predecessor's numbers.
        #
        # Belt and braces only. `instance_id` is already None above, so this
        # call rebinds the record to None and zeroes it — but the same would
        # happen on the next access even if this line never ran, which is the
        # point: THIS reset is exactly what a failed teardown skipped once.
        # Keep it; do not rely on it.
        self._bound()
        self.mirrored_assets = set()
        self.last_ready = False
        self.may_hold_render = False
        self.machine_id = 0
        self.offer_id = 0
        self.deploy_failures = 0
        self.transport_bytes = 0
        self.stalled_rounds = 0
        self.heartbeat_failures = 0
        self.status = "down"

    def _refuse_if_rendering(self) -> None:
        """Raise `WorkerBusy` if a render is in flight. Never blocks.

        Two distinct situations arrive here and neither may touch the worker:

          * a render this broker did not start — the worker survives a broker
            restart, so a fresh dispatcher meets a frame already in progress;
          * a render of the very job now being dispatched — a requeue after a
            dropped job socket, where the frame is often nearly finished.

        The previous shape of this blocked in a poll loop *while holding the
        fleet lock*, which made every waiting decision the fleet's and hid the
        second case entirely: the caller never learned whose frame it was, and
        the fast path (`last_ready` and the right scene already loaded) skipped
        the wait altogether and handed out an endpoint whose worker was mid-frame
        — so the job was rendered a second time on top of itself.

        Raising instead puts one decision in one place, in the dispatcher, which
        is the only code that knows which job it is running: reattach to my own
        frame, queue behind someone else's, redeploy only over a worker that is
        demonstrably not rendering.
        """
        act = self.activity()
        if not act.rendering:
            return
        raise remote.WorkerBusy(
            f"instance {self.instance_id} is {act.describe()} — refusing to touch the "
            f"worker. Deploying over it would SIGKILL a frame that is minutes or hours "
            f"of GPU time.",
            job_id=act.job_id,
            progress=act.progress,
        )

    def ensure_ready(self, scene: Path) -> Endpoint:
        with self.lock:
            # Never deploy over a render in progress, and never hand out a
            # worker that is inside one.
            self._refuse_if_rendering()
            if self.ep and self.last_ready and self._worker_alive():
                if self.scene_hash == remote.scene_hash(scene):
                    # Stamp it as used. "Least recently used" has to mean the
                    # last time a JOB wanted this scene, not the last time one
                    # was uploaded — otherwise a scene rendered all day looks
                    # older than one pushed an hour ago and never opened, and
                    # the eviction throws away exactly the wrong one.
                    self.mark_scene_used(self.scene_hash)
                    return self.ep
                # A scene switch is NOT a redeploy. Blender, the worker source
                # and the other cached scenes are all still in place; only the
                # loaded .blend has to change. Doing this as a full redeploy
                # re-pushed a 481 MB bundle and re-provisioned for what is
                # really a worker restart.
                if self._switch_scene(scene):
                    return self.ep
                # Switching failed — fall through to the shared retry path,
                # which treats a dropped upload as retryable rather than as a
                # reason to destroy the GPU.

            # Hibernated: the disk still holds Blender and the scene, so waking
            # is seconds of container start plus a worker restart rather than a
            # fresh rental, an image pull, and a 63 MB upload.
            if self.instance_id and self.stopped_at:
                try:
                    self._resume(scene)
                    self.resume_failures = 0
                    assert self.ep
                    return self.ep
                except Exception as exc:
                    self.resume_failures += 1
                    log.warning("resume of instance %s failed (%d/%d): %s",
                                self.instance_id, self.resume_failures,
                                RESUME_ATTEMPTS, remote.diagnose(exc))
                    if self.resume_failures < RESUME_ATTEMPTS:
                        raise
                    # vast is not acting on start_instance — observed as
                    # `actual=exited, intended=stopped` for 490 s per attempt.
                    # Looping on it strands the queue behind an instance that is
                    # never coming back, so let go and rent working hardware.
                    #
                    # Destroyed HERE, not left for "the hibernation deadline":
                    # that deadline lives in maybe_idle_down, which returns the
                    # moment fleet.instance_id is falsy — so the previous shape
                    # of this code forgot the instance and then promised a
                    # safety net that could never fire, leaving a stopped
                    # container (which runs no watchdog) billing storage until
                    # a human noticed. Destroying a *stopped* instance loses no
                    # render — a stopped container runs no processes — and an
                    # instance that will not wake has no other value left.
                    stranded = self.instance_id
                    log.error(
                        "giving up on hibernated instance %s after %d failed "
                        "resumes — destroying it (a stopped container runs "
                        "nothing, so nothing is lost) and renting a replacement.",
                        stranded, self.resume_failures,
                    )
                    self._teardown_locked("resume failed; instance will not wake")
                    self.resume_failures = 0

            # An adopted instance, or one whose worker died, still has usable
            # hardware with Blender and possibly the scene already on disk.
            # Redeploying onto it is far cheaper than renting a replacement, so
            # only discard it if that genuinely fails.
            if self.ep:
                if self._try_deploy(scene, "existing instance"):
                    return self.ep
                if self.ep:
                    # Kept deliberately: transport failed, the hardware did not.
                    # Renting here would leave two instances billing at once.
                    raise RuntimeError(
                        f"deploy to instance {self.instance_id} failed on transport "
                        f"({self.deploy_failures}/{MAX_TRANSPORT_ROUNDS} rounds) — "
                        f"keeping the GPU, the job will retry on it"
                    )
                # Torn down by _try_deploy; fall through and rent a replacement.

            self._rent()
            if not self._try_deploy(scene, "freshly rented instance"):
                raise RuntimeError(
                    f"deploy failed on freshly rented instance {self.instance_id} "
                    f"({self.deploy_failures}/{MAX_TRANSPORT_ROUNDS} rounds)"
                )
            assert self.ep
            return self.ep

    def _wait_for_worker(self, ep: Endpoint, scene: Path) -> "remote.WaitResult":
        """Block until the new worker answers, with a budget the scene earns.

        Two things used to be wrong here and they compounded. The budget was a
        flat 1800 s regardless of scene size, and the wait was silent — the
        broker log simply stopped between "scene uploaded" and either "worker
        ready" or, 30 minutes later, a failure. A 4.17 GB scene whose worker was
        demonstrably healthy (load average 99, 22 GB resident, GPU busy, the
        warm-up sweep advancing once a minute in the instance's own worker.log)
        was therefore indistinguishable from a wedged one, condemned, and
        redeployed — which restarted the sweep it had just been condemned for
        not finishing.

        So the budget now scales with the bytes (config.worker_ready_budget) and
        the wait narrates itself from the instance's log, which is the only
        place the truth was ever written down.
        """
        size = scene.stat().st_size
        budget = config.worker_ready_budget(size)
        log.info("waiting for worker on %s: %s is %.2f GB, so the readiness budget "
                 "is %.0f s (%.0f base + %.0f/GB)",
                 ep, scenes.label(scene), size / 1e9, budget,
                 config.WORKER_READY_SEC, config.WORKER_READY_PER_GB_SEC)

        def report(elapsed: float, total: float) -> None:
            got = remote.probe(ep, f"tail -3 {config.REMOTE_ROOT}/worker.log", timeout=60)
            note = " | ".join(ln.strip() for ln in got.out.strip().splitlines()[-2:]) \
                if got.ok else got.describe()
            log.info("still waiting for worker — %.0f s of %.0f s. Instance says: %s",
                     elapsed, total, note or "worker.log is empty")

        return remote.wait_worker(self.local_port, timeout=budget,
                                  tunnel=self.tunnel, on_wait=report)

    def _switch_scene(self, scene: Path) -> bool:
        """Point the running worker at a different .blend. True on success.

        Implemented as a worker *relaunch* rather than an in-process
        `bpy.ops.wm.open_mainfile`. The dominant cost is identical either way —
        the per-camera OptiX prewarm, measured at 40-60 s — while a relaunch
        reuses the launch path that is already hardened: pid-verified kill that
        cannot match its own checking shell, a real `/proc/net/tcp` port check,
        and `setsid --fork` detachment. `open_mainfile` would additionally drop
        the render_stats handler that publishes progress, since Blender clears
        handlers on file load.

        Returns False rather than raising so the caller can fall back to its
        retry-and-do-not-destroy path.
        """
        assert self.ep
        ep = self.ep
        began = time.time()
        try:
            self.status = "switching-scene"
            log.info("switching scene to %s", scenes.label(scene))
            self.last_ready = False
            self._mirror_assets(scene)
            blend = self._ensure_scene_cached(scene)

            # The worker is relaunched a few lines below, so refreshing its code
            # here is free and it is the difference between a worker-side fix
            # taking effect at the next scene switch and taking effect at the
            # next full redeploy — which, on a warm instance held deliberately
            # for hours, may be never. `_deploy` and the resume path already do
            # this; the switch path was the one that did not.
            #
            # Guarded, and the guard is the point: a failed push here must not
            # become "this instance cannot switch scenes". That verdict starts a
            # redeploy, and on this project a redeploy has repeatedly meant a
            # healthy 5090 destroyed over something that was never the hardware.
            # The old code keeps running; that is a stale worker, not a broken
            # box.
            try:
                remote.push_file(ep, config.ROOT / "worker" / "server.py",
                                 f"{config.REMOTE_ROOT}/server.py")
            except Exception as exc:
                log.warning("could not refresh worker code before switching to "
                            "%s: %s — continuing with the worker already on the "
                            "instance", scenes.label(scene), remote.diagnose(exc))

            self.status = "starting-worker"
            # From here on this instance may hold a render, so it is no
            # longer replaceable on an unanswered probe. Set BEFORE the
            # call: start_worker can launch Blender and then fail to return.
            self.may_hold_render = True
            remote.start_worker(ep, blend)
            if self.tunnel and self.tunnel.poll() is None:
                self.tunnel.terminate()
            self.tunnel = remote.open_tunnel(ep, self.local_port)
            time.sleep(2)
            ready = self._wait_for_worker(ep, scene)
            if not ready:
                tail = remote.probe(ep, f"tail -40 {config.REMOTE_ROOT}/worker.log",
                                    timeout=120)
                raise remote.WorkerUnreachable(
                    f"worker did not come back after switching to "
                    f"{scenes.label(scene)} — {ready.describe()}. "
                    f"remote worker.log: {tail.out.strip() or tail.describe()}",
                    tunnel_died=ready.tunnel_died,
                    local=ready.local_bind_failed,
                )
            self._report_missing(ep, scene)

            self.scene_hash = remote.scene_hash(scene)
            self.scene_path = scene
            self.last_ready = True
            self.status = "ready"
            cost = time.time() - began
            # What it cost to get here is what it will cost to come back. The
            # dispatcher reads this to decide whether leaving is affordable.
            self.switch_cost[self.scene_hash] = cost
            self.load_sec += cost
            log.info("scene switch to %s complete in %.1fs (no redeploy)",
                     scenes.label(scene), cost)
            return True
        except remote.DiskFull:
            # Not a switch failure. Falling through to the deploy path would
            # re-run the identical preflight against the identical disk, three
            # more times, and then start reasoning about whether the GPU is
            # broken. The disk is full; say so once.
            raise
        except Exception as exc:
            # Counted even though it failed. The GPU was rented for every one
            # of those seconds and rendered nothing in them; a load-vs-render
            # ratio that quietly drops the failures flatters exactly the case
            # an operator most needs to see.
            self.load_sec += time.time() - began
            log.warning("scene switch to %s failed after %.1fs: %s",
                        scenes.label(scene), time.time() - began, remote.diagnose(exc))
            return False

    # --- deploy with a retry policy that does not burn GPUs --------------

    def _try_deploy(self, scene: Path, what: str) -> bool:
        """Deploy with retries. Returns True on success.

        Returns False *keeping the instance* when every attempt failed for
        transport reasons and the per-instance budget is not yet exhausted —
        the skill's rule that a failed transfer must not destroy the instance.
        Tears the instance down and returns False when the failures implicate
        the host itself, or when transport has failed too many rounds to keep
        blaming the network.
        """
        errors: list[Exception] = []
        for attempt in range(1, DEPLOY_ATTEMPTS + 1):
            try:
                log.info("deploying onto %s %s (attempt %d/%d)",
                         what, self.instance_id, attempt, DEPLOY_ATTEMPTS)
                started = time.time()
                self._deploy(scene)
                self.deploy_failures = 0
                self.stalled_rounds = 0
                self.transport_bytes = 0
                log.info("deploy finished in %.1fs", time.time() - started)
                return True
            except remote.WorkerBusy:
                # Not a failure: the worker is mid-render. Counting this would
                # burn the retry budget and then classify it host-level, and the
                # replace-the-hardware branch would destroy a GPU that is deep
                # into an expensive frame. Hand it straight back up.
                raise
            except remote.DiskFull:
                # Also not a failure of this instance. The preflight has already
                # evicted everything evictable and MEASURED that the scene still
                # does not fit; three more attempts measure the same disk, and
                # the host-level branch below would then destroy the GPU and rent
                # another one with an identically sized volume. Straight up to the
                # dispatcher, which fails the job with the numbers in the message.
                raise
            except remote.SshNeverReady as exc:
                # No point doing this two more times. `wait_ssh` has already
                # spent four minutes patiently retrying the cheapest possible
                # command; the answer will not change in the next five seconds,
                # and each repeat is another four minutes of GPU billing.
                errors.append(exc)
                log.error("instance %s: %s — not retrying this round",
                          self.instance_id, remote.diagnose(exc))
                # Four minutes of silence from a box is the strongest cue this
                # code gets that there may be no box. Ask vast before spending
                # anything else; if it is gone there is nothing here to keep,
                # retry, condemn or destroy.
                if self.reconcile("sshd never accepted a command") == "gone":
                    return False
                break
            except Exception as exc:
                errors.append(exc)
                log.warning("deploy attempt %d/%d on instance %s failed: %s",
                            attempt, DEPLOY_ATTEMPTS, self.instance_id, remote.diagnose(exc))
                if attempt < DEPLOY_ATTEMPTS:
                    time.sleep(5 * attempt)

        self.deploy_failures += 1
        # What counts as "transport" — i.e. retry, do not replace the GPU.
        #
        # A TransferError is a dropped upload. An SshError carrying rc 255 is
        # ssh saying "I could not connect at all", which indicts the network,
        # not the host: instance 45907537 rendered fine while intermittently
        # refusing SSH, and calling that host-level would have destroyed a
        # working GPU. Anything else — a command that actually ran and failed —
        # does implicate the instance.
        #
        # An auth rejection is the one exit-255 that is NOT transport. sshd
        # answered; it read our key and refused it. Counting that as "the
        # network might be flaky" is what made the broker retry a host that had
        # already given its final answer.
        def is_auth(e: Exception) -> bool:
            if isinstance(e, remote.SshNeverReady):
                return e.auth
            return isinstance(e, remote.SshError) and e.ran.auth_rejected

        # A dead SSH forward is transport by its own account — the message has
        # always said so, only the type was missing. And a forward that could
        # not BIND is not the remote's problem at all, it is ours.
        def is_local_fault(e: Exception) -> bool:
            return isinstance(e, remote.WorkerUnreachable) and e.local

        def is_transport(e: Exception) -> bool:
            if isinstance(e, remote.TransferError):
                return True
            if is_auth(e):
                return False
            if isinstance(e, remote.WorkerUnreachable):
                return e.tunnel_died or e.local
            return isinstance(e, remote.SshError) and e.ran.transport_failed

        # The worker came up on a reachable box and still never answered. The
        # tunnel was fine and the bind was fine, so what failed is most likely
        # the SCENE — a .blend that crashes or hangs Blender on load looks
        # exactly like this. Replacing the hardware cannot fix it, and doing so
        # in a loop costs a rental plus a 481 MB Blender push plus a 291 MB
        # scene push per attempt while the fault travels along with the scene.
        def is_scene_fault(e: Exception) -> bool:
            return (isinstance(e, remote.WorkerUnreachable)
                    and not e.tunnel_died and not e.local)

        auth_failed = any(is_auth(e) for e in errors)
        local_fault = any(is_local_fault(e) for e in errors)
        scene_fault = errors and all(is_scene_fault(e) for e in errors)
        transport_only = all(is_transport(e) for e in errors)

        if scene_fault:
            log.error(
                "instance %s is reachable and its tunnel is healthy, but the "
                "worker never became ready — that indicts the SCENE, not the "
                "GPU. KEEPING the instance and failing the job: a .blend that "
                "will not load will not load on new hardware either, and "
                "replacing the box would re-rent, re-push Blender and re-push "
                "the scene for the identical failure. last: %s",
                self.instance_id, remote.diagnose(errors[-1]),
            )
            self.status = "deploy-retry"
            return False

        if local_fault:
            # Never destroy a GPU, and never condemn a machine, for something
            # that happened on this side of the wire. The local port is ours to
            # fix; the instance on the far end did nothing wrong.
            stale = remote.reap_stale_tunnels(self.local_port)
            log.error(
                "instance %s: the worker forward could not BIND on THIS machine "
                "(local port %d). That is our fault, not the host's — most "
                "likely an orphaned `ssh -L` from a kill -9'd broker. Reaped %d "
                "stale tunnel(s); KEEPING the GPU and retrying. last: %s",
                self.instance_id, self.local_port, stale,
                remote.diagnose(errors[-1]),
            )
            self.status = "deploy-retry"
            return False

        if auth_failed:
            log.error(
                "instance %s (machine %s) refuses our ssh key. This is not a "
                "flaky link and not a young container: sshd completed the "
                "handshake and denied publickey auth. vast.ai writes the "
                "authorised key at container start, so this host never "
                "provisioned it. The key itself is fine — it is the account's "
                "only key and vast reports it attached to this instance — so "
                "retrying here cannot succeed and the instance must be "
                "replaced.",
                self.instance_id, self.machine_id or "?",
            )
            # Do not rent this machine again this session. The offer alone is
            # not enough: the same host re-lists under a new offer id within
            # seconds, and we would buy the identical broken key injection.
            if self.machine_id:
                self.bad_machines.add(self.machine_id)
                log.warning("machine %s blacklisted for this session (ssh key "
                            "injection failed)", self.machine_id)

        if transport_only:
            # Every failure here was transport, so before deciding anything ask
            # the one question SSH cannot answer: is there still an instance?
            # A destroyed or preempted box produces exactly this error shape,
            # and retrying, condemning its offer or destroying it are all wrong
            # answers to "it does not exist".
            if self.reconcile("every deploy attempt failed on transport") == "gone":
                return False

            # DID THIS ROUND MOVE ANYTHING? Pushes resume, so the instance's
            # high-water mark is the honest measure of whether retrying is
            # achieving anything — not the count of attempts, which a host that
            # resets everything will happily let us spend in full.
            # getattr for the same reason `offer_id` below uses it: this path is
            # reachable on a Fleet built by hand — adoption, and every stub in
            # test_broker — that never ran __init__, and an AttributeError here
            # would take the broker down while it is holding a rented GPU.
            self.transport_bytes = getattr(self, "transport_bytes", 0)
            self.stalled_rounds = getattr(self, "stalled_rounds", 0)

            # IS THIS ROUND HOPELESS? Two independent symptoms, either of which
            # is a statement about the host rather than about the weather:
            #
            #  * it delivered nothing. Pushes resume, so a link that is merely
            #    dropping keeps whatever bytes it lands and its high-water mark
            #    climbs. A round that ends with the instance holding no more of
            #    the bundle than before achieved literally nothing, and further
            #    rounds are further nothing.
            #
            #  * a SINGLE stream was reset the same way eight were. One
            #    connection cannot be tripping a connection-rate limit or
            #    sshd's MaxStartups, so this is the far end hanging up on
            #    whatever it is given. It clears us and convicts the host.
            #
            # Either alone would be too quick to act on — the first round
            # against a young container legitimately looks like both. Requiring
            # two consecutive hopeless rounds keeps the skill's "retry before
            # condemning" for anything that might be a hiccup, while cutting
            # the pathological case from 80 minutes to about 25.
            moved = max((e.sent for e in errors
                         if isinstance(e, remote.TransferError) and e.sent > 0),
                        default=0)
            chronic = any(getattr(e, "chronic", False) for e in errors)
            progressed = moved > self.transport_bytes
            self.transport_bytes = max(self.transport_bytes, moved)
            if progressed and not chronic:
                self.stalled_rounds = 0
            else:
                self.stalled_rounds += 1

            hopeless = self.stalled_rounds >= MAX_STALLED_ROUNDS
            if not hopeless and self.deploy_failures < MAX_TRANSPORT_ROUNDS:
                log.warning(
                    "instance %s: %d transport failures this round (%d/%d rounds, "
                    "%d/%d hopeless) — KEEPING the GPU and retrying later; a dropped "
                    "upload is not broken hardware. The instance holds %.1f MB of "
                    "the bundle%s. last: %s",
                    self.instance_id, len(errors), self.deploy_failures,
                    MAX_TRANSPORT_ROUNDS, self.stalled_rounds, MAX_STALLED_ROUNDS,
                    self.transport_bytes / 1e6,
                    "" if progressed else " — unchanged by this round",
                    remote.diagnose(errors[-1]),
                )
                self.status = "deploy-retry"
                return False

            if hopeless:
                log.error(
                    "instance %s: %d consecutive deploy round(s) that got nowhere "
                    "— it holds %.1f MB of the bundle%s.%s Retrying this host again "
                    "would only buy the same answer at another ~25 minutes of "
                    "billing, so it is being condemned and replaced.",
                    self.instance_id, self.stalled_rounds,
                    self.transport_bytes / 1e6,
                    "" if progressed else ", unchanged by the last round",
                    " A SINGLE stream was reset exactly as eight were, so this is "
                    "the host hanging up and not our own concurrency."
                    if chronic else "",
                )

        # THE GATE, same tri-state rule as the idle timer: only a reachable,
        # parsed, definitely-idle answer licenses a destroy. A deploy that
        # failed on transport says nothing about the GPU — the broker's own log
        # has an instance destroyed 90 seconds after it wrote "This is a
        # TRANSPORT failure, not a statement about the render", with $1.27 of
        # frame in flight. `unknown` blocks too: an endpoint too flaky to
        # deploy over is exactly the endpoint whose activity probe fails, and
        # answering that with a destroy is the same collapse of "could not ask"
        # into "not rendering" that has cost every frame this project has lost.
        # A genuinely dead box is not kept forever — the in-container watchdog
        # (installed by vast's onstart, not by this SSH) destroys it once the
        # heartbeat is 30 min stale, and the idle timer's bounded-unknown path
        # stops it from here.
        # ...with exactly one exception, and it is not a weakening of the rule.
        # `unknown` means "we could not ask, so something might be rendering".
        # On an instance this broker rented itself and has never once run a
        # command on, there is nothing that *could* be: the worker arrives over
        # ssh and every frame is dispatched over ssh, so no contact means no
        # worker, no scene and no render. That is not an unknown, it is a known
        # empty — and treating it as unknown is what let a host with a broken
        # key injection wedge the broker permanently while billing.
        #
        # `may_hold_render` is set only where a WORKER is started, and set
        # unconditionally on adoption, so the escape hatch cannot open on an
        # instance that ever ran one or that we inherited. `rendering` still
        # blocks unconditionally, whatever the flag says.
        act = self.activity()
        empty_by_construction = not self.may_hold_render and not act.rendering
        if act.rendering or (act.unknown and not empty_by_construction):
            log.error(
                "instance %s failed %d deploy round(s) (%s) but is %s — NOT "
                "destroying it. Keeping the GPU; the job will retry, and the "
                "in-container watchdog reaps the instance if it is truly gone.",
                self.instance_id, self.deploy_failures,
                "transport budget exhausted" if transport_only else "host-level failure",
                act.describe(),
            )
            self.status = "deploy-retry"
            return False

        if act.unknown:
            log.warning(
                "instance %s is unreachable (%s), but this broker rented it and "
                "has never run a single command on it — it holds no worker and "
                "no frame, so there is nothing to lose. Replacing it.",
                self.instance_id, act.describe(),
            )

        log.warning(
            "instance %s unusable after %d rounds x %d attempts (%s) — replacing it. "
            "The instance is %s, so nothing is lost by destroying it. reasons: %s",
            self.instance_id, self.deploy_failures, DEPLOY_ATTEMPTS,
            "transport budget exhausted" if transport_only else "host-level failure",
            act.describe(),
            " || ".join(remote.diagnose(e) for e in errors[-DEPLOY_ATTEMPTS:]),
        )
        # NOTE: the machine blacklist deliberately lives in the auth branch
        # above and NOT here. Blacklisting on any host-level deploy failure is
        # too broad: "host-level" is also what a .blend that crashes Blender on
        # load looks like from here, and that would walk the broker through
        # every machine on vast.ai — a fresh rental, a 481 MB Blender push and a
        # 291 MB scene push each time — condemning good hardware for a fault
        # that travels with the scene. A failed key injection is provably the
        # host; almost nothing else at this layer is.
        #
        # The OFFER, though, must be condemned here, or destroying the instance
        # achieves nothing at all. Offers are ranked by projected cost and that
        # ranking does not change because we just failed on one, so the next
        # rent buys the identical offer back and repeats the identical failure,
        # forever, while billing. Measured 2026-08-02: machine 55313 / offer
        # 43856614 reset every ssh connection it was given; the broker burned
        # 3 rounds x 3 attempts x 4 streams failing to push Blender, destroyed
        # instance 46579745 at 08:14:25 — and re-rented offer 43856614 one
        # second later, because it was still the cheapest. Condemning the offer
        # is strictly narrower than condemning the machine, and it is the part
        # that actually breaks the loop.
        # getattr, because the deploy path is reachable on a Fleet built by
        # hand (adoption, and every stub in test_broker) that never ran
        # __init__. A missing offer id means "we do not know which offer this
        # instance came from", which is exactly the case where there is nothing
        # to condemn — not a reason to take the broker down with an
        # AttributeError while it is holding a GPU.
        offer_id = getattr(self, "offer_id", 0)
        if offer_id:
            self.bad_offers.add(offer_id)
            log.warning("offer %s blacklisted for this session — it was just "
                        "destroyed as unusable and is still the cheapest, so "
                        "the next rent would buy it straight back", offer_id)
        # And the machine, but ONLY for a pure transport wipeout. The scene-fault
        # worry above cannot reach this branch: the Blender bundle is the same
        # 481 MB whatever the scene is, so a .blend that breaks Blender cannot
        # make pushing Blender fail. A host that dropped every push across
        # MAX_TRANSPORT_ROUNDS rounds has proved its link, and if only the offer
        # were condemned it would re-list under a fresh offer id in seconds.
        if transport_only and self.machine_id:
            self.bad_machines.add(self.machine_id)
            log.warning("machine %s blacklisted for this session — every deploy "
                        "attempt across %d round(s) died on transport, which is "
                        "the host's link and not the scene",
                        self.machine_id, self.deploy_failures)
        self._teardown_locked("deploy failed")
        self.deploy_failures = 0
        self.stalled_rounds = 0
        self.transport_bytes = 0
        return False

    # --- hibernate -------------------------------------------------------

    def hibernate(self, force: bool = False) -> None:
        """Stop the container: GPU billing ends, the disk survives.

        Never a substitute for teardown — a stopped instance still bills
        storage, so HIBERNATE_SEC must eventually destroy it.

        Refuses outright while a render is in flight. The caller checks too, but
        that check and this call are separated by an SSH round trip on an
        endpoint that flaps, and the failure mode is not recoverable: stopping
        the container kills the frame. The recorded incident is exactly this —
        the queue went empty because a job had been wrongly failed, and 300 s
        later the instance was stopped while the GPU was at 99% and 420 W
        finishing that very frame.
        """
        with self.lock:
            if not self.instance_id or self.stopped_at:
                return
            if not force:
                act = self.activity()
                if act.rendering:
                    raise remote.WorkerBusy(
                        f"refusing to stop instance {self.instance_id}: it is "
                        f"{act.describe()}. An idle queue is not an idle GPU.",
                        job_id=act.job_id, progress=act.progress,
                    )
                if act.unknown:
                    # Same tri-state rule as everywhere else: only a reachable,
                    # parsed, definitely-idle answer licenses a stop. The caller
                    # checked too, but on this flapping endpoint the two checks
                    # can disagree — and when they do, the safe reading wins,
                    # because stopping the container kills any frame in flight
                    # and that mistake is unrecoverable. A caller that has
                    # already applied its own bounded-unknown policy (the idle
                    # timer, after IDLE_UNKNOWN_MAX_SEC of silence) passes
                    # force=True and takes the decision explicitly.
                    raise remote.WorkerBusy(
                        f"refusing to stop instance {self.instance_id}: "
                        f"{act.describe()}. An unanswered probe is not an idle "
                        f"GPU — pass force=True only after a deliberate, "
                        f"bounded decision to stop blind.",
                        job_id=act.job_id, progress=act.progress,
                    )
            if self.tunnel and self.tunnel.poll() is None:
                self.tunnel.terminate()
                self.tunnel = None
            if self.ep:
                remote.close_master(self.ep)
            gpu_spend, uptime = self.spend, self.uptime
            self.client.stop_instance(self.instance_id)
            self.stopped_at = time.time()
            self.gpu_seconds += uptime
            self.last_ready = False
            self.status = "stopped"
            log.info(
                "instance %s stopped after %.1f min running (~$%.3f gpu). "
                "disk keeps billing ~$%.3f/hr; destroying in %.0f min",
                self.instance_id, uptime / 60, gpu_spend,
                config.DISK_GB * 0.0004667, config.HIBERNATE_SEC / 60,
            )

    @property
    def hibernated_for(self) -> float:
        return time.time() - self.stopped_at if self.stopped_at else 0.0

    def _resume(self, scene: Path) -> None:
        assert self.instance_id
        self.status = "resuming"
        log.info("waking instance %s from hibernation", self.instance_id)
        self.client.start_instance(self.instance_id)

        inst = vastctl.wait_ready(self.client, self.instance_id)
        # The forwarded SSH port is reassigned on restart, so never reuse the
        # old endpoint.
        ep = self._endpoint(inst)
        if ep is None:
            raise RuntimeError(
                f"instance {self.instance_id} woke with no SSH endpoint ({inst.status_detail})"
            )
        self.ep = ep
        self.stopped_at = None
        self.started_at = time.time()
        log.info("instance %s awake at %s", self.instance_id, ep)

        # Trust nothing about the disk without checking it. If the volume did
        # not survive, fall through to a full deploy rather than starting a
        # worker against a missing scene. This must be a probe: `run(check=False)`
        # returned "" for an ssh that never ran, which logged the useless line
        # `disk state unusable after resume () — full redeploy` and then paid for
        # a full 481 MB re-push.
        wanted = remote.scene_hash(scene)
        blend = remote.scene_cache_path(wanted, scene.name)
        checked = remote.probe(
            ep,
            f"test -x {config.REMOTE_ROOT}/blender/blender && "
            f"test -f {shlex.quote(blend)} && "
            # The completion marker, not just the .blend: a scene whose sim
            # caches did not finish uploading before the hibernate would
            # otherwise look intact and render a silently different simulation.
            f"test -f {shlex.quote(remote.scene_dir(wanted) + '/' + remote.SCENE_COMPLETE)} "
            f"&& echo INTACT || echo GONE",
            timeout=120,
        )
        if not checked.ok:
            raise remote.SshError(checked)
        intact = checked.out.strip() == "INTACT"

        # Content addressing answers "is the right scene already here?" outright
        # — no need to hash the remote file or trust in-memory state that a
        # restart or adoption has cleared.
        if not intact:
            log.warning("disk state after resume: %s — full redeploy",
                        f"scene {wanted} not cached" if checked.ok else checked.describe())
            self._deploy(scene)
            return
        log.info("resumed disk already holds scene %s — no upload needed", wanted)

        remote.push_file(ep, config.ROOT / "worker" / "server.py",
                         f"{config.REMOTE_ROOT}/server.py")
        self._mirror_assets(scene)
        freed = remote.drop_blender_bundle(ep)
        if freed:
            log.info("deleted %s/blender.tar.zst on resume — %.0f MB freed",
                     config.REMOTE_ROOT, freed / 1e6)
        remote.touch_scene(ep, wanted)
        self._touched[wanted] = time.time()
        # From here on this instance may hold a render, so it is no longer
        # replaceable on an unanswered probe. Set BEFORE the call, not
        # after: start_worker can launch Blender and then fail to return.
        self.may_hold_render = True
        remote.start_worker(ep, blend)
        self.tunnel = remote.open_tunnel(ep, self.local_port)
        time.sleep(2)
        ready = self._wait_for_worker(ep, scene)
        if not ready:
            tail = remote.probe(ep, f"tail -40 {config.REMOTE_ROOT}/worker.log", timeout=120)
            raise remote.WorkerUnreachable(
                f"worker on {ep} did not come back after resume — {ready.describe()}. "
                f"remote worker.log: {tail.out.strip() or tail.describe()}",
                tunnel_died=ready.tunnel_died,
                local=ready.local_bind_failed,
            )
        self.scene_hash = wanted
        self.scene_path = scene
        self.last_ready = True
        self.status = "ready"
        log.info("instance %s serving %s again (no re-upload needed)",
                 self.instance_id, scenes.label(scene))

    def _worker_alive(self) -> bool:
        """Is the remote worker reachable — repairing the tunnel if only that died.

        A dead forward and a dead worker look identical from here (connection
        refused), but the remedies differ by minutes and dollars: one needs a
        new SSH process, the other a full redeploy. Always try the cheap repair
        before concluding the instance is broken.
        """
        try:
            return bool(remote.worker_call({"cmd": "ping"}, self.local_port, timeout=20).get("ok"))
        except Exception as exc:
            first = remote.diagnose(exc)

        if not self.ep:
            return False

        # A serial worker CANNOT answer a ping while rendering — it is inside
        # bpy.ops.render.render() on its only thread. Reading that silence as
        # death is what killed three consecutive 8K frames. Ask the instance
        # instead, over the SSH command channel, which keeps working when the
        # forwarded job port does not.
        act = remote.activity(self.ep)
        if act.rendering:
            log.info("worker on %s is alive and %s — a serial worker cannot answer a "
                     "ping mid-render", self.ep, act.describe())
            return True
        if act.unknown:
            # Not "dead": unasked. Say so plainly and let the caller's own
            # guards decide — the deploy path re-checks atomically before it
            # signals anything, so an unknown here can no longer kill a render.
            log.warning("worker liveness on %s is UNKNOWN, not dead — %s (ping said: %s)",
                        self.ep, act.describe(), first)

        if self.tunnel and self.tunnel.poll() is None:
            # A live forward and no answer: the worker is gone, or — if the
            # activity probe above came back UNKNOWN — we simply have no second
            # opinion. Either way the caller redeploys, and the redeploy is now
            # the thing that re-checks atomically before it signals anything, so
            # being wrong here costs a worker restart rather than a frame.
            log.warning("worker on %s did not answer through a live tunnel: %s", self.ep, first)
            return False

        rc = self.tunnel.returncode if self.tunnel else None
        log.warning("tunnel to %s is down (ssh exit %s, ping said: %s) — reopening "
                    "before redeploying", self.ep, rc, first)
        try:
            self.tunnel = remote.open_tunnel(self.ep, self.local_port)
            return bool(remote.worker_call({"cmd": "ping"}, self.local_port, timeout=30).get("ok"))
        except Exception as exc:
            log.warning("tunnel repair to %s failed: %s", self.ep, remote.diagnose(exc))
            return False

    def worker_postmortem(self) -> str:
        """Everything the instance can tell us about why the worker is gone.

        Attached to the job's error so a death is diagnosable from `rq status -v`
        alone. The original failure reported only "worker closed connection
        without replying", which named the wrong component and carried no
        evidence at all — the operator's next four hours went into RAM and VRAM
        that were never the problem.
        """
        if not self.ep:
            return "no endpoint to inspect"
        bits = []
        try:
            alive = remote.probe(
                self.ep,
                "for p in $(pgrep -f /workspace/server.py 2>/dev/null); do "
                "case \"$(cat /proc/$p/comm 2>/dev/null)\" in blender*) echo $p;; esac; done | wc -l; "
                "awk '$4==\"0A\" && $2 ~ /:225F$/{n++} END{print n+0}' /proc/net/tcp; "
                "cat /sys/fs/cgroup/memory.events 2>/dev/null | tr '\\n' ' '; echo; "
                "free -g | awk 'NR==2{print \"ram_used_gb=\"$3\" avail=\"$7}'; "
                "df -h /workspace | awk 'NR==2{print \"disk=\"$5}'; "
                "nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader",
                timeout=60,
            )
            if alive.ok:
                rows = [r for r in alive.out.strip().splitlines() if r.strip()]
                bits.append("instance says: " + " | ".join(rows))
            else:
                bits.append(f"instance unreachable: {alive.describe()}")
            tail = remote.probe(self.ep, f"tail -12 {config.REMOTE_ROOT}/worker.log",
                                timeout=60)
            if tail.ok and tail.out.strip():
                bits.append("worker.log tail: " + remote.tail(tail.out, lines=6))
        except Exception as exc:
            bits.append(f"post-mortem failed: {remote.diagnose(exc)}")
        return " || ".join(bits) or "no diagnostics available"

    def repair_tunnel(self) -> bool:
        """Reopen the job forward. True if the local port is usable again."""
        if not self.ep:
            return False
        with contextlib.suppress(Exception):
            if self.tunnel and self.tunnel.poll() is None:
                self.tunnel.terminate()
        try:
            self.tunnel = remote.open_tunnel(self.ep, self.local_port)
            return True
        except Exception as exc:
            log.warning("tunnel repair to %s failed: %s", self.ep, remote.diagnose(exc))
            return False

    def collect_finished(self, job_id: str, render_sec: float = 0.0) -> Optional[dict]:
        """The reply the caller would have got, if the frame is already on disk.

        The PNG outlives every connection that cared about it, so this is what
        makes a lost socket — or a broker that gave up entirely — cost nothing.

        The rebuilt reply carries a `png.sha256` hashed on the instance, so the
        fetch of a recovered frame gets the same digest check as a normal one —
        it used to get only a size match, on exactly the path a flaky link makes
        most likely. `finished_png_info` also refuses to report a file whose
        size is still changing, closing the race where a PNG mid-write was
        collected as "complete".
        """
        if not self.ep:
            return None
        info = remote.finished_png_info(self.ep, job_id)
        if info is None:
            return None
        return {
            "ok": True, "job_id": job_id,
            "path": info["path"],
            "bytes": info["bytes"], "render_sec": render_sec, "recovered": True,
            "png": {"sha256": info["sha256"]} if info.get("sha256") else {},
        }

    def await_render(self, job_id: str, deadline_sec: float,
                     on_poll=None, unknown_grace: float = 900.0) -> Optional[dict]:
        """Recover a render whose job socket died, WITHOUT re-rendering it.

        The worker writes its PNG to disk and publishes progress independently
        of the socket that requested it, so a dropped tunnel costs a connection,
        not 40 minutes of GPU. This polls the instance over SSH — the channel
        that stays up while the forward flaps — until the worker reports the job
        finished, then rebuilds the reply the caller would have received.

        Returns None only when the instance is **reachable** and says it is not
        rendering this job and holds no finished PNG for it. That is the genuine
        "it really died" case.

        An unreadable poll is NOT that case, and the difference is the whole
        bug: this loop used to return None the first time `read_progress` came
        back empty, and on this flapping endpoint that happened twice inside
        twelve minutes — each time reported to the operator as "the instance is
        not rendering this job either, so the worker really is gone" while the
        GPU was at 96% on that exact frame. Unreachable now means *keep waiting*,
        bounded by `unknown_grace` of continuous silence, and if that runs out
        the error says which component actually failed to answer.

        `on_poll` is called with each Activity — the dispatcher uses it to renew
        the job lease, which otherwise expires under a long reattach and lets
        the row be requeued out from under the render it is waiting for.
        """
        assert self.ep
        ep = self.ep
        deadline = time.time() + deadline_sec
        last_note = 0.0
        unknown_since: Optional[float] = None
        last_detail = ""

        while time.time() < deadline:
            act = remote.activity(ep)
            if on_poll is not None:
                with contextlib.suppress(Exception):
                    on_poll(act)

            if act.unknown:
                # Could not ask. Says nothing about the render.
                last_detail = act.describe()
                if unknown_since is None:
                    unknown_since = time.time()
                    log.warning("job %s: %s — still waiting for it, NOT concluding it "
                                "died. A dropped SSH probe is not a dead render.",
                                job_id, last_detail)
                elif time.time() - unknown_since > unknown_grace:
                    raise RuntimeError(
                        f"job {job_id}: the instance has not answered a single progress "
                        f"probe for {(time.time() - unknown_since) / 60:.1f} min while "
                        f"reattaching. This is a TRANSPORT failure, not a statement "
                        f"about the render — last attempt: {last_detail}"
                    )
                time.sleep(config.PROGRESS_INTERVAL)
                continue
            unknown_since = None

            prog = act.progress or {}
            state, owner = act.state, act.job_id

            if owner == job_id and state == "done":
                reply = self.collect_finished(job_id, float(prog.get("elapsed_sec") or 0.0))
                if reply is None:
                    return None
                log.info("recovered job %s from the instance — %.1f MB already rendered, "
                         "no re-render needed", job_id, reply["bytes"] / 1e6)
                return reply

            if owner == job_id and state == "failed":
                raise RuntimeError(
                    f"worker reported job {job_id} failed: {prog.get('phase')}"
                )

            if act.renders(job_id):
                if time.time() - last_note > 60:
                    last_note = time.time()
                    log.info("job %s still rendering on the instance (%s/%s, tile %s/%s, "
                             "%.0f%%) — waiting rather than re-rendering",
                             job_id, prog.get("sample"), prog.get("total"),
                             prog.get("tile"), prog.get("tiles"), prog.get("pct") or 0)
                time.sleep(config.PROGRESS_INTERVAL)
                continue

            # Reachable, and the worker is not on this job: either it never
            # started it, or it finished and moved on. Before believing the
            # pessimistic reading, look for the frame itself — progress.json
            # names only the LATEST job, so a finished render is invisible there
            # the moment the next one begins.
            reply = self.collect_finished(job_id)
            if reply is not None:
                log.info("job %s is not in progress.json (%s) but its PNG is on the "
                         "instance — %.1f MB, collecting it rather than re-rendering",
                         job_id, act.describe(), reply["bytes"] / 1e6)
                return reply
            log.warning("job %s: the instance is reachable and %s, and holds no PNG for "
                        "this job — it really is not rendering it", job_id, act.describe())
            return None

        raise RuntimeError(
            f"job {job_id} did not finish within {deadline_sec:.0f}s of reattaching "
            f"after the job socket dropped"
        )

    def _rent(self) -> None:
        """Rent a 5090, moving on to the next offer if one will not come up.

        Retrying the *same* offer is close to useless and not free: the broker
        destroyed an instance that never became reachable, searched again, was
        handed the same cheapest offer, and rented it a second time five minutes
        later. Offers that fail are remembered for the session, and a host that
        failed for host-level reasons is remembered by machine id too, since one
        machine backs many offers.
        """
        self.status = "renting"
        credit = vastctl.guard_credit(self.client)
        log.info("credit $%.2f", credit)

        offers = vastctl.search_offers(self.client, hours=8.0, disk_gb=config.DISK_GB)
        if not offers:
            raise RuntimeError(
                f"no RTX 5090 offers matched: {vastctl.build_query(disk_gb=config.DISK_GB)}"
            )

        fresh = [
            o for o in offers
            if int(o.get("id") or 0) not in self.bad_offers
            and int(o.get("machine_id") or 0) not in self.bad_machines
            and int(o.get("machine_id") or 0) not in self.stalled_machines
        ]
        if not fresh:
            # Everything on the market has already failed for us this session.
            # Better to try the cheapest again than to stop renting entirely.
            # `stalled_machines` clears here too: skipping a host is only ever
            # a preference, so when it is the difference between renting and
            # not renting, it loses. This is also what makes a genuine
            # control-plane outage — which stalls every host we try — recover
            # instead of deadlocking on an empty candidate list.
            log.warning("all %d matching offers failed earlier this session — "
                        "clearing the blacklist and retrying", len(offers))
            self.bad_offers.clear()
            self.bad_machines.clear()
            self.stalled_machines.clear()
            fresh = offers

        failures: list[str] = []
        for offer in fresh[: vastctl.MAX_OFFER_ATTEMPTS]:
            offer_id = int(offer["id"])
            machine_id = int(offer.get("machine_id") or 0)
            # Exclusivity is stated on EVERY rent, not only on the shared ones.
            # `gpu_frac` was queryable for the whole life of this broker and was
            # never asked for or reported, so a card we shared with seven
            # strangers logged identically to one we owned — and R2-382 cost an
            # afternoon, four dud instances and two wrong diagnoses to work out
            # from the inside what one field would have said up front.
            exclusive = offer.get("_exclusive")
            log.info(
                "renting offer %s (machine %s) — $%.3f/hr, rel %.3f, est $%.2f/8h, "
                "up %s Mbps, direct ports %s, %s",
                offer_id, machine_id or "?", offer.get("dph_total", 0),
                offer.get("reliability2", 0), offer["_est"],
                int(offer.get("inet_up", 0)), offer.get("direct_port_count", "?"),
                f"EXCLUSIVE (gpu_frac={offer.get('gpu_frac')}, whole machine)"
                if exclusive else
                f"*** SHARED (gpu_frac={offer.get('gpu_frac')}) — a co-tenant can "
                f"take VRAM mid-render; black frames here mean nvidia-smi FIRST ***",
            )

            # An offer that cannot be created is not a reason to stop renting.
            # Offers are a snapshot of a live market and go stale in seconds, so
            # `create` returning 400 is routine — but it used to propagate
            # straight out of this loop, past the other two candidates, and burn
            # a job attempt. Observed: three of a job's attempts spent in two
            # seconds on the same disappearing offer, which then failed a job
            # that nothing was wrong with.
            try:
                instance_id = vastctl.create(self.client, offer_id, disk_gb=config.DISK_GB)
            except Exception as exc:
                self.bad_offers.add(offer_id)
                failures.append(f"offer {offer_id}: create refused: {remote.diagnose(exc)}")
                log.warning("offer %s could not be created (%s) — trying the next one",
                            offer_id, remote.diagnose(exc))
                continue
            self.instance_id = instance_id
            self.dph = float(offer.get("dph_total") or 0.0)
            # Carried from the OFFER, which is where exclusivity was actually
            # decided. `search_offers` already annotates `_exclusive`, but the
            # raw fraction is what `rq status` shows and what a later adoption
            # will compare against.
            raw_frac = offer.get("gpu_frac")
            self.gpu_frac = None if raw_frac is None else float(raw_frac)
            if self.gpu_frac is not None and self.gpu_frac < vastctl.EXCLUSIVE_GPU_FRAC:
                log.warning("rented a SHARED card: gpu_frac %.3f on instance %s "
                            "— the exclusive pass found nothing rentable",
                            self.gpu_frac, instance_id)
            self.started_at = time.time()
            # Freshly minted: nothing has ever run on it, so it provably holds
            # no render. Cleared here rather than at teardown alone so the flag
            # can never be inherited from the instance this one replaces.
            self.may_hold_render = False
            self.machine_id = machine_id
            # Remembered so the deploy-failure path can condemn the offer it
            # came from; without it that path destroys the instance and the
            # next rent buys the same offer back.
            self.offer_id = offer_id
            log.info("instance %s created on offer %s, waiting for ssh (timeout %.0fs)",
                     instance_id, offer_id, vastctl.READY_TIMEOUT)

            # A boot that never completes must not leave the instance rented. The
            # idle timer cannot save us here: it keys off a live endpoint, and a
            # failed boot never produces one — so this instance would bill until a
            # human noticed. Destroy it on the way out.
            self.status = "booting"
            try:
                inst = vastctl.wait_ready(self.client, instance_id)
            except Exception as exc:
                provisioning = getattr(exc, "provisioning", False)
                # Blame is its OWN question — see NotReachable. Defaults to
                # `not provisioning`, so nothing here changes except the case
                # this exists for: a host that stopped progressing because it
                # could not resolve a vast.ai name. That is a control-plane
                # failure, it is true of every host during a zone outage, and
                # it must not cost hardware a 24 h ban.
                host_at_fault = getattr(exc, "host_at_fault", not provisioning)
                log.error(
                    "instance %s (offer %s, machine %s) never became reachable — "
                    "destroying it and trying the next offer. %s",
                    instance_id, offer_id, machine_id or "?", remote.diagnose(exc),
                )
                # Blacklist the offer either way so we do not immediately re-rent
                # it. Whether the MACHINE is condemned is a separate question,
                # and it is asked twice below, because "is this hardware to
                # blame?" and "should we buy this box again right now?" are not
                # the same question — see the stalled_machines note.
                self.bad_offers.add(offer_id)
                if host_at_fault and machine_id:
                    self.bad_machines.add(machine_id)
                    log.warning("machine %s blacklisted for this session (host-level "
                                "failure, not slow provisioning)", machine_id)
                elif machine_id:
                    # Stopped dead, or never started moving, but demonstrably not
                    # its own fault. Say so loudly: this is the line whose absence
                    # let a DNS outage look identical to broken hardware for 40
                    # minutes.
                    #
                    # BLAME and AVOIDANCE are different decisions, and conflating
                    # them cost 30 minutes on 2026-08-04. Machine 73811 sat at
                    # `loading` — pulling the CUDA image, zero transitions — for
                    # the full budget on offer 46234730. It was correctly not
                    # blamed, so only that offer was condemned; 15 minutes later
                    # the loop rented offer 46234736, which is the SAME machine,
                    # and it stalled identically. One machine backs many offers,
                    # so condemning the offer alone does not stop us buying the
                    # same box back, and each proof costs a full readiness budget.
                    #
                    # `bad_machines` is the wrong home for that: it persists to
                    # bad_hosts.json with a 24 h TTL, which is precisely the ban
                    # a control-plane fault must not earn. So avoidance gets its
                    # own set — in memory, this process only, never written to
                    # disk, cleared the moment the market runs dry. The hardware
                    # keeps its clean record; we simply stop paying to re-learn
                    # the same thing inside one recovery.
                    self.stalled_machines.add(machine_id)
                    log.warning(
                        "machine %s NOT blacklisted despite failing to come up (%s) "
                        "— the hardware is not blamed and earns no 24 h ban. But it "
                        "just burned the whole %.0fs readiness budget, and one "
                        "machine backs many offers, so condemning the offer alone "
                        "would let us re-rent the same box minutes later (it did, "
                        "twice, on 2026-08-04). Skipping it for the rest of this "
                        "process only. Detail: %s",
                        machine_id,
                        "still provisioning, never reached running" if provisioning
                        else "reached the control plane but could not be raised",
                        vastctl.READY_TIMEOUT, getattr(exc, "detail", "") or "?",
                    )
                failures.append(f"offer {offer_id}: {remote.diagnose(exc)}")
                self._destroy_confirmed(instance_id, "never became reachable")
                self.instance_id = None
                self.started_at = None
                self.status = "down"
                continue

            ep = self._endpoint(inst)
            if ep is None:      # wait_ready guarantees an endpoint; belt and braces
                failures.append(f"offer {offer_id}: running but no endpoint")
                self._destroy_confirmed(instance_id, "running but no endpoint")
                self.instance_id = None
                self.started_at = None
                continue

            self.ep = ep
            self.deploy_failures = 0
            # A different box holds none of the previous one's bytes, and
            # inheriting its stall count would condemn a fresh rental for its
            # predecessor's sins.
            self.stalled_rounds = 0
            self.transport_bytes = 0
            self.heartbeat_failures = 0
            log.info("instance %s reachable at %s in %.0fs",
                     instance_id, ep, time.time() - (self.started_at or time.time()))
            return

        raise RuntimeError(
            f"could not bring up any of {min(len(fresh), vastctl.MAX_OFFER_ATTEMPTS)} "
            f"offers: {' || '.join(failures) or 'no attempts made'}"
        )

    # --- scene cache -----------------------------------------------------

    @staticmethod
    def _sibling_bytes(scene: Path) -> int:
        """Bytes of physics cache and friends that land beside the .blend.

        Counted into the preflight because they are part of the upload, not an
        afterthought: a rigid-body bake is one `.bphys` per frame and can be
        larger than the blend it belongs to. Sizing the preflight on the .blend
        alone would clear exactly enough room to run out of it halfway through
        the cache push — and an incomplete cache tree does not fail a render,
        it makes Blender simulate.
        """
        total = 0
        for d in scenes.sibling_dirs_for(scene):
            for path in d.rglob("*"):
                with contextlib.suppress(OSError):
                    if path.is_file():
                        total += path.stat().st_size
        return total

    def _ensure_scene_cached(self, scene: Path) -> str:
        """Get this scene onto the instance, content-addressed. Returns its path.

        Cached by content hash, so re-selecting a scene the instance already
        holds costs one `stat` instead of a 22 s upload — which is what makes
        interleaving jobs across scenes affordable at all.

        Uploaded to a `.part` and renamed only once the byte count matches: a
        truncated .blend sitting in a content-addressed cache would be
        indistinguishable from a good one on every later lookup.
        """
        assert self.ep
        ep = self.ep
        digest = remote.scene_hash(scene)
        size = scene.stat().st_size
        final = remote.scene_cache_path(digest, scene.name)

        if remote.scene_cached(ep, digest, size, scene.name):
            remote.touch_scene(ep, digest)
            self._touched[digest] = time.time()
            log.info("scene %s already cached on the instance (hash %s) — no upload",
                     scenes.label(scene), digest)
            return final

        # PREFLIGHT. Make room before writing, never after, and prove the room
        # exists rather than hoping: this is the only moment the broker knows
        # both how many bytes are about to land and how many the disk has.
        #
        # Everything that could be rendering is protected. Nothing can be
        # rendering *here* — the only callers are `_switch_scene` and `_deploy`,
        # both reached through `ensure_ready`, which holds the fleet lock and has
        # already raised WorkerBusy if the instance is mid-frame — but the pins
        # do not depend on that argument holding forever.
        #
        # A refusal raises DiskFull, which is neither transport nor a host fault:
        # it fails the job with the numbers in the message and leaves the GPU
        # alone. Retrying it, or replacing the hardware for it, buys the same
        # disk and the same answer.
        keep = {digest} | self.protected_scenes()
        # Soft protection for scenes that still have jobs waiting on them:
        # evicted only after every idle scene, never instead of one. See
        # `demanded_scenes` for why this is an ordering and not a pin. Passed
        # as a thunk: answering it costs a content hash per queued scene, and
        # most preflights evict nothing at all.
        def defer() -> set[str]:
            return self.demanded_scenes() - keep
        reserve = int(config.DISK_RESERVE_GB * 1e9)
        state = remote.disk_state(ep)
        self.disk = state
        self._disk_sampled = time.time()
        # An unmeasurable disk gets the configured ceiling here only so the
        # number exists; `evict_to_fit` refuses outright on `state.ok == False`
        # a line later, which is the behaviour that matters.
        budget = (remote.cache_budget(state, reserve) if state.ok
                  else int(config.SCENE_CACHE_FLOOR_GB * 1e9))
        incoming = size + self._sibling_bytes(scene)
        report = remote.evict_to_fit(ep, keep, incoming=incoming, budget=budget,
                                     reserve=reserve, state=state, defer=defer)
        self.disk = report.after
        self._disk_sampled = time.time()
        log.info("scene cache preflight for %s (%.2f GB incoming): %s",
                 scenes.label(scene), incoming / 1e9, report.describe())
        if report.after.cache_bytes + incoming > budget:
            # Policy exceeded, physics satisfied. Loud, but not a failure: every
            # remaining scene is pinned, and refusing the job over a ceiling that
            # exists to protect a disk which demonstrably has room would be the
            # check refusing legitimate work.
            log.warning(
                "scene cache will exceed its %.1f GB budget (%.2f GB cached + "
                "%.2f GB incoming) — nothing else is evictable: %s are loaded or "
                "in flight. Free space is still fine (%.2f GB).",
                budget / 1e9, report.after.cache_bytes / 1e9, incoming / 1e9,
                ", ".join(d[:8] for d in sorted(keep)), report.after.free / 1e9)

        # `mkdir -p` succeeds on a directory that already exists and fails only
        # when the path exists as something that is *not* one — a stray file or
        # a dangling symlink where this scene's directory belongs. That makes an
        # EEXIST here a corrupted entry in the broker's own cache, not a
        # statement about the host.
        #
        # It was read as one anyway. On 2026-08-03 `mkdir -p
        # /workspace/scenes/139698d62abee3bf` (relief_2light_A2.blend) failed
        # three times in nine seconds, the deploy retry gave up, and
        # `_deploy` classified it "host-level failure" and destroyed instance
        # 46668588 — reachable, idle, 7 h uptime, 28 scenes and 5.46 GB of warm
        # cache. The replacement cost a 900 s rental wait, a 481 MB Blender
        # push, a 148 s deploy and an empty cache, and the queue starved for
        # ~17 min, all for one bad inode that `rm -f` fixes.
        #
        # `test -d` first so a real cache directory is never touched: the
        # removal only ever reaches a path that cannot be a scene directory.
        #
        # And it DESCRIBES the thing before removing it. The self-heal above
        # fixed the outage but destroyed the evidence: `rm -f` ran silently, so
        # after instance 46668588 was thrown away nothing could say what had
        # written a non-directory into a content-addressed cache path — the one
        # root cause that post-mortem could not close. Healing without looking
        # would have guaranteed the next recurrence was equally unexplained,
        # and a stray inode is gone the moment it is fixed, so the capture
        # cannot be left to whoever reads the log afterwards. One round trip,
        # only on a path that is already known to be wrong.
        healed = remote.run(ep, heal_scene_dir_cmd(remote.scene_dir(digest)),
                            timeout=60)
        if STRAY_MARK in healed:
            log.warning(
                "a non-directory was occupying the cache path for %s (hash %s) "
                "on %s and has been removed. This is the failure that cost "
                "instance 46668588; it has never been explained, so here is "
                "what was actually there:\n%s",
                scenes.label(scene), digest, ep,
                "\n".join(line for line in healed.splitlines()
                          if line.strip() and line.strip() != STRAY_MARK))
        self.status = "uploading-scene"
        log.info("pushing scene %s (%.0f MB) hash=%s", scenes.label(scene), size / 1e6, digest)
        self.transfer = {"what": scenes.label(scene), "bytes": size,
                         "began": time.time()}
        try:
            elapsed = remote.push_scene(ep, scene, remote_path=final + ".part")
        finally:
            # Cleared on the failure path too, or a push that raised would
            # leave `rq status` reporting a transfer that ended minutes ago.
            self.transfer = None
        landed = remote.probe(ep, f"stat -c %s {shlex.quote(final + '.part')}", timeout=60)
        got = int(landed.out.split()[-1]) if landed.ok and landed.out.split() else -1
        if got != size:
            remote.run(ep, f"rm -f {shlex.quote(final + '.part')}", timeout=60, check=False)
            raise remote.TransferError(
                f"scene push of {scenes.label(scene)}", str(ep),
                f"remote holds {got} bytes, expected {size}", elapsed,
                sent=max(got, 0), expected=size,
            )
        remote.run(ep, f"mv -f {shlex.quote(final + '.part')} {shlex.quote(final)}",
                   timeout=60)
        log.info("scene uploaded in %.1fs (%.1f MB/s raw)",
                 elapsed, size / 1e6 / max(elapsed, 0.1))

        # Physics caches and anything else the .blend references relatively, in
        # the one place Blender will look for them: beside the uploaded scene.
        # Pushed AFTER the blend and BEFORE any worker starts, so a worker never
        # opens a scene whose caches are still in flight — an unfinished cache
        # tree reads as an absent one, and an absent one makes Blender simulate.
        siblings = scenes.sibling_dirs_for(scene)
        if siblings:
            self.status = "uploading-caches"
            began = time.time()
            files, cache_bytes = remote.push_scene_siblings(
                ep, digest, scene.parent, siblings)
            log.info("pushed %d cache/sibling file(s), %.1f MB, in %.1fs from %s",
                     files, cache_bytes / 1e6, time.time() - began,
                     ", ".join(d.name for d in siblings))
        # Last, and only now: this is what makes the cache entry mean "the whole
        # scene is here", rather than "a .blend is here and its caches may be".
        remote.mark_scene_complete(ep, digest)
        return final

    def _report_missing(self, ep, scene: Path) -> list[str]:
        """Say what Blender could not resolve on the instance, at both severities.

        Two classes, and they are not the same defect:

        * A missing IMAGE is a warning. Blender renders anyway, the frame may
          still be exactly what was wanted, and it may be nothing worse than an
          HDRI that changes the key light.
        * A missing LIBRARY means the linked datablocks are not in the render at
          all. That is what returned a strip of sky over pure black in 0.83 s
          from job 82ebdd064292, passed the blank gate, and was recorded done.

        Reported here and ENFORCED in the worker, deliberately. Raising from a
        scene switch would land in the `except Exception` below and be read as
        "this instance failed to switch scenes" — which starts a redeploy, and
        every farm outage on this project so far has been a healthy 5090 thrown
        away over something that was not the hardware's fault. A scene that
        links libraries is wrong wherever it runs; the box is fine. So the
        broker says so, and `worker.server` refuses the render terminally.
        """
        libs = remote.missing_libraries(ep)
        for line in libs:
            log.error("MISSING LIBRARY for %s: %s — every datablock it holds is "
                      "ABSENT from this render. Renders from this scene will be "
                      "refused by the worker. Make the scene self-contained "
                      "(File > External Data > Make Local: All).",
                      scenes.label(scene), line)
        for line in remote.missing_assets(ep):
            if line in libs:
                continue
            log.warning("MISSING ASSET for %s: %s — the render will not match "
                        "local. Add its directory to VASTRENDER_ASSET_DIRS.",
                        scenes.label(scene), line)
        return libs

    def _mirror_assets(self, scene: Path) -> None:
        """Mirror this scene's external assets — per scene, not once per deploy.

        Different variants can live in different trees, and a missing HDRI does
        not fail the render: it returns a plausible frame lit differently from
        the local one. Directories already mirrored onto this instance are
        skipped, so switching back and forth stays cheap.
        """
        assert self.ep
        for d in scenes.asset_dirs_for(scene):
            if d in self.mirrored_assets:
                continue
            count, size = remote.push_assets(self.ep, [d])
            self.mirrored_assets.add(d)
            if count:
                log.info("mirrored %d asset file(s), %.2f MB, from %s", count, size / 1e6, d)

    def _deploy(self, scene: Path) -> None:
        assert self.ep
        ep = self.ep
        began = time.time()
        # Nothing on this instance is trustworthy until the deploy completes. A
        # deploy that fails halfway must not leave last_ready True, or the next
        # ensure_ready hands out an endpoint whose worker is still holding the
        # previous scene.
        self.last_ready = False

        # FIRST, before anything spends the deploy budget: wait for sshd to
        # actually run a command. A freshly booted container answers TCP on the
        # forwarded port a minute or more before it accepts our key, and every
        # command in that window returns `exit 255 after 0.4s` with no stderr.
        # Three deploy attempts five seconds apart burn straight through it and
        # the instance gets blamed for being young. Covers rent, adoption and
        # resume in one place, because all three arrive here.
        self.status = "waiting-for-ssh"
        ready = remote.wait_ssh(ep)
        if not ready.ok:
            # Deliberately NOT an SshError. A container that has not started
            # sshd in four minutes is a broken host, not a flaky link, and
            # classifying it as transport would retry it three times a round for
            # three rounds — an hour of billing to relearn the same fact.
            #
            # Say which of the two failures it actually was. The message used to
            # assert "the container behind it is not serving" unconditionally,
            # which is false for an auth rejection — there, sshd is up, has
            # spoken to us, and has refused our key — and a debugging session
            # was spent hunting a down sshd that had never been down.
            if ready.auth_rejected:
                why = (
                    "sshd IS SERVING and REFUSED OUR KEY — it completed the "
                    "handshake, sent vast's banner, then denied publickey auth. "
                    "The container is up; what is missing is authorized_keys. "
                    "vast.ai writes that file at container start, so a key still "
                    f"absent {remote.WAIT_SSH_SEC:.0f}s in was never written: THE "
                    "HOST DID NOT INSTALL IT, and it will not appear now. This is "
                    "not a young container and not a flaky link. Verify with "
                    f"`ssh -v -p {ep.port} -i {config.SSH_KEY} root@{ep.host} true` "
                    "and `vastctl.py status`"
                )
            else:
                why = (
                    "The port answers TCP — vast's forwarder does that from the "
                    "moment the instance exists — but the container behind it is "
                    "not serving"
                )
            raise remote.SshNeverReady(
                f"sshd on {ep} never accepted a command within "
                f"{remote.WAIT_SSH_SEC:.0f}s of trying. {why}. Last attempt: "
                f"{ready.describe()}",
                ran=ready,
            )


        # Ship Blender from here when we have it bundled, before provisioning
        # extracts it. Skipped if the instance already has a build on disk —
        # which is the normal case after a hibernate/resume cycle.
        if config.BLENDER_BUNDLE.exists():
            # A probe, not `run(check=False)`: an ssh that never executed
            # returns the same empty string as "blender is not installed", and
            # answering that with a 481 MB upload to a host we cannot reach is
            # both futile and how the push failure got misattributed.
            have = remote.probe(
                ep, f"test -x {config.REMOTE_ROOT}/blender/blender && echo YES || echo NO",
                timeout=120,
            )
            if have.transport_failed:
                raise remote.SshError(have)
            if have.out.strip() != "YES":
                mb = config.BLENDER_BUNDLE.stat().st_size / 1e6
                self.status = "uploading-blender"
                log.info("pushing blender bundle (%.0f MB) to %s%s", mb, ep,
                         "" if ep.direct else " — OVER THE PROXY RELAY, expect ~6.9 Mbps")
                started = time.time()
                try:
                    took = remote.push_blender(ep, config.BLENDER_BUNDLE)
                except Exception as exc:
                    log.warning("blender push aborted after %.1fs: %s",
                                time.time() - started, remote.diagnose(exc))
                    raise
                log.info("blender pushed in %.1fs (%.2f MB/s)", took, mb / max(took, 0.1))

        self.status = "provisioning"
        log.info("provisioning (blender %s)", config.BLENDER_VERSION)
        out = remote.provision(ep)
        for line in out.strip().splitlines():
            log.info("  %s", line.strip())

        # The pushed archive is dead weight the moment the install works: 460 MB
        # on a 30 GB disk, 3% of a 16 GB one, read by nothing. Deleted only
        # after `blender --version` answers, and both paths that could want it
        # again test for the installed build rather than the archive, so a
        # resume never re-pushes because of this.
        freed = remote.drop_blender_bundle(ep)
        if freed:
            log.info("deleted %s/blender.tar.zst — %.0f MB of dead weight freed "
                     "(the install is verified working; a resume tests for "
                     "blender/blender, never the archive)",
                     config.REMOTE_ROOT, freed / 1e6)

        self.status = "uploading"
        remote.push_file(ep, config.ROOT / "worker" / "server.py",
                         f"{config.REMOTE_ROOT}/server.py")

        # External references (HDRIs, textures) are stored as absolute paths
        # inside the blend and are not carried by the blend itself. Without
        # them the render still completes and looks plausible while being lit
        # differently from the local one.
        self._mirror_assets(scene)

        digest = remote.scene_hash(scene)
        blend = self._ensure_scene_cached(scene)
        # scene_hash is recorded only once a worker is actually serving it, at
        # the end of this method. Recording it here meant a deploy that died
        # between the upload and the worker restart left the broker believing
        # the new scene was live while the old worker still held the old one —
        # every later render silently returning the previous geometry.

        self.status = "starting-worker"
        # From here on this instance may hold a render, so it is no longer
        # replaceable on an unanswered probe. Set BEFORE the call, not
        # after: start_worker can launch Blender and then fail to return.
        self.may_hold_render = True
        remote.start_worker(ep, blend)

        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
        self.tunnel = remote.open_tunnel(ep, self.local_port)
        time.sleep(2)

        ready = self._wait_for_worker(ep, scene)
        if not ready:
            tail = remote.probe(ep, f"tail -40 {config.REMOTE_ROOT}/worker.log", timeout=120)
            raise remote.WorkerUnreachable(
                f"worker on {ep} {ready.describe()}. remote worker.log: "
                f"{tail.out.strip() or tail.describe()}",
                tunnel_died=ready.tunnel_died,
                local=ready.local_bind_failed,
            )

        self._report_missing(ep, scene)

        self.scene_hash = digest
        self.scene_path = scene
        self.last_ready = True
        self.status = "ready"
        # Same measurement `_switch_scene` records, and it has to be recorded
        # here too: a broker restart reaches its first scene through _deploy,
        # so without this the dispatcher priced its FIRST scene — the one it
        # just spent the most time on — off the size estimate alone. Measured
        # 07:46 on 2026-08-03: film7 deployed in 922 s against an estimate of
        # 1419 s.
        self.switch_cost[digest] = time.time() - began
        self.load_sec += time.time() - began
        log.info("worker ready — instance %s serving %s (hash %s) in %.1fs",
                 self.instance_id, scenes.label(scene), digest, time.time() - began)

    # --- tear down -------------------------------------------------------

    # Seconds between retries of an unconfirmed destroy. Long enough to respect
    # vast's per-IP rate limits, short enough that a leaked instance bills
    # cents, not hours, before the retry lands.
    DOOMED_RETRY_SEC = 300.0

    def _destroy_confirmed(self, instance_id: int, why: str) -> bool:
        """Destroy an instance, and never lose track of one that would not die.

        `destroy()` has two failure shapes and both used to be swallowed: an
        exception (suppressed) and a False return ("not confirmed gone",
        ignored). Either way the instance kept billing with nothing tracking
        it, because reconciliation only runs at startup. Now an unconfirmed
        destroy is remembered in `self.doomed` and retried from the heartbeat
        thread until vast confirms it.
        """
        ok, err = False, ""
        try:
            ok = bool(vastctl.destroy(self.client, instance_id))
        except Exception as exc:
            err = remote.diagnose(exc)
        if ok:
            self.doomed.pop(instance_id, None)
            return True
        self.doomed[instance_id] = time.time()
        log.error(
            "instance %s was NOT confirmed destroyed (%s)%s — it is still rented "
            "and still billing. Kept on the reap list; retrying every %.0f s "
            "until vast confirms it is gone.",
            instance_id, why, f": {err}" if err else "",
            self.DOOMED_RETRY_SEC,
        )
        return False

    def reap_doomed(self) -> None:
        """Retry every unconfirmed destroy. Runs on the heartbeat thread —
        cheap when the list is empty, which is always, except when it is the
        only thing standing between a leaked instance and an open-ended bill."""
        for iid, last in list(self.doomed.items()):
            if time.time() - last < self.DOOMED_RETRY_SEC:
                continue
            if self._destroy_confirmed(iid, "reap retry"):
                log.warning("instance %s confirmed destroyed on retry — it had "
                            "survived an earlier destroy attempt", iid)

    def teardown(self, reason: str = "idle") -> None:
        with self.lock:
            self._teardown_locked(reason)

    def _teardown_locked(self, reason: str = "idle") -> None:
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
            self.tunnel = None
        if self.ep:
            remote.close_master(self.ep)
        if self.instance_id:
            gpu, disk, hib = self.spend, self.disk_spend, self.hibernated_for
            # Persist this instance's cost before the counters are cleared,
            # otherwise every teardown silently resets the batch cap to zero.
            if self.on_teardown:
                try:
                    self.on_teardown()
                except Exception as exc:
                    log.warning("could not bank spend: %s", remote.diagnose(exc))
            ok = self._destroy_confirmed(self.instance_id, reason)
            log.info(
                "destroyed %s (%s) — gpu $%.3f, disk $%.3f, hibernated %.1f min — %s",
                self.instance_id, reason, gpu, disk, hib / 60,
                "confirmed gone" if ok
                else "NOT CONFIRMED — kept on the reap list and retried",
            )
        self.ep = None
        self.instance_id = None
        # Per-instance, like gpu_seconds. Carrying a dead box's exclusivity
        # onto its replacement is exactly the silence this field exists to end.
        self.gpu_frac = None
        self.started_at = None
        self.stopped_at = None
        self.gpu_seconds = 0.0
        self.scene_hash = None
        self.scene_path = None
        # A new instance has none of the old one's files, cached scenes or
        # mirrored assets. Keeping this set would silently skip the re-push.
        self.mirrored_assets = set()
        self.last_ready = False
        # The next instance is a different box: no worker has been started on
        # it, and it is not this one's machine. Leaving either set would let a
        # fresh rental inherit the predecessor's provenance — the flag that
        # decides whether an unreachable instance may be destroyed.
        self.may_hold_render = False
        self.machine_id = 0
        self.offer_id = 0
        # Transport history belongs to the instance that made it. Carrying it
        # onto the replacement is how one bad host condemns the next good one.
        self.transport_bytes = 0
        self.stalled_rounds = 0
        self.heartbeat_failures = 0
        self.status = "down"

    def heartbeat(self) -> None:
        """Beat whenever an endpoint exists — deliberately NOT gated on
        last_ready.

        last_ready is only set at the end of a successful deploy, so gating on
        it means no beat is sent during provisioning, the Blender push, the
        scene upload, or worker startup. That is precisely the multi-minute
        window where the watchdog would otherwise destroy a perfectly healthy
        instance mid-deploy. Reachable-over-SSH is the correct liveness signal
        here, not finished-deploying.
        """
        if self.ep and not self.stopped_at:
            try:
                remote.heartbeat(self.ep)
                self.heartbeat_failures = 0
            except Exception as exc:
                # Not fatal on its own, but the in-container watchdog destroys
                # the instance if these stop landing for 30 minutes, so it needs
                # to be diagnosable the first time it happens.
                self.heartbeat_failures += 1
                log.warning("heartbeat to %s failed (%d in a row): %s",
                            self.ep, self.heartbeat_failures, remote.diagnose(exc))
                # The heartbeat is the only thing that touches the instance on a
                # fixed cadence regardless of what the dispatcher is doing, which
                # makes it the right place to notice that there is no instance.
                # A deploy can sit inside a single ssh timeout for minutes; this
                # runs every HEARTBEAT_INTERVAL whatever else is happening.
                if self.heartbeat_failures >= RECONCILE_AFTER_HEARTBEATS:
                    self.heartbeat_failures = 0
                    self.reconcile(f"{RECONCILE_AFTER_HEARTBEATS} consecutive "
                                   f"failed heartbeats", locked=False)
