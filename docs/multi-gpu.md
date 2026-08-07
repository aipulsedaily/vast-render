# Multi-worker instances — approved in principle, not yet built

**Status 2026-08-04: APPROVED IN PRINCIPLE, NOT SCHEDULED. Do not rent an
8-GPU box for interactive work under any circumstances.** This exists so the
next person to price it does not re-derive it, and so the one thing that would
make it a *regression* is written down before anyone starts.

> **SUPERSEDED 2026-08-07 — READ THE CORRECTION IMMEDIATELY BELOW BEFORE
> ANYTHING ELSE ON THIS PAGE.** The N-worker build this document approves is
> **not recommended any more**: it was measured, and it is dearer and slower
> than eight separate single-GPU boxes, which need no code at all. The original
> text is kept intact because its *reasoning* about couplings and hazards is
> still correct and still worth reading; its numbers and its verdict are not.

---

# CORRECTION 2026-08-07 — THE HEADLINE IS FALSE. BOTH WAYS ROUND.

**This document's conclusion was "the dollar case does not exist, the time case
does". Measured on rented hardware today, BOTH halves are wrong:**

* **The time case is real and it is FREE — but it does not come from a wide
  box.** Eight *separate* single-GPU boxes, driven by eight broker PROCESSES
  with the frame range split by hand, take the master from 6.8 days to 0.8 days
  for **the same money** and **zero new code**. That is the pattern already
  built and proven in "Two brokers, one card each" below, run eight times.
* **The dollar case against a wide box is much STRONGER than this doc thought,
  and for a reason it never considered.** The 8-GPU box is not merely
  break-even; it is **$9 dearer and 50 % slower** than eight single cards,
  because *its individual GPUs are 45 % slower than a good single card*.

Everything below the "Status 2026-08-04" line was fit to **510.5 s/frame**, the
`render3.blend` anchor. That anchor is not the film, and it has now produced
**four** wrong master estimates in both directions — 322 h/$146, then 180 h/$80,
then 172 h/$76, then 155 h/$70. See `STAGING-R2-971-to-R2-999.md`.

## What was measured, and how

One frame — frame 30 of `film16_breach.blend`, 3840x2160, 512 spp, adaptive
0.01, **`spec_hash 1983dced5cacabb6` on every host**, luminance identical to six
decimal places every time. So these are the same render, and only the hardware
differs.

| host | $/hr all-in | $/GPU-hr | frame 30 |
|---|---|---|---|
| `47039886` Florida, 1x, Ryzen 9 9950X3D 32c/61.6 GB | 0.4488 | 0.4488 | **151.0 s** |
| `47065580` S. Africa, 1x, Ryzen 9 7950X 32c/63.4 GB | 0.39987 | 0.39987 | **166.8 s** |
| `47083562` California, 8x, EPYC 192c/503 GB | 2.7100 | **0.3387** | **219.65 s** one GPU |
| — the same box, all 8 GPUs on that ONE frame | | | **172.8 s** |
| — the same box, 8 concurrent workers, one GPU each | | | **225.42 s** mean |

### The two things this doc modelled, now measured

```
                                    modelled here   MEASURED    verdict
N independent workers, 1 GPU each        8.00x        7.80x     RIGHT
N GPUs on ONE frame                      4.49x        1.27x     WRONG by 3.5x
```

**The independent-worker model was right, and it is the one that was never
built.** Eight concurrent Blenders on one box cost **2.6 %** against a solo run
on the same box (225.42 s vs 219.65 s), for **7.80x** throughput. Peak host RAM
was **322 GB of 503**, and per-process RSS was **41.4-41.6 GB across all nine
runs** — which pins the "22 GB for a 4.17 GB scene" extrapolation at 5.2x and
makes RAM-per-GPU the binding rental filter (see `vastctl.MIN_CPU_RAM_GB`).

**The multi-GPU-per-frame model was wrong in the expensive direction.** This doc
called it "zero code, $243, 3.8 days" and honestly labelled it *a model, not a
measurement*. It is 1.27x, not 4.49x, so a master run that way costs **$512 and
takes 7.8 days** — you rent eight cards and get 1.27 of them. It is not a
fallback. **It is the worst option on the board and it is the one that happens
BY DEFAULT**, because `enable_gpu()` sets `d.use` on every OptiX device: point
today's broker at an 8-GPU box and this is what you get, silently.

