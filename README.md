# vast-render

Offload Blender Cycles rendering from the local GTX 1070 to an on-demand
vast.ai RTX 5090, via a local broker that keeps **one warm GPU** and feeds it
jobs from many agent clients.

Kept deliberately separate from `~/opus5-car-render` — this is infrastructure,
that is the artwork. The scene is an input here, not a dependency.

## Why

| | local GTX 1070 | vast RTX 5090 |
|---|---|---|
| VRAM | 8 GB → 4K renders **tile** | 32 GB → 4K in one pass |
| RT cores | none (OptiX via shaders) | 4th-gen hardware RT |
| measured | 4K/170 samples ran **>26 min** | est. 2–8 min |

Cost: **$0.326/hr**, billed per second while running.

## Settled decisions

**Assembly stays local.** Procedural `bpy` geometry construction is
single-threaded Python — no GPU path exists (Geometry Nodes evaluation is also
CPU-bound), and the remote EPYC 7K62 is ~1.5× slower per-core than the local
i7-7700K. Assembly would be *worse* remotely on both axes.

**Ship the assembled blend, not the source.** Follows from the above.
`zstd -19 -T6` takes 285 MB → **62.9 MB** (4.53×). rsync delta sync is useless
here — measured only **14.7%** match between revisions, because Blender rewrites
the whole file on save and embeds pointer addresses that shift every run.
Never enable Blender's "Compress File" preference; a zstd-wrapped blend would
defeat external compression.

**The compression level is chosen per scene, not fixed at 19.** The figure
above is a *ratio*, but the thing being optimised is time-to-GPU, and at 19 the
**compressor is the bottleneck, not the link**. Caught live: eight minutes into
a 4.22 GB push, `zstd -19` was at 190% CPU and the receiving `ssh` at 0.0% — it
fed the wire 1.3 MB/s over a link that does 4+, so a rented GPU idled while the
network ran at a third of capacity. Default is now **10**, which puts the
bottleneck back on the wire. The unambiguous case is a scene that arrives
**already compressed** (Blender's "Compress File" left on, or packed EXR/PNG
textures): it gets level 1, because there is no ratio to win and `-19` was
measured spending **59 s to achieve 1.06×** on one 602 MB blend.
`remote.scene_zstd_level` decides and logs which level it picked and why; see
`config.SCENE_ZSTD_LEVEL` for the measurements. More SSH streams do **not**
help — `push_blender` measured 4.02 MB/s parallel against 4.68 MB/s single —
so payload size is the only lever the broker has on the wire.

**No dedup.** Two agents requesting identical parameters get two renders. A
params-only hash cannot see scene state, so dedup would silently serve stale
frames across a reassembly — the exact failure `render_all.sh` warns about.
Job IDs are UUIDs. An opt-in `--reuse` flag may exist later; never automatic.

**Warm worker, not cold-per-job.** `scene.render.use_persistent_data = True`
(note: `render`, not `cycles`) collapses per-render overhead:

| | measured on the 1070 |
|---|---|
| first render — sync + BVH + OptiX pipeline | 23–32 s |
| subsequent, persistent data **off** | 22–29 s each |
| subsequent, persistent data **on** | **0.6–2.2 s** |

15–40× on fixed cost. RSS stayed flat at 2.29 GB over 20 renders.

**Blender binary, not pip `bpy`.** The wheel does ship `kernel_sm_120.cubin`
and works on a 5090, but installs no signal handlers — meaning **no crash
logs**. Not worth the ergonomics.

**On-demand, not interruptible.** Preemption mid-render costs more than the bid
spread at $0.33/hr.

## Architecture

```
50 agents ──HTTP──> BROKER (local)                WARM WORKER (vast 5090)
                    ├── FastAPI + SQLite WAL      ├── blender -b scene.blend -P server.py
                    │   single uvicorn process    ├── serial loop, main thread only
                    ├── dispatcher (asyncio)      ├── use_persistent_data = True
                    ├── vast poller (10 s)        ├── OPTIX_CACHE_PATH persisted
                    └── heartbeat ────────────────┴── watchdog: self-destruct if stale
```

Agents **never** touch the vast API — it rate-limits per endpoint *and per
client IP*, 429s with no `Retry-After`, and thresholds are unpublished. One
poller, 50 clients served from cache.

