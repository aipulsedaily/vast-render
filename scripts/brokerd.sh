#!/usr/bin/env bash
# Keep exactly one broker running, detached from whoever started it.
#
#   scripts/brokerd.sh start [VASTRENDER_SCENE]   # detach and supervise
#   scripts/brokerd.sh run                        # supervise in the foreground
#   scripts/brokerd.sh status
#   scripts/brokerd.sh stop                       # stop supervising; SIGKILL the broker
#
# WHY THIS EXISTS
#
# Two multi-hour render batches were lost to a broker that "crashed". It did not
# crash. It was started as a *background task of the agent harness*, like this:
#
#     run_in_background: true
#     cd ~/vast-render && VASTRENDER_SCENE=... exec .venv/bin/python -m broker.app
#
# `exec` replaces the task's shell with the broker, so the broker **is** that
# task's process. Whatever reaps the task reaps the broker — with a signal, mid
# render, leaving no traceback (nothing ran), no teardown (nothing ran), and a
# rented GPU with nobody managing it. Nine brokers were started that way; every
# one of them ended without a shutdown line in broker.log.
#
# So this script's first job is not restarting. It is *ownership*: `setsid`
# puts the supervisor in its own session and process group, out of reach of a
# group-kill aimed at the shell that ran it, and the broker is a child of the
# supervisor rather than of anything the harness holds a pid for.
#
# WHAT IT WILL NOT DO
#
#   * It never touches the vast.ai API. It cannot adopt and it cannot destroy —
#     only the broker does either, under its own singleton lock. A supervisor
#     that adopted and then destroyed on exit is precisely the bug that made
#     that lock necessary in the first place.
#   * It respects the singleton lock rather than competing with it. Exit code 3
#     from the broker means "another broker already holds state/broker.lock";
#     restarting on that would be a loop that can only ever lose, so it stops.
#   * It runs at most one broker at a time — the restart happens strictly after
#     the previous child has been reaped.
#   * A clean exit (status 0) is a deliberate shutdown and is not restarted.
#
# It also does the one thing the broker itself provably cannot: report **how**
# the broker died. A supervisor sees the wait status, so "killed by signal 9"
# lands in broker.log — the single fact that was missing for two incidents.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
# Overridable so this can be exercised end to end against a scratch state
# directory and a spare port, without going near the live broker or its log.
LOG="${VASTRENDER_LOG:-$ROOT/state/broker.log}"
SUP_LOCK="${VASTRENDER_BROKERD_LOCK:-$ROOT/state/brokerd.lock}"
BPID_FILE="$SUP_LOCK.broker"
CHILD_PID=""

# Restart policy. Deliberately generous on attempts and short on backoff: the
# expensive failure here is staying down while a paid GPU idles, not restarting
# once too often. The broker's own flock makes a redundant start harmless.
BACKOFF_START=2
BACKOFF_MAX=60

say() { printf '%s %-7s %-9s %s\n' "$(date +%H:%M:%S)" "$1" brokerd "$2" >> "$LOG"; }

usage() { sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 2; }

# --- the supervision loop -------------------------------------------------

supervise() {
    local scene="${1:-${VASTRENDER_SCENE:-}}" backoff=$BACKOFF_START n=0 rc sig

    # Our own flock, so two supervisors cannot fight over one broker. Held on fd
    # 9 for the lifetime of this process and released by the kernel however it
    # dies — same reasoning as the broker's own lock, no stale state to clear.
    exec 9>"$SUP_LOCK"
    if ! flock -n 9; then
        echo "another brokerd already holds $SUP_LOCK — refusing to start" >&2
        exit 4
    fi
    echo $$ >&9

    # Stop supervising — and kill the broker on the way out, saying so.
    #
    # This used to log "leaving broker N alone", which is not what happens and
    # never could be. The broker runs with PR_SET_PDEATHSIG=SIGKILL (set because
    # supervise() exports VASTRENDER_SUPERVISED=1 — see
    # broker/diagnostics.py:parent_death_signal), so the kernel SIGKILLs it the
    # instant this process exits. It is not left alone; it is already dead, and
    # being SIGKILLed it cannot log a word about it. The result was a log that
    # reads, to whoever investigates next, as "the supervisor politely stepped
    # aside and then the broker vanished for no reason" — in the one script
    # whose stated job is to report HOW the broker died. On 2026-08-03 that cost
    # an incident escalation: a deliberate `brokerd.sh stop` at 08:05:16 was
    # reported up the chain as an unexplained crash in the 4.5 GB upload path.
    #
    # So do it here, explicitly, and name it. Same signal, same outcome, same
    # rented instance left up for the next broker to adopt — the only thing that
    # changes is that the death is now on the record.
    on_stop() {
        if [ -n "$CHILD_PID" ] && kill -0 "$CHILD_PID" 2>/dev/null; then
            say WARNING "supervisor asked to stop — SIGKILLing broker $CHILD_PID (PR_SET_PDEATHSIG would do this the moment this process exits, so it cannot be left running). -9 not -15: no shutdown path runs, so the rented instance stays up for the next broker to adopt."
            kill -9 "$CHILD_PID" 2>/dev/null
        else
            say WARNING "supervisor asked to stop — no broker was running"
        fi
        exit 0
    }
    trap on_stop TERM INT

    say INFO "supervisor up (pid $$, scene ${scene:-default})"

    while :; do
        n=$((n + 1))
        say INFO "starting broker (attempt $n)"

        # VASTRENDER_SUPERVISED makes the broker set PR_SET_PDEATHSIG, so it
        # cannot outlive this supervisor still holding the singleton lock.
        if [ -n "$scene" ]; then
            VASTRENDER_SUPERVISED=1 VASTRENDER_SCENE="$scene" \
                "$PY" -m broker.app >> "$LOG" 2>&1 &
        else
            VASTRENDER_SUPERVISED=1 "$PY" -m broker.app >> "$LOG" 2>&1 &
        fi
        CHILD_PID=$!
        echo "$CHILD_PID" > "$BPID_FILE"
        say INFO "broker pid $CHILD_PID"

        wait "$CHILD_PID"; rc=$?
        CHILD_PID=""; rm -f "$BPID_FILE"

        # THE LINE THAT WAS MISSING. A process cannot report its own SIGKILL;
        # only its parent can, and nothing was anyone's parent.
        if [ "$rc" -gt 128 ]; then
            sig=$((rc - 128))
            say ERROR "broker KILLED BY SIGNAL $sig ($(kill -l "$sig" 2>/dev/null || echo "?")) — it did not exit on its own, so no shutdown or teardown ran"
        elif [ "$rc" = 0 ]; then
            say INFO "broker exited cleanly (status 0)"
        else
            say ERROR "broker exited with status $rc"
        fi

        case "$rc" in
            0)  say INFO  "clean exit — not restarting"; break ;;
            3)  say ERROR "another broker holds the singleton lock — not restarting"; break ;;
            4)  say ERROR "startup refused (status 4) — not restarting"; break ;;
        esac

        say WARNING "restarting in ${backoff}s — queued jobs survive in SQLite and every 'running' row is reclaimed at startup"
        sleep "$backoff"
        backoff=$(( backoff * 2 )); [ "$backoff" -gt "$BACKOFF_MAX" ] && backoff=$BACKOFF_MAX
    done
    say INFO "supervisor down"
}