## THE NUMBER THIS DOC NEVER ASKED FOR: $/FRAME

`$/GPU-hr` is a trap, and it is the trap this project asked to be steered into.

```
Florida 1x               $0.4488/GPU-hr x 151.0 s = $0.01882/frame
S. Africa 1x             $0.3999/GPU-hr x 166.8 s = $0.01853/frame
California 8x, per GPU   $0.3387/GPU-hr x 225.4 s = $0.02121/frame   <- DEAREST
```

**The cheapest $/GPU-hr box on the market is the dearest per frame.** Across the
whole exclusive 5090 market the per-GPU price spread from 1x to 8x is **8.8 %**
($0.3803 -> $0.3470). The *host lottery* — how fast the silicon you happened to
draw actually renders — is **±45 %** across three measured hosts. Width is
inside the noise of which host you get.

Note also the two 1x hosts: an 11 % price difference and a 10 % speed difference
**cancelled to 1.6 % in $/frame.** Shopping on sticker price is close to
worthless; shopping on measured s/frame is the whole game.

## The master, every architecture, on measured inputs

Beat-weighted 211.8 s/frame at `adaptive 0.01` on the Florida card, x0.927 for
`adaptive 0.02`, 2,978 frames, serial broker work at the worst measured 4K host.

| architecture | wall | days | total $ | code |
|---|---|---|---|---|
| 1 broker, 1 card (today) | 163.1 h | 6.8 | **$74.11** | — |
| **8 brokers, 8 cards** | **20.4 h** | **0.8** | **$74.21** | **none** |
| 1 broker, 8 workers, 8x box | 30.4 h | 1.3 | $83.60 | ~1,300 lines |
| 1 broker, 1 worker, 8x box | 186.6 h | 7.8 | $512.05 | none (the default) |

> **Eight brokers on eight single cards is the recommendation: 8x the speed for
> +0.1 % money and no new code.** The ~1,300-line N-worker build buys nothing —
> it is $9 dearer and 50 % slower than the free option, because the wide boxes
> on this market have slow GPUs.

**What would change that verdict:** an 8-GPU box whose per-GPU speed is within
~10 % of a good single card. That box may exist — n=1 here, and only 11 exclusive
8x offers were on the market. The test is one frame and ~$1: rent it, render
frame 30, compare against 151.0 s. **Do not buy a wide box without doing that
first.** It is the entire difference between $74 and $84.

## What the 8-broker path actually costs, honestly

Not free of friction, just free of code:

* **The queue is per-broker.** The master must be split into eight contiguous
  `rq anim --frames A-B` submissions with eight resume keys and eight output
  directories, then merged.
* **CONTIGUOUS BLOCKS, NOT STRIPES.** `parse_range` supports `1-2978x8`, which
  would balance load perfectly — and it is exactly wrong here. PNGs from
  different hosts are **not bit-identical** (different driver, different OIDN
  build): measured, luminance agrees to 6 dp but the bytes differ. Striping puts
  a machine boundary between *every adjacent frame pair*; contiguous blocks put
  seven boundaries in the whole film. Size the blocks by measured per-beat cost.
* **The local workstation is the bottleneck, not the market.** It has **6 cores
  and 11 GB RAM**. Eight concurrent `zstd -10` compressions of a 7.97 GB scene
  will serialise on it — one push took 405 s today while competing with the exec
  builds, and the 8x box's push took **617 s**. This is the one real argument
  this doc made for a wide box (one push serves every worker) and it survives;
  it is just worth ~$0, not ~$1,300 of code.
* **Eight labels, eight ports, eight state dirs.** Both hazards in "Two brokers,
  one card each" apply eight times over: prefixes must be pairwise disjoint (not
  merely different), and `VASTRENDER_TUNNEL_LOCAL_PORT` /
  `VASTRENDER_EXEC_LOCAL_PORT` must not collide or startup reaping SIGKILLs a
  sibling's tunnel mid-frame.

## Corrections to specific claims below this line

* **"exclusive 8x (`46354162`) $0.3337/GPU-hr, ~9 h, $24.0"** — the rate is
  right and the conclusion drawn from it is not. It assumes a GPU-hour on a wide
  box does the same work as a GPU-hour on a narrow one. Measured, it does 69 %
  as much.
