# Using the render farm from an agent

For agents building and inspecting the F1 car. You submit a render, a GPU
appears if one isn't already up, your PNG comes back, and the GPU disappears
when everyone stops asking.

**You decide everything about the image.** Camera, resolution, samples,
denoiser, bounces, exposure, crop — all yours, per job. The farm has no render
policy; it owns transport and the GPU's lifecycle, nothing else.

## One-time: is the broker up?

```bash
cd ~/vast-render && ./rq status
```

If it says it can't reach the broker, start it:

```bash
cd ~/vast-render
VASTRENDER_SCENE=/path/to/assembled.blend .venv/bin/python -m broker.app &
```

The scene is the **locally assembled** `.blend`. Assembly stays on this machine
— procedural `bpy` geometry is single-threaded Python with no GPU path, and the
rented CPU is slower per-core than this one.

## Render something

```bash
./rq render --cam CAM_HeroLow --res 3840 2160 --samples 220 -o hero.png
```

That blocks until the PNG lands in `hero.png`. Drop `-o` to get a job id back
immediately and collect it later:

```bash
./rq render --cam CAM_FrontQuarter --res 1920 1080 --samples 128
# -> 7f3a91c2b004  queued (depth 1)

./rq get 7f3a91c2b004 -o front.png
```

## Pixel-peeping a region

`--zoom` multiplies pixel density *before* cropping, so the result is real
extra detail rather than an upscale. To inspect the front wing endplate at 7×:

```bash
./rq render --cam CAM_HeroLow --res 1920 1080 --samples 220 \
            --border 0.66 0.90 0.48 0.80 --zoom 7 --nodof -o endplate.png
```

`--border` is `MINX MAXX MINY MAXY`, normalised, origin at the **bottom-left**.

## Every flag

| flag | meaning |
|---|---|
| `--scene NAME` | which `.blend` to render; bare names resolve against the broker's scene root. Omit for the broker's default scene |
| `--cam NAME` | camera object in the scene (required) |
| `--res W H` | base resolution (required) |
| `--samples N` | Cycles sample ceiling (required) |
| `--engine` | `CYCLES` or `BLENDER_EEVEE` — **not** `BLENDER_EEVEE_NEXT`, renamed away in Blender 5.0 and rejected |
| `--denoiser` | `OPENIMAGEDENOISE`, `OPTIX`, `NONE` |
| `--no-denoise-gpu` | force CPU denoising |
| `--nodof` | disable depth of field |
| `--film-transparent` | transparent background |
| `--border MINX MAXX MINY MAXY` | normalised crop |
| `--zoom N` | pixel density multiplier before crop |
| `--exposure F` | override view exposure |
| `--max-bounces N` | override light bounces |
| `--adaptive-threshold F` | noise threshold (default 0.01) |
| `--prio N` | lower runs sooner (default 100) |
| `--agent NAME` | who's asking — drives fair-share |
| `--allow-blank` | accept a render with no picture in it (see below) |
| `--wait` | block until finished |
| `-o FILE` | save PNG here (implies `--wait`) |

## Every render is measured

Every returned PNG is decoded and its luminance measured, and the numbers are
printed whether or not anything is wrong:

```
7f3a91c2b004  done in 182.7s -> ~/vast-render/out/7f3a91c2b004.png
          image     OK  mean 0.5173  sd 0.1686  range 0.082-1.000  234 levels
```

**Why.** On 2026-07-28 job `0908e534b1d3` reported `done` in 33.2 s and returned
an 8,734-byte 640x480 PNG with a valid signature, an IEND chunk, the requested
dimensions, and a sha256 matching the digest the worker computed for it. It was
entirely black — mean 0.00000, sd 0.00000, one distinct luminance level across
307,200 pixels. Every check the farm had verified the FILE. None of them could
see that the IMAGE had no content in it.

The verdict is one of:

| verdict | meaning | job |
|---|---|---|
| `OK` | there is a picture in it | delivered |
| `SUSPICIOUS` | almost no variation (sd ≤ 0.02), or a megapixel image on ≤ 16 luminance levels | delivered, reported loudly |
| `BLACK` | mean and standard deviation both ~0 | **FAILS** |
| `UNIFORM` | one flat colour at any brightness | **FAILS** |
| `TRANSPARENT` | alpha is zero everywhere — `--film-transparent` with nothing composited behind it | **FAILS** |
| `UNREADABLE` | the file will not decode | delivered, reported loudly |

