#!/usr/bin/env python3
"""Instance lifecycle for the render broker.

Every rule enforced here exists because vast.ai will otherwise bill you
indefinitely, and none of it is recoverable after the fact:

  * **Destroy, never stop.** Storage bills for as long as an instance *exists*.
    `stop` ends only the GPU meter and leaves the disk charge running.
  * **`cancel_unavail=True` on every create.** Without it, a create that cannot
    be scheduled silently produces a *stopped* instance — an orphan that bills
    storage from birth.
  * **Label everything.** The label is the only handle for finding instances
    this broker created after a crash. Reap by label *before* creating anything.
  * **Verify destroys.** The API returning success is not the instance being
    gone; poll until the id disappears.

There is no server-side safety net to fall back on: verified against the CLI
source, vast.ai has no spend cap, no instance TTL, no `--end-date`, and
`destroy` is not schedulable. Prepaid credit and the on-instance watchdog are
the only real ceilings.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

try:
    from vastai import VastAI
except ModuleNotFoundError:
    # `vastai` lives in this project's venv, and the shebang above says
    # `python3` — so running this file the obvious way (`./vastctl/vastctl.py`,
    # or `python3 vastctl/vastctl.py status`) picks the SYSTEM interpreter and
    # dies with a bare `ModuleNotFoundError: No module named 'vastai'`.
    #
    # That is a false alarm with real cost. During the 2026-08-03 queue stall it
    # was read as "the instance-control path is broken, so nothing can query or
    # restart the instance" — a plausible root cause for a worker that never
    # comes up and never gets reaped. It was neither: the broker, which runs
    # under the venv, was talking to vast.ai perfectly the whole time.
    #
    # Re-exec under the venv rather than just improving the error, because the
    # command the operator typed is the command that should work — this file is
    # the emergency tool, reached when the broker is what broke.
    import os as _os
    import sys as _sys
    _venv = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                          ".venv", "bin", "python")
    if _os.path.exists(_venv) and _os.path.realpath(_venv) != _os.path.realpath(_sys.executable):
        _os.execv(_venv, [_venv, _os.path.abspath(__file__), *_sys.argv[1:]])
    raise SystemExit(
        f"vastctl needs the 'vastai' package, which lives in this project's "
        f"venv. Run it as: {_venv} {_os.path.abspath(__file__)} <command>"
    )

# --- policy ---------------------------------------------------------------

LABEL_PREFIX = "renderbroker"
DEFAULT_DISK_GB = 30          # 285 MB blend + ~7 GB output + image + Blender
# `base`, not `devel`: Blender 5.2 ships precompiled sm_120 cubins and never
# invokes nvcc, so the ~5 GB toolkit in the devel image buys nothing and cost
# ~6 minutes of image pull on the first live boot. The driver itself is injected
# by the host's container runtime. What actually matters for Blackwell is the
# host's CUDA version, which the offer query filters on (cuda_vers>=12.8).
DEFAULT_IMAGE = "nvidia/cuda:12.8.0-base-ubuntu24.04"

POLL_INTERVAL = 10.0          # matches vast-cli's own wait_for_instance
# Measured: a healthy host reaches SSH in ~3m20s (image pull plus container
# start). 300 s sat barely above that and rejected working hosts.
#
# Raised from 480 s on 2026-07-28 after four consecutive hosts were abandoned
# while **still pulling**, with vast's own status message showing forward
# progress at the deadline:
#
#     unreachable after 481s ... msg=7c19210cd82d: Download complete
#     unreachable after 490s ... msg=#8 223.1 Get:96 http://archive.ubuntu.com/...
#
# Timing those out is the expensive mistake, not the cheap one: the pull already
# paid for is discarded and the replacement host starts its own from zero, so a
# slow night turns into a rent-timeout-rent loop that never converges. That loop
# burned ~25 minutes and five instances to render nothing.
#
# The downside is bounded and small — a genuinely dead host bills 15 minutes
# instead of 8, about four cents — and `wait_ready` still fails immediately on a
# host that reports an error or goes offline, which is the case that actually
# indicts the hardware.
READY_TIMEOUT = 900.0

# How many times wait_ready may ask vast to start an instance it created but
# parked in `stopped`. Bounded so that a vast which ignores start_instance
# still falls through to READY_TIMEOUT rather than spinning forever.
COLD_START_NUDGES = 3
SSH_PROBE_TIMEOUT = 180.0
# How long to keep waiting for the *direct* port mapping once the instance is
# running, before accepting the proxy relay. The relay measured 6.9 Mbps against
# 37 Mbps direct and the very next thing the broker does is push 481 MB, so a
# short wait here is far cheaper than the transfer it saves.
DIRECT_PORT_GRACE = 60.0
# Distinct offers to try before giving up on renting anything. An offer that
# never came up is not retried immediately: the broker rented the *same* offer
# id twice in five minutes and lost both.
MAX_OFFER_ATTEMPTS = 3
MAX_INSTANCE_HOURS = 12.0     # hard wall-clock cap, also enforced in-container
# Raised from 4.0 deliberately, 2026-07-26. A 4 h cap destroyed a healthy instance
# mid-batch with ~4 h of 8K renders still queued, forcing a ~10 min cold start
# (fresh rental, 481 MB Blender push, scene upload, prewarm) and losing the frame
# in flight. This is a runaway-spend guard, not a correctness guard: at ~$0.31/hr
# a 12 h ceiling is ~$3.70, which still bounds an abandoned instance to something
# trivial while being longer than any realistic batch. The in-container watchdog
# enforces the same number, so an orphaned box still dies on its own.
MIN_CREDIT_USD = 2.00         # refuse to create below this

# Heartbeat: broker touches this path; the in-container watchdog destroys the
# instance if it goes stale. Survives broker crash and local power loss.
HEARTBEAT_PATH = "/workspace/.broker_heartbeat"
# 30 min, not 15. The window has to comfortably exceed the longest legitimate
# gap between beats — a slow provision, a big scene upload, or a single long
# render — or the watchdog destroys healthy work. It only needs to be short
# enough that a genuinely dead broker is caught quickly, and 30 minutes of a
# $0.31/hr GPU is about 15 cents.
HEARTBEAT_STALE_SEC = 1800

GONE_STATES = {"destroyed", "terminated", "offline"}

# The only `status_msg` contents that indict the *host*. Matched lowercased,
# as substrings. See `Instance.classify` for why this list is short and why
# nothing about the registry, DNS, TLS or timeouts belongs on it: those are
# retried by containerd and by vast, and condemning a machine for one costs a
# 24 h ban on hardware that was about to work.
#
# The test for adding an entry: *would waiting fifteen more minutes fix it?*
# If yes, it is not terminal, and READY_TIMEOUT is already the right handler.
TERMINAL_MSG_PATTERNS = (
    "no space left on device",     # host disk full
    "oci runtime create failed",   # host container runtime broken
    "cannot allocate memory",      # host oversubscribed
    "invalid reference format",    # our image string is malformed — ours, not theirs
)


class VastError(RuntimeError):
    pass


class NotReachable(VastError):
    """An instance did not come up in time.

    Carries `provisioning`, which is the difference between two situations the
    broker used to treat identically: a host that was still pulling an image and
    working through its onstart script, and a host that reported an error, went
    offline, or published no SSH endpoint at all. Only the second says anything
    about the *host*; retrying the first on a different offer is the correct
    move either way, but blacklisting a machine for being slow once is not.
    """

    def __init__(self, instance_id: int, phase: str, detail: str,
                 elapsed: float, provisioning: bool) -> None:
        super().__init__(
            f"instance {instance_id} unreachable after {elapsed:.0f}s in phase "
            f"'{phase}' ({'still provisioning' if provisioning else 'not progressing'}): "
            f"{detail or 'no status message from vast.ai'}"
        )
        self.instance_id = instance_id
        self.phase = phase
        self.detail = detail
        self.elapsed = elapsed
        self.provisioning = provisioning


# --- offers ---------------------------------------------------------------


# HARD CEILING ON BANDWIDTH PRICE. Not a weighting — a filter.
#
# This exists because the projected-total sort picked instance 46229358 at
# $0.015625/GB down and $0.016927/GB up, replacing one at $0.004/GB, and called
# it a saving. It was not: this workload is UPLOAD-HEAVY. Each per-item test
# blend is ~882 MB and the campaign has 435 items, so ~384 GB goes up the wire.
# At $0.016927/GB that is $6.50 against $1.54 on the cheaper box, which cancels
# most of the $0.0445/hr the GPU line saved.
#
# estimate_cost() could not see it because it assumed egress_gb=8.0 — a 48x
# under-count — and ignored download cost entirely even though every render is
# fetched back. A cost model with the wrong volume in it will keep making this
# mistake in new ways, so the ceiling is enforced as a QUERY FILTER as well:
# an offer that is expensive per GB never enters the candidate set at all,
# whatever the model believes about how much we will transfer.
#
# $4/TB up AND down, set by the user. That is $0.004/GB — the rate the previous
# instance charged, so it is a known-achievable bar and not an aspiration.
MAX_INET_COST_PER_TB = 4.0
MAX_INET_COST_PER_GB = MAX_INET_COST_PER_TB / 1000.0        # 0.004

# CPU FLOOR. Every 5090 offer ships with a different CPU allocation and the
# spread is enormous — measured across 54 live offers on 2026-08-02:
#
#     min 8.0    median 30.2    max 384.0 effective cores
#
# The query never asked for CPU at all, so we drew randomly from that pool and
# landed on 23.04 (measured from the cgroup: cpu.max 2304000/100000). That is
# BELOW the median.
#
# It then cost a wrong conclusion. The remote-exec A/B measured build throughput
# plateauing near 160 items/h no matter whether it was given 12 or 20 slots, and
# read that as "the remote box does not scale with slots" — rejecting remote
# execution at 1.68x against a 2.0x bar. But a 23-CPU cgroup asked for 20
# concurrent Blender builds is simply FULL; the plateau was the box, not the
# approach. Offers at 48, 96 and 128 effective cores were available the whole
# time, one of them CHEAPER than the box we were on.
#
# Blender build work is single-threaded Python per process, so throughput here
# is very close to linear in cores until memory or the host's own load binds.
# Ask for the cores.
MIN_CPU_CORES_EFFECTIVE = 32.0


def build_query(min_reliability: float = 0.98, disk_gb: int = DEFAULT_DISK_GB,
                max_inet_cost: float = MAX_INET_COST_PER_GB,
                min_cpu: float = MIN_CPU_CORES_EFFECTIVE) -> str:
    """A 5090 with a driver new enough for Blackwell Cycles kernels.

    cuda_vers>=12.8 is not optional: below it Cycles ships no sm_120 cubin and
    the render either fails or silently falls back to CPU.

    `max_inet_cost` caps BOTH directions in $/GB. Filtered server-side here and
    re-checked client-side in search_offers(), because a silently-ignored query
    term would put us straight back on an expensive host.
    """
    return (
        f"gpu_name=RTX_5090 num_gpus=1 cuda_vers>=12.8 "
        f"reliability>{min_reliability} inet_down>400 inet_up>400 "
        f"inet_up_cost<={max_inet_cost} inet_down_cost<={max_inet_cost} "
        f"cpu_cores_effective>={min_cpu} "
        f"direct_port_count>=2 "
        f"disk_space>{disk_gb + 15} rentable=true verified=true"
    )


# Transfer actually moved per GPU-hour, measured on this project rather than
# guessed. During the item campaign the broker pushed a fresh ~882 MB test blend
# roughly every 2 minutes and fetched every render back, so ~8 GB/hr is the real
# order of magnitude. The old default of a flat 8 GB FOR THE WHOLE BATCH was a
# 48x under-count across a 435-item campaign, and it is why a host charging 4x
# for bandwidth was scored as cheaper.
UPLOAD_GB_PER_HOUR = 8.0
DOWNLOAD_GB_PER_HOUR = 2.0


def estimate_cost(offer: dict, hours: float, disk_gb: int,
                  egress_gb: float | None = None,
                  ingress_gb: float | None = None) -> float:
    """Total projected cost, not the sticker price.

    Sorting offers on `dph_total` alone picks the wrong host: GPU rates cluster
    tightly while disk rates spread ~6x and BANDWIDTH rates spread ~13x, so
    either line can dominate the difference on a long batch.

    Both directions are counted. The previous version charged only for upload,
    which understated every render-fetch this system exists to perform.
    """
    up = UPLOAD_GB_PER_HOUR * hours if egress_gb is None else egress_gb
    down = DOWNLOAD_GB_PER_HOUR * hours if ingress_gb is None else ingress_gb
    gpu = offer.get("dph_total", 0.0) * hours
    disk = offer.get("storage_cost", 0.0) * disk_gb * (hours / 730.0)
    net = (offer.get("inet_up_cost", 0.0) * up
           + offer.get("inet_down_cost", 0.0) * down)
    return gpu + disk + net


def search_offers(
    client: VastAI,
    hours: float = 8.0,
    disk_gb: int = DEFAULT_DISK_GB,
    min_reliability: float = 0.98,
    limit: int = 20,
) -> list[dict]:
    """Candidate offers, cheapest projected total first."""
    offers = client.search_offers(
        query=build_query(min_reliability, disk_gb),
        type="on-demand",
        order="dph_total",
        limit=limit,
    )
    # RE-CHECK THE CEILING CLIENT-SIDE. A query term the API silently ignores
    # looks exactly like a term it honoured, and the failure mode is landing on
    # a host that costs 4x per GB while the log says the filter was applied.
    # This project has been bitten repeatedly by checks that reported success
    # without measuring anything; belt and braces is cheap here.
    kept, dropped = [], []
    for o in offers:
        up = o.get("inet_up_cost") or 0.0
        down = o.get("inet_down_cost") or 0.0
        if up > MAX_INET_COST_PER_GB or down > MAX_INET_COST_PER_GB:
            dropped.append((o.get("id"), up, down))
            continue
        kept.append(o)
    if dropped:
        print(f"[vastctl] dropped {len(dropped)} offer(s) over the "
              f"${MAX_INET_COST_PER_TB:.2f}/TB bandwidth ceiling: "
              + ", ".join(f"{i}(up ${u*1000:.2f} down ${d*1000:.2f}/TB)"
                          for i, u, d in dropped[:6]))
    if not kept:
        raise VastError(
            f"no 5090 offer met the ${MAX_INET_COST_PER_TB:.2f}/TB bandwidth "
            f"ceiling ({len(dropped)} candidate(s) exceeded it). Raise "
            f"MAX_INET_COST_PER_TB deliberately, or wait for cheaper stock — "
            f"do not silently rent an expensive host.")
    for o in kept:
        o["_est"] = estimate_cost(o, hours, disk_gb)
    return sorted(kept, key=lambda o: o["_est"])


# --- instance state -------------------------------------------------------


@dataclass
class Instance:
    raw: dict = field(repr=False)

    @property
    def id(self) -> int:
        return int(self.raw.get("id", 0))

    @property
    def label(self) -> str:
        return self.raw.get("label") or ""

    @property
    def dph(self) -> float:
        return float(self.raw.get("dph_total") or 0.0)

    @property
    def uptime_hours(self) -> float:
        mins = self.raw.get("duration") or 0
        return float(mins) / 3600.0 if mins else 0.0

    @property
    def ssh_direct(self) -> Optional[tuple[str, int]]:
        """The instance's own IP and published port, or None.

        This is the one worth waiting for: the proxy relay measured 6.9 Mbps
        against 37 Mbps direct, and every cold start pushes a 481 MB Blender
        bundle through it. `runtype="ssh_direc ssh_proxy"` asks for both, and
        the direct mapping usually appears a little after the proxy does.
        """
        ports = self.raw.get("ports") or {}
        mapping = ports.get("22/tcp")
        ip = (self.raw.get("public_ipaddr") or "").strip()
        if mapping and ip:
            try:
                return ip, int(mapping[0]["HostPort"])
            except (KeyError, IndexError, TypeError, ValueError):
                return None
        return None

    @property
    def ssh_proxy(self) -> Optional[tuple[str, int]]:
        host, port = self.raw.get("ssh_host"), self.raw.get("ssh_port")
        return (host, int(port)) if host and port else None

    @property
    def ssh(self) -> Optional[tuple[str, int]]:
        """Direct mapping if the host published one, else the proxy."""
        return self.ssh_direct or self.ssh_proxy

    @property
    def status_detail(self) -> str:
        """Everything vast.ai will tell us about why this instance is not up."""
        bits = [
            f"actual={self.raw.get('actual_status')}",
            f"intended={self.raw.get('intended_status')}",
            f"cur_state={self.raw.get('cur_state')}",
            f"machine={self.raw.get('machine_id')}",
        ]
        msg = (self.raw.get("status_msg") or "").strip()
        if msg:
            bits.append(f"msg={' '.join(msg.split())[:300]}")
        direct, proxy = self.ssh_direct, self.ssh_proxy
        bits.append(f"direct={direct[0] + ':' + str(direct[1]) if direct else 'none'}")
        bits.append(f"proxy={proxy[0] + ':' + str(proxy[1]) if proxy else 'none'}")
        return ", ".join(bits)

    def classify(self) -> str:
        """running | loading | cold | bad | gone

        Ported from vast.ai's own autoscaler, which handles the cases a naive
        `actual_status == "running"` check misses — notably a docker pull
        failure, which surfaces only as a string inside status_msg.

        **A registry error in status_msg is not a verdict on the host.** This
        used to read `... or "Error" in msg: return "bad"`, and that single
        clause was the whole of a re-provisioning loop on 2026-08-02: every
        host rented was destroyed 41-52 s in and its machine blacklisted for
        24 h, ten offers and eight machines burned in twenty minutes, no job
        ever reaching a GPU.

        containerd retries each layer with backoff and vast retries the whole
        create, so `status_msg` is a *snapshot of the last failed attempt
        inside a retry loop*, not a terminal state. Polled directly off the
        API, instance 46588777 on machine 142679 read, over 58 s, while this
        function was calling that host dead:

            5f5407c3a203: Retrying in 1 second
            de44b265507a: Retrying in 13 seconds
            de44b265507a: Retrying in 3 seconds
            5f5407c3a203: Retrying in 8 seconds

        That is layer backoff — forward progress. And the most terminal-looking
        string of the lot,

            Error response from daemon: failed to resolve reference
            "docker.io/nvidia/cuda:12.8.0-base-ubuntu24.04": not found

        is simply false: a manifest HEAD for that exact tag against
        registry-1.docker.io returned 200 from this machine at the same
        moment. Docker Hub says "not found" when it is shedding load.

        So anything that smells of the registry or the network is `loading`.
        A host that is genuinely wedged is still caught — by READY_TIMEOUT,
        which raises with provisioning=True and condemns the *offer* without
        condemning the machine. Only conditions that are about this host and
        cannot resolve themselves are worth the 24 h machine ban below.
        """
        actual = self.raw.get("actual_status")
        intended = self.raw.get("intended_status")
        msg = self.raw.get("status_msg") or ""
        low = msg.lower()

        if (intended or actual) in GONE_STATES:
            return "gone"
        if actual == "offline" or any(p in low for p in TERMINAL_MSG_PATTERNS):
            return "bad"
        if actual == "running":
            return "running" if intended == "running" else "cold"
        if actual in (None, "loading") or (actual == "created" and intended == "running"):
            return "loading"
        if actual in ("stopping", "exited") or (actual == "created" and intended == "stopped"):
            return "cold"
        return "loading"


def our_instances(client: VastAI) -> list[Instance]:
    """Only instances this broker created. The label is the sole handle."""
    return [
        Instance(r)
        for r in client.show_instances()
        if (r.get("label") or "").startswith(LABEL_PREFIX)
    ]


# --- watchdog -------------------------------------------------------------


def watchdog_script() -> str:
    """Self-destruct loop, installed via onstart.

    CONTAINER_API_KEY is injected by vast and scoped to this instance alone, so
    leaving it on the box grants nothing beyond self-termination. This is the
    only teardown path that survives the broker dying or the local machine
    losing power — vast provides no server-side equivalent.
    """
    return f"""