* **"8x box, ONE worker using all 8 cards (zero code) — 91 h, $243"** — measured
  at 1.27x, not 4.49x: **186.6 h and $512** on today's film rate.
* **"~14.1 s per frame of SERIAL broker work"** — measured at 720p on a
  different host and a different code path. Re-derived at **4K on the shipping
  film, two hosts, n=9 each: 4.52 s (Florida) and 11.14 s (S. Africa).** It is
  host-dependent (fetch throughput), not a constant. **Parallel collect is NOT
  required for a 4K master at any width up to 8** — at the worst measured host
  the dispatch thread is 42 % busy with eight workers. It remains required for a
  720p ladder pass, which is what the original measurement was of.
* **"CPU per worker drops below the floor — 192/8 = 24 against MIN_CPU 32,
  unquantified"** — quantified: 24 effective cores per worker cost **2.6 %**
  with eight concurrent renders including eight simultaneous scene loads.
* **"Peak host RSS during a scene load ... never measured"** — measured:
  **41.4-41.6 GB per process** for a 7.97 GB scene, n=9, load peak and steady
  state alike. `num_gpus=1` was hardcoded in `vastctl.build_query` until
  2026-08-07 (`f8aa76b`), which is why this doc had to source its offers by
  hand.


> **A SECOND CARD IS NOW RUNNING, AND IT IS NOT THIS.** See "Two brokers, one
> card each" at the bottom. Everything below is about N workers inside ONE
> broker driving ONE box, which is still unbuilt. The second card was obtained
> instead by running a SECOND BROKER PROCESS, which needs none of it: every
> single-instance assumption in `Fleet` stays true inside each process. Read
> that section before starting any of the work sized below — it may already
> cover what you need, and it cost 3 lines of behaviour change rather than
> 1,100.

## The dollar case does not exist. The time case does.

Measured against the live market and the live farm on 2026-08-04. A full rung-1
ladder pass (2,978 frames @ 63.4 s = 52.4 render-hours):

| shape | $/hr | $/GPU | wall | pass cost |
|---|---|---|---|---|
| shared 1× (what we were on) | 0.4203 | 0.4203 | 64 h | $26.9 |
| **exclusive 1×** | 0.455 | 0.455 | 64 h | $29.1 |
| exclusive 2× | 0.8014 | 0.4007 | ~33 h | $26.4 |
| exclusive 4× | 1.5481 | 0.3870 | ~17 h | $26.3 |
| exclusive 8× (`46354162`) | 2.6694 | **0.3337** | ~9 h | $24.0 |

**Every shape lands within about $5 of the others on a ~$25 pass.** The
per-GPU saving on the 8-way box is real and is the best on the exclusive
market — and it still does not translate into money at ladder scale. Only
wall-clock and code volume vary.

**The master is the only case that justifies the work**, and it justifies it on
time (290.2 render-hours, 2,978 frames, +25.6 h non-render overhead):

| | wall | cost |
|---|---|---|
| exclusive 1× | 322 h = **13.4 days** | ~$146 |
| 8× box, ONE worker using all 8 cards (zero code) | 91 h = 3.8 days | $243 |
| **8× box, 8 workers** | **40.5 h = 1.7 days** | **$108** |

13.4 days → 1.7 days is the entire prize. It changes what a defect costs after
the master renders, which is the whole reason the ladder discipline exists.