A job failed for being blank is **not retried**. Every other failure the broker
sees might be transport; a camera aimed at empty space renders black three times
for three times the money. The PNG is kept where it landed so you can look at it.

If the frame is genuinely meant to be blank — a fade, a black plate, a shot into
deep shadow — pass `--allow-blank`. It suppresses the refusal, never the report.

`SUSPICIOUS` never fails a job. A dark or low-contrast frame is something people
make on purpose, and a check that refuses legitimate work gets switched off,
after which it protects nothing.

## Rendering an animation

A shot is not a pile of stills. One job covers a whole **frame range**, the
scene stays resident for all of it, and the frames land in one directory that
ffmpeg can read:

```bash
./rq anim --name beat3_breach --scene beat3.blend \
          --cam CAM_Oner --res 3840 2160 --samples 512 \
          --frames 620-980 --agent breach
```

That prints the projected cost **before** anything is rented:

```
a41f9c2b7e10  queued — sequence beat3_breach
          frames 620-980  (361 total)
          0 already delivered, 361 to render
          scene beat3.blend (99d8f30a6e51)  spec 23773a092160b4b5
          -> ~/vast-render/out/seq/beat3_breach
          PROJECTED COST $14.82 — 361 frames x 412s = 41.3 GPU-hours ...
```

`--name` is the **resume key**, not decoration. Re-submitting the same name and
range renders only the frames that are absent:

```bash
./rq anim --name beat3_breach ... --frames 620-980     # renders what is missing
```

"Absent" means *absent or not verifying*. Every planning pass re-checks the file
on disk, so deleting a frame is a supported way to force one re-render, and a
frame that got corrupted after it landed is picked up rather than trusted.

### `--frames` takes lists, not only ranges

```
620-980        a range
620-980x2      every second frame
1600           one frame
701,744-745    a list of any of those
```

The list form exists because the holes in a shot are not contiguous. It is the
**same syntax `rq seq status` prints**, so the answer to "what is missing?" can
be pasted straight back into the command that fixes it:

```
MISSING   3 frame(s): 701, 744-745
```
```bash
./rq anim --name beat3_breach ... --frames 701,744-745
```

The broker echoes the frame set it parsed before anything is rented — a list
read as a range would render 45 frames nobody asked for, into a sequence whose
spec they match, so nothing downstream would ever flag them.

### Where the frames land, and whether they fit

`rq anim` prints the local disk arithmetic beside the cost, because they fail
the same way if nobody looks:

```
local disk: 102.1 GB needed (2978 x 34.3 MB), 84.2 GB free at .../out/seq/beat3
!! IT DOES NOT FIT. Room for 2455 of 2978 frames — the batch fills this disk
!! and every frame after that fails on write, hours or days in.
```

The **instance's** disk cannot fill: each frame is deleted there the moment its
fetch verifies, so no more than one is ever on the box. Every byte accumulates
**here**, and a 4K master is ~34 MB a frame. Free the space, or point
`VASTRENDER_SEQ_DIR` at a volume that can hold it, before starting.

It is a warning, not a refusal — you may be about to free space, or moving
frames off as they land. It is never silent.

### Depth of field: `--dof scene` is the default here

For a still, `--nodof` off/on is fine. For an animation whose camera has keyed
focus, **overriding DOF destroys the focus pull** — round 1 lost a render
exactly that way, to a submission that merely forgot `--nodof`. So `rq anim`
defaults to `--dof scene`: use the .blend's own depth of field including its
animation. `--dof on` / `--dof off` are still there when you mean it.

### Simulation caches

Bake them, and bake them to a **disk** cache. The broker ships any
`cache`, `sim`, `textures` (etc.) directory sitting beside the .blend into the
scene's directory on the instance, so `//`-relative references resolve there
exactly as they do here.

`blendcache_X` is special: Blender derives that name from the .blend's own
filename, so it travels **only with `X.blend`** and not with the other blends in
the same directory. That matters where the round-2 assemblies live —
`render/world/assembly/r2/` holds five 4.2 GB .blends in one directory, and
attaching one destruction bake to all five would put five copies of it in a
scene cache with an 8 GB budget on a 32 GB disk. Keep a bake's name matching its
blend, or put it in a generically-named directory if you really do want it
shared.

If a cache does not cover a frame, the job **refuses that frame** rather than
rendering it. That is deliberate: Blender does not fail on a missing cache, it
simulates — and a simulation reached by jumping to frame 700 is not the one that
was baked. The refusal names the cache and why:

