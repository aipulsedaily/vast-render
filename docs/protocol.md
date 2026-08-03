# Worker protocol

Newline-delimited JSON over TCP. One request per connection, one reply, connection
closed. Jobs are served **strictly serially** — the worker never renders two at
once, because Blender's `bpy` is not thread-safe and concurrent GPU renders
timeslice rather than add throughput.

```
client ──> {"job_id": "...", "camera": "...", ...}\n
client <── {"ok": true, "path": "...", "render_sec": 4.2, ...}\n
```

## Job spec

**Every field is required.** A spec missing any field is rejected with an error
naming it, and never silently defaulted.

This is deliberate. The worker holds no render policy — the agents building the
models decide everything about the image. A default on the worker side would be
an invisible policy, and worse, in a *warm* process an omitted field would be
served by whatever the previous job left behind. Rejecting is the only way a
job's output depends solely on the job.

| field | type | notes |
|---|---|---|
| `job_id` | string | names the output file; must be unique per job |
| `camera` | string | must name a `CAMERA` object in the loaded scene |
| `resolution` | `[int, int]` | base resolution, before `zoom` |
| `samples` | int | Cycles sample ceiling; adaptive sampling may stop earlier |
| `engine` | string | `CYCLES` or `BLENDER_EEVEE` — **not** `BLENDER_EEVEE_NEXT`, which Blender 5.0 renamed away; the worker rejects it |
| `denoiser` | string | `OPENIMAGEDENOISE`, `OPTIX`, or `NONE` |
| `denoise_gpu` | bool | without this OIDN runs on **CPU** even on a 5090 |
| `use_dof` | bool | ignored for orthographic cameras |
| `film_transparent` | bool | transparent background |
| `border` | `[minx, maxx, miny, maxy]` or `null` | normalised, origin bottom-left |
| `zoom` | float | multiplies resolution *before* cropping — see below |
| `exposure` | float or `null` | `null` restores the scene's value |
| `max_bounces` | int or `null` | `null` restores the scene's value |
| `adaptive_threshold` | float | noise threshold; lower converges slower |
| `frame` | int or `null` | `null` renders the scene's **own** frame — the one captured at load, like every other `null` restore. Never "whatever frame the previous job left" |
| `persistent_data` | bool or `null` | keep BVH/sync between renders; `null` restores the scene's value |
| `require_caches` | bool | refuse the frame if a physics cache does not cover it |

One **optional** field, and the only one in the whole protocol:

| field | type | notes |
|---|---|---|
| `allow_blank` | bool, default `false` | accept a returned image with no content in it. Never part of the worker's required set, and deliberately absent from `IMAGE_FIELDS` in `broker/seq.py`, so setting it does not change `spec_hash` and cannot invalidate frames already delivered |

Optional rather than required because it is not a render parameter. Like
`persistent_data` and `require_caches` it changes what the **broker** does, and
unlike every field above it does not affect a single pixel — so an omitted value
cannot be served by whatever the previous job left behind, which is the entire
reason the other fields are mandatory. Omitting it gets the safe answer.

`use_dof` is **three-valued**, not two. `null` means *use the depth of field the
.blend authors, including its animation*, restored from a per-camera baseline
captured at load. `true`/`false` override it. Round 1 lost a render to the
two-valued version: a submission that merely omitted `--nodof` got DOF forced on
and came back blurred, so for an animation whose focus is keyed on the camera,
`null` is the only safe default and is what `rq anim` sends.

Restoring, not skipping, is what makes `null` mean anything in a warm process:
"leave it alone" would mean "inherit the previous job".

### Frames and the order things are applied

    apply_spec   every knob set; `null` fields restored to the .blend's values
    frame_set    the animation system overwrites every keyed property
    reassert     the job's explicitly-stated values win back

Each step deliberately undoes part of the last. A job that asks for exposure 0.5
gets it on every frame, keyed or not; a job that says `null` gets whatever the
animation evaluates to on that frame.

### Physics caches

A missing simulation cache does not fail — Blender **simulates** instead, and a
simulation reached by jumping to a frame does not continue the frame before it.
In a cut-free video that is a delivery-blocking defect no single-frame
inspection can find.

So the worker inspects every point cache (rigid body, cloth, soft body,
particles, dynamic paint, Mantaflow) before each frame and refuses the frame
when one does not cover it — not baked, outdated, memory-only, or a disk cache
whose `.bphys` files are not actually present. `require_caches: false` renders
anyway, and the reply still reports `cache_problems` so the caller cannot not
know. The worker never bakes, frees, or re-points a cache.