> ### THOSE THREE ROWS ARE SUPERSEDED — measured 2026-08-07
>
> **The master is 180.0 h and $79.99 on one exclusive card, not 322 h and
> $146.** The old figure priced beats 4-6 at `render3.blend`'s 510.5 s/frame
> and beats 1-3 at `beat1_anim.blend`'s 60.2 s. Neither is the film. Nine
> frames of `film16_breach.blend` — the ship candidate — were rendered at
> 3840×2160 / 512 samples on an exclusive 5090 (`gpu_frac 1.0`, $0.4444/hr
> all-in, instance 47039886), sampled across all six beats because the ladder
> had already shown beat 1 costing 72 s/frame against the close-out's 43 s:
>
> | beat | frames | measured s/frame | sampled at |
> |---|---|---|---|
> | 1_assembly | 792 | 161.8 | f30 151, f400 183, f760 152 |
> | 2_launch | 72 | 158.1 | f830 |
> | 3_breach | 192 | 216.0 | f950 |
> | 4_transit | 134 | 230.5 | f1120 |
> | 5_lap | 1,524 | 240.7 | f1500 271, f2300 210 |
> | 6_ending | 264 | 197.1 | f2850 |
>
> Weighted: **175.2 render-hours**, mean 212 s/frame. Non-render overhead is
> **4.8 s/frame**, not 31 — measured as the wall gap between eight consecutive
> frames minus their render times, on a host that took a 7.97 GB scene at
> 97.6 MB/s and deployed in 102 s.
>
> **The whole-film spread is 1.5×, not 8.5×**, and beat 6 — the closing wide,
> the row the old table feared most — is *cheaper* than beat 5. So the two-point
> `P = 6.30 s, F = 57.1 s` fit below, and every conclusion drawn from the 8.5×
> spread, should be re-derived before being used. The fit's *shape* argument
> (eight independent workers beat eight GPUs on one frame) is unaffected; its
> magnitudes are not.
>
> The lesson is the one this file already teaches in another key: **the numbers
> came from a neighbouring configuration, and neighbouring configurations on
> this project are wrong by factors, not by percentages.**

## PARALLEL COLLECT IS PART OF THE FIRST CUT, NOT A FOLLOW-UP

**Read this before writing any code.** Measured on the live box 2026-08-04,
`r1full` frames landed 77/79/83/79 s apart for 62.5–68.0 s renders:

```
~14.1 s per frame of SERIAL broker work
   activity probe, ping, fetch_file, sha256, imgstat.measure (0.35 s at 4K),
   rm -f over SSH — all on the single dispatch thread
```

Rung 1 is 2,978 × 14.1 s = **11.7 h of serial broker work against 6.55 h of
eight-way render.**

> **Ship eight workers without parallelising fetch/verify and a rung-1 pass
> takes 11.7 h with four of eight GPUs idle, and costs $31.2 — worse and slower
> than the single card it replaced.**

A change that makes the farm slower and more expensive while looking like an
upgrade is exactly the shape this project keeps logging. `run_sequence`'s frame
loop must fan out across N threads in the same cut. The `frames` table is
already keyed `PRIMARY KEY (seq, frame)`, so concurrent frame writes are safe.

## Which parallelism, and why the other loses

Solve the ladder's own two measurements — `63.4 = F + P` at 720p/64 and
`510.5 = F + 72P` at 4K/512 — and you get **P = 6.30 s, F = 57.1 s**:

| | 8 independent workers | 8 GPUs on ONE frame |
|---|---|---|
| 4K/512 | **8.0×** | 57.1 + 453.4/8 = 113.8 s → **4.49×** |
| 720p/64 | **8.0×** | 57.1 + 6.3/8 = 57.9 s → **1.10×** |

**Eight independent single-GPU workers.** Multi-GPU-per-frame collapses to
1.10× at rung 1 — the rung that costs 52 hours — because it never removes `F`,
and it pays 57 s of fixed cost on every frame regardless of device count.

It is *not* worthless: it needs **zero code** (`enable_gpu()` in
`worker/server.py` already sets `d.use` on every OptiX device) and would take
the master to 3.8 days. It loses on money — $243 against $146 on one card.
Wall-clock only, and only for a single master attempt.

Caveat, stated: that is a **two-point fit from n=50 and n=2 across different
.blends with adaptive sampling on**, so the effective sample ratio may not be a
clean 8×. The multi-GPU figure is a model, not a measurement, and it cannot be
measured without renting.

## What is already safe, and what is coupled

Already concurrent-safe, verified by reading:

* `db.claim` takes `BEGIN IMMEDIATE` **explicitly so two dispatchers cannot
  select the same row**; one connection per thread; WAL.
* `frames` is `PRIMARY KEY (seq, frame)`.
* PNGs are job-id keyed (`{root}/out/{job_id}.png`, sequence keys
  `{job}_f{frame:06d}`).