## Quick start

```bash
# 1. start the broker, pointed at the locally assembled scene
VASTRENDER_SCENE=~/opus5-car-render/work/f1_complete.blend \
  .venv/bin/python -m broker.app &

# 2. render — rents a GPU on the first job, returns the PNG
./rq render --cam CAM_HeroLow --res 3840 2160 --samples 220 -o hero.png

# 3. watch cost and queue
./rq status

# 4. emergency stop, works even with the broker dead
scripts/panic.sh
```

The GPU destroys itself 10 minutes after the last job. Nothing is left running.

## Animations

One job is a frame **range**, not a frame. The scene uploads once and stays
resident for the whole range; frames land in one directory; `--name` is the
resume key.

```bash
./rq anim --name beat3 --scene beat3.blend --cam CAM_Oner \
          --res 3840 2160 --samples 512 --frames 620-980
./rq anim --name beat3 ... --frames 701,744-745    # just the holes
./rq seq status beat3     # present / missing / corrupt, by frame number
./rq seq verify beat3     # same, re-hashing every file
./rq budget --set 40      # raise the cumulative spend cap, no restart
```

`--frames` takes `A-B`, `A-BxN`, a bare number, or a comma-separated list of
those — the same syntax `rq seq status` prints for missing frames, so its output
pastes straight back in.

Re-submitting the same name renders only what is absent — and "absent" is
re-checked against the files on disk every pass, so deleting or corrupting a
frame forces exactly that frame to re-render, **including a frame edited in
place to the same length**: the file's mtime is compared against the moment its
row was written. A frame counts as delivered only once it has been fetched, its
sha256 matches the digest the worker computed when it wrote it, **and there is a
picture in it**.

The projected **local disk** is printed beside the projected cost. The instance
deletes each frame as its fetch verifies and so cannot fill; this machine
accumulates all of them, at ~34 MB a 4K frame.

`rq anim` defaults to `--dof scene`: use the .blend's own depth of field
including its animation, because overriding an animated focus pull is how a
round-1 render came back wrongly blurred. A frame whose physics cache does not
cover it is refused rather than rendered, because Blender does not fail on a
missing cache — it simulates, and produces a different image.

The projected cost is printed before anything is rented.

## Blank frames

Every returned PNG is decoded and measured — mean, standard deviation, range,
distinct luminance levels, alpha — then classified `OK`, `SUSPICIOUS`, `BLACK`,
`UNIFORM` or `TRANSPARENT`. The last three fail the job, terminally, unless the
caller passes `--allow-blank`. `SUSPICIOUS` is reported loudly and never fails
anything: a dark frame is a thing artists make on purpose, and a check that
refuses legitimate work gets switched off.

This exists because job `0908e534b1d3` returned an 8,734-byte 640x480 PNG with a
valid signature, an IEND chunk, exactly the requested dimensions and a matching
sha256 — and it was **entirely black**, mean 0.00000, sd 0.00000. Every check
the farm had verified the *file*. None could see the *image* had nothing in it.
In a 3,000-frame single unbroken take, one such frame passes verification,
counts as delivered, survives every resume as "already done", and is discovered
when the finished video is watched. See [docs/incidents.md](docs/incidents.md).

```bash
./rq seq verify shot01              # re-hashes AND re-measures every frame
./rq seq stats  shot01 --sort sd    # every frame's pixels, flattest first
```

A sequence also gets a *relative* check that no fixed threshold can make: each
frame is compared against a rolling window of its 25 neighbours, so one dropped
frame at 1,600 is flagged while a fade — which walks the whole neighbourhood
down together — is not.

## Layout

    rq         agent-facing CLI — submit, collect, status   [stdlib only]
    broker/    FastAPI + SQLite queue, dispatcher, fleet
    worker/    warm Blender server, deployed to the instance
    vastctl/   instance lifecycle — search, create, reap, destroy
    scripts/   panic button, offer probe
    scenes/    test scenes built by scripts here (blank_probe.blend)
    docs/      agent guide, operations runbook, protocol
    state/     sqlite db, logs, heartbeat   (not for version control)
    out/       returned renders

## Docs

- **[docs/agents.md](docs/agents.md)** — for agents building the car. Start here
  if you just want renders.
