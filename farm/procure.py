#!/usr/bin/env python3
"""Rank 5090 offers on $/FRAME, from measured throughput. Never on $/GPU-hr.

    .venv/bin/python farm/procure.py --frames 48
    .venv/bin/python farm/procure.py --frames 2978 --show-rejected

THE TRAP THIS EXISTS TO CLOSE
-----------------------------
Measured across the exclusive 5090 market on 2026-08-07 (docs/multi-gpu.md),
one frame of `film16_breach.blend` at 3840x2160 / 512 spp, `spec_hash
1983dced5cacabb6` on every host so it is the same render every time:

    Florida 1x               $0.4488/GPU-hr x 151.0 s = $0.01882/frame
    S. Africa 1x             $0.3999/GPU-hr x 166.8 s = $0.01853/frame
    California 8x, per GPU   $0.3387/GPU-hr x 225.4 s = $0.02121/frame  <- DEAREST

**The cheapest $/GPU-hr on the board is the dearest per frame.** Width buys
8.8 % across the whole market ($0.3803 -> $0.3470 per GPU-hour); the host
lottery — how fast the silicon you happened to draw actually renders — is
**+-45 %**. Width is inside the noise of which host you get. And the two 1x
hosts differ 11 % in price and 10 % in speed and **cancel to 1.6 % per frame**:
shopping on sticker price is close to worthless.

So this ranks on `$/hr x measured s/frame`, and where there is no measurement
it says so in the output rather than quietly substituting a prior. An estimate
that cannot be told apart from a measurement is the defect this project logs
most often.

THE HARD REJECTS, AND WHY THEY ARE REJECTS AND NOT PENALTIES
------------------------------------------------------------
* **`gpu_frac < 0.99`.** `vastctl.search_offers` treats exclusivity as a
  PREFERENCE and falls back to shared supply, loudly, because a hard filter
  that returns nothing strands the queue — correct for a single interactive
  broker that must keep serving. It is wrong for fleet procurement: a fleet is
  provisioned deliberately, in one go, and a shared card is not "slightly
  worse", it is R2-382 — a co-tenant took 17,737 MiB out from under a render
  and Cycles answered VRAM exhaustion with a **structurally perfect all-black
  PNG**. The measured shared card was also 1.64x slower per frame while
  costing 12 % less. Waiting for exclusive supply is always cheaper than
  rendering a block twice.

* **`num_gpus > 1`.** Of 42 two-GPU listings surveyed, **4 were exclusive and
  28 were quarter shares**, and the listing text reads "2x RTX 5090" either
  way. Worse, a multi-GPU box reaching a broker that has not been told about it
  enables every OptiX device and delivers 1.27x for N times the rent — the
  $512 row. `worker/server.py` now refuses that outright (see `TooManyGPUs`),
  and this refuses to buy it in the first place.

Both can be overridden, and both require saying so out loud.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RATES = Path(__file__).resolve().parent / "hostrates.json"

# The prior used for an UNMEASURED host, and it is labelled as a prior
# everywhere it appears. Beat-weighted 4K/512 on the fast measured host.
PRIOR_SEC_PER_FRAME = 211.8
PRIOR_SPREAD = 0.45


# --- the measured table ----------------------------------------------------


def load_rates() -> dict:
    """machine_id -> {spec_hash: {"sec_per_frame":…, "n":…, "when":…}}

    Keyed on MACHINE, not on instance: an instance is one rental of a machine
    and the thing that renders at 151 s or at 225 s is the machine. Keyed
    within that on `spec_hash`, because a rate measured at 720p/64 says nothing
    about 4K/512 — the two-point fit that assumed otherwise produced four wrong
    master estimates in both directions.
    """
    if not RATES.exists():
        return {}
    try:
        return json.loads(RATES.read_text())
    except (OSError, ValueError):
        return {}


def record(machine: int, spec_hash: str, sec_per_frame: float, n: int,
           dph: float = 0.0, note: str = "") -> None:
    """Write down what a host ACTUALLY achieved. The input that keeps being wrong.

    docs/multi-gpu.md has been re-estimated five times and been wrong in both
    directions every time, always because the rate came from a neighbouring
    configuration. Neighbouring configurations on this project are wrong by
    factors, not percentages. So: measured rate, the spec it was measured at,
    how many frames it is an average of, and when.
    """
    data = load_rates()
    key = str(int(machine))
    data.setdefault(key, {})[spec_hash] = {
        "sec_per_frame": round(float(sec_per_frame), 2),
        "n": int(n), "dph": round(float(dph), 4),
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "note": note,
    }
    RATES.write_text(json.dumps(data, indent=2, sort_keys=True))


def rate_for(machine, spec_hash: str, rates: dict):
    """(sec_per_frame, basis) — basis is 'measured', 'other-spec' or 'prior'."""
    entry = (rates.get(str(machine)) or {})
    if spec_hash and spec_hash in entry:
        e = entry[spec_hash]
        return e["sec_per_frame"], f"measured n={e['n']}"
    if entry:
        # Deliberately NOT reused as a number. A rate at another spec is
        # evidence the machine exists and was benchmarked, and nothing more.
        return PRIOR_SEC_PER_FRAME, f"PRIOR (this machine measured at {len(entry)} other spec(s))"
    return PRIOR_SEC_PER_FRAME, "PRIOR — never measured"


# --- ranking ---------------------------------------------------------------


def rank(offers: list[dict], frames: int, spec_hash: str = "",
         allow_shared: bool = False, allow_wide: bool = False):
    """(kept, rejected). Sorted by projected $/frame, cheapest first."""
    rates = load_rates()
    kept, rejected = [], []
    for o in offers:
        frac = o.get("gpu_frac")
        ngpu = int(o.get("num_gpus") or 1)
        if not allow_shared and (frac is None or float(frac) < 0.99):
            rejected.append((o, f"gpu_frac={frac} — SHARED. R2-382: a co-tenant "
                                f"took 17,737 MiB and Cycles returned a perfect "
                                f"black PNG. Also 1.64x slower per frame."))
            continue
        if not allow_wide and ngpu > 1:
            rejected.append((o, f"num_gpus={ngpu} — a wide box delivers 1.27x for "
                                f"{ngpu}x the rent unless one worker per card is "
                                f"built, and worker/server.py refuses it."))
            continue
        dph = float(o.get("dph_total") or 0.0)
        sec, basis = rate_for(o.get("machine_id"), spec_hash, rates)
        per_gpu = dph / max(1, ngpu)
        o = dict(o)
        o["_sec_per_frame"] = sec
        o["_basis"] = basis
        o["_dph_per_gpu"] = per_gpu
        o["_usd_per_frame"] = per_gpu * sec / 3600.0
        o["_batch_usd"] = o["_usd_per_frame"] * frames
        kept.append(o)
    return sorted(kept, key=lambda o: o["_usd_per_frame"]), rejected


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=48)
    ap.add_argument("--spec-hash", default="",
                    help="rank using rates measured at THIS spec only")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--disk", type=int, default=80)
    ap.add_argument("--show-rejected", action="store_true")
    ap.add_argument("--allow-shared", action="store_true",
                    help="buy co-tenanted cards. Say why in the commit message.")
    ap.add_argument("--allow-wide", action="store_true",
                    help="buy multi-GPU boxes. See docs/multi-gpu.md first.")
    a = ap.parse_args()

    from vastctl import vastctl                       # noqa: PLC0415
    client = vastctl._client()
    # The RAW query, both passes' worth, so this module does its own rejecting
    # rather than inheriting search_offers' deliberate shared fallback.
    offers = client.search_offers(
        query=vastctl.build_query(disk_gb=a.disk, exclusive=False),
        type="on-demand", order="dph_total", limit=a.limit * 3)

    kept, rejected = rank(offers, a.frames, a.spec_hash,
                          a.allow_shared, a.allow_wide)
    print(f"{len(offers)} offer(s) matched the production filter; "
          f"{len(kept)} survived procurement, {len(rejected)} rejected\n")
    print(f"{'offer':<10}{'machine':<10}{'$/hr':<9}{'$/GPU-hr':<10}"
          f"{'s/frame':<9}{'$/frame':<10}{'batch':<9}basis")
    for o in kept[:a.limit]:
        print(f"{o.get('id'):<10}{o.get('machine_id'):<10}"
              f"{float(o.get('dph_total') or 0):<9.4f}{o['_dph_per_gpu']:<10.4f}"
              f"{o['_sec_per_frame']:<9.1f}{o['_usd_per_frame']:<10.5f}"
              f"${o['_batch_usd']:<8.2f}{o['_basis']}")

    if kept:
        by_price = sorted(kept, key=lambda o: o["_dph_per_gpu"])
        cheap, best = by_price[0], kept[0]
        if cheap["id"] != best["id"]:
            print(f"\n  the cheapest $/GPU-hr offer ({cheap['id']}, "
                  f"${cheap['_dph_per_gpu']:.4f}) is NOT the cheapest per frame "
                  f"({best['id']}, ${best['_usd_per_frame']:.5f} vs "
                  f"${cheap['_usd_per_frame']:.5f}) — the trap this module exists "
                  f"for.")
        n_meas = sum(1 for o in kept if o["_basis"].startswith("measured"))
        print(f"\n  {n_meas} of {len(kept)} candidate(s) have a MEASURED rate at "
              f"this spec. The rest are ranked on a prior of "
              f"{PRIOR_SEC_PER_FRAME:.0f} s/frame and are therefore ranked on "
              f"PRICE ALONE, which is the thing this module says does not work "
              f"(+-{PRIOR_SPREAD:.0%} host lottery vs 8.8 % price spread).")
        if n_meas < len(kept):
            print(f"  To turn a guess into a number: rent one, render one frame, "
                  f"`fleetctl record`. That is ~$0.02 and it is the input the "
                  f"master's cost estimate has been wrong about five times.")

    if rejected and a.show_rejected:
        print(f"\nREJECTED")
        for o, why in rejected[:40]:
            print(f"  {o.get('id'):<10} ${float(o.get('dph_total') or 0):<8.4f} "
                  f"{o.get('num_gpus')}x  {why}")
    elif rejected:
        shared = sum(1 for _, w in rejected if "SHARED" in w)
        wide = len(rejected) - shared
        print(f"\n  rejected {shared} shared and {wide} multi-GPU offer(s) "
              f"(--show-rejected to list them)")
    return 0 if kept else 1


if __name__ == "__main__":
    sys.exit(main())