Measured on the test fixture: frame 24 rendered with its cloth cache present is
55,042 bytes; the same frame with the cache directory absent is 51,423 — a
different image, silently.

### Border and zoom

`zoom` multiplies the pixel density before the border crop is applied, so a
bordered render is **true extra detail, not an upscale**. To inspect a region at
7× density:

```json
{"resolution": [1920, 1080], "zoom": 7.0, "border": [0.66, 0.90, 0.48, 0.80]}
```

That renders at 13440×7560 internally and returns only the cropped rectangle.

## Replies

Success:

```json
{"ok": true, "job_id": "a1b2c3", "path": "/workspace/out/a1b2c3.png",
 "bytes": 8049632, "apply_sec": 0.36, "render_sec": 7.38,
 "resolution": [3840, 2160], "frame": 412,
 "png": {"width": 3840, "height": 2160, "sha256": "9f1c..."},
 "effective": {"dof": true, "focus_distance": 7.872, "lens": 40.0,
               "exposure": 0.0, "motion_blur": true, "shutter": 0.5,
               "fps": 24.0, "persistent_data": true},
 "caches": [...], "cache_problems": []}
```

`png.sha256` is computed by the worker on the file it just wrote, so the broker
can prove the file it fetched is byte-for-byte the file that was rendered — a
size check catches a truncated transfer but not a corrupted one, and nobody
eyeballs 3,000 frames.

`effective` reports what was actually rendered rather than what was asked for.
Temporal continuity across a batch boundary is a defect category in its own
right, and it is only checkable if every frame can say what state produced it.

Failure — the connection still returns cleanly, so a bad job never wedges the
worker:

```json
{"ok": false, "job_id": "a1b2c3", "error": "no camera named 'CAM_Nope'"}
```

## Control commands

| command | effect |
|---|---|
| `{"cmd": "ping"}` | `{"ok": true, "jobs": N}` — also the readiness probe |
| `{"cmd": "scan"}` | cameras, frame range, fps, motion blur, and every physics cache |
| `{"cmd": "shutdown"}` | graceful exit |

`scan` answers "will this batch silently re-simulate?" for the price of one
socket round trip, before any GPU time is spent on finding out.

`ping` does not answer until the scene is loaded and pre-warm has finished, so
it doubles as "is this worker ready for real work".

## Progress is out of band, not a command

There is deliberately no `{"cmd": "progress"}`. The worker renders on the main
thread and serves strictly serially, so for the whole duration of a frame it
cannot answer *any* request — over this socket a 40-minute 8K job looks
identical to a dead one.

Instead the worker writes `--progress PATH` (the broker passes
`/workspace/progress.json`) and the broker reads it over SSH. Written
temp-then-`rename`, so a reader never catches a half-written file:

```json
{"state": "rendering", "job_id": "eb1fe5e0252d",
 "sample": 3712, "total": 8192, "pct": 45.3,
 "elapsed_sec": 751.4, "remaining_sec": 1328.1,
 "phase": "Sample 3712/8192", "updated": 1785041234.5}
```

`state` is `idle`, `rendering`, `done` or `failed`, and `remaining_sec` is
Blender's own estimate. `elapsed_sec` is measured entirely inside the worker and
is safe to trust; `updated` is on the **instance's** clock, which has been
observed running minutes away from the broker's, so never compare it against
local time.

The numbers come from `bpy.app.handlers.render_stats` and advance at Cycles'
adaptive-sampling checkpoints, not once per sample. A frozen counter is weak
evidence of trouble on its own — which is why the broker warns about one rather
than acting on it.

## Determinism

Two identical specs produce **near-identical**, not byte-identical, output.
Cycles accumulates samples in parallel on the GPU, so float ordering varies run
to run.

Measured spread on a 400×400 frame:

| | spread |
|---|---|
| `denoiser: NONE` | 2 bytes |
| `denoiser: OPENIMAGEDENOISE` | ~49 bytes |

Both under 0.03%. OIDN amplifies the difference because it magnifies sub-pixel
variation. Anything moving output size by whole percent is a real bug — that is
what `test_worker.py` asserts against.

---

# EXEC protocol — `worker/exec_server.py`, port 8800

A second server on the **same instance**, on its own port, over its own SSH
forward, speaking the same newline-delimited JSON. It runs `blender -b -P
<entry>` child processes on the box's CPUs — item builds, gates, placement and
collision passes — and it is **concurrent** where the render worker is serial,
because a build is single-threaded CPU-bound Python and the box has 23 CPUs of
cgroup quota while it has one GPU.

