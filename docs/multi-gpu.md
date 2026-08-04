# Multi-worker instances — approved in principle, not yet built

**Status 2026-08-04: APPROVED IN PRINCIPLE, NOT SCHEDULED. Do not rent an
8-GPU box for interactive work under any circumstances.** This exists so the
next person to price it does not re-derive it, and so the one thing that would
make it a *regression* is written down before anyone starts.

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