```
frame 700 refused: physics caches do not cover it ... RIGIDBODY
scene.rigidbody_world: disk cache directory /workspace/scenes/<hash>/blendcache_beat3
holds no .bphys files on this machine — the bake did not travel with the scene
```

`--no-require-caches` overrides it. The reply still reports the problem, so a
frame rendered that way is at least identifiable afterwards.

**Blender 5.2 bug, measured:** setting
`scene.rigidbody_world.point_cache.use_disk_cache = True` from Python
**segfaults Blender**. Cloth and particle caches are fine. Tick the rigid-body
world's *Disk Cache* box in the UI instead, then bake.

### Checking on a sequence

```bash
./rq seq list
./rq seq status beat3_breach          # cheap: structure + size of every frame
./rq seq verify beat3_breach          # deep: re-hash AND re-measure every file
./rq seq status beat3_breach --frames 620-700
./rq seq stats  beat3_breach --sort sd   # every frame's pixels, flattest first
```

It never says a bare "done". Missing, corrupt and blank frames are listed by
number:

```
image     OK 2975  SUSPICIOUS 2  BLACK 1
MISSING   3 frame(s): 701, 744-745
BLANK     1 frame(s) have no picture in them: 1600
            1600: BLACK: the image is black: mean 0.00000, sd 0.00000, 1 level
CORRUPT   1 frame(s): 812
            812: sha256 4a1c8e02f11b != recorded 9f1c2d55ab30
ODD       1 frame(s) do not match their neighbours: 1600
            1600: much darker than its neighbours: mean 0.0000 vs 0.5324 median
                  over 24 frames (333 MADs)
```

Exit code is 1 if anything is missing or bad, so a script can gate on it.

**A blank frame is never counted as delivered.** That is the resume-poisoning
case and the most dangerous one: a `done` row makes every future re-submission
skip the frame — "already delivered" — so a hole in a cut-free shot survives
every retry and only appears when the whole thing is assembled and watched.

**BLANK vs ODD.** `BLANK` is an absolute judgement about one frame, and it fails
that frame. `ODD` is relative: the frame verified, but its statistics do not
belong with its neighbours'. That is the stronger signal in a single unbroken
take — a fixed threshold cannot see that frame 1,600 is nothing like 1,590-1,610
— and it is deliberately advisory, because a fade walks a whole neighbourhood
down to black together and must not be flagged. Each frame is compared with a
rolling window of 25 neighbours using median and MAD, and needs both a robust
z-score past 8 and an absolute deviation worth re-rendering for.

**Frames delivered before this check existed** report as `UNMEASURED`. They are
not counted as bad; they have not been looked at. `rq seq verify` measures them.

**A frame edited after it was delivered is not a delivered frame.** `status`
compares each file's modification time against the moment its row was written,
so a frame changed in place — even to exactly the same length, with the same
dimensions, still a structurally perfect PNG — is stale and re-renders:

```
4: modified 72s AFTER it was recorded delivered — the file on disk is not
   the one that was fetched and verified
```

This is what a resume can afford to check. The sha256 that would also catch it
runs only under `--deep`, and re-hashing a 2,978-frame 4K master is 100 GB of
reads on every planning pass. A consequence worth knowing: `touch` on a frame
forces exactly that frame to re-render, the same way deleting it does.

`rq seq stats` is the dump for finding one bad frame in 3,000 — every frame's
mean, standard deviation, range, level count and a 16-bucket tone histogram, in
frame order or sorted so the flattest are first. `--csv` for a spreadsheet.

It also warns when a sequence is not internally consistent — mixed frame sizes,
or frames rendered from more than one spec. For a single unbroken shot that is a
seam, and a seam is a defect even when every individual frame is perfect. The
broker refuses to *create* that situation: submitting a range into a sequence
that already holds frames from a different spec or a different .blend is a 409.

### Money

```bash
./rq budget                 # cap, spend, and vast.ai's own credit figure
./rq budget --set 40        # raise the cumulative cap without restarting
```

The cap is cumulative across every instance the broker has rented, persisted,
and checkpointed every minute — so it survives the `kill -9` that restarting a
broker requires. Hitting it pauses the queue rather than stopping mid-frame.

A batch that outlives 12 hours will be interrupted: the in-container watchdog
retires an instance at that wall-clock cap regardless of what it is doing. That
costs a cold start (~10 min) and the frame in flight; everything delivered is
recorded, and the job resumes on new hardware without re-rendering any of it.
The cost estimate accounts for those restarts.