It is a separate process on purpose. `worker/server.py`'s first law is *never
thread*, which is correct for Cycles and exactly wrong here; routing builds
through it would serialise every build behind every render and let a build
corrupt the warm scene. The exec server never imports `bpy`.

It runs *under* Blender (`blender -b --factory-startup -P exec_server.py`)
because `/usr/bin/python3` on the CUDA base image has no numpy — measured on the
instance — and every item module imports numpy at module scope.

## Job spec

**Every field is required**, for the same reason as the render protocol: this is
a warm process, so an omitted field would be served by whatever the previous job
used, not left unset. A spec missing any field is rejected with the field named,
and a spec carrying a field the server does not know is rejected too.

| field | type | notes |
|---|---|---|
| `job_id` | string | broker-minted, `[A-Za-z0-9_-]{1,64}`; becomes a directory name |
| `bundle` | string | digest of a staged input bundle; must already carry its `.complete` marker |
| `entry` | string | script path **relative to the bundle root**; realpath'd, then required to be inside the job's own bundle copy |
| `argv` | `list[str]` | passed after `--`. Never a shell string; the child is spawned from an argv list |
| `outputs` | `list[str]` | files the script writes into `out/`, fetched and verified. Explicit, never a glob |
| `timeout_s` | int | 1..3600. Hard kill of the child's whole **process group** |
| `blender_args` | `list[str]` | e.g. `["-b", "--factory-startup"]`. May not contain `-P` or any `--python*` flag |
| `cpu_slots` | int | slots this job occupies; must be ≤ the server's total |

## What a job gets, and what it may touch

    /workspace/exec/<job_id>/bundle/   a COPY of /workspace/bundles/<digest>/
    /workspace/exec/<job_id>/out/      the declared outputs
    /workspace/exec/<job_id>/tmp/      TMPDIR, HOME and BLENDER_USER_RESOURCES
    /workspace/exec/<job_id>/job.log   the child's stdout+stderr

The bundle is **copied**, not shared: several of these modules write beside
their own file, and one job must not be able to corrupt the cache every other
job is reading.

`entry`, every `outputs` element, and every `argv` / `blender_args` token that
contains a `/` are **resolved with realpath first and then required to be inside
the job directory**. Resolving first is the whole point — a prefix test applied
before resolution passes `bundle/../../../etc/shadow` happily.

This is containment, not a sandbox, and the difference is stated plainly: EXEC
runs caller-supplied Python, so a script can still construct a path in code.
What is enforced is that the *accidental* case lands inside the job, and that
nothing the broker fetches was not declared.

## After the child exits

Everything except `out/` and `job.log` is deleted **immediately** — the bundle
copy, TMPDIR, and anything the script dropped in its CWD. Twelve concurrent
builds saving multi-gigabyte .blends do not fit on a 30 GB volume otherwise. The
rest goes on `{"cmd": "release"}`, after the broker has fetched and verified.

## Replies

```json
{"ok": true, "job_id": "a1b2c3", "rc": 0, "timed_out": false, "exec_sec": 76.7,
 "outputs": [{"name": "gate.json", "bytes": 1214, "sha256": "9f1c…",
              "path": "/workspace/exec/a1b2c3/out/gate.json"}],
 "missing": [], "scrubbed_bytes": 8112640, "log": "…last 4 KB…"}
```

A child that exits 0 **without writing a declared output FAILS the job**. So
does a non-zero exit, and so does a timeout — which reports `timed_out: true`
and has already signalled the process group.

## Control commands

| command | effect |
|---|---|
| `{"cmd": "ping"}` | slots, in-flight job ids, free disk, **container** memory available |
| `{"cmd": "release", "job_id": …}` | delete one job directory; refuses while it is running |
| `{"cmd": "purge", "older_than_s": N}` | sweep job directories older than N seconds |
| `{"cmd": "bundles"}` | staged bundles and whether each is complete |
| `{"cmd": "shutdown"}` | exit |

## Memory is measured from the cgroup, never from `free`

`nproc`, `/proc/meminfo` and `/proc/loadavg` inside this container are all the
**host's** and all overstate what may be used. Measured on the instance running
this campaign:

    /sys/fs/cgroup/cpu.max      2304000 100000  ->  23.04 CPUs
    /sys/fs/cgroup/memory.max   97169440768     ->  90.5 GiB
    nproc                       96
    MemTotal                    188 GB
    loadavg                     99.5   (mostly other tenants)

A job is held at the door until the container has `--min-free-mem-gb`
available, and each child sets `oom_score_adj=800` — because a cgroup OOM picks
by RSS, and the largest RSS on this box is the render worker holding a
multi-gigabyte scene resident.
