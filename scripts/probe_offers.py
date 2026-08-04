#!/usr/bin/env python3
"""Read-only probe: what RTX 5090 capacity is actually on the market right now,
and what does each host charge for bandwidth and disk?

Bandwidth cost is host-set and asymmetric, so it is a selection criterion, not a
constant. Filtering it at rental time removes it as a question entirely.

The query comes from `vastctl.build_query` rather than being spelled out again
here. It used to be a second, looser copy — no `direct_port_count>=2`, no
`inet_up` floor — so this tool listed hosts the broker would never rent,
including proxy-only ones whose transfers run at 6.9 Mbps instead of 37. A probe
that does not answer "what can I actually rent?" is worse than no probe.
"""

import sys
from pathlib import Path

from vastai import VastAI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vastctl"))
import vastctl  # noqa: E402

DISK_GB = 45


def show(v: VastAI, label: str, query: str) -> list:
    offers = v.search_offers(query=query, type="on-demand", order="dph_total", limit=12)
    print(f"\n=== {label}: {len(offers)} offer(s) ===\n  {query}\n")
    if not offers:
        print("  (none)")
        return []
    # Rates in $/TB and $/GB-month, not the raw per-GB floats the API returns.
    # Those printed as `0.0026041666666666665` and ran straight into the next
    # column, which made the one table that exists to compare hosts unreadable —
    # and $/TB is how the ceiling itself is expressed (MAX_INET_COST_PER_TB).
    cols = (
        f"{'id':<11}{'$/hr':<7}{'frac':<7}{'VRAM':<6}{'rel':<7}{'up Mb':<7}{'down Mb':<9}"
        f"{'dl $/TB':<9}{'ul $/TB':<9}{'disk $/GB/mo':<14}{'CPU'}"
    )
    print(cols)
    print("-" * (len(cols) + 12))
    for o in offers:
        print(
            f"{o.get('id', 0):<11}"
            f"{o.get('dph_total', 0):<7.3f}"
            f"{str(o.get('gpu_frac')):<7.7}"
            f"{o.get('gpu_ram', 0) / 1024:<6.0f}"
            f"{o.get('reliability2', 0):<7.3f}"
            f"{o.get('inet_up', 0):<7.0f}"
            f"{o.get('inet_down', 0):<9.0f}"
            f"{(o.get('inet_down_cost') or 0.0) * 1000:<9.2f}"
            f"{(o.get('inet_up_cost') or 0.0) * 1000:<9.2f}"
            f"{(o.get('storage_cost') or 0.0):<14.3f}"
            f"{str(o.get('cpu_name'))[:24]}"
        )
    return offers


def main() -> int:
    v = VastAI(raw=True, quiet=True)
    # BOTH passes, because the broker now runs both: exclusive first, shared
    # only as a logged fallback. A probe that showed one of them would answer a
    # question the broker no longer asks — and the `frac` column is the whole
    # point, since a shared card and an exclusive one used to look identical
    # here and everywhere else. See vastctl.EXCLUSIVE_GPU_FRAC / R2-382.
    exclusive = show(v, "EXCLUSIVE — what the broker prefers",
                     vastctl.build_query(disk_gb=DISK_GB, exclusive=True))
    shared = show(v, "SHARED — the fallback, co-tenants possible",
                  vastctl.build_query(disk_gb=DISK_GB, exclusive=False))
    if not exclusive and not shared:
        print("\nno offers matched either query")
        return 1

    print(f"\nexclusive supply depth: {len(exclusive)} offer(s) / "
          f"{len({o.get('machine_id') for o in exclusive})} machine(s). "
          f"Thin supply is why exclusivity is a preference and not a hard "
          f"requirement — see vastctl.search_offers.")
    pool = exclusive or shared
    cheapest = pool[0]
    print(
        f"cheapest {'exclusive' if exclusive else 'SHARED'}: "
        f"id={cheapest.get('id')} at ${cheapest.get('dph_total', 0):.3f}/hr"
        f" — 900 frames @ 4 min/frame = 60 GPU-hr = "
        f"${cheapest.get('dph_total', 0) * 60:.2f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
