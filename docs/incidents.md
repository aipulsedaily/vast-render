# Incident notes

Diagnoses worth keeping, because each one cost hours and none of them was the
thing it first looked like. Newest first.

> Rented-host identifiers (instance, machine and offer ids, addresses, ports)
> are redacted. Where two events have to be linked, a stable alias is used
> instead — `machine A`, `offer P`. **The aliases are local to this document**:
> `host A` in [operations.md](operations.md) is a different host.

---

## 2026-08-18 — the redactor guarded one of six credential paths

**Found during the pre-publication audit, not by a failure.** Nothing leaked
this time. The point of the entry is that the previous entry about this — the
2026-07-28 fix — recorded the problem as solved, and it was solved at one call
site out of six.

### What was believed

`broker/remote.py` defines `redact()`, and `diagnose()` funnels logged
exceptions through it. The README said so. `test_broker.py` had a thorough test
for it, written against the exact string that leaked. All of that was true.

The belief that did not survive contact was **"the broker logs through
`diagnose()`, therefore the key cannot reach a log."**

### What the evidence actually said

The vast.ai SDK puts the API key in the **query string** of every request, so any
`HTTPError` it raises carries a live billing credential in `str(exc)`. So the
question is not "is `diagnose()` correct" — it is "how many ways can an SDK
exception become text?" There are six, and `diagnose()` was one:

```
broker/remote.py    diagnose()                     REDACTED
broker/diagnostics.py  _excepthook / _threadhook /
                       _unraisablehook /
                       loop_exception_handler      NOT REDACTED
broker/db.py        jobs.err, frames.err           NOT REDACTED at the column
broker/seq.py       write_manifest()               NOT REDACTED
fleetctl            five raw `print(f"...{exc}")`  NOT REDACTED
vastctl/vastctl.py  top-level `except VastError`   NOT REDACTED
```

Demonstrated rather than asserted — `traceback.format_exception` ends with
`str(exc)`, so:

```python
>>> tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
>>> FAKE in tb
True                     # ...?api_key=aaaa… straight into broker.log
>>> FAKE in remote.diagnose(e)
False                    # the guarded path, next door, working perfectly
```

The three worst of these are not the obvious one:

- **`fleetctl`** is the CLI a human runs interactively and pastes the output of.
  `api_credit()` and `api_instances()` both call the SDK; both were printed raw.
- **`vastctl`** is the module *closest* to the key and had no redaction at all,
  including in the handler that prints `create failed: {resp}`.
- **`seq.write_manifest`** is the artefact that is **built to be handed to
  somebody** along with the frames. That is precisely where the key went in
  2026-07-28, via `jobs.err`, and the column was never closed — only the one
  caller that wrote to it. `db.seq_summary` still selects `err`, and `extra` is
  a caller-supplied dict merged in whole with no schema.

### Root cause

`redact()` lived in the module that needed it first. Everything else either
could not import it (`vastctl` is loaded two different ways, `diagnostics` is
deliberately dependency-free and also runs on the worker) or nobody checked.
A fix placed at a call site protects that call site.

### Fixes

- `redaction.py` at the repository root: **no project imports**, so every layer
  can reach it. `broker/remote.py` re-exports `redact` and `_SECRET_RE` from it
  so existing callers and tests are untouched.
- All six paths above routed through it, including `db._safe_err()`, which
  redacts on the way **into** the database — so the credential is not in the
  store to be copied out of, whatever a caller did. The `err[:2000]` truncation
  moved in there with it: truncating *then* redacting can leave a half-key that
  the patterns no longer match.
- The coverage widened past `api_key=` to `Authorization: Bearer`, `X-Api-Key:`,
  `"api_key": "…"` in JSON, and a renamed query parameter carrying a key-shaped
  value on a `console.vast.ai` URL. `docs/operations.md` tells a human to type
  the bearer form by hand with curl, so that shape was reachable by design.

### The bound that was kept, deliberately

A bare 64-hex token with no key-ish context is **still not redacted**, and that
is a choice. A sha256 is the same shape, and `frames.sha256`, the frame
integrity check and `write_manifest` are all built on full digests.
Blanket-redacting 64 hex characters would corrupt the manifests and silently
break every "does this frame match" diagnostic — a security control turning
itself into a data-integrity bug. Asserted as a test so the trade stays visible.

### The lesson worth keeping

The old test ended with an assertion that the `Authorization: Bearer` form went
through **unredacted**, labelled `KNOWN BOUND` — an honest, deliberately
documented limitation, and the right way to write it down. It still sat there
for three weeks while `docs/operations.md`, four files away, instructed a human
to construct exactly that string against the live account.

Writing a limitation down is not the same as bounding it. A documented gap needs
a second question: *who else is already standing in it?*

---

## #169 — a per-broker blacklist with a 6 h TTL burned a job's last retry attempt

**2026-08-12. This is the instance that moves #169 from "worth doing" to "do it
before the next multi-day render."** The two earlier instances cost price rungs.
This one cost a job.

### What happened

```
18:22:57  fleet05  machine A blacklisted for this session (ssh key injection failed)
18:34:32  fleet04  renting offer P (machine A)                     <- 12 minutes later
18:39:10  fleet04  machine A blacklisted for this session (ssh key injection failed)
18:39:22  broker   job 467247848cc6 FAILED: sequence master4k stopped at frame 1766
                   after 0 frame(s) this pass: FleetUnavailable (0/3 rounds)
```

fleet04 rented a machine its sibling had condemned **twelve minutes earlier**,
spent ~5 minutes discovering the same `authorized_keys` failure, and ended the
pass having rendered **zero** frames. Zero-progress passes spend a retry
attempt; it was its third. **The job failed and the broker went idle with ~101
frames of a 2,978-frame master unrendered.**

### The two independent causes, each sufficient on its own

**1. The blacklist is per-process.** `Fleet.bad_offers` / `bad_machines` live in
one broker; nothing publishes them. Across a 7-cycle render, 4 of 10
condemnations were one broker rediscovering another's verdict. The 12-minute gap
above is far inside any TTL — **scoping alone caused it.**

**2. `BLACKLIST_TTL_SEC = 6 * 3600` is shorter than the defect it records.**
Measured lifetimes of `authorized_keys` failures on the same machine:

| machine | first condemned | condemned again | interval |
| --- | --- | --- | ---: |
| B | 2026-08-09 16:37 | 2026-08-12 05:56 | **61 h 19 min** |
| C | 2026-08-10 05:00 | 2026-08-11 05:02 | **24 h 02 min** |

**No condemned host in this render ever recovered.** The TTL's rationale — *"a
machine having a bad hour is not written off for the week"* — describes a
failure mode never once observed here. The retirement period is 12 h, so **every
verdict expires before the cycle that could use it.**

### A third gap, cheaper but real

A create that returns **HTTP 400** never produces an instance, so the
"destroyed as unusable" path that blacklists an offer is never reached. Offer
Q 400'd for **all three brokers** across two days and stayed top of the
cheapest-first list each time, costing a price rung per re-rent.
**The blacklist only learns from failures that got far enough to be destroyed.**

> **2026-08-14: this third gap is a NULL as written, and the evidence is in the
> state files.** `_rent` already condemns the offer on a create failure —
> `except Exception: self.bad_offers.add(offer_id)`, right around the
> `vastctl.create` call — and it always has. `state3/bad_hosts.json` contains
> exactly one entry, `{"offers": {"Q": ...}}`, written by that path at
> 17:17:35 on 2026-08-11. So each broker *did* record the 400 and did not buy
> that offer again. What produced the three-brokers-two-days pattern was cause
> **2**: fleet03 condemned it at 17:17:35, fleet05 bought it at 17:29:06 and
> fleet04 at 17:48:18, each rediscovering a verdict that was on disk in a
> directory it never reads. **The 400 was recorded. Nobody could see it.**

### What would have prevented it

A fleet-wide blacklist, persisted where all brokers can read it, with a TTL on
the order of the observed defect lifetime (days, not hours), and an entry
written on **any** terminal rental failure including a 400 on create.