# --- commands -------------------------------------------------------------

sup_pid() { flock -n "$SUP_LOCK" true 2>/dev/null && return 1; cat "$SUP_LOCK" 2>/dev/null; }

# OUR broker, by pid file — never by pattern. `pgrep -f "python -m broker.app"`
# also matches a broker this supervisor does not own (a foreground one, another
# state directory, a spare port), and `stop` would then SIGKILL somebody else's
# live batch. The pid file is written while the child runs and removed when it
# is reaped, and is cross-checked against /proc so a stale one is inert.
our_broker() {
    local p; p="$(cat "$BPID_FILE" 2>/dev/null)" || return 0
    [ -n "$p" ] || return 0
    case "$(cat "/proc/$p/comm" 2>/dev/null)" in python*) echo "$p" ;; esac
}

other_brokers() {
    local mine found="" p; mine="$(our_broker)"
    for p in $(pgrep -f "python -m broker.app" 2>/dev/null); do
        [ "$p" = "$$" ] && continue
        [ "$p" = "$mine" ] && continue
        case "$(cat "/proc/$p/comm" 2>/dev/null)" in python*) found="$found $p" ;; esac
    done
    echo $found
}

case "${1:-}" in
run)
    shift; supervise "${1:-}" ;;

start)
    shift
    scene="${1:-${VASTRENDER_SCENE:-}}"
    if p="$(sup_pid)" && [ -n "$p" ]; then
        echo "brokerd already running (pid $p)"; exit 0
    fi
    # setsid: its own session and process group, so a group-kill aimed at the
    # shell that ran this — the harness reaping a background task, a closing
    # terminal — cannot reach it. This is the actual fix for the lost batches.
    setsid nohup "$ROOT/scripts/brokerd.sh" run "$scene" >> "$LOG" 2>&1 < /dev/null &
    disown 2>/dev/null || true
    sleep 3
    p="$(sup_pid)"
    echo "brokerd started${p:+ (pid $p)}, broker pid: $(our_broker)"
    echo "log: $LOG"
    ;;

status)
    p="$(sup_pid)"
    echo "supervisor : ${p:-not running}"
    b="$(our_broker)"
    echo "broker     : ${b:-not running}"
    for x in $b; do
        printf '  pid %-8s pgid %-8s sid %-8s up %s\n' "$x" \
            "$(ps -o pgid= -p "$x" 2>/dev/null | tr -d ' ')" \
            "$(ps -o sid= -p "$x" 2>/dev/null | tr -d ' ')" \
            "$(ps -o etime= -p "$x" 2>/dev/null | tr -d ' ')"
    done
    o="$(other_brokers)"
    [ -n "$o" ] && echo "NOT ours   :$o  (left alone by stop)"
    ;;

stop)
    # Note the broker BEFORE signalling the supervisor. Afterwards there is
    # nothing left to see: the supervisor's TERM handler kills it (and failing
    # that PR_SET_PDEATHSIG does), so `our_broker` comes back empty and a stop
    # that killed a live broker used to be indistinguishable, on stdout, from a
    # stop that found nothing running. Whoever runs this is entitled to know a
    # render was just cut short.
    was="$(our_broker)"
    # Stop supervising FIRST, or the supervisor faithfully restarts the broker
    # we are about to kill.
    p="$(sup_pid)"
    [ -n "$p" ] && { kill "$p" 2>/dev/null; sleep 1; }
    b="$(our_broker)"
    if [ -n "$b" ]; then
        # kill -9, never SIGTERM. SIGTERM runs the broker's shutdown path, and
        # with KEEP_ON_EXIT off that destroys the instance; -9 leaves the GPU
        # and the warm worker for the next broker to adopt.
        kill -9 $b 2>/dev/null
    fi
    [ -n "$was$b" ] && echo "broker killed (${b:-$was}) — any render in flight is\
 cut; queued jobs survive in SQLite; instance left running for the next broker"
    echo "brokerd stopped${p:+ (was pid $p)}"
    ;;

*) usage ;;
esac