* The scene cache is content-addressed on a **shared filesystem**
  (`/workspace/scenes/{digest}/{name}` + `.complete`), so **one push serves all
  eight workers** — the single strongest argument for one 8-GPU box over eight
  1-GPU instances, which would pay 8× every push (5.2 GB × 8 = 42 min at
  16.6 MB/s versus 5).
* **`ExecService` is a working template**: 12 concurrent slots on the same
  rented box, own thread, own port (8800), own tunnel, own `claim_exec`, and a
  `ready_lock` so twelve simultaneous first-jobs do not each restart the server.

Coupled to exactly one worker per instance:

| coupling | where | breaks how |
|---|---|---|
| `WORKER_PORT = 8799` | `config.py` | `start_worker` reads `/proc/net/tcp` and **refuses to launch if the port is bound** |
| `Fleet.ep` / `tunnel` / `local_port` | `fleet.py` | one endpoint, one forward |
| `Fleet.scene_hash` / `scene_path` | `fleet.py` | *one resident scene* is the premise of `ensure_ready`, `next_job`, `starve_threshold`, `cheaper_to_finish` |
| `{root}/progress.json` | `remote.py`, worker `--progress` | **one file.** `activity()` reads it, and every do-not-kill-a-running-frame guard reads `activity()` |
| `WORKER_PIDS` matching `{root}/server.py` | `remote.py` | kill-by-pid matches **all eight**; one restart kills eight |
| `enable_gpu()` enables every OptiX device | `worker/server.py` | eight workers would each grab all eight cards |
| `dispatch_once` runs one job and blocks in it | `app.py` | one dispatch thread == one worker by construction |

## Size

Mirror `ExecService`: one supervisor, 8 slots, `--slot N` → port `8799+N`,
`progress-N.json`, `worker-N.log`, `CUDA_VISIBLE_DEVICES=N`.

| file | change | lines |
|---|---|---|
| `worker/server.py` | slot arg, pin one device | ~30 |
| `remote.py` | slot-parameterise `start_worker`, `WORKER_PIDS`, `activity`, `finished_png_info`, `worker_launch_cmd`, `missing_libraries`; one `ssh -L` with eight forwards | ~150 |
| `fleet.py` | per-slot `Worker` record; `ensure_ready` → `acquire_slot`; per-slot liveness/postmortem/reattach; `protected_scenes()` returns all eight | ~400–600 |
| `app.py` | N dispatch threads, per-thread `current_job`/`current_key`, `maybe_idle_down` requires all slots idle, `progress_loop` polls 8 files | ~300 |
| `run_sequence` | fan `plan.todo` out to N threads — **first cut, see above** | ~100 |
| `config.DISK_GB` | 60 → ~300 | 1 |

**~1,100–1,500 lines across 5 files, concentrated in the two that carry the most
incident-derived invariants.** 2–4 days plus a shakedown that must happen on a
rented 8-GPU box, because none of the failure modes reproduce locally.
`test_broker.py` (4,415 lines) needs substantial work and is **not runnable
against a live broker** — it submits real jobs.

Highest-risk items, in order:

1. **`progress.json` is load-bearing for every frame-protection guard.**
   Splitting it wrong does not fail loudly. It kills frames.
2. **`WORKER_PIDS` kills by pattern.** Until slot-qualified, any worker restart
   kills all eight — silent, and the same class as the stale-worker-serving-the-
   old-scene bug the code already documents.
3. **`activity()` becomes ambiguous.** `_refuse_if_rendering` needs "is slot N
   rendering"; `hibernate` / `maybe_idle_down` need "is *any* slot rendering".
   One function cannot keep answering both.

## Measured unknowns — do not discover these during a master run

* **CPU per worker drops below the floor we already set.** 192 cores / 8 = **24
  per worker against `MIN_CPU_CORES_EFFECTIVE = 32`.** Cores are not
  partitioned, so it only binds during simultaneous scene loads — which is
  exactly the `F = 57.1 s` fixed cost, the thing multi-worker is meant to
  amortise. **Unquantified.** Measure eight concurrent 5 GB scene loads before
  committing to a master.
* **Peak host RSS during a scene load.** The only measurement is **22 GB
  resident for a 4.17 GB scene** (steady state, from the `_wait_for_worker`
  incident). 8 × 22 = 176 GB against 472 GB reported — fits with 2.7× margin —
  but the *load peak* versus steady state has never been measured, and eight
  simultaneous loads are the worst case.