## Rendering different scenes

Jobs carry their own scene, so several agents can iterate on different variants
through one broker and one GPU:

```bash
./rq render --agent ghost   --scene f1_ghost_posed_hq.blend    --cam CAM_HeroLow ...
./rq render --agent explode --scene f1_exploded_posed_hq.blend --cam CAM_HeroLow ...
./rq render --agent room                                        --cam CAM_HeroLow ...  # default scene
```

Omit `--scene` and you get the broker's default, exactly as before.

The worker holds one scene at a time and switching costs ~40-60 s, so the queue
**batches by scene**: it drains the loaded scene, then switches to whichever
scene has the oldest waiting job. Your job is never deferred indefinitely — a
scene that has waited too long pre-empts the batch. Fair-share between agents
still applies within each scene.

Practical consequence: submitting ten jobs for one scene together is much
cheaper than alternating scene by scene. `rq status` shows what is loaded and
what is waiting per scene.

Scene names are validated against the broker's scene **roots** — a list, not one
directory, so several projects can be served at once:

    ~/opus5-car-render/work      round 1
    ~/f1-round2/world            round 2
    ~/f1-round2/render
    ~/vast-render/scenes         test and preview scenes

Set `VASTRENDER_SCENE_ROOTS` (colon-separated) to override the list wholesale;
the defaults live in `broker/config.py` and are included only when the directory
exists. Anything resolving outside every root is rejected with HTTP 400 at
submit time — resolved first, symlinks and `..` included, then checked for
containment, so `~/f1-round2-evil/` does not slip past on a prefix.

## Running many agents at once

Pass `--agent` so the queue can be fair. Ordering considers each agent's recent
service first, so an agent that just got ten renders sorts behind one that got
none — fifty agents share the GPU instead of the loudest winning.

```bash
./rq render --agent wing-inspector --cam CAM_HeroLow --res 1920 1080 --samples 96
```

Limits, so one agent can't swamp the rest:

- 25 queued jobs per agent
- 200 jobs in the queue overall

Past either you get **HTTP 429 with a `Retry-After`**. Submitting again later is
safe — every submit creates a distinct job.

## Two identical requests give two renders

There is no dedup, on purpose. A parameter hash cannot see *scene state*, so
reusing an earlier render would silently hand you a stale frame after the car
was reassembled. If you asked for it, it gets rendered.

The cost is that GPU time scales linearly with job count — 50 jobs is 50
renders. Prefer fewer, deliberate renders over spraying variations.

## Checking on things

```bash
./rq status          # queue, GPU state, spend so far
./rq status -v       # plus the last 15 jobs
./rq cancel <id>
```

`cancel` on an **exec** job now stops the child on the instance, not just the
row: the broker asks the exec server to kill that job's process group, the slots
are released, and the job is not retried. The reply carries an `exec` section
saying whether the child was actually signalled — if the box was unreachable the
row is still cancelled, and the child is collected by `reap_orphans` when the
exec server next starts. A `render` cancel is unchanged.

`status` reports live spend for the session, so you can see what a batch is
costing while it runs.

It also reports the **instance's disk**, because a scene cache quietly filling
it is how a 30-hour run dies, and finding that out used to require sshing in and
running `du`:

```
disk     8.7G used of 30.0G (29%)  free 22.9G   cache 7.76G in 18 scene(s) (budget 8.0G)  measured 41s ago
```

`cache` is every scene the instance is holding. It is bounded and evicted
least-recently-used, so it should sit near its budget and stop, not climb. Two
lines you should act on:

```
DISK LOW free 1.42G is under the 2.0G reserve — the next scene upload may be refused
disk     UNKNOWN — <why the measurement failed>
```

`UNKNOWN` is deliberately printed rather than omitted. A disk nobody could
measure is a thing to know about; a blank line reads as "fine".

A running job reports live progress, so a long frame is distinguishable from a
stuck one:

```
running  eb1fe5e0252d  finals  3712/8192 (45%)  12m31s elapsed, ~22m08s remaining
```

The ETA is Blender's own estimate. It updates at Cycles' adaptive-sampling
checkpoints rather than once per sample, so on a big frame it can sit still for
a while and then jump — that is normal, not a stall. If it really does stop, the
broker logs a loud warning after `STALL_WARN_SEC` and leaves the job alone for a
human to judge.

`rq get` tells you *which* kind of not-finished you have, by exit code:

