#!/usr/bin/env bash
# brokerN — start/stop/status ANY broker by index. The generalisation of
# scripts/broker2.sh, which hardcoded nine environment variables by hand.
#
#   scripts/brokerN.sh 3 start          # broker 3: state3, 8762, fleet03
#   scripts/brokerN.sh 3 status
#   scripts/brokerN.sh 3 stop
#   VASTRENDER_URL=http://127.0.0.1:8762 ./rq status
#
# For a whole fleet at once — and for status, cost and a VERIFIED teardown
# across all of them — use `.venv/bin/python fleetctl` instead. This exists for
# the single-broker case and so that the shell path and the Python path cannot
# disagree about what broker N is.
#
# WHY THIS DOES NOT SET ANYTHING ITSELF
#
# It asks `farm/brokers.py`. That module is the ONE declaration of what broker
# N is — its port, its tunnel ports, its state directory, its label — and it
# checks at import time that no two brokers share a port and that no label is a
# PREFIX of another. Both properties are load-bearing:
#
#   * `vastctl.our_instances` selects on `label.startswith(LABEL_PREFIX)` and
#     `Fleet.adopt_or_reap` DESTROYS every instance it returns bar the one it
#     adopts. `broker2.sh` explains this at length for one pair of labels; the
#     generalisation is that `fleet1` is a prefix of `fleet10`, so fleet labels
#     are fixed width and the check is mechanical rather than remembered.
#
#   * `app` calls `remote.reap_stale_tunnels(local_port)` at startup, which
#     pgreps for `-L <port>:127.0.0.1:8799` and SIGKILLs every match that is
#     not its own child. A duplicated tunnel port does not fail to bind — it
#     kills a sibling's forward mid-frame, and the sibling reads that as a
#     transport failure on a box it may then condemn as bad hardware.
#
# Nine hand-copied `export` lines cannot check either property. This can.
#
# BROKERS 1 AND 2 ARE REFUSED. They are live: broker 1 is the stills queue on
# `rq`'s default port and broker 2 carries the client's beat-1 proxy. They have
# their own scripts (`brokerd.sh`, `broker2.sh`) and their own operational
# history, and nothing generated should be able to restart either by accident.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

N="${1:-}"
shift || true
case "$N" in
    ''|*[!0-9]*) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2 ;;
    1|2) echo "refusing: broker $N is LIVE and has its own script." >&2
         echo "  broker 1 -> scripts/brokerd.sh   (stills queue, rq's default 8760)" >&2
         echo "  broker 2 -> scripts/broker2.sh   (bulk / client beat-1 proxy, 8761)" >&2
         exit 3 ;;
esac

# One source of truth, read rather than restated. If this fails, nothing starts.
ENVSH="$(cd "$ROOT" && python3 -c "
import sys, shlex
sys.path.insert(0, '$ROOT')
from farm.brokers import broker
b = broker(int('$N'))
for k, v in b.env(disk_gb=int('${VASTRENDER_DISK_GB:-80}'),
                  hibernate=int('${VASTRENDER_HIBERNATE:-14400}'),
                  max_batch_usd=float('${VASTRENDER_MAX_BATCH_USD:-20}'),
                  scene='${VASTRENDER_SCENE:-}' or None).items():
    print(f'export {k}={shlex.quote(v)}')
print(f'export VASTRENDER_IDENT={shlex.quote(b.ident)}')
")" || { echo "farm/brokers.py refused broker $N — nothing started" >&2; exit 4; }

eval "$ENVSH"
echo "$VASTRENDER_IDENT"

mkdir -p "$(dirname "$VASTRENDER_DB")" "$VASTRENDER_SEQ_DIR"
exec "$ROOT/scripts/brokerd.sh" "$@"