On this render that would have removed **4 repeat condemnations**, several price
rungs, and — the reason this is filed as an incident rather than a nit — **the
one job failure in an otherwise fully self-healing 82-hour render.**

### Not fixed here

Deliberately. There is no CLI to seed a blacklist (`rq` and `fleetctl` have no
such subcommand; the state lives in the broker's SQLite `meta.bad_hosts`), and
the only routes in are writing to a running broker's database or restarting it —
neither acceptable under a live render. Recorded for whoever picks this up.

### Fixed 2026-08-14, once the fleet was destroyed and the master delivered

Three changes, and each one had to be watched failing first.

**1. The TTL is seven days, and the constant that was raised is not the one
this was filed against.** `Broker.BLACKLIST_TTL_SEC = 6 * 3600` in `app.py` was
real, but it governed a *second* copy of the list in the broker's SQLite
`meta.bad_hosts`, and `load_blacklist` only ever ADDED ids from it. It could not
shorten anything. **The TTL that actually decided re-renting was
`fleet.BAD_HOST_TTL_SEC`, 24 h**, applied to `state<n>/bad_hosts.json` — which
is why the two re-condemnations in the table above are 24 h 06 m and 61 h 19 m
apart rather than 6 h apart. It is now `7 * 24 * 3600`: longer than the longest
defect ever measured here (2.7x), fourteen times the 12 h retirement period that
re-asks the question, and still finite, so a repaired host returns on its own.

That second store is **deleted**, not raised. Two stores of one fact drift, and
this pair had already started to: `load_blacklist` merged with
`bad_offers |= set(offers)`, and `set.__ior__` is implemented in C and does not
call an overridden `add` — so restored ids entered the set with no timestamp,
were never persisted by the container that owned them, and got re-stamped with
`now` by the next unrelated save, **silently restarting the ban clock of a host
condemned hours earlier**. `CondemnedIds.update` now routes through `add`.

**2. The store is fleet-wide: `farm/bad_hosts.json`, one file, cross-process
locked.** `farm/` because it is the only directory in this tree that belongs to
the fleet rather than to a broker. Three properties, and the third is the one
that is easy to miss: every write merges under an `flock` before it writes, so
one broker's verdict never erases another's; every write publishes immediately;
and **`refresh()` re-reads before the list is used**. A shared file nobody
re-reads is still a private file — fleet04 had been up for hours when fleet05
condemned machine A, so a load that happens only at construction misses the
verdict by exactly as much as a separate file did. `_rent` refreshes.

Two consequences fell out of sharing:

* `_rent`'s market-dry path used to call `bad_offers.clear()`, which wrote an
  empty list. Shared, that is one broker with a momentarily thin market
  **deleting evidence every other broker paid for**. It now ignores the list for
  that one attempt, which is all it ever meant to do; `CondemnedIds.clear()` is
  memory-only so the wipe cannot be reintroduced by accident.
* the file is the seeding route this write-up said did not exist. Add
  `"<machine id>": <unix time>` under `machines` with a text editor and every
  broker skips it at its next rent.

**3. The create-failure gap was already closed.** See the note above; it is a
null, and there is now a test so it stays closed.

#### Watched failing first

`broker/test_broker.py::test_a_condemnation_outlives_the_retirement_and_reaches_every_broker`,
13 checks, run against a copy of the pre-fix tree and then against the fix:
**7/13 before, 13/13 after.** What the six failures were:

| check | pre-fix |
| --- | --- |
| a host still broken 61 h later is still condemned | machine B gone from the list |
| the TTL exceeds the longest measured defect | 24 h vs 61 h 19 m |
| the TTL exceeds the retirement period that re-asks | 24 h vs 4 x 12 h |
| the re-rent skips the machine a sibling just condemned | bought offer P |
| two brokers condemning at once keep both verdicts | one verdict lost to the read-modify-write race |
| the fleet's verdicts survive an empty market | file emptied by one broker's `clear()` |

And separately, because a single-process test cannot show it, the incident's own
shape was replayed with **two real processes** — a sibling broker started,
left running, and sent shopping only after the other one condemned machine
A and took a 400 on offer Q. Pre-fix it bought both
(`attempted=[P, Q, R]`); post-fix it bought neither
(`attempted=[R]`). 1/5 → 5/5.

#### One more defect, found by writing that proof

Sharing the file turns `_save`'s read-modify-write into a race, so the lock went
in on principle. Two processes condemning 60 hosts each, on the pre-fix code:

    run 1  109/120 survived, 11 lost      run 2  120/120, 0 lost      run 3  117/120, 3 lost

**Run 2 is the point.** The race is timing-dependent, so a fifty-fifty test
passes half the time and the guard would have looked fine. Post-fix: 120/120,
three runs out of three.

The lost verdicts had *two* causes, and only one was the obvious one. The
temp file was `bad_hosts.json.tmp` for every writer, so two brokers staged
their writes through the **same scratch path** — one `replace()` moves the
other's file out from under it and the loser's write dies with
`FileNotFoundError`, logged as a warning and otherwise invisible. It is now
`bad_hosts.json.<pid>.tmp`, which makes that impossible even on the degraded
path where the lock could not be taken.

The eight surviving `state<n>/bad_hosts.json` files were merged into
`farm/bad_hosts.json` (newest stamp per id, 7 d TTL applied): 13 offers and 8
machines carried forward, including machines A and B and offer Q.

---

## Measured over the master render — about a quarter of rentals draw a host that never installs our SSH key

**Provenance, stated first, because it is weaker than every other measurement on
this page.** This number was never written to a file. It
came from watching the fleet during the four-day master render and counting
condemnations against rentals as they happened; the state files that would let
anyone re-derive it have since been merged and TTL'd. It is recorded because a
finding that lives only in a conversation is a finding that is already lost —
which is the failure this page spends most of its length documenting.

**The rate, with the sample beside it:**

| sampled at | hosts condemned | rentals | rate |
| --- | ---: | ---: | ---: |
| mid-campaign | 7 | ~28 | ~25 % |
| end of campaign | 9 | ~34 | ~26 % |

Early sampling put it nearer **21 %** and it converged upward as the sample
grew, so **21–26 % is the honest range and ~25 % the working number.** What
matters is that it held steady rather than drifting as the denominator doubled.

Do not difference these against #169's *"4 of 10 condemnations"* above. That
counts condemnation **events** — repeats included, which is the entire point it
is making — over one 7-cycle render inside this campaign, where the table counts
hosts over the whole of it. Two windows, two units. Reconciling them properly
would need both re-derived, and the state files that would allow it are gone.

**Read the sample size as part of the figure.** This is ~30 rentals, on one
account, over four days, against the production offer filter of the time. It is
a useful planning number — it says a fleet of eight should expect to throw away
roughly two rentals on the way up — and it is **not a property of the
marketplace**. Anyone quoting it elsewhere should re-measure it.

**One failure mode, not several.** Every condemnation in the campaign was the
same defect: the rented host never wrote our key to `authorized_keys`. It is
established during the SSH handshake, before any data transfer, so it is cheap
to discover and it is discovered the same way every time — the `auth_rejected`
exit-255 described further down this page, never a timeout and never a slow
link.

**No condemned host ever recovered.** Two were re-drawn from the market later in
the campaign and condemned again on sight, at **24 h** and **61 h 19 m** — the
durability table under #169 above. That is the whole justification for a TTL
measured in days.

**The cost of one bad host is small and does not accumulate:** ~5 minutes and
about **$0.05** of billed rental to discover it, plus a price rung when `_rent`
falls through to the next-cheapest offer. The rung is **transient, not
cumulative** — once the session blacklist has absorbed the bad hosts the fleet
returns to the cheap tier. So at ~25 % the base rate is an argument for
remembering condemnations across brokers and across days, which is #169, and
**not** an argument for renting more expensively on purpose.

---

## A recurring shape: a comment describing a path the code's own ordering makes unreachable

**Not an exec bug. A class.** Logged separately because the exec instance is the
third time this project has paid for it, and the first two looked like entirely
different problems:

* a `ranked` safeguard that was **documented and never implemented**;
* a guard that was implemented but sat **inside `if not a.no_rig:`**, so it ran
  only when the thing it guarded against was already switched off;
* and now `ExecService.ensure_ready` (2026-08-04), below.

### The shape

Someone writes a comment stating what a call does *in the case they care about*.
The statement is true of the function's body. It is false of the function's
**entry**, because a guard, a branch or an early return sits in front of it. The
comment then documents an intent the ordering has already made unreachable, and
because it reads as a design note rather than a claim, nobody re-derives it.

Every instance so far has been **invisible in the passing case**. That is what
makes it expensive: the code works whenever the guard happens not to fire, so it
survives review, survives testing, and fails only under the exact conditions it
was written to handle.

### The exec instance, in full

```python
# The scene the render worker already holds, so this is the no-op
# fast path of ensure_ready rather than a scene switch. An exec job
# must never restart the render worker.
scene = self.fleet.scene_path or config.SCENE
ep = self.fleet.ensure_ready(Path(scene))
```

Both sentences are true. The conclusion does not follow, because
`Fleet.ensure_ready` opens like this:

```python
def ensure_ready(self, scene: Path) -> Endpoint:
    with self.lock:
        self._refuse_if_rendering()          # <-- FIRST statement
        if self.ep and self.last_ready and self._worker_alive():
            if self.scene_hash == remote.scene_hash(scene):
                return self.ep               # <-- the "no-op fast path"
```

The fast path is real and the caller reached it constantly — **whenever no frame
was rendering**. The one condition under which an exec job needed it was the one
condition under which `_refuse_if_rendering` fired first. So exec was refused for
asking a question it already knew the answer to, and the comment asserting
otherwise was three lines above the call.

### What actually catches it

Not review: three reviewers read that comment and agreed with it, because it *is*
a correct description of the intent. Two things do:

1. **Read the callee's first statements, not its docstring**, whenever a comment
   claims a call is cheap, a no-op, or takes a fast path. "Fast path" is a claim
   about ordering, and ordering is only visible at the top of the callee.
2. **Ask which branch runs in the case the comment is about.** Every instance
   here was found by asking "when this actually matters, is this line reached?"
   — the exec one by noticing the failures happened *only* while rendering,
   which is precisely when the fast path was claimed to apply.

The general lesson has a sibling already in this file, from the rent-time guards:
**a guarantee written at one point in a flow says nothing about a different entry
into that flow.** `adopt_or_reap` bypassed `gpu_frac`; `_refuse_if_rendering`
preceded the fast path. Same failure, opposite directions.

## 2026-08-04 — OPEN DEFECTS: a sequence never yields, and `rq exec` refuses instead of queueing

Both are **open**. Both were worked around on 2026-08-04 by adding a second
broker and routing bulk work to it, and a workaround is why they need writing
down: with pinning in place the symptom mostly disappears, and the next person
to see it will otherwise conclude the queue was merely deep.

### `run_sequence` does not yield between frames

**This is the root cause of the 6:1 wait-to-work ratio, not queue depth.**

Measured over one 7.6 h instance life: the queue held **11 jobs, roughly 660 s
of render work**, while the longest-waiting scene had been waiting **4,086 s**.
Six times as much waiting as there was work to do. That is not a capacity
shortage and adding capacity does not fix it.

The cause is that a sequence job runs its whole frame loop inside one
`dispatch_once`. Two `r1full` jobs took **7,461 s and 2,739 s — 10,200 s, 37 %
of the instance's life** — and for all of it seven other agents' ~60 s
verification renders could not run, because there is no yield point between
frames at which the dispatcher can reconsider. Every frame is an independent
74 s unit of work; nothing about the job requires them to be contiguous.

Note the interaction with the scene-switch veto: `cheaper_to_finish` was fixed
in `1fe0de4` to price a sequence at its real remaining work, which correctly
stops the veto from rubber-stamping a 21-hour job as "nearly done". But that
governs which scene is chosen NEXT — it cannot preempt a sequence already
running, because there is no point at which control returns.

**Mitigated, not fixed**, by pinning bulk sequence work to its own broker
(`docs/multi-gpu.md`). A second card relieves the blocking; it does not remove
it, and a single broker running a long sequence still starves everything
behind it.

### `rq exec` fails `WorkerBusy` instead of queueing behind a render

`exec` work is refused outright while the worker is rendering, rather than
waiting for it. From the live log, three escalating attempts inside four
seconds and then a hard failure:

```
10:26:34 ERROR exec  exec job 69d028eb4b65 queued: WorkerBusy: refusing to restart the worker ...
10:26:36 ERROR exec  exec job 69d028eb4b65 queued: WorkerBusy: ... at sample 80/400, 1.1 min in
10:26:37 ERROR exec  exec job 69d028eb4b65 failed: WorkerBusy: ... at sample 192/400, 1.1 min in
```

The refusal itself is correct and must stay — it is the guard that stops a
worker restart discarding a frame in flight, and it was written after a
40-minute 8K render was thrown away. The defect is the **verdict**: a job that
cannot run *yet* is failed as though it could never run, when the same
condition would clear on its own within a minute.

**Two workers do NOT fix this.** The brokers do not share a queue, so a broker
whose own worker is busy still refuses; it has no idea the other card is idle.
Routing exec to the bulk broker (`VASTRENDER_URL=http://127.0.0.1:8761 rq exec`)
is a workaround that happens to land on an idle box, not a fix.

**FIXED 2026-08-04**, after it killed four jobs in twenty minutes, every one at
`attempts 3/3`. Two things were wrong and only one of them was the scheduler.

*The verdict was wrong.* `WorkerBusy` fell through to `db.fail(..., MAX_ATTEMPTS)`
and the three retries fired within four seconds, so **the whole retry budget
expired inside a single 39 s frame**. It now requeues without spending an
attempt, after waiting `EXEC_BUSY_BACKOFF_SEC` (90 s, more than two frames) **in
the worker thread, holding its exec slot**. Holding the slot is the point: a
bare requeue is re-claimed in milliseconds, and the first version of this fix
shipped without the wait and produced ~10 requeues per second.

*Chunking does not help here, and that is worth stating because it looks like it
should.* The ladder's <=62-frame chunks are ~40 minutes apart; the retry budget
expired in under two. **Chunking bounds how long a job waits for the SCHEDULER;
this bounds how long it waits for the DEPLOY GUARD.** Two mechanisms, and only
one was covered.

*The question was wrong too.* `ExecService.ensure_ready` asked
`Fleet.ensure_ready` for an endpoint, calling it "the no-op fast path". It is
not: `Fleet.ensure_ready` runs `_refuse_if_rendering()` as its FIRST statement,
before the fast path, so the no-op could never be reached while a frame was in
flight. `endpoint_without_disturbing_the_worker` now returns `fleet.ep`
directly whenever the box is up. Exec needs an instance; it does not need the
render worker idle, a scene loaded, or `Fleet.lock`. This cannot disturb a
render structurally, not by intention: `stop_exec_server` kills by
`{root}/exec_server.py` and `WORKER_PIDS` by `{root}/server.py`, and neither
string contains the other, so `pgrep -f` cannot cross them.

Do NOT gate that fast path on `fleet.last_ready`. It means "the RENDER WORKER
came up", which exec does not ask; gating on it made every queued exec job spin
after a restart that ADOPTS a running instance, because `ep` is set immediately
and `last_ready` stays False until the first render dispatch finishes.

*Verified live, bracketed by two consecutive frames of a running pass:*

```
18:50:34  sequence r2beat1 frame 740 done
18:51:44  exec job 0ba1bd361c0e DONE - 0.7 s on the box, attempts=1, no WorkerBusy
18:51:58  sequence r2beat1 frame 741 done
```

### A retry budget is the wrong instrument for "nobody's fault"

Same pass, same principle. `the input bundle changed between submit and
dispatch` was raised as a bare `ValueError` and went through
`db.fail(..., MAX_ATTEMPTS)`. Retrying cannot help - the digest is recomputed
from the same moving tree every time - and it was typically reached with
attempts already spent on something unrelated (an instance replacement burned
two on 2026-08-04), so it reported `3/3` for a job that had never run once.
It is now `StaleBundle`, terminal on sight, reported as a verdict with what to
do about it. The refusal itself is unchanged and correct.

**A live consequence, for whoever owns `r2651-occ-full`.** Its bundle is 96
files / 38.3 MB drawn from `world/*.py`, `world/items/*` and friends - files
eight agents are actively editing. Every second between submit and dispatch is
a chance for the digest to move, and the 90 s busy-backoff *widens that window*.
The fix stops `WorkerBusy` killing the job; it cannot stop the tree moving under
it. **Narrow `--include` to the files the script actually reads.**

## 2026-08-03 — the stray inode that destroyed a healthy instance, and the fix that ate the evidence

`mkdir -p /workspace/scenes/139698d62abee3bf` (relief_2light_A2.blend) failed
three times in nine seconds with `File exists`. The deploy retry gave up,
`_deploy` classified it a **host-level failure**, and the instance was
destroyed: reachable, idle, 7 h uptime, 28 scenes and **5.46 GB of warm
cache**. The replacement cost a 900 s rental wait, a 481 MB Blender push, a
148 s deploy, an empty cache, and ~17 minutes of queue starvation.

`mkdir -p` succeeds on a directory that already exists. It fails only when the
path exists as something that is **not** one. So an EEXIST here is a corrupted
entry in *the broker's own content-addressed cache* — it says nothing whatever
about the host, and destroying the host cannot fix it. `rm -f` fixes it.

### The first fix closed the outage and opened a worse hole

    test -d $dir || rm -f $dir; mkdir -p $dir

Correct, and **silent**. It removed the offending thing without ever looking at
it, so what wrote a non-directory into a content-addressed cache path remained
unknown — and every future recurrence would have destroyed its own evidence the
same way. A stray inode stops existing the moment it is healed, so "capture it
next time" is not something an operator can be asked to do afterwards.

The heal now **describes before it removes** (`ls -ld`, `readlink -f`, the
first bytes through `od`) and logs that at WARNING, in the same round trip, on
a path already known to be wrong.

### And the guard missed the one shape that best explains it

`[ -e ]` **follows symlinks**, so it is false on a dangling one — while
`mkdir -p` still refuses that path with EEXIST. A dangling symlink was
therefore simultaneously invisible to the check and fatal to the deploy: the
exact failure signature, and the heal skipped it. Found by writing the first
test this code ever had, run against a real filesystem with a real shell; it
failed on the symlink case immediately. The guard is now
`{ [ -e ] || [ -L ]; } && [ ! -d ]`.

Root cause of the original inode is **still unproven** — but a dangling symlink
is now both a candidate and no longer able to cause the outage, and if anything
else does it, the log will say what it was.

---

## 2026-08-03 — 32 minutes of `starting-worker`, and every guard against it was reading the wrong engine

`./rq status` showed the farm stopped dead:

    queue  {'canceled': 101, 'done': 1477, 'failed': 50, 'queued': 32}  depth=32
    gpu    starting-worker  instance=<id>  up=2368.2s  idle=1574.7s

Nothing `running`, `done` frozen at 1477, 32 jobs from four agents behind it.

Everything that usually explains this was innocent. The instance was up and
`ssh` answered in under a second. The worker process was alive
(`pgrep -af /workspace/server.py` → pid 57692). The GPU was at **100% util,
274 W, 16.6 GB VRAM** — not idle, not wedged, not booting. The broker was not
failing to dispatch; it was correctly blocked inside `_switch_scene`, which
holds the whole farm by design, waiting on `film9.blend` (4.71 GB).

It was **slow, not stalled**, and it was slow doing something worth nothing:
a throwaway 64x64 warm-up render that ran **1,440 seconds**. The job it was
warming up for then rendered in **66.1 s**.

### The tell was one null in progress.json

    {"state": "idle", "pct": null, "phase": "Rendering 50 / 64 samples"}

`pct: null`, and no `sample` key — so `_SAMPLE_RE` (`/Sample N \/ M/`) had not
matched the very line that obviously contains a sample count. That regex is
Cycles' format. **"Rendering N / M samples" is EEVEE's**, and the only engine
id in `film9.blend` is `BLENDER_EEVEE`.

`prewarm()` clamps a render it believes is Cycles:

| guard | what it sets | on an EEVEE scene |
|---|---|---|
| sample clamp | `scene.cycles.samples = 1` | inert — EEVEE reads `scene.eevee.taa_render_samples`, **default 64** |
| step ceiling | `scene.cycles.time_limit = 45` | inert — EEVEE has no `time_limit` |

Both no-op'd silently, because `scene.cycles` exists on every scene regardless
of engine, so neither assignment raises. The sweep rendered the scene's own 64
TAA samples of a 4.7 GB, 28k-object scene, and **no guard in the function could
stop it** — including the one whose comment called it "the backstop for
whatever the next scene invents".

`apply_spec` has always known this. Its `else:` branch sets
`taa_render_samples` "because EEVEE ignores cycles.samples entirely". Prewarm
never learned it. Two code paths, one fact, one of them wrong.

### Why this was the second stall of the day, not the first

The morning's fix for the same symptom blamed the compositor — a Render Layers
node dragging in another scene at production settings — and added the
`time_limit` backstop. That diagnosis was plausible (film9 does ship with
compositing and sequencer on, and both are now correctly forced off) but it was
not the cause, and the "backstop" it added could not reach the real one. The
symptom returned within the hour **with the fix deployed**.

### Two changes, because one of them is the general case

* `prewarm()` now clamps `scene.eevee.taa_render_samples = 1` and restores it,
  and the log line names `engine=` alongside *both* sample counts. The old line
  said `samples=1` while the engine rendered 64 and nothing on it said which
  number was being read.
* `PREWARM_MAX_SCENE_BYTES` (default **1.0 GB**) skips the sweep outright on a
  big .blend. This is the part that does not depend on guessing the engine.
  Every in-render limit is engine-specific and therefore defeatable by the next
  engine, and a render already in progress cannot be interrupted from Python at
  all — so the only bound that always holds is the one applied *before* the
  render starts. Above the threshold the sweep's cost is scene synchronisation
  (geometry upload, shader compilation, shadow/light-grid build), which is
  proportional to scene size and which no sample cap touches, because it
  happens before sample 1.

The threshold catches exactly the two scenes that have ever stalled an instance
— `verify_world.blend` (4.17 GB) and `film9.blend` (4.71 GB) — and nothing
under 200 MB has ever come close. What is given up is paid back at once: the
first real job pays the sync instead, under a job timeout, reporting progress,
and delivers a frame for it. The warm-up produced a 64x64 PNG that the
`finally` deletes, and it held every agent's queue while it did.

### The part that was pure loss

Agent `ramp` cancelled its two `film9` jobs at 10:08 and 10:19, halfway through
the stall. The broker had no way to notice: the dispatcher was blocked inside
the scene switch. It finished the 32-minute switch, rendered `eae312b873dd`
anyway at 10:28:40, logged it `done`, and wrote the result to a row whose state
was already `canceled`. That is also why `done` sat at 1477 through a completed
render — it was not a `recent()` windowing artefact this time.

### What to check first, next time `starting-worker` looks stuck

`starting-worker` plus a busy GPU is a **slow** switch, not a dead one. Read
`/workspace/progress.json` before anything else: `state`, and whether `pct` is
null. A null `pct` next to a phase string that plainly contains numbers means
the parser and the engine disagree, and the engine is the thing to identify.

    ssh … 'cat /workspace/progress.json; tail -5 /workspace/worker.log'
    grep -a -o -m5 -E 'BLENDER_EEVEE[A-Z_]*|CYCLES' <scene>.blend | sort -u

---

## 2026-07-28 — a perfect PNG with no picture in it, delivered and counted

Job `0908e534b1d3` reported `done` in 33.217 s. Its output,
`out/0908e534b1d3.png`, was:

* 8,734 bytes
* a valid PNG signature and an IEND chunk
* 640x480 — exactly the dimensions requested
* sha256 matching the digest the worker computed when it wrote the file
* **mean 0.00000, standard deviation 0.00000, maximum 0.0000**

An entirely black image. It passed every check the farm performed, because every
check the farm performed verified that the FILE was intact. Nothing looked at
the picture. The broker's own log line even carried the evidence, and there was
no rule that could read it:

    21:56:07 INFO broker  job 0908e534b1d3 done — render 33.2s, total 799.8s, 0.0 MB

`0.0 MB`, in a batch where every neighbouring frame from the same instance and
the same worker session logged 27-38 MB.

### Root cause: a caller's camera, not a farm bug

The job named `CAM_CAL` in `world/assembly/assembly_render.blend`. Linking that
one datablock out of the 4.1 GB file — cheap, `bpy.data.libraries.load` reads
only what you ask for — against its siblings:

| camera | location | rotation X |
|---|---|---|
| `CAM_BEAT4_ROOF` | (14.0, 0.4, 3.3) | 88.6° |
| `CAM_PIT_EDGE` | (-48.0, -103.8, 14.9) | 86.0° |
| `CAM_T1_RUNOFF` | (590.5, 296.0, 10.8) | 84.8° |
| `CAM_T10_HELI` | (-638.0, 422.5, 65.3) | 74.2° |
| `CAM_HAIRPIN_KERB` | (253.6, 928.6, -2.6) | 88.2° |
| **`CAM_CAL`** | **(2600.0, 2597.0, 4.3)** | **36.9°** |

Every working camera sits within ~950 m of the origin looking near-horizontally
at the track. `CAM_CAL` sits 3.7 km diagonally away from all of it, 4.3 m up,
pitched 36.9° — which is **53° below the horizon**, pointing down into ground
that does not exist out there. The file does have a world (`SKY_World`, a Sky
Texture) and it does have lights: the four jobs that followed on the same
instance, in the same worker process, from the same .blend, returned 27-38 MB
frames measuring mean 0.30-0.68. One camera was aimed at nothing.

So: a throwaway calibration camera in a caller's scene, not a defect in the
broker or the worker. **The farm was still wrong**, because it reported success.

A second frame turned up in the same sweep: `f36725c40f08`, a 3840x2160 render
through `MACRO_SP` that came back at sd 0.00794, mean 0.774, **14 distinct
luminance levels across 8.3 million pixels** — a flat light grey — in 15.3 s,
where comparable 4K frames took 120-190 s. Not proven wrong, but nobody had ever
been shown the number.

### Fix

`broker/imgstat.py` measures every returned frame and classifies it: `BLACK`,
`TRANSPARENT`, `UNIFORM`, `SUSPICIOUS`, `OK`, `UNREADABLE`. The measurement
happens inside `Broker.collect`, beside the sha256, structure and dimension
checks rather than in a pass of its own, and the verdict is recorded on the job
and frame rows so it stays queryable afterwards.

Three decisions are load-bearing:

* **A classification, not a threshold.** A near-black frame can be correct — a
  fade, a night interior. `BLACK`, `TRANSPARENT` and `UNIFORM` fail a job;
  `SUSPICIOUS` is reported loudly and never fails one. A check that refuses
  legitimate work gets switched off, and then it protects nothing.
* **`rq seq verify` will not count a blank frame as delivered.** That is the
  resume-poisoning case: a `done` row makes every future pass skip the frame
  forever, so the hole survives every retry and appears at assembly. In a shot
  that is one continuous take there is nothing to cut around it.
* **Blank fails terminally.** `MAX_ATTEMPTS` is 3, and a camera pointed at empty
  space renders black three times for three times the money.

Thresholds come from the 240 frames this farm had already returned. Sorted by
standard deviation: 0.00000 (the defect), 0.00794 (the flat grey above), then
0.03494 for the flattest legitimate frame and 237 more up to 0.34069. There is
an empty gap between 0.008 and 0.035 and the thresholds sit in it —
`BLANK_SD_MAX = 0.005`, one and a third 8-bit quantisation steps, and
`SUSPECT_SD_MAX = 0.02`. A deliberately blank Cycles render measured
mean 0.0003 / sd 0.0011, not exactly zero, so an `== 0` test would have missed
it; 0.005 catches it with 4x margin.

For sequences there is a second, relative check. A fixed threshold cannot see
that frame 1,600 is nothing like frames 1,590-1,610, and a fade legitimately
walks a whole neighbourhood to black together. `imgstat.outliers` compares each
frame with a rolling window of 25 neighbours using median and MAD, and requires
both a robust z past 8 **and** an absolute deviation worth re-rendering for —
without that second condition, a locked-off shot has MAD ~0 and every frame in
it is infinitely many MADs from the median.

### The lesson worth keeping

Round 1 of this project already learned it, in `f1-site/tools/drive.mjs`:

> A frame whose pixels are effectively uniform is reported BLANK. A black canvas
> from a dead GL context is the single most common headless-3D failure and it
> passes every "file exists" check ever written.

The render farm inherited the transport rigour — sha256 end to end, truncation
detection, spec hashes, refusing to delete the remote copy until the local one
verifies — and none of the content rigour. Every one of those checks answers
"do the bytes I have match the bytes that were written". None of them answers
"is there anything in the picture", and that is the question that decides
whether a delivery is a delivery.

---

## 2026-07-28 — fixing the probe unmasked a misclassification, and it cost two GPUs

Immediately after the `unknown`-forever wedge above was fixed, the fleet
destroyed two **healthy** instances and blacklisted a **good** machine.
The trigger was on this machine, not on vast.ai:

    bind [127.0.0.1]:8798: Address already in use
    channel_setup_fwd_listener_tcpip: cannot listen to port: 8798
    Could not request local forwarding.

### Root cause

Three faults stacked, and the third had been latent for the life of the broker.

**The port conflict.** `kill -9` is the ONLY sanctioned way to restart this
broker — SIGTERM runs the shutdown path, which destroys the instance — so a
restarted broker *cannot by construction* clean up its own `ssh -L`. The orphan
holds the local forward port and the next deploy's tunnel dies instantly. This
is the documented restart procedure's guaranteed side effect, not a race.

**The misclassification.** `wait_worker` reported the dead tunnel with a message
that said, in English, *"this is a transport failure, not a worker failure"* —
and the call sites raised it as a bare `RuntimeError`. `is_transport()` matches
on **type**. So the text and the type said opposite things, and the type won: a
local port conflict was read as the remote host being broken.

**Why it had never bitten.** The `cat progress.json` bug made every
never-rendered instance permanently `unknown`, and `unknown` blocked the
destroy. The misclassification had been there all along, harmlessly, because a
second bug was vetoing every action it could have caused. Fixing the probe
removed the veto and the latent fault fired on the first restart.

### Fix

`WorkerUnreachable` carries `tunnel_died` and `local`, so the fleet classifies
on type rather than prose. A local bind failure keeps the GPU, blacklists
nothing, and reaps the stale forward (`remote.reap_stale_tunnels`, also run at
broker startup — the orphan is expected after every `kill -9`). A remotely
dropped tunnel is transport. A worker that never answers over a *healthy*
tunnel on a *reachable* box now indicts the **scene**, not the hardware: a
.blend that will not load will not load on new hardware either, and cycling
hosts for it costs a rental plus a 481 MB Blender push plus a ~290 MB scene push
per attempt.

The machine blacklist was also pulled back to auth rejections only. "Host-level
deploy failure" is also what a Blender-crashing .blend looks like from here, and
blacklisting on it would walk the broker through every machine on vast.ai
condemning good hardware for a fault that travels with the scene.

`Fleet.contacted` became `Fleet.may_hold_render`, because those are not the same
question. It had been set by any successful ssh command — but running `true` on
a box cannot start a render, and one instance proved the gap: ssh worked
long enough to provision, the Blender push then failed at 3.5% on all four
retries, and the flag insisted a box that had never had Blender on it might be
mid-frame. It is now set only where `start_worker` is called, plus
unconditionally on adoption.

### The lesson worth keeping

**Fixing a bug can arm one that was already there.** The dangerous change is not
the one that adds a behaviour; it is the one that removes a veto. The probe fix
was correct in isolation and correct in the end, but it converted a dormant
misclassification into destroyed hardware in under a minute — so after
unblocking anything that had been suppressing action, look for what that
suppression was protecting you from.

And: **a message is not a classification.** The string said "transport failure"
for as long as the code has existed; nothing read it. Facts that the code must
branch on belong in the type, not the prose.

---

## 2026-07-28 — a host that never wrote our ssh key wedged the broker permanently

Every job failed for sixteen minutes against a rented instance while a
5090 billed at $0.356/hr. The broker's own message named the wrong cause:

    SshNeverReady: sshd on <host>:<port> never accepted a command within
    240s of trying. The port answers TCP ... but the container behind it is not
    serving.

sshd was serving the whole time.

### Root cause

Two independent faults, one on vast.ai's side and one ours.

**The host's fault.** `ssh -vvv` — the step that settles this in thirty seconds
and was worth doing before reading any code — showed a completed handshake, a
vast.ai banner, our key offered, and a refusal:

    debug1: Offering public key: /root/.ssh/id_vast_render ED25519 SHA256:<redacted>
    debug1: Authentications that can continue: publickey
    root@<host>: Permission denied (publickey).

Not a young container, not a port-mapping error, not our ssh options. Proved by:
the account has exactly one key and it is the right one (`GET /api/v0/ssh/`);
vast's control plane reported that key attached to that instance
(`GET /api/v0/instances/<id>/ssh/`); `attach_ssh` answered *"SSH key already
associated with instance"*; the **proxy relay refused it identically**, which
rules out the direct port mapping; and `detach_ssh` + `attach_ssh` did not repair
it in the following five minutes. Meanwhile the container's own onstart watchdog
ran and self-destructed the instance at the 30-minute stale-heartbeat mark — so
the container was healthy and only `authorized_keys` was missing. The machine
simply failed to inject it.

**Our fault, and the one that actually cost the evening.** `exit 255` is ssh's
generic "I could not run your command", and the broker had exactly one bucket
for it: `transport_failed`. So a *refused key* was classified as *flaky
network* — the one reading that says "retry, it may heal". It cannot heal: vast
writes the key at container start, so a key absent four minutes in was never
written.

Then the safety rule closed the trap. The destroy gate requires a reachable,
definitely-idle answer, and `unknown` blocks it. But the activity probe is
itself ssh, so on a host that refuses ssh the probe is *permanently* unknown.
The broker correctly identified a host-level failure, correctly declined to
destroy a possibly-rendering GPU — and had no way out. Three deploy rounds, the
job failed, and the instance was kept forever.

### Fix

`Ran.auth_rejected` distinguishes the two exit-255s: an auth rejection means the
handshake **completed**, which is positive evidence the box is up and final
evidence we can never use it. It is excluded from `is_transport`, so it replaces
the instance on the first round instead of the third, and it blacklists the
`machine_id` — the same host re-lists under a new offer id within seconds.

The gate keeps its tri-state rule with one exception, which is a strengthening
rather than a weakening. `unknown` means "something might be rendering". On an
instance this broker rented and has **never once run a command on**, nothing
can be: the worker arrives over ssh and every frame is dispatched over ssh, so
no contact means no worker, no scene, no frame. `Fleet.contacted` records that,
`rendering` still blocks unconditionally, and adoption sets it True *without
asking* — a previous broker may have left a frame in flight.

Adoption has one carve-out, or a restart would launder a dead host back into a
protected one. An adopted instance that **auth-rejects** is replaceable, because
vast writes the key at container start: a container without it either never
provisioned it or has restarted since, and a restart has already killed any
Blender process. It cannot belong to a sibling broker either — ownership is per
account, and this is the account's only key.

### The lesson worth keeping

A generic error code is not a diagnosis. `exit 255` was carrying two opposite
facts under one name for as long as the broker has existed, and the message
built on top of it asserted the one that was false. One `ssh -v` outranked all
of it — run the failing command by hand before reading the code that wraps it.

The structural lesson is narrower and worse: **a safety rule whose evidence
comes through the channel that is broken has no bottom**. "Only a definitely-idle
answer licenses a destroy" is right, but when the only way to ask is the thing
that failed, it degrades to "never destroy" and the broker cannot escape. Such a
rule needs a floor that does not depend on the failing channel — here, what the
broker knows *by construction* about an instance it has never spoken to.

---

## 2026-07-28 — `brokerd.sh stop` was destroying the GPU it promised to keep

Found while restarting the broker to load the frame-sequence code. Two log
lines, in the same second:

    04:39:18 WARNING brokerd  supervisor asked to stop — leaving broker 2656354 alone
    04:39:18 ERROR   broker   SIGNAL: received SIGTERM (pid 2656354, ppid 1)

The script had just printed *"broker killed — instance left running for the next
broker"*.

### Root cause

`brokerd.sh stop` deliberately signals the supervisor **first**, so the
supervisor does not faithfully restart the broker it is about to `kill -9`. But
the broker runs with `PR_SET_PDEATHSIG`, and it was set to **SIGTERM**. The
supervisor's death therefore delivered SIGTERM to the broker before the script's
own `kill -9` could — and SIGTERM runs the broker's shutdown path, which with
`KEEP_ON_EXIT` off **destroys the instance**.

The one signal this whole project is written to avoid was being delivered by the
mechanism meant to prevent orphans, on the documented, recommended way to
restart a broker. It cost nothing that day only because nothing was rented.

### Fix

`parent_death_signal()` defaults to `SIGKILL`. That serves the mechanism's whole
purpose — no unsupervised broker may hold the singleton lock — while leaving the
instance alone, which is what every deliberate restart wants. The instance is
not orphaned: the next broker adopts it in seconds, and the in-container
watchdog destroys it 30 minutes after the heartbeat stops if none comes back.

### The lesson worth keeping

An automatic signal counts as a caller. "kill -9, never SIGTERM" was enforced
everywhere a human types a command and nowhere the kernel sends one.

---

## 2026-07-28 — Blender 5.2 segfaults setting a rigid-body disk cache

Building a test fixture with a baked rigid-body sim. Blender dies with a core
dump — backtrace through `PyObject_SetAttr` into the point-cache RNA setter — on:

```python
bpy.ops.wm.save_as_mainfile(filepath=out)
scene.rigidbody_world.point_cache.use_disk_cache = True    # segfault
```

Reproduced in isolation on an otherwise empty scene (one passive plane, one
active cube), background mode, `--factory-startup`. The same assignment on a
**cloth** or **particle** point cache is fine, so it is specific to the
rigid-body world's cache — not to point caches generally, and not to background
mode's ability to write disk caches at all: a cloth bake produced 48 `.bphys`
files without complaint.

Setting it *before* saving does not crash, and does not work either — the flag
silently stays False, because `//blendcache_<name>/` has nothing to resolve
against until the file has a path.

### Why it matters here

Round 2's wall breach is a rigid-body destruction sim, and a **memory** cache
does not travel to a rented instance. A scene baked to memory renders on the
farm by *simulating*: silently, differently, per frame.

### Workaround

Tick the rigid-body world's *Disk Cache* checkbox in the UI, then bake. The
broker's handling of the resulting cache is identical either way, and it now
refuses any frame a cache does not cover rather than rendering it. The test
fixture (`scenes/build_anim_test.py`) uses cloth for exactly this reason.

---

## 2026-07-26 — the broker "crashed" nine times and never crashed once

**Symptom.** The broker process vanished mid-batch, repeatedly. `broker.log`
ended mid-sentence inside the reattach loop, no traceback, no shutdown line,
stdout empty. Clients saw

```
http.client.RemoteDisconnected: Remote end closed connection without response
```

after 29m48s, then `Connection refused` for every job after it. Five queued
renders lost, twice.

The suspected cause was new code — a wait loop that polls the instance while a
foreign render finishes — on the reasoning that repeated SSH timeouts in it
"may raise somewhere unguarded".

**That loop was innocent.** It is simply where the broker spends ~95% of its
wall-clock time during an 8K frame, so it is where any clock-driven death lands.
Duration is not causation.

### What the evidence actually said

| check | result |
|---|---|
| `dmesg` | no `oom_kill`, no `Killed process`, no segfault |
| `coredumpctl` | five Blender SIGSEGVs from the day before, **no python core at all** |
| the rented instance | **still running** after every death |
| `broker.log`, whole history | nine `dispatcher started`, exactly **one** `dispatcher stopping` |
| the launch command | `run_in_background: true` + `exec .venv/bin/python -m broker.app` |

The third row is the one that decides it. `KEEP_ON_EXIT` is false, so *every*
in-process exit path — an exception out of `main()`, `SystemExit`, uvicorn's
SIGTERM handler, a clean shutdown — runs `broker.stop()` and destroys the
instance. **The instance survived. Therefore no Python code ran on the way
out.** Not an unhandled exception, not an exception on the event loop, not a
thread dying: all of those would have exited through `stop()` and taken the GPU
with them. And an active `systemd-coredump` that recorded Blender's segfaults
but no python core rules out every core-dumping signal.

### Root cause

The broker was **started as a background task of the agent harness**, with
`exec`:

```
run_in_background: true
cd ~/vast-render && VASTRENDER_SCENE=... exec .venv/bin/python -m broker.app >> state/broker.log 2>&1
```

`exec` replaces the task's shell with the broker, so **the broker *is* that
task's process**. Its lifetime belongs to whatever reaps the task, and when that
happened it was signalled — mid-render, with no traceback (nothing ran) and no
teardown (nothing ran). Nine brokers were started this way; not one of them
logged a shutdown line.

The single `dispatcher stopping` in the whole history is the same bug in its
other form: that broker got SIGTERM rather than SIGKILL, ran the graceful path,
and **destroyed a healthy instance on the way out** — the next start had to rent
fresh hardware and re-push 481 MB. Both signals lose the batch; SIGTERM also
loses the GPU.

### Fixes

* **`scripts/brokerd.sh`** — `setsid` into its own session and process group, so
  a group-kill aimed at the shell that started it cannot reach it, and the
  broker is a child of the supervisor rather than of anything the harness holds
  a pid for. It restarts the broker on any abnormal exit, and refuses to restart
  on status 3 (singleton lock held) or 0 (deliberate shutdown). It never touches
  the vast API — it cannot adopt and cannot destroy, so it cannot repeat the
  adopt-then-destroy bug the singleton lock exists for.
* **The supervisor reports the wait status.** A process cannot report its own
  SIGKILL; only its parent can, and nothing was anyone's parent. `broker
  KILLED BY SIGNAL 9` now lands in `broker.log` — the one fact missing from both
  incidents.
* **`broker/diagnostics.py`** — `sys.excepthook`, `threading.excepthook`,
  `sys.unraisablehook`, `loop.set_exception_handler`, `faulthandler`, and a
  chaining handler that names SIGTERM/SIGINT/SIGHUP/SIGQUIT *before* uvicorn
  acts on it, plus an `atexit` line. Every in-process death now writes one line
  identifying itself, which makes **silence itself the diagnosis**: nothing in
  the log means SIGKILL and nothing else. SIGHUP is now ignored, as a daemon's
  should be. `kill -USR1 <pid>` dumps every thread's stack.
* **Startup says who owns the process.** A detached broker is a session leader
  (`pid == pgid == sid`); anything else logs `THIS BROKER IS NOT DETACHED`
  naming the group that can kill it. The two lost batches were both diagnosable
  from four integers nobody had.
* **Shutdown no longer discards a live frame.** `stop()` asks the instance what
  it is doing and destroys only on the same three-valued rule used everywhere
  else — `rendering` or `unknown` keeps the GPU and says so loudly. The
  in-container watchdog (30 min stale heartbeat, 12 h cap) remains the backstop,
  so worst case is ~$0.15 of billing against a frame that is often 40 minutes of
  GPU.
* **Every loop thread is supervised.** `Broker.supervised` restarts dispatch,
  heartbeat and progress if their body ever escapes — including `BaseException`,
  which the `except Exception` inside each loop does not cover. A dead dispatch
  thread used to be silent: HTTP kept answering while nothing claimed a job.
* **`/teardown` runs in a thread**, not on the event loop. Minutes of blocking
  SSH behind the fleet lock froze every HTTP handler, which reads to a client
  exactly like the broker having died — the symptom this investigation started
  from.

### Also fixed, and wrong independent of any of this

`wait_out_foreign_render` held `fleet.lock` for the entire wait — up to
`REATTACH_SEC`, 5400 s — blocking `/teardown` and `hibernate` for the length of
a render. It had already been replaced by `_refuse_if_rendering()` raising
`WorkerBusy` so the *dispatcher* decides whose frame it is, with the waiting
done in `await_render` outside the lock. That is now pinned by a test that
starts a wait and asserts the lock is still acquirable:
`test_wait_does_not_hold_the_fleet_lock`.

### The lesson worth keeping

The two previous entries say: check that the code producing a diagnosis can
observe the component it names. This one is about the process itself.

**Ask what the absence of evidence rules out.** An empty log looked like no
information, and it was the whole answer: a broker that dies without destroying
its instance did not run any Python, and everything the investigation had been
looking for — an unguarded raise, an exception on the loop, a dying thread —
requires Python to run. Every hypothesis on the table was excluded by a fact
already in hand.

And: **a process whose lifetime is owned by something else has no bugs worth
fixing until that is fixed.** The broker was hardened for a month against
failures it was never having.

---

## 2026-07-26 — the busy-guard worked, and the job was failed anyway

**Symptom.** An 8K frame reached the queue as

```
job 54ed3b8bd22f  ->  failed: "WorkerBusy: refusing to restart the worker on <host>..."
```

while the same instance, at the same moment, reported

```
{"state":"rendering","job_id":"54ed3b8bd22f","sample":6896,"total":8192,
 "tile":1,"tiles":12,"elapsed_sec":756}     # one blender process, GPU 96%
```

The guard added by the previous fix did its job — it refused to SIGKILL the
render. Then the broker marked the job `failed` for the very frame the guard had
just protected, the queue emptied, and the render ran to completion with nobody
waiting for it.

### Root cause

The same conflation as the entry below, one layer up. `WorkerBusy` had been made
non-fatal to the **worker** and was still fatal to the **job**.

Three separate holes, all the same shape — *"I could not ask" was read as "it is
not happening"*:

1. **The identity was thrown away.** `WorkerBusy` is only ever raised off a
   *successful* read of `progress.json`, so at the moment of raising, the job id
   of the running render is known for certain. It was formatted into the message
   and discarded. The handler then **asked the instance a second time** — over an
   SSH endpoint that flaps on this host — got nothing, and concluded the worker
   was not rendering the job the exception had just named.

2. **`await_render` gave up on one unreadable poll.** `read_progress` returns
   None for a failed SSH probe *and* for "not rendering", so a single flap ended
   the reattach with the operator-facing line *"the instance is not rendering
   this job either, so the worker really is gone"*. It appears twice in one
   twelve-minute window in the log, both times while the GPU was at 96% on that
   frame.

3. **There was no case for someone else's frame.** `WorkerBusy` naming a
   *different* job fell through to the generic handler, so an agent's job could
   be failed by the mere fact that another agent's render was on the GPU.

### Fixes

* **`Activity` — three states, never two.** `rendering` / `idle` / `unknown`,
  where `unknown` means the probe did not answer. Only `idle` — reachable,
  parsed, and definitely not rendering — licenses anything destructive. Every
  caller that makes a decision takes an `Activity`; `rendering_now()` survives
  only as a compatibility shim whose signature cannot express the difference.
* **`WorkerBusy` carries `job_id` and `progress`.** Handlers use the evidence in
  the exception instead of re-asking a channel that may have just failed.
* **The dispatcher decides among three cases explicitly**, because
  `progress.json` carries the job id and this is therefore decidable:
  busy with **my** job → reattach and collect; busy with **another** job → queue
  behind it, never kill it, never fail my job for it; **not rendering** →
  deploy as usual.
* **A job is never written `failed` while the instance is rendering it.** The
  last gate before the queue records a verdict asks the box, and requeues
  *without spending an attempt* if a render is in flight or the finished PNG is
  already on disk.
* **The kill guards itself, on the instance.** The pre-check and the kill were
  two SSH round trips, and on a flapping endpoint they can disagree — three
  failed probes report "unknown", the flap ends, and the kill lands on a live
  render. The kill command now re-reads `progress.json` itself and exits with
  `BUSY:<job_id>` before signalling anything, so there is no window at all.
* **A finished frame is collected, never re-rendered.** Any retry first asks
  whether `/workspace/out/<job_id>.png` already exists — the case this incident
  created, where a completed 8K render sat on the instance belonging to a job
  the queue had given up on.
* **The idle timer refuses to stop a GPU it cannot ask about**, bounded by
  `IDLE_UNKNOWN_MAX_SEC` (30 min, deliberately longer than the in-container
  watchdog's heartbeat deadline, so an unreachable box self-destructs before
  this branch ever has to guess).

### The lesson worth keeping

The previous entry's lesson was "check that the code producing a diagnosis can
observe the component it names". This one is its corollary: **a guard that
refuses to act must also tell the caller what it saw, or the caller will go and
ask something less reliable.** The refusal and the recovery are one decision, and
splitting them across two queries of a flapping channel is what turned a
correct guard into a lost frame.

Verification for this class of bug does not need a GPU:
`broker/test_broker.py::test_busy_dispatch` drives all three cases plus the idle
timer against a stub fleet, because the live path only exercises them when
something has already gone wrong — which is exactly how the previous fix shipped
unverified and failed the first 8K frame it met.

---

## 2026-07-26 — 8K renders "died" with `worker closed connection without replying`

**Symptom.** `7680x4320 @ 8192 samples` failed after ~4m36s with

```
RuntimeError: worker closed connection without replying
```

The identical spec had succeeded earlier the same night (2425 s, valid 91 MB
PNG), so it was not inherently impossible. Suspicion fell on instance RAM, VRAM,
disk, the 16-bit colour depth, and OpenImageDenoise's host buffers at 33
megapixels.

**All of those were wrong.** The worker never crashed. Nothing ran out of
memory.

### What the evidence actually said

| check | result |
|---|---|
| `cgroup memory.events` | `oom_kill 0` — the kernel never OOM-killed anything |
| `cgroup memory.max` | 183.5 GB limit, **peak 11.2 GB** (6%) |
| container restart? | no — pid 1 up since 12:32, so the counters cover the failure |
| host RAM | 503 GB total, 460 GB available |
| disk | 2.3 GB of 30 GB used, `/workspace/out` held one file |
| Blender crash file | none — a segfault writes `.crash.txt`, there was none |
| `worker.log` | ends at `[worker] ready`, no error, no traceback |

Then the decisive one. The broker's own failure line quoted the worker
contradicting it:

```
deploy attempt 1/3 failed: worker ... not ready after 1800s and 599 pings:
ConnectionRefusedError. remote worker.log: ... [worker] ready on 127.0.0.1:8799
```

The worker was **alive, listening on 8799, and rendering at sample 832/8192**
while the broker declared it dead. The broker was pinging its own dead SSH
tunnel 599 times over 30 minutes and blaming the process at the other end.

### Root cause

**This instance's SSH endpoint flapped**, visible directly as

```
ssh: connect to host <host> port <port>: Connection refused
mux_client_request_session: read from master failed: Broken pipe
```

When the tunnel carrying the job socket dies, the broker reads EOF. It reported
that as *"worker closed connection without replying"* — an assertion it had no
basis for. **EOF on a forwarded port means the forward ended; it says nothing
about the process at the far end.**

From there four failures compounded, each one making the next look justified:

1. **Tunnel drops** mid-render → broker reads EOF → blames the worker.
2. Broker "repairs" by **redeploying**, and the redeploy SIGKILLs a healthy
   worker 33 s into a 40-minute frame. Three attempts, three destroyed renders.
3. Job exhausts its attempts and is marked `failed`.
4. The queue is now empty, so 300 s later the **idle timer stops the instance** —
   while the GPU was still at 99% and 420 W finishing that very frame.

A serial worker **cannot** answer a ping while rendering: it is inside
`bpy.ops.render.render()` on its only thread. Silence on the job socket is the
*expected* state during the exact window when the broker most wants to test
liveness. Treating that silence as death is the whole bug.

### Fixes

* `worker_call` raises **`ConnectionDropped`**, never a bare RuntimeError, and
  the message says the tunnel may be at fault rather than asserting the worker
  closed anything.
* On a drop the broker **reattaches instead of re-rendering**: the worker writes
  its PNG to disk independently of the socket that asked for it, so the broker
  polls `progress.json` over the SSH command channel (which stays up while the
  forward flaps) and collects the finished frame. A dropped tunnel now costs a
  connection, not 40 minutes of GPU.
* `rendering_now()` asks the instance what it is doing. `_worker_alive()`
  consults it before declaring death; `start_worker()` raises **`WorkerBusy`**
  rather than killing a render in progress; `WorkerBusy` is re-raised through
  the deploy-retry path so it can never reach the replace-the-hardware branch
  and destroy a GPU mid-frame.
* `wait_worker()` takes the tunnel handle and gives up the moment it dies —
  one poll instead of 599 pings over 1800 s.
* The idle timer asks the same question: **an idle queue is not an idle GPU.**
* `rq` downloads to a temp file and verifies the PNG signature *and* `IEND`
  before renaming, so a truncated 91 MB transfer can never look like a finished
  render.

### The lesson worth keeping

Every layer here reported a component it had not tested. The socket layer
reported on the worker. The readiness check reported on the worker. The idle
timer reported on the GPU. In each case the honest statement was much narrower
— "my forward died", "nothing answered on my local port", "my queue is empty" —
and the honest statement was also the one that pointed at the fix.

When a diagnosis names a component, check that the code that produced it can
actually observe that component.

---

## Earlier, same session

* **A second broker destroyed the first one's GPU.** uvicorn runs lifespan
  startup *before* binding the port, so a second start got through
  `adopt_or_reap`, took ownership of the live instance, failed its bind, and
  destroyed on the way out what it had just adopted. Fixed with an exclusive
  `flock` taken before any fleet call. See `operations.md`.
* **`blender push failed:` with an empty message.** `run(check=False)` returned
  only stdout, so an ssh that never executed was indistinguishable from a
  command that printed nothing. Fixed by `probe()`/`Ran`, which carry exit code,
  stderr tail, elapsed and endpoint.
* **A worker launch that always timed out at 600 s.** `&` binds looser than
  `&&`, so `A && B && blender … &` backgrounds the whole list as one subshell,
  which then holds sshd's stdout/stderr pipes while waiting on blender. The
  render had already started fine every time. `< /dev/null` does not fix it;
  `setsid --fork` with no `&` does.
* **A missing HDRI rendered a plausible but differently-lit frame**, silently,
  because an unpacked `.blend` stores absolute paths and the broker shipped only
  the blend. Assets are now mirrored per scene and anything unresolved is logged
  loudly.