| exit | meaning |
|---|---|
| 0 | downloaded |
| 1 | the job failed or was cancelled (reason on stderr) |
| 3 | still queued or running — add `--wait` to block instead |

```bash
./rq get <id> -o hero.png            # exits 3 if it is not ready yet
./rq get <id> -o hero.png --wait     # blocks, printing progress, until it lands
```

## What happens under you

First job after an idle period pays a one-time startup — renting (~1-5 min),
installing Blender, uploading the scene (285 MB compressed to ~63 MB), loading
it, and pre-warming the OptiX pipeline for every camera. Expect several minutes
before the first PNG.

After that, jobs are fast: the scene stays resident and BVH data is reused, so
per-job overhead drops from ~25 s to a couple of seconds.

Five minutes after the last job the instance is **stopped** — GPU billing ends,
the disk (Blender + scene) survives at ~1.4 ¢/h, so a job inside the next hour
wakes it in seconds instead of re-paying the cold start. An hour stopped and it
is **destroyed**. Nothing is left running or billing.

## If something breaks

A failed job shows its error in `./rq status -v`. Jobs retry up to 3 times, then
stay `failed` so you can see them — they are never silently dropped.

**A job is never marked `failed` while the instance is still rendering it.**
Before the queue records that verdict the broker asks the instance what it is
doing, and if your frame is in flight — or its PNG is already on the box — the
job is requeued instead, without spending an attempt. So a `failed` row means
the render really did not happen, not that a connection blinked. You may also
see your job wait behind someone else's frame:

```
job <id> is queued behind job <other>, which the instance is still rendering
job <id> is ALREADY RENDERING on the instance — reattaching to it
```

Both are the broker protecting work in progress. Neither needs action; the job
keeps its place and returns normally.

If the broker pauses (spend cap, no credit, no GPUs available), `status` shows
the reason. Clear it with `./rq resume` once the cause is fixed.

To force the GPU down without stopping the broker: `./rq teardown`.

**Emergency, works even if the broker is dead:**

```bash
scripts/panic.sh
```

---

## Running a BUILD on the farm — `rq exec`

**Read the measurement before you reach for this.** `rq exec` runs
`blender -b -P <script>` on the rented box's CPUs, several at a time, and it
works — but on the hardware this farm actually rents it is **1.7x** the local
machine's build throughput, not the 3-5x the plan that motivated it predicted.
Measured on 52 units of 26 real wave-1 item modules, an identical unit of work
both sides:

| | slots | 52 items in | items/hour |
|---|---|---|---|
| local, i7-7700K, 6 cores | 4 | 1964 s | 95.3 |
| remote, EPYC 7R32 | 12 | 1184 s | 158.1 |
| remote, same box | 20 | 1170 s | 160.0 |

The remote box does not scale with slots: mean per-item wall clock is **80 s at
1-way, 206 s at 12-way, 289 s at 20-way**, so throughput plateaus near 160
items/hour however the slots are set. Its cgroup allows 23.04 CPUs and 90.5 GiB
— not the 32 cores and 515 GB the sizing assumed.

So this is a real but modest lever, and it is worth using for what it is
uniquely good at rather than as a blanket policy:

  * **it does not consume the local machine**, which matters when agents need
    the six cores for anything else;
  * **the `.blend` is born where the render happens**, so an item that is built
    and gated remotely never pushes a multi-hundred-megabyte assembly up a
    4-5 MB/s uplink. Across 553 broker jobs against item scenes, 81 % of in-job
    wall clock was not rendering, and that transfer was most of it;
  * **it has 90 GiB**, against 11 GB and a swap file here.

Ship code, not blends: the whole input for an item build is `world/*.py`,
`world/items/*.py`, `tools/*.py` and a manifest — 7.9 MB, pushed once per wave
and cached — and nothing else goes up.

```bash
./rq exec --root ~/f1-round2 \
          --include 'world/*.py' --include 'world/items/*.py' \
          --include 'tools/*.py' --include 'docs/*.json' \
          --entry tools/build_item.py \
          --arg --item --arg kerb_precast_unit \
          --arg --save --arg tmp/kerb_test.blend \
          --arg --gate --arg out/gate.json \
          --output gate.json \
          --timeout 1800 --agent kerb --wait
```

### The rules, and why each one is there

**Nothing is defaulted.** `--timeout`, `--entry`, `--include`, `--output` are all
required. The exec server is a warm process serving many jobs, so an omitted
field would be served by the previous job's value rather than being unset —
the same reason the render worker rejects an incomplete spec.

