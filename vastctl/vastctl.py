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
  * **The CLI is ACCOUNT-WIDE; only the library is label-scoped.** `status` and
    `reap` answer the question an operator is actually asking — "what am I
    paying for, and stop it" — which is a question about the ACCOUNT, not about
    one broker's label. Label scoping belongs in `Fleet.adopt_or_reap`, where it
    stops two brokers destroying each other's card mid-frame. Putting it in the
    emergency stop instead is how the emergency stop became a no-op; see
    `reap()` and `cmd_status()` for the measurement.

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

# Resolved off `__file__`, not off a package-relative import, because this file
# is loaded BOTH ways: `from vastctl import vastctl` (as a package, from
# `fleetctl` and `farm/procure.py`) and `sys.path.insert(ROOT/"vastctl");
# import vastctl` (as a top-level module, from `broker/fleet.py`). A
# `from . import redaction` would work in the first case and raise in the
# second, and the second is the one the broker uses.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from redaction import redact_exc as _redact_exc          # noqa: E402

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

# THE LABEL IS THE OWNERSHIP BOUNDARY, AND IT IS THE ONLY ONE.
#
# `our_instances` selects on this prefix and `Fleet.adopt_or_reap` DESTROYS
# every instance it returns except the one it adopts. So the label is not a
# cosmetic tag: it is the entire definition of "mine", and two brokers sharing
# it cannot coexist for one round of adoption. The second one reaps the first
# one's GPU out from under a running frame.
#
# Making it settable is what allows a SECOND, fully independent broker (its own
# DB, port, lock and state directory) to drive a second rented card without any
# change to the running one. The running broker keeps the default in memory and
# would re-read the same default on restart, so nothing about it changes.
#
# THE SECOND PREFIX MUST NOT START WITH THE FIRST. The filter is `startswith`,
# so "renderbroker2" is matched BY "renderbroker" — a second broker labelled
# that way would be visible to the first, and reaped by it. Pick a disjoint
# word: "ladderbroker", not "renderbroker-ladder".
LABEL_PREFIX = os.environ.get("VASTRENDER_LABEL") or "renderbroker"

# CLI AND PROBE DEFAULT ONLY — NOT WHAT THE BROKER RENTS. `Fleet._rent` passes
# `config.DISK_GB` explicitly to `search_offers`, `build_query` and `create`, so
# this value is reached only by `vastctl.py` run by hand and by
# scripts/probe_offers.py. Do not "fix" a rental size here; it will look like it
# worked and change nothing. See broker/config.py:DISK_GB, which is 80.
#
# Kept env-overridable so a hand-run probe can ask the market the same question
# the broker will ask.
DEFAULT_DISK_GB = int(os.environ.get("VASTRENDER_DISK_GB") or 30)
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

# `start_instance` returns a JSON body, and until 2026-08-07 nothing read it.
# Measured that day on instance 47049525 (machine 138180), hibernated at 14:22
# and woken at 15:05 for a queued job:
#
#     {'success': False, 'error': 'resources_unavailable',
#      'msg': 'Required resources are currently unavailable, state change queued.'}
#
# That is vast saying the machine's GPUs are all let to other tenants, so the
# container cannot be restarted and the request has been parked on a queue with
# no deadline. Nothing about it is transient in any useful sense — the instance
# read `actual=exited, intended=stopped` for the whole window, and the identical
# response on instance 47040457, on the SAME machine, never cleared across the
# 30 minutes the broker spent waiting on it that morning.
#
# Ignoring the body meant the nudge looked like it had worked: the transition
# trace logged `start_instance#1` and the loop then slept out all 900 s, twice
# (RESUME_ATTEMPTS), before fleet.py destroyed the instance and rented working
# hardware. Half an hour of a blocked queue to learn something the very first
# API call had already said in plain words.
#
# So: once vast has refused for want of resources, stop budgeting a provisioning
# timeout for it. Keep a short grace — a co-tenant releasing a card really does
# unblock the start, and the nudges continue through it — then fail so the
# caller can go rent a GPU that exists. The machine is NOT condemned: it is
# full, not broken (this one served 4.8 h of renders earlier the same day).
COLD_UNAVAIL_GRACE = 120.0
# vast's own error slug for that condition. Matched on the response body, never
# on prose, so a wording change cannot silently restore the 900 s stall.
UNAVAIL_ERRORS = ("resources_unavailable", "no_such_resource")
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
# seven-day ban on hardware that was about to work.
#
# The test for adding an entry: *would waiting fifteen more minutes fix it?*
# If yes, it is not terminal, and READY_TIMEOUT is already the right handler.
TERMINAL_MSG_PATTERNS = (
    "no space left on device",     # host disk full
    "oci runtime create failed",   # host container runtime broken
    "cannot allocate memory",      # host oversubscribed
    "invalid reference format",    # our image string is malformed — ours, not theirs
)

# `status_msg` contents that prove the HOST COULD NOT REACH VAST'S OWN CONTROL
# PLANE. Matched lowercased, as substrings.
#
# These are the mirror image of TERMINAL_MSG_PATTERNS and they exist because of
# a real 24 h ban on good hardware. On 2026-08-03 the whole `vast.ai` DNS zone
# went NXDOMAIN for ~30 minutes — this broker's own API calls died with
# `Failed to resolve 'console.vast.ai'` from 14:44:55 to 15:15:24. Machine
# 56717 was rented at 15:18:50, once our resolver had recovered, and vast
# reported the instance:
#
#     actual=offline, intended=running, cur_state=running, machine=56717,
#     msg=) Could not resolve host: cloud.vast.ai
#
# The host was fine. Its resolver was still holding the negative cache entry —
# NXDOMAIN is cached for the zone's SOA minimum, which routinely outlives the
# outage that caused it, so hosts keep failing for minutes after the control
# plane is well. `actual == "offline"` classified that `bad`, which raises with
# provisioning=False, which blacklisted machine 56717 for 24 hours.
#
# A CONTROL-PLANE FAILURE MUST NOT CONDEMN HARDWARE. There is nothing wrong
# with a machine that cannot resolve a name we could not resolve either, and a
# fleet-wide DNS event would otherwise walk the blacklist through every host it
# touched — condemning the entire fleet for one resolver's bad afternoon.
CONTROL_PLANE_MSG_PATTERNS = (
    "could not resolve host",             # curl, on the host side
    "temporary failure in name resolution",
    "name or service not known",
    "no address associated with hostname",
    "could not resolve",                  # wget/apt phrasing
    "servfail",
    "nxdomain",
)


