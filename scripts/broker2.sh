#!/usr/bin/env bash
# broker2 — a SECOND, fully independent broker driving a SECOND rented card.
#
#   scripts/broker2.sh start|stop|status        (thin wrapper over brokerd.sh)
#   VASTRENDER_URL=http://127.0.0.1:8761 ./rq status
#
# WHY A SECOND PROCESS AND NOT A SECOND WORKER INSIDE THE FIRST
#
# `Fleet` is one instance by construction — one `instance_id`, one `ep`, one
# tunnel, one resident `scene_hash` — and `docs/multi-gpu.md` sizes real
# multi-worker support at 1,100-1,500 lines across the two files carrying the
# most incident-derived invariants, with `progress.json` and the kill-by-pattern
# `WORKER_PIDS` named as the two things that fail silently by killing frames.
# None of that is needed to run a second CARD. Two processes, disjoint state,
# disjoint labels: every single-instance assumption stays true inside each one.
#
# WHAT MAKES IT SAFE, AND THE TWO THINGS THAT WOULD MAKE IT CATASTROPHIC
#
# 1. THE LABEL. `vastctl.our_instances` selects on `LABEL_PREFIX` with
#    `startswith`, and `Fleet.adopt_or_reap` DESTROYS every instance it returns
#    bar the one it adopts. So the prefixes must be DISJOINT, not merely
#    different: "renderbroker2" is matched by "renderbroker" and would be reaped
#    by broker 1 at its next restart, mid-frame. "ladderbroker" shares no
#    prefix with "renderbroker", so neither broker can see, adopt, or destroy
#    the other's card. This is why nothing about broker 1 has to change.
#
# 2. THE TUNNEL PORT. `app` calls `remote.reap_stale_tunnels(local_port)` at
#    startup, which pgreps for `-L <port>:127.0.0.1:<WORKER_PORT>` and SIGKILLs
#    every match that is not its own child. Starting a second broker on the
#    default 8798 does not fail to bind and back off — it KILLS BROKER 1'S
#    TUNNEL, mid-frame, and broker 1 reads that as a transport failure on a box
#    it may then condemn as bad hardware. 8796 here, 8795 for the exec tunnel.
#
# WHAT IS NOT SHARED, AND SO MUST BE ASKED FOR TWICE
#
#   * The queue. A job submitted to 8760 can never be served by this broker.
#     There is no load balancing; the split is by ROUTING, deliberately.
#   * `rq status`, `rq budget`, `rq teardown` are PER BROKER. `rq teardown` on
#     8760 destroys broker 1's card and reports success while this one keeps
#     billing. Tear both down.
#   * `MAX_BATCH_USD` is per broker. Set low here on purpose.
#   * vast.ai credit is account-wide, so `rq budget`'s `credit` line is the one
#     figure that already counts both.
#
# WHAT TO SEND HERE: BULK SEQUENCE WORK ON ONE SCENE. Measured 2026-08-04 on
# the live farm, that is the only shape that pays. A cold worker must be sent
# every scene it renders over a single unresumable stream, ~290-460 s for the
# ~5 GB film scenes. Amortised over a 21 h ladder pass on one scene that is
# 0.4 % overhead; spread over a rotation of short 60 s verification stills it
# is 3-9x the render it enables, and the second card would finish them slower
# than the queue it was bought to relieve. Stills stay on 8760.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Disjoint from "renderbroker" — see note 1 above. Do not make this a suffix.
export VASTRENDER_LABEL="ladderbroker"

export VASTRENDER_PORT=8761
export VASTRENDER_DB="$ROOT/state2/broker.db"
export VASTRENDER_LOCK="$ROOT/state2/broker.lock"
export VASTRENDER_OUT="$ROOT/out2"
export VASTRENDER_SEQ_DIR="$ROOT/out2/seq"

# See note 2 above. Both must differ from broker 1's 8798 / 8797.
export VASTRENDER_TUNNEL_LOCAL_PORT=8796
export VASTRENDER_EXEC_LOCAL_PORT=8795

# Bulk work is one big scene, and the default 30 GB rental leaves a ~23 GB
# cache — smaller than the live working set, which is why broker 1 spent
# 2,975 s (10.9 % of a 7.6 h instance life) re-pushing scenes it had already
# had. Disk is $0.20/GB/month, so 80 GB costs ~$0.022/hr: cheaper than ONE
# re-push of a 5 GB scene, every hour, forever.
export VASTRENDER_DISK_GB=80

# Its own cap. The two brokers cannot see each other's spend, so neither cap
# bounds the pair; this one is deliberately small.
export VASTRENDER_MAX_BATCH_USD=12

export VASTRENDER_LOG="$ROOT/state2/broker.log"
export VASTRENDER_BROKERD_LOCK="$ROOT/state2/brokerd.lock"

mkdir -p "$ROOT/state2" "$ROOT/out2/seq"
exec "$ROOT/scripts/brokerd.sh" "$@"