* **Multi-GPU-per-frame scaling (4.49×) is modelled, not measured.**
* **sshd session headroom** with 8 forwards plus heartbeat, progress polls and
  the exec tunnel. `direct_port_count` is 97, so ports are not the limit;
  `MaxSessions` / `MaxStartups` might be.

## VRAM and utilisation — settled

**VRAM fits with a 2.4× margin.** Measured peak of our Blender is **13,432 MiB**
on the 5 GB film scenes (R2-382); the card is 32,607 MiB. One worker per card is
41 % used. With `gpu_frac 1.0` there is no co-tenant to take the rest — see
`operations.md`.

**Never hold the box for interactive work.** Measured across 390 jobs in 24 h
with five agents interleaving, time with N jobs runnable:

```
nothing runnable  25.6 %      >=4 runnable  42.4 %
>=1 runnable      74.4 %      >=8 runnable  29.3 %
```

Seven of eight GPUs idle most of the day, ~**$1.07 per useful GPU-hour — 2.7×
worse than an exclusive single card**. Rent it per-pass for the master and for
full ladder runs, and destroy it after. (Honest caveat: queues build partly
*because* the box is a bottleneck, so this overstates steady-state depth — and
understates it, because agents throttle themselves to a slow farm.)

---

# Two brokers, one card each — BUILT 2026-08-04

The farm had one card and eight agents on it. This is what was added, what was
measured first, and the two things that would have destroyed the running farm.

## What the broker already supported: one instance, and it enforces that

There was no partial multi-worker support to finish. `Fleet`'s own docstring is
`"""One instance, its tunnel, and the money it is spending."""`, and the
singularity is not an oversight, it is enforced:

* **`Fleet.adopt_or_reap` DESTROYS every labelled instance except the one it
  adopts.** Renting a second card under the same label does not give you two
  workers; it gives you one worker and a destroyed box, at the first restart.
* `reconcile` is a *ghost check* — "does the instance I am bound to still exist
  on vast.ai" — not fleet management. It fetches the full `our_instances` list
  every cycle and discards everything but `mine.get(iid)`.
* The **bandwidth ceiling** (`MAX_INET_COST_PER_TB`) is an offer *filter*, and
  the **auto-migrate to cheaper offers does not exist in this tree at all** —
  no code, no flag, no dead branch. A dead-code sweep over every `def` in
  `fleet.py`, `app.py` and `vastctl.py` found zero unreferenced functions. The
  only price-awareness is rent-time ranking (`estimate_cost`), consulted once
  from `_rent`. Instance replacement exists but is quality-driven
  (`condemn_slow_link`, stalled rounds), never price-driven.

So it was a development job, not a provisioning job — but a *small* one, because
the unit that has to be duplicated is the **process**, not the worker.

## The two things that would have killed the farm

Both are load-bearing. Neither is obvious.

1. **The label is the entire definition of "mine", and the filter is
   `startswith`.** A second broker labelled `renderbroker2` is matched by
   `renderbroker`, so broker 1 would adopt-or-reap it out from under a running
   frame at its next restart. The second prefix must share no prefix with the
   first: `ladderbroker`, not `renderbroker-ladder`. `LABEL_PREFIX` is now
   `VASTRENDER_LABEL`, default unchanged.

2. **`local_port` was a hardcoded default argument, and startup reaps it.**
   `app` calls `remote.reap_stale_tunnels(local_port)`, which pgreps for
   `-L <port>:127.0.0.1:<WORKER_PORT>` and SIGKILLs every match that is not its
   own child. A second broker on the default 8798 does not fail to bind and back
   off — **it kills the first broker's tunnel, mid-frame**, and broker 1 reads
   that as a transport failure on a box it may then condemn as bad hardware.
   Now `config.TUNNEL_LOCAL_PORT`, default unchanged; broker 2 uses 8796.

Because the prefixes are disjoint, **nothing about broker 1 had to change and it
was never restarted.** Its tunnel was verified as the only one on 8798, and its
in-flight frame ran through the whole exercise untouched.

## What is NOT shared, and is therefore a routing decision