- **[docs/operations.md](docs/operations.md)** — runbook for whoever owns the
  money: safety commands, host selection, tuning.
- **[docs/protocol.md](docs/protocol.md)** — job spec reference and wire format.

## Status

Everything is built and tested. **No instance has ever been rented — $25.00
credit untouched.**

| | |
|---|---|
| `vastctl` | verified against the live vast.ai API |
| warm worker | 25 real jobs on the GTX 1070 — 7/7 tests |
| broker | 13/13 tests — queue semantics, HTTP, crash recovery |

Warm-worker effect measured on a 6.2 MB scene: **20.8 s cold, then 8.5 s and
7.4 s**. The research measured 23-32 s down to 0.6-2.2 s on a 208 MB scene, so
the 285 MB assembly should gain considerably more.

Four independent paths destroy an instance, so no single failure strands one:
idle timeout, broker shutdown, the in-container watchdog, and `panic.sh`.

**Untested until first rental:** provisioning on a real instance, the 5090
render path, and cold-start timing. The calibration render below is what closes
that gap.

## Next: calibrate

One 4K frame on a real 5090 replaces the 2-8 min/frame estimate with a measured
number, and tells you what 900 frames actually cost before committing. About
five cents.

```bash
VASTRENDER_SCENE=~/opus5-car-render/work/f1_complete.blend \
  .venv/bin/python -m broker.app &
time ./rq render --cam CAM_FrontQuarter --res 3840 2160 --samples 220 -o calib.png
./rq status          # spend for the session
./rq teardown        # or let the 10-minute idle timer do it
```

## Credentials

| what | where |
|---|---|
| vast API key | `~/.config/vastai/vast_api_key` (0600) |
| SSH key | `~/.ssh/id_vast_render` (ed25519, no passphrase — broker is unattended) |
| CLI | `vastai` 1.5.0 via `uv tool install`, on PATH at `~/.local/bin` |

Account 627622. **Credit $25.00, balance $0, autobilling appears off** — which
is the correct safe posture: prepaid credit is the only hard spend ceiling
vast.ai offers. Verified against the CLI source: there is no API-level spend cap,
no `--ttl`, no `--end-date`, and `destroy` is not schedulable.

> The API key was pasted into a chat transcript on 2026-07-26. Rotate it once
> the build settles.

## Safety rails (all mandatory)

1. `--cancel-unavail` on every create. Without it a failed schedule silently
   creates a **stopped** instance that still bills storage — a built-in orphan
   generator.
2. **Destroy, never stop.** Storage bills while an instance *exists*, running or
   not. `stop` only ends the GPU meter.
3. On-instance watchdog self-destructs via the injected `CONTAINER_API_KEY`
   (scoped to that instance alone) if the broker heartbeat goes stale. This is
   the only thing that survives broker crash or local power loss.
4. Label every instance `renderbroker-<runid>`; reap orphans by label at broker
   startup, *before* creating anything.
5. Hard wall-clock cap per instance, enforced in broker *and* watchdog.
6. `panic` — destroy everything labelled, idempotent, runnable with broker dead.
7. Destroy on every exit path: `try/finally` + `atexit` + SIGTERM/SIGINT, then
   re-poll until the ID is gone.

## Hands off

**Nothing under `~/opus5-car-render` is ever edited by this project.** Not
`tools/*.py`, not `build/*.py`, not the blends. Read-only, always. Any renderer
code lives in `worker/` here. Test fixtures live here too — never in the scene
project.

## The worker is a dumb executor

Render policy belongs to the agents building the models. vast-render owns
*transport and lifecycle*, nothing else. It has **no opinion** about resolution,
samples, cameras, denoiser, bounces, or exposure — every one of those arrives in
the job payload, per job, and is applied explicitly.

Consequences:

- **No defaults baked in on the vast side.** A job that omits a field is a bug in
  the client, not something the worker guesses at. Reject incomplete specs
  loudly rather than silently substituting.
- **Every parameter is set on every job.** Not just the ones that changed. This
  is also what makes the warm worker safe — state cannot leak from job N-1 into
  job N.
- **Cameras are discovered, not configured.** At scene load the worker
  enumerates every `CAMERA` object in the blend and pre-touches each. No
  hardcoded list to drift out of sync with what the agents actually build.