def control_plane_fault(msg: str) -> bool:
    """Is this status message a name-resolution failure rather than a verdict?

    Deliberately NOT applied to `failed to resolve reference` — that is the
    docker REGISTRY phrasing, it is about an image and not about DNS, and
    `Instance.classify` already treats it as `loading` for its own well-earned
    reasons. Matching it here would be harmless but would blur two distinct
    lessons, and the next person to read this list deserves to see only one.
    """
    low = (msg or "").lower()
    if "resolve reference" in low:
        return False
    return any(p in low for p in CONTROL_PLANE_MSG_PATTERNS)


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

    Also carries `host_at_fault`, which is what the 24 h machine ban should
    actually key off. It defaults to `not provisioning`, so every existing call
    site keeps its exact behaviour, but the two are NOT the same question:

        provisioning  — is this instance still making forward progress?
        host_at_fault — is this machine to blame for it stopping?

    Conflating them cost machine 56717 a 24 h ban during the 2026-08-03 vast.ai
    DNS outage. It had stopped progressing (so `provisioning=False`, correctly —
    do not sit out a 900 s timeout on it) but it was not at fault: it could not
    resolve `cloud.vast.ai` because that zone was NXDOMAIN. Failing fast and
    assigning blame are separate decisions and now have separate flags.
    """

    def __init__(self, instance_id: int, phase: str, detail: str,
                 elapsed: float, provisioning: bool,
                 host_at_fault: Optional[bool] = None) -> None:
        if host_at_fault is None:
            host_at_fault = not provisioning
        super().__init__(
            f"instance {instance_id} unreachable after {elapsed:.0f}s in phase "
            f"'{phase}' ({'still provisioning' if provisioning else 'not progressing'})"
            f"{'' if host_at_fault else ' [CONTROL-PLANE FAULT — host not blamed]'}: "
            f"{detail or 'no status message from vast.ai'}"
        )
        self.instance_id = instance_id
        self.phase = phase
        self.detail = detail
        self.elapsed = elapsed
        self.provisioning = provisioning
        self.host_at_fault = host_at_fault


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
#
# BUT THIS IS A FLOOR FOR `rq exec` BUILD WORK, AND IT IS PRICING RENDER-ONLY
# RENTALS OUT OF THE CHEAPEST QUARTER OF THE MARKET.
#
# Every justification above is about BUILD throughput — items/h, concurrent
# Blender processes, slots. A GPU render is the opposite shape: one Blender
# process, Cycles on the GPU, `denoise_gpu: true`, and the scene loaded once
# under `persistent_data`. Measured on instance 47039886 (32 effective cores),
# the entire CPU-bound part of a bulk pass is `load 341s` against `render
# 17730s` — 1.9 %. Even if an 8-core host tripled the load phase it would add
# ~0.6 % to a master, and the scene loads ONCE across all 2,978 frames.
#
# What the floor costs, measured on the live market 2026-08-07:
#
#     cpu_cores_effective>=32   cheapest EXCLUSIVE offer   $0.4547/hr
#     cpu_cores_effective>=8    cheapest EXCLUSIVE offer   $0.3356/hr   -26 %
#
# Both are `gpu_frac=1.0`, so this is NOT the R2-382 co-tenancy trade — the
# cheap cards are whole machines, verified, reliability 0.993-0.997, direct
# ports, and inside the $4/TB bandwidth ceiling. The only thing they have less
# of is CPU, which this workload does not use. On a 4K master that spread is
# ~$15 — the difference between fitting in prepaid credit and not.
#
# So it stays 32 by DEFAULT (nothing that runs `rq exec` changes behaviour, and
# the broker that serves builds must keep its cores), and becomes overridable
# per broker for the render-only case — same mechanism as VASTRENDER_DISK_GB
# above, set alongside it in the launcher script. A broker that rents on a
# lowered floor MUST NOT be sent `rq exec` build work; that is the whole reason
# this is a per-process env knob and not a new default.
MIN_CPU_CORES_EFFECTIVE = float(os.environ.get("VASTRENDER_MIN_CPU") or 32.0)

# SYSTEM RAM FLOOR — and the reason the CPU floor above appeared to work.
#
# `cpu_cores_effective>=32` was never a RAM term, but it behaved like one.
# Measured against the live market 2026-08-07, adding `cpu_ram>=50` to the
# 32-core exclusive query drops ZERO offers: every >=32-core 5090 on sale
# carries 60-126 GB. Lowering the floor to 8 does not merely buy fewer cores —
# it changes hardware class, from server boards to consumer desktops, and every
# single $0.3356/hr offer in that tier is the same SKU: Ryzen 7 7800X3D,
# 8C/16T, **30.5 GB of RAM**.
#
# 30.5 GB is not enough to open this project's scene. `EXEC_SCENE_MEM_FACTOR`
# already carries the measurement — 22 GB resident for a 4.17 GB .blend, 5.3x —
# and film16_breach.blend is 7.97 GB, so it needs about 42 GB. MEASURED on
# instance 47064284 (offer 39904635, $0.3356/hr, 30.5 GB): the worker reported
# ready, the render started, and then the box went into swap so hard that sshd
# could not complete a banner exchange while ping stayed clean at 205 ms and
# TCP connected instantly. Nine minutes on a frame the 61.6 GB anchor renders in
# 151 s, no progress, no frame.
#
# THIS IS THE FAILURE MODE THE CPU FLOOR WAS ACCIDENTALLY PREVENTING, and it is
# worse than a slow render: the box does not fail, it goes catatonic. Every
# probe the broker has — heartbeat, progress, disk — travels over the same ssh
# that is being starved, so a thrashing host looks exactly like a network fault
# and gets diagnosed as one.
#
# Default 50 rather than 0. It is behaviour-preserving where the old floor
# already applied (nothing at >=32 cores is excluded by it), and it is the guard
# that makes lowering MIN_CPU safe rather than a trap. A floor that only exists
# when someone remembers to set it is the floor that was not there.
#
# NOTE THE UNITS: the vast.ai query language takes `cpu_ram` in GB, while the
# offer dict returns it in MB. Asking for `cpu_ram>=50000` matches nothing at
# all and reads as "no capacity" rather than as a malformed query. It also means
# the query floor and the offer's own figure are in DIFFERENT UNITS, so a floor
# of 64 "GB" admits a 62.7 GiB box. That is why `search_offers` re-checks the
# returned dict in GiB rather than trusting the query term — see
# `_meets_scene_working_set`.
#
# ---------------------------------------------------------------------------
# 2026-08-08: THE FLOOR AND THE REQUIREMENT HAD BECOME THE SAME NUMBER.
#
# Everything above was measured when the film was `film16_breach.blend` at
# 7.97 GB and needed ~42 GB. **The shipping scene is `film23_breach.blend` at
# 10.19 GiB and it is 50.6 GiB RESIDENT.** Measured from the cgroup on instance
# 47189253 while it served the scene for the master-cost probe (R2-3018):
#
#     /sys/fs/cgroup/memory.max      63,803,752,448   =  59.4 GiB
#     /sys/fs/cgroup/memory.current  58,342,010,880   =  54.3 GiB   (91 % used)
#     render worker RSS              53,035,096 kB    =  50.6 GiB
#
# A 50.0 GB floor against a 50.6 GiB working set is not a floor. It is the
# requirement itself, and it would have rented a box the render dies on — where
# "dies" means the catatonic swap-thrash described above, which every probe the
# broker has reads as a network fault because every probe travels over the ssh
# being starved. A cgroup OOM picks the largest RSS, and the largest RSS on a
# rendering box IS the render worker.
#
# WHY 72 AND NOT 56 OR 64. The exclusive 5090 market is bimodal and there is a
# hole in it. Surveyed 2026-08-08 17:5x, exclusive offers by floor:
#
#     floor  50 GB -> 11 offers   RAM 62.7, 60.5, 124.9, 247.3, 251.5, 125.2 GiB
#     floor  64 GB ->  8 offers   RAM 62.7, 124.9, 247.3, 251.5, 125.2, 125.7
#     floor  72 GB ->  7 offers   RAM 124.9, 247.3, 251.5, 125.2, 125.7, 251.4
#     floor  80 GB ->  7 offers   (identical set)
#     floor  96 GB ->  6 offers   (identical set)
#
# **Nothing at all is on sale between 63 GiB and 125 GiB.** So any floor above
# ~64 buys the same 125 GiB tier, and 72 is simply the cheapest way to ask for
# it. 80 and 96 cost nothing extra and buy nothing extra; 64 keeps one 62.7 GiB
# box in the set, which is 12 GiB of headroom on a scene that is still growing
# (49 of 435 item modules exist).
#
# WHAT IT COSTS: at three cards the mean rises $0.4409 -> $0.4703/GPU-hr, about
# **$8** on a 245.5 GPU-hour master. That is the price of removing a failure
# mode that presents as a network fault.
#
# WHAT IT COSTS IN DEPTH, and this is the number that decided the master's
# shape: **seven exclusive offers meet it.** Three cards is comfortable, five is
# the whole market, nine is not purchasable at a memory this scene can load.
MIN_CPU_RAM_GB = float(os.environ.get("VASTRENDER_MIN_RAM_GB") or 72.0)

# The measurement the floor exists to protect, kept as a number rather than as
# prose so the refusal message can quote it and so a future scene can update one
# constant. GiB, because this is compared against the offer dict (MB/1024), not
# against the query language (GB).
SCENE_WORKING_SET_GIB = float(
    os.environ.get("VASTRENDER_SCENE_WORKING_SET_GIB") or 50.6)

# How much of a box must be left over after the scene is resident. The probe box
# ran the whole 14-frame master-spec batch at 91 % of its cap and finished every
# frame — so 1.09x is survivable and is NOT the recommendation. `rq exec`'s
# 20 GB memory gate could not open on it at all, and the scene has only grown so
# far. 1.25x puts a 50.6 GiB scene on a >=63 GiB box, which is where the market
# splits anyway.
#
# AND THE ADVERTISED FIGURE IS NOT THE CONTAINER'S CAP. Offer 43255050 lists
# 61.9 GiB; the container it produced reported `memory.max` = 59.4 GiB — 96 % of
# what was sold. The headroom factor absorbs that gap as well as the scene's
# growth, which is a second reason not to shave it toward 1.0.
RAM_HEADROOM = float(os.environ.get("VASTRENDER_RAM_HEADROOM") or 1.25)

# HOW MANY GPUs THE BOX HAS — and why this was invisible.
#
# `num_gpus=1` was hardcoded in the query string. Not a default, not a
# parameter: a literal. **The broker has never seen a multi-GPU machine**, so
# every survey of "the market" this project has ever run was a survey of one
# eighth of it, and the doc that priced multi-GPU (docs/multi-gpu.md) had to get
# its offers by hand.
#
# It is the same shape as the CPU floor above and the RAM floor below it: a term
# nobody questioned, silently excluding the best option. Three in one file.
#
# WHAT IT COSTS, measured against the live market 2026-08-07, exclusive only
# (gpu_frac>=0.99) and only counting offers that carry >=42 GB RAM and >=8
# effective cores PER GPU — i.e. offers that could actually run N concurrent
# renders:
#
#     1x   $0.4014/hr   $0.4014/GPU-hr    15 of 21 offers qualify
#     2x   $0.7767/hr   $0.3883/GPU-hr     5 of 5
#     4x   $1.8681/hr   $0.4670/GPU-hr     1 of 1     <- n=1, noise not shape
#     8x   $2.6703/hr   $0.3338/GPU-hr    12 of 12
#
# **$/GPU-hr is the only honest denominator.** A 2x box at $0.78/hr is not
# "expensive" beside a 1x at $0.40; it is $0.39/GPU-hr, and it is the GPU-hour
# that renders a frame.
NUM_GPUS = int(os.environ.get("VASTRENDER_NUM_GPUS") or 1)

# CORES ARE A PER-BOX FLOOR **AND** A PER-GPU ONE, AND THEY ARE NOT THE SAME
# REQUIREMENT. `MIN_CPU_CORES_EFFECTIVE` is a BUILD constant — 12 concurrent
# `rq exec` Blender processes on one box — so it does not multiply by GPU count.
# What does multiply is the render side: one Blender per GPU, each wanting a
# handful of cores to load and sync a scene. The query asks for whichever is
# larger, so N=1 is bit-for-bit what it was.
#
# Expressing the build floor per GPU would be actively wrong: 32 x 8 = 256 cores
# would exclude the 192-core 8x box that is the cheapest GPU-hour on the market.
MIN_CPU_CORES_PER_GPU = float(os.environ.get("VASTRENDER_MIN_CPU_PER_GPU") or 8.0)

# EXCLUSIVITY. `gpu_frac` is the fraction of a machine's GPUs an offer covers,
# and it is the field that decides whether anyone else can be on our card.
#
# This was queryable the whole time and nobody asked. Measured 2026-08-04, the
# offers this broker had been renting read `gpu_frac: 0.125` with `cpu 32/256`
# — one eighth of a box, seven strangers alongside us. That is not a billing
# detail. It is R2-382:
#
#   A co-tenant held a fixed 17,737 MiB, constant to the megabyte across 40+
#   minutes, while our Blender swung 518 -> 13,432 MiB against a 32,607 MiB
#   card. Cycles under VRAM exhaustion returns a ZERO-FILLED BUFFER that
#   becomes a structurally perfect PNG — correct dimensions, correct sha256,
#   no picture. Six OOMs on one 4K frame, then success on the seventh with
#   nothing changed. Four dud instances and two wrong diagnoses before the
#   cause was found, because co-tenancy is invisible from inside the container.
#
# `gpu_frac>=0.99` makes that failure STRUCTURALLY IMPOSSIBLE rather than
# merely unlikely, at any GPU count, because there is no second tenant to take
# the memory. It is the cheapest guard in this file.
#
# 0.99 rather than == 1.0: a float equality against a server-side computed
# ratio is the kind of thing that silently matches nothing after a schema
# change, and nothing between 0.5 and 1.0 exists to be caught by the gap.
EXCLUSIVE_GPU_FRAC = 0.99


def build_query(min_reliability: float = 0.98, disk_gb: int = DEFAULT_DISK_GB,
                max_inet_cost: float = MAX_INET_COST_PER_GB,
                min_cpu: float = MIN_CPU_CORES_EFFECTIVE,
                min_ram_gb: float = MIN_CPU_RAM_GB,
                num_gpus: int = NUM_GPUS,
                exclusive: bool = True) -> str:
    """A 5090 with a driver new enough for Blackwell Cycles kernels.

    cuda_vers>=12.8 is not optional: below it Cycles ships no sm_120 cubin and
    the render either fails or silently falls back to CPU.

    `max_inet_cost` caps BOTH directions in $/GB. Filtered server-side here and
    re-checked client-side in search_offers(), because a silently-ignored query
    term would put us straight back on an expensive host.

    `exclusive` asks for the whole machine's GPUs (see EXCLUSIVE_GPU_FRAC). It
    is a PREFERENCE, not a requirement — search_offers falls back and says so
    loudly, because exclusive supply is thin enough to strand the queue. See
    that function for the measurement behind the choice.

    `num_gpus` is the box width. **RAM scales with it and disk does not**, and
    that asymmetry is the whole argument for one N-GPU box over N boxes:

      * RAM is per concurrent Blender — ~42 GB for this project's 7.97 GB
        scene, measured — so N workers need N x that. Per-GPU is the correct
        expression of a per-process requirement.
      * The scene cache is CONTENT-ADDRESSED on a shared filesystem
        (`/workspace/scenes/{digest}/`), so ONE push serves every worker on the
        box. N instances would pay N pushes of 7.97 GB. Disk stays per-box.
      * `collect` deletes each frame the moment its fetch verifies, so output
        does not accumulate with width either.
    """
    frac = f"gpu_frac>={EXCLUSIVE_GPU_FRAC} " if exclusive else ""
    n = max(1, int(num_gpus))
    # Per-box build floor vs per-GPU render floor — see MIN_CPU_CORES_PER_GPU.
    cores = max(min_cpu, MIN_CPU_CORES_PER_GPU * n)
    return (
        f"gpu_name=RTX_5090 num_gpus={n} {frac}cuda_vers>=12.8 "
        f"reliability>{min_reliability} inet_down>400 inet_up>400 "
        f"inet_up_cost<={max_inet_cost} inet_down_cost<={max_inet_cost} "
        f"cpu_cores_effective>={cores:g} "
        f"cpu_ram>={min_ram_gb * n:g} "
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


def _within_bandwidth_ceiling(offers: list[dict]) -> tuple[list[dict], list[tuple]]:
    """Split offers on the $/GB ceiling. Returns (kept, dropped).

    RE-CHECK THE CEILING CLIENT-SIDE. A query term the API silently ignores
    looks exactly like a term it honoured, and the failure mode is landing on a
    host that costs 4x per GB while the log says the filter was applied. This
    project has been bitten repeatedly by checks that reported success without
    measuring anything; belt and braces is cheap here.

    Split out of search_offers when the exclusivity fallback arrived, so both
    passes are filtered by the same code rather than by two copies that drift.
    """
    kept, dropped = [], []
    for o in offers:
        up = o.get("inet_up_cost") or 0.0
        down = o.get("inet_down_cost") or 0.0
        if up > MAX_INET_COST_PER_GB or down > MAX_INET_COST_PER_GB:
            dropped.append((o.get("id"), up, down))
            continue
        kept.append(o)
    return kept, dropped


def ram_gib_per_gpu(offer: dict) -> float:
    """The offer's own RAM figure, in GiB, per GPU. MB/1024, not /1000.

    Per-GPU because the requirement is per concurrent Blender process and a
    wide box runs one worker per card — the same reasoning `build_query`
    already applies when it scales the query term by `num_gpus`.
    """
    return (float(offer.get("cpu_ram") or 0.0) / 1024.0
            / max(1, int(offer.get("num_gpus") or 1)))


def _meets_scene_working_set(offers: list[dict]) -> tuple[list[dict], list[tuple]]:
    """Split offers on RAM measured in GiB. Returns (kept, dropped).

    THIS IS NOT A DUPLICATE OF THE QUERY TERM, AND THE UNITS ARE THE REASON.
    `build_query` asks `cpu_ram>={MIN_CPU_RAM_GB * n}` and the vast.ai query
    language reads that as **GB**; the offer dict answers in **MB**, which is
    GiB once divided by 1024. Those differ by 7.4 %, so a 64 "GB" floor admits a
    62.7 GiB box — observed, on the cheapest offer on the market on 2026-08-08.
    Seven per cent is not a rounding argument when the requirement is 50.6 GiB
    and the box that carried it ran at 91 % of its cap.

    So the query narrows and this DECIDES, in the units the requirement is
    measured in. Same belt-and-braces as `_within_bandwidth_ceiling` above, and
    for a better reason: that one guards against a term the API might ignore,
    this one guards against a term the API honours in units we did not mean.
    """
    need = SCENE_WORKING_SET_GIB * RAM_HEADROOM
    kept, dropped = [], []
    for o in offers:
        ram = ram_gib_per_gpu(o)
        if ram < need:
            dropped.append((o.get("id"), round(ram, 1), o.get("dph_total")))
            continue
        kept.append(o)
    return kept, dropped


def _refuse_for_ram(dropped_ram: list[tuple], num_gpus: int) -> "VastError":
    """The refusal, with the measurement and the market depth in it.

    RENTING THE BEST OF A BAD SET IS THE FAILURE, not the shortage. An operator
    reading "renting offer X" has no way to know the filter that mattered was
    the one that found nothing — the same shape as `reap` printing a confident
    empty kill list while ten GPUs billed. So this raises, and it says which
    floor, against which measurement, and how much of the market cleared it.

    THIS DELIBERATELY CONTRADICTS `search_offers`' OWN PRINCIPLE, and the
    contradiction is the point. That docstring says "a hard filter that returns
    nothing does not degrade, it RAISES, stranding every queued job...
    Availability wins over preference here", and it is right about
    EXCLUSIVITY: a shared card renders, just riskily, so having one beats
    having none. RAM is not a preference. A box that cannot hold the scene does
    not render slowly, it swaps until sshd cannot complete a banner exchange —
    and then it is diagnosed as a network fault, because every probe the broker
    owns travels over the ssh being starved. Availability of a box that cannot
    do the work is worth nothing, so this one refuses.
    """
    lines = ", ".join(f"{i} ({r:.1f} GiB, ${float(d or 0):.4f}/hr)"
                      for i, r, d in dropped_ram[:8])
    return VastError(
        f"REFUSING TO RENT: no exclusive RTX 5090 offer carries enough RAM for "
        f"this project's scene, and renting the best of a bad set is how a "
        f"render dies as a network fault.\n"
        f"  need    {SCENE_WORKING_SET_GIB:.1f} GiB resident x {RAM_HEADROOM:.2f} "
        f"headroom = {SCENE_WORKING_SET_GIB * RAM_HEADROOM:.1f} GiB per GPU\n"
        f"  measured on instance 47189253, 2026-08-08: film23_breach.blend is "
        f"10.19 GiB on disk and 53,035,096 kB (50.6 GiB) resident, on a "
        f"59.4 GiB cgroup cap running at 91 %\n"
        f"  query floor  cpu_ram>={MIN_CPU_RAM_GB * max(1, num_gpus):g} GB "
        f"(MIN_CPU_RAM_GB={MIN_CPU_RAM_GB:g}, num_gpus={num_gpus})\n"
        f"  rejected     {len(dropped_ram)} offer(s): {lines}\n"
        f"  NOTE: the exclusive 5090 market is bimodal and thin. Surveyed "
        f"2026-08-08, NOTHING at all is on sale between 63 GiB and 125 GiB, and "
        f"the count clearing this floor was EIGHT. Three cards is comfortable, "
        f"five is most of the market, and nine is not purchasable at a memory "
        f"this scene can load. If you are sizing a fleet, run "
        f"`vastctl offers` first and count — do not assume depth.\n"
        f"  If you are renting for something OTHER than the film scene (an "
        f"`rq exec` build wave, a small verify blend), lower it deliberately "
        f"and per-process: VASTRENDER_SCENE_WORKING_SET_GIB=... or "
        f"VASTRENDER_MIN_RAM_GB=... . Do not change the default.")


def search_offers(
    client: VastAI,
    hours: float = 8.0,
    disk_gb: int = DEFAULT_DISK_GB,
    min_reliability: float = 0.98,
    limit: int = 20,
    num_gpus: int = NUM_GPUS,
) -> list[dict]:
    """Candidate offers, cheapest projected total first, EXCLUSIVE ones first.

    Two passes. The first asks for whole-machine hosts (`gpu_frac>=0.99`), which
    is what makes the R2-382 co-tenant VRAM exhaustion structurally impossible
    — see EXCLUSIVE_GPU_FRAC. The second drops that term and is reached only
    when the first found nothing rentable.

    **Why a preference and not a hard requirement**, measured 2026-08-04 across
    the full production filter:

        shared    (no gpu_frac term)   19 offers / 19 machines
        exclusive (gpu_frac>=0.99)      8 offers /  8 machines

    Eight machines is thin. `bad_offers` and `bad_machines` persist for seven
    days and are now shared by every broker, so a bad day that condemns three of
    them leaves five for the whole fleet — and a hard filter that returns nothing
    does not degrade, it RAISES, stranding every queued job behind an empty
    candidate list. That is the same failure the blacklist code already refuses
    to walk into: `_rent` ignores the bans for one attempt rather than deadlock
    (it used to DELETE them, which with a shared store threw away other brokers'
    evidence), and `stalled_machines` is dropped "when it is the difference
    between renting and not renting". Availability wins over preference here for
    the same reason it wins there.

    **The fallback is LOUD, and that is the actual guard.** Co-tenancy was never
    dangerous because it was likely; it was dangerous because it was INVISIBLE —
    nothing in the container, the broker log or `rq status` said another tenant
    held 17,737 MiB of our card. A shared rental is acceptable. A shared rental
    nobody was told about is what cost the afternoon.
    """
    exclusive = client.search_offers(
        query=build_query(min_reliability, disk_gb,
                          num_gpus=num_gpus, exclusive=True),
        type="on-demand", order="dph_total", limit=limit,
    )
    kept, dropped = _within_bandwidth_ceiling(exclusive)
    kept, dropped_ram = _meets_scene_working_set(kept)

    shared_fallback = False
    if not kept:
        shared_fallback = True
        offers = client.search_offers(
            query=build_query(min_reliability, disk_gb,
                              num_gpus=num_gpus, exclusive=False),
            type="on-demand", order="dph_total", limit=limit,
        )
        kept, more_dropped = _within_bandwidth_ceiling(offers)
        kept, more_ram = _meets_scene_working_set(kept)
        dropped += more_dropped
        dropped_ram += more_ram

    # RAM IS CHECKED BEFORE BANDWIDTH IS BLAMED, and before the co-tenancy
    # fallback gets to speak. Both of those messages are confident and neither
    # is about memory, so an operator who hit the RAM wall would read a sentence
    # about $/TB or about sharing a card and go looking in the wrong place.
    # Refuse on the real reason, first.
    if not kept and dropped_ram:
        raise _refuse_for_ram(dropped_ram, num_gpus)
    if dropped_ram:
        print(f"[vastctl] dropped {len(dropped_ram)} offer(s) under the "
              f"{SCENE_WORKING_SET_GIB * RAM_HEADROOM:.1f} GiB/GPU scene "
              f"working-set floor: "
              + ", ".join(f"{i}({r} GiB)" for i, r, _ in dropped_ram[:6]))
    if dropped:
        print(f"[vastctl] dropped {len(dropped)} offer(s) over the "
              f"${MAX_INET_COST_PER_TB:.2f}/TB bandwidth ceiling: "
              + ", ".join(f"{i}(up ${u*1000:.2f} down ${d*1000:.2f}/TB)"
                          for i, u, d in dropped[:6]))
    if not kept:
        raise VastError(
            f"no 5090 offer met the ${MAX_INET_COST_PER_TB:.2f}/TB bandwidth "
            f"ceiling ({len(dropped)} candidate(s) exceeded it), exclusive or "
            f"shared. Raise MAX_INET_COST_PER_TB deliberately, or wait for "
            f"cheaper stock — do not silently rent an expensive host.")

    # Annotated on every offer, not just the fallback ones, so the rent log line
    # states exclusivity in BOTH directions. A field that only appears when
    # something is wrong leaves "exclusive" and "the check did not run" looking
    # identical from the outside — the exact hole the blank gate fell into.
    for o in kept:
        frac = o.get("gpu_frac")
        o["_exclusive"] = frac is not None and float(frac) >= EXCLUSIVE_GPU_FRAC
        o["_est"] = estimate_cost(o, hours, disk_gb)
        # THE NUMBER TO COMPARE ACROSS WIDTHS, and never the one to sort on
        # here: `_est` is what THIS broker will spend, and a broker running one
        # worker on an 8-GPU box pays for eight and uses one. `_dph_per_gpu`
        # only becomes the deciding figure once there is a worker per card.
        o["_dph_per_gpu"] = float(o.get("dph_total") or 0.0) / max(
            1, int(o.get("num_gpus") or 1))
        # One definition, used by the filter and by the display alike, so the
        # number an operator reads is the number that was decided on.
        o["_ram_gb_per_gpu"] = ram_gib_per_gpu(o)

    if shared_fallback:
        cheapest = min(kept, key=lambda o: o["_est"])
        print(
            f"[vastctl] *** RENTING A SHARED GPU — NO EXCLUSIVE HOST WAS "
            f"AVAILABLE *** No offer with gpu_frac>={EXCLUSIVE_GPU_FRAC} passed "
            f"the filter, so this falls back to a host we SHARE with other "
            f"tenants. The cheapest candidate is offer {cheapest.get('id')} at "
            f"gpu_frac={cheapest.get('gpu_frac')} "
            f"(cpu {cheapest.get('cpu_cores_effective')}/{cheapest.get('cpu_cores')}). "
            f"A co-tenant can take VRAM out from under a render at any moment, "
            f"and Cycles answers VRAM exhaustion with a structurally perfect "
            f"all-black PNG (R2-382: 17,737 MiB held by a stranger, six OOMs on "
            f"one 4K frame). If black frames appear on this instance, CHECK "
            f"`nvidia-smi` FOR A SECOND COMPUTE PROCESS FIRST — do not re-diagnose "
            f"it as a scene fault."
        )
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
    def gpu_frac(self) -> Optional[float]:
        """Fraction of the machine's GPUs this instance holds, or None.

        THE EXCLUSIVITY GUARD IS RENT-TIME ONLY, AND THIS IS HOW YOU SEE PAST
        THAT. `build_query` filters offers on `gpu_frac>=0.99`, but
        `Fleet.adopt_or_reap` never re-asks: a card rented before the guard
        existed, or taken on the loud shared fallback, is re-adopted unchecked
        by every subsequent restart, forever, and nothing in the log or in
        `rq status` distinguishes it from an exclusive one.

        Measured 2026-08-04, which is exactly how it went: instance 46780377
        was rented 10:15:25 from an offer with `gpu_frac 0.125` — one eighth of
        a box — under code written before the guard landed at 13:01:19. The
        broker then restarted at 15:31, 16:03 and 16:22 WITH the guard loaded,
        and adopted the same shared card all three times. It measured 1.64x
        slower per frame than an exclusive 5090 on identical work, and the only
        number anyone saw was a $0.4203/hr headline that looked like a bargain.
        """
        raw = self.raw.get("gpu_frac")
        return None if raw is None else float(raw)

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


def other_instances(client: VastAI) -> list[Instance]:
    """Instances on this account that are NOT ours — a second broker's cards.

    THE OTHER HALF OF THE BILL. `our_instances` is scoped to `LABEL_PREFIX`
    precisely so two brokers cannot reap each other, and the cost of that
    isolation is that neither can SEE the other's spend. `rq teardown`
    therefore destroys one card, reports success, and leaves the other billing
    — the obvious action, reporting success, with half the money still running.

    This is not `our_instances`' inverse by accident: teardown must never
    DESTROY these (there may be a frame in flight on a sibling broker's box,
    and cross-broker destruction is the exact bug the label split prevents).
    It must only ever NAME them, so nobody walks away believing the farm is
    down when it is half up.
    """
    return [
        Instance(r)
        for r in client.show_instances()
        if not (r.get("label") or "").startswith(LABEL_PREFIX)
    ]


def all_instances(client: VastAI) -> list[Instance]:
    """EVERY instance on this account. The whole bill, no filter, no exceptions.

    THE ONLY ENUMERATION AN EMERGENCY STOP MAY USE. `our_instances` and
    `other_instances` partition the account by `LABEL_PREFIX`, and that split is
    correct for a *broker* deciding what it may destroy. It is catastrophic for
    an *operator* asking what is running, because `LABEL_PREFIX` comes from the
    environment and a hand-run CLI has no environment: it silently answers
    "renderbroker", a label the fleet does not use.

    Measured 2026-08-08 on this account, with nine RTX 5090s rendering:

        $ vastctl status
        credit $51.13   autobill=None
        no broker instances          <-- nine cards, $4.3065/hr, $103/day

        our_instances()   -> []              (this was reap's entire kill list)
        other_instances() -> 9 instances, ladderbroker + fleet03..fleet10

    Nine live cards, and both the report AND the panic button read zero. The
    fleet labels its instances `fleet03`…`fleet11` (`farm/brokers.py:_label`)
    and the second broker uses `ladderbroker`; none of them start with
    `renderbroker`, so `scripts/panic.sh` — the project's only emergency stop —
    destroyed nothing, printed "no broker instances", and told the operator the
    farm was down while it billed at $103/day.

    That is strictly worse than a missing feature. A stop that fails loudly gets
    escalated; this one reported success.
    """
    return sorted((Instance(r) for r in client.show_instances()), key=lambda i: i.id)


def live_broker_labels() -> dict[str, list[int]]:
    """{VASTRENDER_LABEL: [pid, …]} for every broker process running right now.

    Read out of `/proc/<pid>/environ`, which is the kernel's record of how the
    process was actually started — not a claim the broker makes about itself,
    and not a state file that goes stale the moment a broker dies. Same
    technique as `farm/brokers.py:verify()`, for the same reason.

    A broker started without `VASTRENDER_LABEL` gets the module default, exactly
    as the broker itself would compute it, so broker 1 is not miscounted as
    label-less.

    Never shells out to `pgrep`/`pkill`: those match on the whole command line,
    which includes the shell that ran them, and this project has eaten its own
    session that way (see `scripts/panic.sh`). Pure /proc reads, no signals.
    """
    found: dict[str, list[int]] = {}
    try:
        pids = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return found
    for entry in pids:
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().decode("utf-8", "replace")
            if "broker.app" not in cmdline:
                continue
            with open(f"/proc/{pid}/comm") as fh:
                if not fh.read().strip().startswith("python"):
                    continue          # the shell that spawned it is not a broker
            with open(f"/proc/{pid}/environ", "rb") as fh:
                env = fh.read().decode("utf-8", "replace")
        except (OSError, PermissionError):
            continue
        label = LABEL_PREFIX
        for kv in env.split("\0"):
            if kv.startswith("VASTRENDER_LABEL="):
                label = kv.split("=", 1)[1] or LABEL_PREFIX
                break
        found.setdefault(label, []).append(pid)
    return found


def broker_for(inst: Instance, brokers: dict[str, list[int]]) -> Optional[str]:
    """The live broker label owning this instance, or None if it is an ORPHAN.

    Instance labels are `<broker label>-<runid>` (`create()`), so the owning
    label is the part before the run stamp. Matched by longest label first:
    `fleet1` would otherwise claim `fleet11`'s card, which is the exact aliasing
    bug `farm/brokers.py` uses fixed-width labels to avoid.
    """
    label = inst.label
    for known in sorted(brokers, key=len, reverse=True):
        if label == known or label.startswith(known + "-"):
            return known
    return None


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


def _unavailable(resp: Any) -> str:
    """vast's refusal slug if a start was declined for want of resources, else "".

    `VastAI(raw=True)` hands back the parsed JSON body, but a transport that
    returns a string, None, or a list is not worth an AttributeError inside a
    wait loop — anything unrecognised reads as "no refusal seen", which restores
    exactly the previous behaviour.
    """
    if not isinstance(resp, dict) or resp.get("success"):
        return ""
    err = str(resp.get("error") or "").strip()
    return err if err in UNAVAIL_ERRORS else ""


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
    unavail_since: Optional[float] = None

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
            # The host said no. Nothing to wait for — fail fast either way.
            #
            # But WHY it said no decides whether the machine is condemned. An
            # `actual=offline` whose status_msg is a name-resolution failure is
            # the host telling us it cannot reach vast.ai, which during a zone
            # outage is true of every host alive and is not hardware's fault.
            # See CONTROL_PLANE_MSG_PATTERNS for the incident this comes from.
            raise NotReachable(
                instance_id, f"classify={state}", inst.status_detail,
                time.time() - started, provisioning=False,
                host_at_fault=not control_plane_fault(
                    inst.raw.get("status_msg") or ""),
            )
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
                resp = client.start_instance(instance_id)
                refusal = _unavailable(resp)
                if refusal:
                    # vast refused for want of free GPUs on the host. See
                    # COLD_UNAVAIL_GRACE: keep nudging, but stop paying the full
                    # provisioning timeout for a start that has been queued
                    # behind another tenant with no deadline attached.
                    seen.append(
                        f"start_instance#{nudges} refused ({refusal}) "
                        f"@{time.time() - started:.0f}s"
                    )
                    unavail_since = unavail_since or time.time()
                    deadline = min(deadline, unavail_since + COLD_UNAVAIL_GRACE)
                    # The nudge budget exists to stop us spinning on a vast that
                    # silently ignores us. This vast is answering, and answering
                    # the same way each time, so let the grace window do the
                    # bounding instead of burning the budget in 30 s.
                    nudges -= 1
                else:
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
        # blacklisting it for seven days would throw away good hardware for a
        # control-plane failure. Condemn the offer, keep the machine.
        #
        # And a host that spent the whole timeout unable to resolve a vast.ai
        # name is in the same position for the same reason: it is not the
        # hardware that is broken. Same guard as the fast-fail path above.
        provisioning = last.classify() in ("loading", "cold")
        raise NotReachable(
            instance_id, "waiting for running",
            f"{last.status_detail}; transitions: {' -> '.join(seen) or 'none observed'}",
            time.time() - started, provisioning=provisioning,
            host_at_fault=(not provisioning
                           and not control_plane_fault(last.raw.get("status_msg") or "")),
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


def reap(client: VastAI, keep: Iterable[int] = (),
         *, labelled_only: bool = False) -> list[int]:
    """Destroy every instance ON THIS ACCOUNT except those explicitly kept.

    THE PANIC BUTTON, AND IT USED TO BE A NO-OP. This enumerated
    `our_instances` — `LABEL_PREFIX`-scoped, and `LABEL_PREFIX` defaults to
    "renderbroker" for anything run without a broker's environment, which is
    precisely how `scripts/panic.sh` runs it. Measured 2026-08-08 with nine
    RTX 5090s live at $4.3065/hr: the kill list was `[]`. panic.sh destroyed
    nothing and then printed "no broker instances", so the operator's evidence
    that the farm was down was produced by the same blind spot that left it up.
    See `all_instances` for the full measurement.

    So the default is now the whole account. That is not a widening of scope,
    it is the scope the caller always believed it had: panic.sh's first line is
    "Destroy every rented GPU, right now", and `--help` has always read
    "destroy all broker instances (panic button)".

    `labelled_only=True` restores the old `LABEL_PREFIX` scoping for a caller
    that genuinely means one broker's cards. NOTHING in this tree passes it —
    `Fleet.adopt_or_reap` does not call this function at all, it walks
    `our_instances` itself precisely so cross-broker destruction stays
    impossible on the *broker's* path. Keep it that way: label scoping is a
    property of automatic teardown, never of the emergency stop.
    """
    keep_set = set(keep)
    killed = []
    for inst in (our_instances(client) if labelled_only else all_instances(client)):
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
    # RAM and geolocation are shown because both decide things and neither was
    # visible. RAM is the floor that stops a 50.6 GiB scene renting a box it
    # dies on (MIN_CPU_RAM_GB). GEOLOCATION is the field the offer dict has
    # always carried and nothing has ever read: `inet_up` says 734 Mbps for the
    # box rented on 2026-08-08 and its RTT was 254 ms, which cost 13.6 min of
    # scene push on ONE stream and 13.5 s per frame of fetch. Advertised
    # bandwidth does not predict a single-stream transfer; distance does.
    print(f"{'id':<11}{'$/hr':<7}{'rel':<7}{'net Mbps':<11}{'RAM GiB':<9}"
          f"{'disk$/GB':<10}{f'est {args.hours:g}h':<10}{'geo':<20}CPU")
    print("-" * 118)
    for o in offers[: args.limit]:
        print(
            f"{o['id']:<11}{o.get('dph_total', 0):<7.3f}{o.get('reliability2', 0):<7.3f}"
            f"{str(int(o.get('inet_up', 0))) + '/' + str(int(o.get('inet_down', 0))):<11}"
            f"{o.get('_ram_gb_per_gpu', 0.0):<9.1f}"
            f"{o.get('storage_cost', 0):<10.4f}{'$' + format(o['_est'], '.2f'):<10}"
            f"{str(o.get('geolocation'))[:19]:<20}"
            f"{str(o.get('cpu_name'))[:24]}"
        )
    print(f"\nRAM floor {SCENE_WORKING_SET_GIB * RAM_HEADROOM:.1f} GiB/GPU "
          f"({SCENE_WORKING_SET_GIB:.1f} GiB measured resident x {RAM_HEADROOM:.2f}); "
          f"{len(offers)} offer(s) cleared it.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Everything this ACCOUNT is paying for, in one screen.

    Not "every instance we own" — that was the old help text and the old bug.
    An operator running `status` is asking "what is billing"; a per-label answer
    to that question is worse than no answer, because it looks like one. On
    2026-08-08 it read "no broker instances" over nine live 5090s.

    The broker column comes from `/proc`, not from a state file: an instance
    whose broker process is gone is an ORPHAN, it is pure waste at ~$0.45/hr
    with nothing left that can dispatch work to it, and it is called out in
    capitals because that is the case worth acting on.
    """
    client = _client()
    user = client.show_user()
    credit = float(user.get("credit") or 0) + float(user.get("balance") or 0)
    print(f"credit ${credit:.2f}   autobill={user.get('autobill_threshold')}")

    instances = all_instances(client)
    if not instances:
        print("no instances on this account (checked the vast.ai API, "
              "not a local state file)")
        return 0

    brokers = live_broker_labels()
    # Wide enough for "ladderbroker pid 1234567" uncut. A truncated pid is not a
    # pid — the operator's next command is `kill -0 <that>` and a clipped digit
    # points at somebody else's process.
    print(f"{'id':<11}{'state':<9}{'gpu':<20}{'$/hr':<8}{'up':<9}"
          f"{'broker':<28}{'endpoint':<24}label")
    print("-" * 128)
    orphans: list[Instance] = []
    total = 0.0
    for inst in instances:
        total += inst.dph
        endpoint = inst.ssh
        where = f"{endpoint[0]}:{endpoint[1]}" if endpoint else "-"
        gpu = str(inst.raw.get("gpu_name") or "?")
        n = int(inst.raw.get("num_gpus") or 1)
        if n != 1:
            gpu = f"{n}x {gpu}"
        frac = inst.gpu_frac
        if frac is not None and frac < 0.99:
            gpu += f" ({frac:.3g} SHARED)"
        owner = broker_for(inst, brokers)
        if owner:
            pids = ",".join(str(p) for p in brokers[owner])
            attached = f"{owner} pid {pids}"
        else:
            attached = "*** NO BROKER ***"
            orphans.append(inst)
        print(f"{inst.id:<11}{inst.classify():<9}{gpu[:19]:<20}{inst.dph:<8.4f}"
              f"{inst.uptime_hours:>6.2f}h  {attached:<28}{where:<24}{inst.label}")

    print("-" * 128)
    print(f"{len(instances)} instance(s)   ${total:.4f}/hr   ${total * 24:.2f}/day"
          + (f"   credit ${credit:.2f} = {credit / total:.1f}h of runway"
             if total > 0 else ""))

    if orphans:
        cost = sum(i.dph for i in orphans)
        print(f"\n*** {len(orphans)} ORPHAN(S): RENTED, BILLING, NO BROKER "
              f"ATTACHED — ${cost:.4f}/hr, ${cost * 24:.2f}/day OF PURE WASTE ***")
        for inst in orphans:
            print(f"      {inst.id}  {inst.label}  ${inst.dph:.4f}/hr  "
                  f"{inst.uptime_hours:.2f}h old")
        print("    Nothing can dispatch work to these. Destroy them by id:")
        print("      " + "; ".join(
            f".venv/bin/python vastctl/vastctl.py destroy {i.id}" for i in orphans))

    idle = sorted(set(brokers) - {broker_for(i, brokers) for i in instances})
    if idle:
        print(f"\n{len(idle)} broker(s) running with no rented card (costing "
              f"nothing, but rendering nothing): " + ", ".join(
                  f"{lbl} pid {','.join(str(p) for p in brokers[lbl])}"
                  for lbl in idle))
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    client = _client()
    doomed = our_instances(client) if args.only_label else all_instances(client)
    if not doomed:
        print("nothing to reap")
        return 0
    # Name the list BEFORE destroying it. A teardown that prints only its result
    # cannot be audited afterwards, and the ids are the one thing that survives
    # into a console check if a destroy is not confirmed.
    cost = sum(i.dph for i in doomed)
    print(f"destroying {len(doomed)} instance(s), ${cost:.4f}/hr:")
    for inst in doomed:
        print(f"    {inst.id}  {inst.classify():<9}${inst.dph:.4f}/hr  {inst.label}")
    killed = reap(client, labelled_only=args.only_label)
    print(f"destroyed {len(killed)}: {killed}" if killed else "nothing to reap")
    missed = [i.id for i in doomed if i.id not in set(killed)]
    if missed:
        print(f"NOT CONFIRMED GONE — CHECK https://cloud.vast.ai/instances/ "
              f"BY HAND: {missed}")
        return 1
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

    s = sub.add_parser("status", help="credit and EVERY instance on the account")
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("reap", help="destroy every instance on the account "
                                    "(panic button)")
    # Deliberately not the default and deliberately awkward to type. The panic
    # button's job is to leave nothing billing; a scoped reap that reports
    # success while eight cards keep running is the defect this flag documents.
    r.add_argument("--only-label", action="store_true",
                   help=f"restrict to labels starting with {LABEL_PREFIX!r} "
                        f"(the old default; this is how the emergency stop "
                        f"came to destroy nothing)")
    r.set_defaults(func=cmd_reap)

    d = sub.add_parser("destroy", help="destroy one instance, verified")
    d.add_argument("id", type=int)
    d.set_defaults(func=cmd_destroy)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except VastError as exc:
        # `VastError` is raised with the SDK's own response text in several
        # places in this file (`create failed: {resp}`), and the SDK's errors
        # name the request URL, which is where the API key lives. This is the
        # last line before the account's credential reaches a terminal.
        print(f"error: {_redact_exc(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