There is no load balancing and that is deliberate. A job submitted to 8760 can
never be served by 8761. Also per-broker, and each reports as if it were the
whole truth: `rq status`, `rq budget`'s `spent`, `MAX_BATCH_USD`, and — the
sharp one — **`rq teardown`, which destroys one card and reports success while
the other keeps billing.** Only `rq budget`'s `credit` line is account-wide.

## Send it bulk sequence work on ONE scene. Nothing else pays.

A cold worker must be sent every scene it renders over a single unresumable
`zstd -c | ssh zstd -d` stream. Measured on the live farm 2026-08-04:

| | cost |
|---|---|
| push a ~5 GB film scene | **290–460 s** (12.75 MB/s raw on the Thailand host; 62 MB/s raw on a good one) |
| one 720p/64 ladder frame | 74–76 s |
| one 4K verification still | 53–90 s |

* **Bulk, one scene:** one push amortised over a 21 h pass = **0.4 % overhead.**
* **Short stills, five scenes:** 5 × ~300 s of push to enable ~750 s of render
  — **2.8x more upload than render.** The second card would finish them *slower
  than the queue it was bought to relieve.*

**Bandwidth is not the ceiling and that was checked.** The local uplink sustains
≥27 MB/s wire (62 MB/s raw at the measured zstd 2.29x); the 12.75 MB/s seen on
the live push is the *host's* ingest limit. Two pushes to two different hosts do
not contend locally — broker 2's 481 MB Blender bundle pushed at 11.88 MB/s
while broker 1 was mid-render on its own box.

## Why capacity was the right lever here, and the number that says so

The queue was **not deep — it was blocked.** Depth 11, roughly 660 s of render
work outstanding, against observed waits of **4,086 s**. A 6:1 wait-to-work
ratio is the signature of blocking, not of insufficient capacity.

What blocks it is bulk sequence work, because **`run_sequence` does not yield
between frames**: two `r1full` jobs consumed 7,461 s and 2,739 s — 10,200 s,
**37 % of a 7.6 h instance life** — during which seven agents' 60 s verification
renders could not run at all.

Scene switching cost 2,975 s (10.9 %) over the same window, and
**`film14_breach_*` — the ladder family — is 47 % of it** (13 switches,
1,385 s). Moving the ladder to its own card removes that from the shared box
*and* stops a 5 GB scene evicting the stills cache.

Beware the obvious mismeasurement: **`broker.log` lines carry `HH:MM:SS` and no
date.** Filtering "since 10:15" across a multi-day log gives 30 % switching
overhead. Anchor on a line unique to the current instance — the boot-time
`scene cache 0.00G in 0 scene(s)` — and the real figure is 10.9 %.

## The cheap fix that is not a GPU: the disk is smaller than the working set

`vastctl.DEFAULT_DISK_GB` was 30, leaving a ~23 GB scene cache. Five ~5 GB film
scenes rotate through it, so **the working set does not fit and scenes evict and
re-push each other all day** — 8 of 19 expensive switches were re-pushes of a
scene the box had already had. Disk is $0.20/GB/month: **80 GB costs ~$0.022/hr,
less than one re-push of a 5 GB scene, per hour, forever.** Now
`VASTRENDER_DISK_GB`, default unchanged; broker 2 rents 80 GB. Exclusive supply
is completely insensitive to it — 8 offers at `disk>45`, `disk>75` and
`disk>95` alike.

**Raising broker 1's disk is the single best remaining spend on this farm and it
has not been done** (it needs a re-rental, which needs a restart in a gap).

## Price the card post-filter, never on the headline

Cheapest *exclusive* 5090 meeting the full production filter on 2026-08-04 was
**$0.4681/hr**, not the ~$0.45 assumed — plus $0.0219/hr for the 80 GB disk =
**$0.490/hr all-in**. `cpu_cores_effective>=32` is already in `build_query`, so
shopping happens post-filter and the "$0.3936/hr exclusive with 20 effective
cores" trap cannot recur. `gpu_frac>=0.99` likewise.

**Note what this exposed: broker 1's own card is `gpu_frac 0.125`** — a shared
eighth of a box, the exact R2-382 co-tenant class, rented before the
exclusivity preference landed and never replaced. It is $0.4203/hr against
$0.490 for an exclusive card with 2.7x the disk.