- **Pre-warm covers feature-set variants too.** The measured 9.5 s first-camera
  cost was an OptiX pipeline rebuild for a new kernel feature set (that camera
  had DOF). So warm-up sweeps the combinations — DOF on/off, border on/off,
  each denoiser — not just the camera list.

## Known constraints in the scene code

*(Observed read-only. These are worked around in `worker/`, never fixed in place.)*

- **`tools/render.py` is not idempotent.** `--nodof` sets `use_dof = False` and
  never restores it; `use_border`, `exposure`, `film_transparent` and `denoiser`
  all persist too. Harmless per-process, silently wrong in a warm worker — job N
  would inherit job N-1's state. Reason `worker/` sets everything, every job.
- **`tools/exploded.py` cannot share a warm worker.** Toggling `hide_render` on
  one object measured **15.1 s** — any geometry change invalidates the BVH that
  `use_persistent_data` exists to cache. Geometry-mutating jobs get their own
  process, or accept the cost.
- **`render.py` sets `denoiser` but never `denoising_use_gpu`** — that leaves
  OpenImageDenoise on the CPU. `worker/` sets it; the scene file keeps its own
  behaviour for local runs.
- **Camera switching costs 9.5 s the first time, 0.97 s after.**

## Open questions

- Animation frames are supported now (see above), and `persistent_data` is a
  per-job field rather than an assumption. The cost question it was raised
  against still stands: if only the camera moves, persistent data holds and each
  frame costs little overhead; if geometry moves, every frame pays a BVH rebuild.
  `rq anim` projects the cost from measured frame times rather than guessing, so
  the answer arrives as a number before the batch, not after it.
- Local assembly wall time — no longer architecture-deciding, but sets how long
  a batch waits before dispatch.
- Calibration render on a real 5090 to replace the 2–8 min/frame estimate range
  with a measured number. ~$0.05.

## EXEC jobs — the CPU half of the farm

`rq render` sends a frame to the GPU. `rq exec` sends a **Blender process** to
the same rented box's CPUs — item builds, gates, placement and collision passes
— and runs several of them at once.

    ./rq exec --root /home/zany/f1-round2 \
              --include 'world/*.py' --include 'world/items/*.py' \
              --include 'tools/*.py' --include 'docs/*.json' \
              --entry tools/item_build.py \
              --arg --item --arg kerb_precast_unit \
              --output gate.json --timeout 1800 --wait

**What it is measured to be worth: 3.65x — but only on a box that meets the
spec.** 48 units of 24 real wave-1 item modules, an identical unit both sides:

    local   i7-7700K            6 CPU     4 slots   2182 s    75.9 items/h
    remote  Threadripper 3960X  46 CPU   12 slots    598 s   276.9 items/h   3.65x

    the same lever, measured 2026-08-04 on a 23-CPU cgroup:
    remote  EPYC 7R32           23 CPU   12 slots   1184 s   158.1 items/h   1.66x

The adoption bar is **2x, and the first measurement missed it at 1.68x on
hardware with half the CPU the plan assumed.** Re-run 2026-08-14 on 46.08 cgroup
CPUs — verified on the box, not read off the offer — it clears the bar by 80 %.
`docs/operations.md` records both runs, the conditions of each, and the three
findings the re-run reversed. **The CPU floor is the whole result: at 23 CPUs
this lever is not worth adopting and at 46 it is.** Beyond that, the three
things it is uniquely good at are real:

  * **the `.blend` is born where the render happens.** Across 553 broker jobs
    against item scenes, 7,687 s of rendering sat inside 40,737 s of job — 81 %
    not rendering, almost all of it pushing assembled blends up a 4-5 MB/s
    uplink. An item built and gated remotely never makes that trip.
  * **it does not consume the local machine.** Six cores stay free.
  * **it has 90 GiB of container memory**, against 11 GB and a swap file here.

An exec job ships **code, not blends** — 7.9 MB of Python, content-addressed,
pushed once per wave. See `docs/protocol.md` for the schema, `docs/agents.md`
for the flags, `docs/operations.md` for the runbook and the A/B.

Two processes, two ports, two dispatchers, one instance:

    worker/server.py       :8799   ONE render at a time   (Blender's never-thread law)
    worker/exec_server.py  :8800   EXEC_SLOTS builds      (never imports bpy)