**`--output` is explicit and is never a glob.** Only what you declare is fetched,
and each file is verified by size *and* sha256 against what the instance
computed on the file it wrote. A script that exits 0 without writing a declared
output **fails the job** — this is the check that catches a build reporting
success while producing nothing.

**Paths must stay inside the job.** `--save tmp/x.blend` is fine;
`--save ~/f1-round2/world/items/x_test.blend` is refused. The project
trees are read-only to this system and they do not exist on the instance
anyway. Write to `out/` what you want back and to `tmp/` what you do not.

**The bundle is content-addressed at submit.** If a module changes between
submit and dispatch, the job is refused rather than built from the new code and
filed under the old request.

**A `.blend` written into `tmp/` is deleted when the child exits**, along with
the bundle copy and anything else outside `out/`. The instance has 30 GB and
twelve of these run at once; wave 1 produced 28 GB of test blends.

### Flags

| flag | meaning |
|---|---|
| `--root DIR` | local directory the inputs come from; must be inside a permitted bundle root |
| `--include GLOB` | repeatable, relative to `--root`. Together these are the whole input |
| `--entry PATH` | script to run, relative to the bundle root |
| `--arg V` | repeatable; passed after `--`. A token containing `/` must resolve inside the job directory |
| `--output NAME` | repeatable; a file the script writes into `out/`. Fetched and sha256-verified |
| `--timeout N` | seconds; hard kill of the child's whole process group. Max 3600 |
| `--slots N` | exec slots the job occupies (default 1) |
| `--blender-arg V` | flags for Blender itself; default `-b --factory-startup` |
| `--gpu` | **declare** that this job uses the card. Off by default and enforced off — see below |
| `--wait` | block until it finishes and print the fetched paths |

`./rq status` shows exec slots in use and what is running in each.

### What it does NOT do

It does not sandbox. EXEC runs your Python; containment stops the accidental
case — a module writing beside itself, or into `~` — and guarantees that nothing
undeclared comes back. It is not a defence against a script that means harm.

It does not give you a GPU. Cycles renders still go through `rq render`; an
exec child gets CPUs. Build remotely, then render the result remotely.

**And that is now enforced rather than assumed.** Every exec child is launched
with an empty `CUDA_VISIBLE_DEVICES`, so `scene.cycles.device = 'GPU'` in your
script finds zero devices and Cycles renders on the CPU. If files in your bundle
select a GPU device, they are named in the broker log at WARNING against your
job id — the clamp is never silent.

`--gpu` opts out, and the exec server then checks who holds the card **before**
admitting the job. If the render worker is on it, the job is **refused by name**
("refusing <id>: the render worker holds <scene> on <card>") and failed
terminally on the first refusal — not retried, because the render worker holds
its scene for the whole campaign and three attempts buy three identical
collisions.

Why: on 2026-08-07 an exec job set `cycles.device = GPU` and put a second 8 GB
film scene on the same 32 GB card as an already-warm render worker. Another
agent's render died twice with `Out of memory in CUDA queue enqueue`, the second
time terminally. VRAM is the one resource on the box with no cgroup, no gate and
no OOM score to bias: the card either fits both scenes or it kills one, and
which one it kills is not a decision anybody made.

## Is the broker running the code you are reading? — `rq drift`

```bash
./rq drift          # every broker on this box; exit 1 if anything drifted
./rq drift --all    # every tracked file, not only the drifted ones
```

It reads `/proc` — never the broker's HTTP API, because "is that process running
the code I am reading?" is the one question a process running the wrong code can
answer wrongly. It compares each broker's start time and its bytecode cache
against the tree, per file, with three verdicts: `STALE`, `ok`, and `?` for what
cannot be determined. `?` is never rendered as `ok`.

Why: on 2026-08-07 an 8 GB blend was pushed three times to a path nothing read,
and the refusal written to make that terminal on first sight never fired —
because the broker process had started at 05:51 and the fix landed at 07:45.
Everyone debugging it, including the agent who wrote the fix, was reading a file
nothing was executing. **A fix in the tree and not on the box is a fix that does
not exist.**

`rq drift` never restarts anything. A restart re-claims jobs mid-flight, so it
is a human decision. Two more places now carry the same fact: the broker logs
its own module hashes at startup, and the exec server reports `code_sha256` on
every ping — which the broker compares against the file it would have pushed and
warns about once when they differ.
