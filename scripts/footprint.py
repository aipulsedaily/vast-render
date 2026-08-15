#!/usr/bin/env python
"""Read the RESIDENT FOOTPRINT of the render worker on the live instance.

WHY THIS EXISTS. `vastctl.SCENE_WORKING_SET_GIB = 50.6` is the number that
decides which boxes the broker may rent (`_meets_scene_working_set`, floor =
SCENE_WORKING_SET_GIB * RAM_HEADROOM). It was never a tool's output: on
2026-08-08 it was a hand reading over ssh on instance 47189253 of

    /sys/fs/cgroup/memory.max      63,803,752,448   =  59.4 GiB
    /sys/fs/cgroup/memory.current  58,342,010,880   =  54.3 GiB   (91 % used)
    render worker RSS              53,035,096 kB    =  50.6 GiB

A hand reading cannot be repeated after the instance is destroyed, and the
instance is destroyed the moment the queue drains. So the reading has to be
taken WHILE THE PROBE RENDERS. This is that reading, as a command.

It is READ-ONLY. It creates nothing, rents nothing and destroys nothing; it
resolves the endpoint the broker is already using and runs `cat` over it.

    .venv/bin/python scripts/footprint.py            # one reading, all instances
    .venv/bin/python scripts/footprint.py --watch 60 # every 60 s until Ctrl-C
    .venv/bin/python scripts/footprint.py --instance 47189253

The peak is what matters, not the instant: RSS climbs through .blend load and
BVH build and only then plateaus. Run it with --watch across at least one whole
frame and take the maximum.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from broker import config, remote  # noqa: E402
from vastctl import vastctl  # noqa: E402

GIB = 1024.0 ** 3

# One command, one ssh round trip, tagged output. Kept as a single string so the
# four readings are simultaneous — a worker whose RSS is climbing would
# otherwise be sampled at four different moments and the ratios would be wrong.
#
# `memory.max` reads "max" on a cgroup with no cap; that is not an error, it
# means the container was sold the whole machine's RAM. Print it verbatim
# rather than coercing it to a number that would be a lie.
READ = (
    "echo cgroup.memory.max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null); "
    "echo cgroup.memory.current=$(cat /sys/fs/cgroup/memory.current 2>/dev/null); "
    "echo cgroup.memory.peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null); "
    "echo cgroup.cpu.max=$(cat /sys/fs/cgroup/cpu.max 2>/dev/null | tr ' ' '/'); "
    "echo cgroup.memory.events=$(cat /sys/fs/cgroup/memory.events 2>/dev/null | tr '\\n' ' '); "
    # The worker is `blender -b <scene> -P /workspace/server.py`. Match on the
    # server.py argument, not on the name `blender`: the bundle ships several
    # blender-ish helper processes and only this one holds the scene.
    # WALK /proc DIRECTLY rather than trusting `pgrep -f server.py`: that pattern
    # also matches the very ssh command carrying it (observed — it matched its
    # own bash, PID 7357), and a shell whose 4 MB RSS lands in the same variable
    # as the worker's 50 GiB is not a rounding error, it is the wrong process.
    # Gate on comm==blender* AND the absolute /workspace/server.py path.
    #
    # AND NOTE THE SEMICOLON AFTER THE FIRST echo. Without it the concatenated
    # one-liner reads `echo worker.pid=$p  rss=$(...)`, so `echo` swallows the
    # assignment as an argument and $rss is empty forever — which presented as
    # "NO WORKER PROCESS" while the cgroup lines all read fine.
    "for d in /proc/[0-9]*; do "
    "  p=${d#/proc/}; "
    "  case \"$(cat $d/comm 2>/dev/null)\" in blender*) ;; *) continue;; esac; "
    "  case \"$(tr '\\0' ' ' < $d/cmdline 2>/dev/null)\" in */workspace/server.py*) ;; *) continue;; esac; "
    "  echo worker.pid=$p; "
    "  rss=$(awk '/^VmRSS:/{print $2}' $d/status 2>/dev/null); "
    "  hwm=$(awk '/^VmHWM:/{print $2}' $d/status 2>/dev/null); "
    "  echo worker.rss_kb=$rss; echo worker.hwm_kb=$hwm; "
    "done; "
    "free -k | awk 'NR==2{print \"host.mem_total_kb=\"$2; print \"host.mem_used_kb=\"$3; print \"host.mem_avail_kb=\"$7}'; "
    "nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu "
    "  --format=csv,noheader | head -1 | sed 's/^/gpu=/'; "
    "echo progress=$(cat " + config.REMOTE_ROOT + "/progress.json 2>/dev/null | tr -d '\\n' | cut -c1-200)"
)


def _fmt(kv: dict) -> str:
    out = []
    mx, cur = kv.get("cgroup.memory.max"), kv.get("cgroup.memory.current")
    peak = kv.get("cgroup.memory.peak")

    def gib(v):
        try:
            return f"{int(v) / GIB:.2f} GiB"
        except (TypeError, ValueError):
            return str(v)

    out.append(f"  cgroup memory.max      {mx:>18}  = {gib(mx)}")
    out.append(f"  cgroup memory.current  {cur:>18}  = {gib(cur)}"
               + (f"   ({int(cur) / int(mx) * 100:.0f} % of cap)"
                  if str(mx).isdigit() and str(cur).isdigit() else ""))
    if peak:
        out.append(f"  cgroup memory.peak     {peak:>18}  = {gib(peak)}")
    out.append(f"  cgroup cpu.max         {kv.get('cgroup.cpu.max'):>18}"
               + _cpus(kv.get("cgroup.cpu.max")))
    if kv.get("cgroup.memory.events"):
        out.append(f"  cgroup memory.events   {kv['cgroup.memory.events']}")
    rss, hwm = kv.get("worker.rss_kb"), kv.get("worker.hwm_kb")
    if rss:
        out.append(f"  worker RSS             {int(rss):>18,} kB = "
                   f"{int(rss) * 1024 / GIB:.2f} GiB   <-- SCENE_WORKING_SET_GIB")
    if hwm:
        out.append(f"  worker peak RSS (HWM)  {int(hwm):>18,} kB = "
                   f"{int(hwm) * 1024 / GIB:.2f} GiB")
    if not rss:
        out.append("  worker RSS             NO WORKER PROCESS — nothing is loaded; "
                   "this reading is meaningless, take it while a frame renders")
    for k in ("host.mem_total_kb", "host.mem_used_kb", "host.mem_avail_kb"):
        if kv.get(k):
            out.append(f"  {k:<22} {int(kv[k]):>18,} kB = {int(kv[k]) * 1024 / GIB:.2f} GiB")
    if kv.get("gpu"):
        out.append(f"  gpu                    {kv['gpu']}")
    if kv.get("progress"):
        out.append(f"  progress               {kv['progress']}")
    if (hwm or rss) and str(mx).isdigit():
        # PEAK, not instantaneous. The constant has to cover the worst moment
        # the process ever reached, not whichever moment we happened to sample —
        # RSS dips back after the .blend load and the BVH build, so sampling the
        # plateau would set the floor below the spike that has to fit under it.
        need = int(hwm or rss) * 1024 / GIB
        out.append("")
        out.append(f"  => SCENE_WORKING_SET_GIB would become {need:.1f}; "
                   f"RAM floor = {need * vastctl.RAM_HEADROOM:.1f} GiB/GPU "
                   f"(x{vastctl.RAM_HEADROOM:g}). Current constant is "
                   f"{vastctl.SCENE_WORKING_SET_GIB:.1f} "
                   f"(floor {vastctl.SCENE_WORKING_SET_GIB * vastctl.RAM_HEADROOM:.1f}).")
    return "\n".join(out)


def _cpus(v) -> str:
    try:
        quota, period = str(v).split("/")[:2]
        if quota == "max":
            return "   (uncapped)"
        return f"   = {int(quota) / int(period):.2f} CPUs"
    except Exception:
        return ""


def read_once(ep, timeout: float) -> dict:
    ran = remote.probe(ep, READ, timeout=timeout, mux=False)
    if not ran.ok:
        return {"_error": ran.describe()}
    kv = {}
    for line in ran.out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    return kv


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", type=int, default=None,
                    help="one instance id; default is every instance on the account")
    ap.add_argument("--watch", type=float, default=0.0,
                    help="seconds between readings; 0 = one reading and exit")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    client = vastctl._client()

    while True:
        insts = vastctl.all_instances(client)
        if args.instance:
            insts = [i for i in insts if i.id == args.instance]
        if not insts:
            # IN --watch MODE THIS IS NOT AN ERROR, AND TREATING IT AS ONE WAS A
            # BUG. The watcher is armed at submit time, which is BEFORE the
            # broker has rented anything — so "no instance" is the expected
            # first state, and exiting on it would surrender the one window in
            # which the reading can be taken at all (IDLE_GRACE_SEC = 300).
            if not args.watch:
                print("no instance to read — nothing is running on this "
                      "account. This reading MUST be taken while the probe "
                      "renders; after the instance is destroyed it cannot be "
                      "recovered.", file=sys.stderr)
                return 1
            print(f"{time.strftime('%H:%M:%S')}  no instance yet — waiting for "
                  f"the broker to rent one", flush=True)
            time.sleep(args.watch)
            continue
        for inst in insts:
            pair = inst.ssh
            if not pair:
                print(f"instance {inst.id}: no ssh yet ({inst.status_detail})")
                continue
            ep = remote.Endpoint(host=pair[0], port=pair[1], instance_id=inst.id,
                                 direct=inst.ssh_direct is not None)
            print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  instance {inst.id}  "
                  f"{ep}  ${inst.raw.get('dph_total', 0):.4f}/hr ===")
            kv = read_once(ep, args.timeout)
            if kv.get("_error"):
                print(f"  UNREACHABLE: {kv['_error']}")
                print("  (a busy box says 'exit -1 after Ns'; a dead one says "
                      "'Network is unreachable'. Read the text before concluding.)")
                continue
            print(_fmt(kv))
        if not args.watch:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
