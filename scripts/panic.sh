#!/usr/bin/env bash
# Destroy every rented GPU, right now.
#
# Deliberately does not talk to the broker: this must work when the broker is
# the thing that went wrong. It calls the vast.ai API directly and verifies each
# teardown, because an API success response is not proof the instance is gone.
#
#   scripts/panic.sh
#
# Safe to run when nothing is rented, and safe to run twice.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# Find the broker by PID, and only accept processes that are actually python.
#
# `pkill -f "broker.app"` matches any process whose *command line* merely
# mentions it: the `bash -c` an agent used to start the broker, a `tail` on
# state/broker.log, and — typed interactively — the shell you typed it into.
# Verified here: `pgrep -f "python -m broker.app"` returns both the broker
# (comm=python) and the shell wrapping the pgrep itself (comm=bash). Killing the
# second one is how this command has eaten the session that ran it.
broker_pids() {
    local found="" p
    for p in $(pgrep -f "python -m broker.app" 2>/dev/null); do
        [ "$p" = "$$" ] && continue
        case "$(cat "/proc/$p/comm" 2>/dev/null)" in
            python*) found="$found $p" ;;
        esac
    done
    echo $found
}

echo "==> stopping broker (if running)"
PIDS="$(broker_pids)"
if [ -n "$PIDS" ]; then
    # SIGTERM first: the broker's own shutdown destroys its instance and
    # verifies it, which is exactly what should happen before the reap below.
    kill $PIDS 2>/dev/null || true
    for _ in $(seq 1 10); do
        sleep 1
        PIDS="$(broker_pids)"
        [ -z "$PIDS" ] && break
    done
    if [ -n "$PIDS" ]; then
        kill -9 $PIDS 2>/dev/null || true
        echo "    broker killed ($PIDS did not exit on SIGTERM)"
    else
        echo "    broker stopped"
    fi
else
    echo "    not running"
fi

echo "==> destroying all broker instances"
"$PY" "$ROOT/vastctl/vastctl.py" reap

echo "==> remaining"
"$PY" "$ROOT/vastctl/vastctl.py" status

echo
echo "If anything is still listed above, destroy it by id:"
echo "    $PY $ROOT/vastctl/vastctl.py destroy <id>"
echo "or in the console: https://cloud.vast.ai/instances/"