mkdir -p /workspace && touch {HEARTBEAT_PATH}
cat >/usr/local/bin/broker-watchdog <<'WD'
#!/bin/bash
# Destroy this instance if the broker stops checking in, or if the hard
# wall-clock cap is reached. Belt and braces: either condition alone suffices.
HB={HEARTBEAT_PATH}
STALE={HEARTBEAT_STALE_SEC}
MAX=$(( {int(MAX_INSTANCE_HOURS)} * 3600 ))
START=$(date +%s)
while :; do
  now=$(date +%s)
  hb=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
  age=$(( now - hb ))
  run=$(( now - START ))
  if [ "$age" -gt "$STALE" ] || [ "$run" -gt "$MAX" ]; then
    logger -t watchdog "self-destruct: hb_age=${{age}}s runtime=${{run}}s"
    curl -s -X DELETE \
      -H "Authorization: Bearer $CONTAINER_API_KEY" \
      "https://console.vast.ai/api/v0/instances/$CONTAINER_ID/"
    sleep 30
  fi
  sleep 30
done
WD
chmod +x /usr/local/bin/broker-watchdog
nohup /usr/local/bin/broker-watchdog >/var/log/watchdog.log 2>&1 &
""".strip()


# --- lifecycle ------------------------------------------------------------


def guard_credit(client: VastAI) -> float:
    """Refuse to create when the prepaid ceiling is nearly spent.

    Prepaid credit with autobilling off is the only hard spend cap vast.ai
    offers, so treating it as one is the entire safety model.
    """
    user = client.show_user()
    credit = float(user.get("credit") or 0.0) + float(user.get("balance") or 0.0)
    if credit < MIN_CREDIT_USD:
        raise VastError(
            f"credit ${credit:.2f} is below the ${MIN_CREDIT_USD:.2f} floor — refusing to create"
        )
    return credit


def create(
    client: VastAI,
    offer_id: int,
    disk_gb: int = DEFAULT_DISK_GB,
    image: str = DEFAULT_IMAGE,
    runid: str = "",
) -> int:
    guard_credit(client)
    label = f"{LABEL_PREFIX}-{runid or int(time.time())}"
    resp = client.create_instance(
        id=offer_id,
        image=image,
        disk=float(disk_gb),
        label=label,
        onstart_cmd=watchdog_script(),
        cancel_unavail=True,   # never leave a stopped orphan behind
        # "ssh_direc ssh_proxy" asks for a direct port mapping and keeps the
        # proxy as fallback. Plain "ssh" lands on proxy-only, which measured
        # 6.9 Mbps — vast's own docs say to use the relay only for transfers
        # under 1 GB. Direct matters on every scene push.
        runtype="ssh_direc ssh_proxy",
    )
    if not resp.get("success"):
        raise VastError(f"create failed: {resp}")
    return int(resp["new_contract"])


def wait_ready(client: VastAI, instance_id: int, timeout: float = READY_TIMEOUT) -> Instance:
    """Poll to running, then wait for a *direct* SSH endpoint, then probe it.

    `actual_status == running` does not mean sshd is accepting connections —
    these are three distinct readiness conditions and conflating them produces
    intermittent connection-refused failures on the first job.

    Every failure raises `NotReachable` carrying the phase it died in, how long
    it took, and vast.ai's own last word on the instance. The previous message
    was `instance NNNN not running after 300s` and nothing else, which is why
    two destroyed-and-re-rented GPUs left no evidence of whether the host was
    broken, slow, or simply still pulling its image.
    """
    started = time.time()
    deadline = started + timeout
    inst: Optional[Instance] = None
    seen: list[str] = []
    nudges = 0

    while time.time() < deadline:
        raw = client.show_instance(instance_id)
        if not raw:
            raise NotReachable(instance_id, "poll", "vanished from the API during startup",
                               time.time() - started, provisioning=False)
        inst = Instance(raw)
        state = inst.classify()
        if state != (seen[-1].split()[0] if seen else None):
            seen.append(f"{state} @{time.time() - started:.0f}s")
        if state == "running":
            break
        if state in ("bad", "gone"):
            # The host said no. Nothing to wait for, and this one *is* about the
            # machine — worth not renting again this session.
            raise NotReachable(instance_id, f"classify={state}", inst.status_detail,
                               time.time() - started, provisioning=False)
        if state == "cold" and nudges < COLD_START_NUDGES:
            # `cold` here means vast created the container and then parked it:
            # actual=created, intended=stopped, with status_msg reporting the
            # image loaded *successfully*. Nothing is pulling, nothing is
            # broken, and nothing will ever move it — `cancel_unavail=True` on
            # create is supposed to prevent this, but instance 46695656 on
            # 2026-08-03 sat exactly there while the queue starved.
            #
            # The old loop had no arm for `cold`: not running, not bad, not
            # gone, so it slept out all 900 s and then raised with
            # provisioning=False, which condemns a *healthy* host and re-rents.
            # The instance only ever needed starting, so ask — bounded, because
            # a vast that ignores start_instance is a real failure mode (see
            # the resume path in fleet.py) and must still reach the timeout.
            nudges += 1
            try:
                client.start_instance(instance_id)
                seen.append(f"start_instance#{nudges} @{time.time() - started:.0f}s")
            except Exception as exc:  # noqa: BLE001 - report, never mask the wait
                seen.append(f"start_instance#{nudges} failed: {exc}")
        time.sleep(POLL_INTERVAL)
    else:
        last = Instance(client.show_instance(instance_id) or {})
        # "loading" at the deadline means the image pull or onstart is still
        # grinding: slow, not dead. The caller destroys either way (an instance
        # we cannot reach still bills) but only the dead case indicts the host.
        #
        # "cold" belongs with it: we asked to start COLD_START_NUDGES times and
        # vast never acted. The image loaded, so the machine did its part —
        # blacklisting it for 24 h would throw away good hardware for a
        # control-plane failure. Condemn the offer, keep the machine.
        provisioning = last.classify() in ("loading", "cold")
        raise NotReachable(
            instance_id, "waiting for running",
            f"{last.status_detail}; transitions: {' -> '.join(seen) or 'none observed'}",
            time.time() - started, provisioning=provisioning,
        )

    assert inst is not None

    # Prefer the direct mapping and give it a moment to appear. Settling for the
    # proxy relay silently is a 5.4x throughput cut on every transfer, and the
    # 481 MB Blender push is the first thing that happens after this returns.
    direct_deadline = time.time() + DIRECT_PORT_GRACE
    while not inst.ssh_direct and time.time() < direct_deadline:
        time.sleep(POLL_INTERVAL)
        raw = client.show_instance(instance_id)
        if raw:
            inst = Instance(raw)

    endpoint = inst.ssh
    if not endpoint:
        raise NotReachable(instance_id, "waiting for an SSH endpoint", inst.status_detail,
                           time.time() - started, provisioning=False)

    host, port = endpoint
    ssh_deadline = time.time() + SSH_PROBE_TIMEOUT
    last_error = ""
    while time.time() < ssh_deadline:
        try:
            with socket.create_connection((host, port), timeout=5):
                return inst
        except OSError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(5)
    raise NotReachable(
        instance_id, f"tcp connect to {host}:{port}",
        f"{last_error or 'no connection error recorded'}; {inst.status_detail}",
        time.time() - started,
        # sshd not answering on a box vast reports as running is a host problem.
        provisioning=False,
    )


def destroy(client: VastAI, instance_id: int, verify: bool = True) -> bool:
    """Destroy and confirm. A success response is not proof of teardown."""
    try:
        client.destroy_instance(instance_id)
    except Exception as exc:  # already gone is success
        if "404" not in str(exc):
            raise

    if not verify:
        return True
    for _ in range(12):
        time.sleep(5)
        if not client.show_instance(instance_id):
            return True
    return False


def reap(client: VastAI, keep: Iterable[int] = ()) -> list[int]:
    """Destroy every instance we own except those explicitly kept.

    Run at broker startup, before creating anything — a crashed previous run is
    otherwise invisible and bills until someone notices.
    """
    keep_set = set(keep)
    killed = []
    for inst in our_instances(client):
        if inst.id in keep_set:
            continue
        if destroy(client, inst.id):
            killed.append(inst.id)
    return killed


# --- CLI ------------------------------------------------------------------


def _client() -> VastAI:
    return VastAI(raw=True, quiet=True)


def cmd_offers(args: argparse.Namespace) -> int:
    offers = search_offers(_client(), hours=args.hours, disk_gb=args.disk)
    if not offers:
        print("no offers matched")
        return 1
    print(f"{'id':<11}{'$/hr':<7}{'rel':<7}{'net Mbps':<11}{'disk$/GB':<10}{f'est {args.hours:g}h':<10}CPU")
    print("-" * 88)
    for o in offers[: args.limit]:
        print(
            f"{o['id']:<11}{o.get('dph_total', 0):<7.3f}{o.get('reliability2', 0):<7.3f}"
            f"{str(int(o.get('inet_up', 0))) + '/' + str(int(o.get('inet_down', 0))):<11}"
            f"{o.get('storage_cost', 0):<10.4f}{'$' + format(o['_est'], '.2f'):<10}"
            f"{str(o.get('cpu_name'))[:24]}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    client = _client()
    user = client.show_user()
    credit = float(user.get("credit") or 0) + float(user.get("balance") or 0)
    print(f"credit ${credit:.2f}   autobill={user.get('autobill_threshold')}")

    instances = our_instances(client)
    if not instances:
        print("no broker instances")
        return 0
    for inst in instances:
        endpoint = inst.ssh
        where = f"{endpoint[0]}:{endpoint[1]}" if endpoint else "-"
        print(
            f"{inst.id:<11}{inst.classify():<10}${inst.dph:<7.3f}"
            f"{inst.uptime_hours:>6.2f}h  {where:<24}{inst.label}"
        )
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    killed = reap(_client())
    print(f"destroyed {len(killed)}: {killed}" if killed else "nothing to reap")
    return 0


def cmd_destroy(args: argparse.Namespace) -> int:
    ok = destroy(_client(), args.id)
    print(f"{args.id} {'destroyed' if ok else 'NOT CONFIRMED GONE — check manually'}")
    return 0 if ok else 1


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="vastctl", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("offers", help="candidate 5090s by projected total cost")
    o.add_argument("--hours", type=float, default=8.0)
    o.add_argument("--disk", type=int, default=DEFAULT_DISK_GB)
    o.add_argument("--limit", type=int, default=10)
    o.set_defaults(func=cmd_offers)

    s = sub.add_parser("status", help="credit and every instance we own")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("reap", help="destroy all broker instances (panic button)")
    r.set_defaults(func=cmd_reap)

    d = sub.add_parser("destroy", help="destroy one instance, verified")
    d.add_argument("id", type=int)
    d.set_defaults(func=cmd_destroy)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except VastError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
