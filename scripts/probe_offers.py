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

QUERY = vastctl.build_query(disk_gb=45)


def main() -> int:
    v = VastAI(raw=True, quiet=True)
    offers = v.search_offers(query=QUERY, type="on-demand", order="dph_total", limit=12)
    if not offers:
        print("no offers matched")
        return 1

    print(f"{len(offers)} offers matching:\n  {QUERY}\n")
    cols = (
        f"{'id':<11}{'$/hr':<7}{'GPU RAM':<9}{'rel':<7}{'up Mb':<7}{'down Mb':<9}"
        f"{'dl $/GB':<9}{'ul $/GB':<9}{'disk $':<8}{'CPU'}"
    )
    print(cols)
    print("-" * (len(cols) + 12))
    for o in offers:
        print(
            f"{o.get('id', 0):<11}"
            f"{o.get('dph_total', 0):<7.3f}"
            f"{o.get('gpu_ram', 0) / 1024:<9.0f}"
            f"{o.get('reliability2', 0):<7.3f}"
            f"{o.get('inet_up', 0):<7.0f}"
            f"{o.get('inet_down', 0):<9.0f}"
            f"{str(o.get('inet_down_cost')):<9}"
            f"{str(o.get('inet_up_cost')):<9}"
            f"{str(o.get('storage_cost')):<8}"
            f"{str(o.get('cpu_name'))[:24]}"
        )

    cheapest = offers[0]
    print(
        f"\ncheapest: id={cheapest.get('id')} at ${cheapest.get('dph_total', 0):.3f}/hr"
        f" — 900 frames @ 4 min/frame = 60 GPU-hr = "
        f"${cheapest.get('dph_total', 0) * 60:.2f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
