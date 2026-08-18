# vast-render

A render broker. It rents GPUs on [vast.ai](https://vast.ai), ships a Blender
scene to them, renders frame ranges, fetches the results, **verifies that there
is actually a picture in them**, and destroys the fleet when the work is done.

It exists because a local 8 GB card cannot hold a 4K Cycles frame in one pass,
and because renting GPUs by hand is how you end up paying for an instance you
forgot about. Every design decision here is downstream of one of those two.

> **Money warning.** This software rents hardware that bills by the second
> against your vast.ai account. It has several independent safeguards (below)
> and it has still, in development, stopped an instance that was mid-frame and
> kept one alive longer than intended. Set a budget, keep prepaid credit as your
> ceiling, and read `docs/operations.md` before pointing it at real work.

## Architecture

```
many agent clients ──HTTP──> BROKER (local)              WORKER (rented GPU)
                             ├── FastAPI + SQLite WAL    ├── blender -b scene.blend -P server.py
                             │   one uvicorn process     ├── serial loop, main thread only   :8799
                             ├── dispatcher (asyncio)    ├── use_persistent_data = True
                             ├── vast poller (10 s)      ├── OPTIX_CACHE_PATH persisted
                             └── heartbeat ──────────────┴── watchdog: self-destructs if stale
                                                         └── exec_server.py, N slots        :8800
```

Clients **never** call the vast.ai API. It rate-limits per endpoint *and* per
client IP, returns 429 with no `Retry-After`, and the thresholds are
unpublished — so exactly one poller talks to it and every client is served from
cache.

    rq          client CLI — submit, collect, status        (stdlib only)
    fleetctl    drive a fleet of brokers as one thing
    broker/     queue, dispatcher, fleet, image statistics
    farm/       multi-broker procurement, shared bad-host list
    worker/     the warm Blender server, deployed to the instance
    vastctl/    instance lifecycle — search, create, reap, destroy
    scripts/    panic button, offer probe, supervisor
    docs/       agent guide, operations runbook, protocol, incidents
    state/      sqlite db, logs, heartbeat        (gitignored)
    out/        returned frames                   (gitignored)

Two dispatchers share one rented box: `worker/server.py` renders **one** frame
at a time on the GPU (Blender's never-thread law), while
`worker/exec_server.py` runs several headless Blender *processes* on the same
box's CPUs and never imports `bpy`.

## Prerequisites

- **A vast.ai account with prepaid credit, and an API key.** There is no free
  mode. Prepaid credit is the only hard spend ceiling vast.ai offers.
- **Python 3.13+** locally, with the `vastai` SDK installed. The repository is
  run from a virtualenv; there is no `requirements.txt` or packaging, so create
  one and install `vastai`, `fastapi` and `uvicorn` into it.
- **An SSH keypair** dedicated to this, ed25519, **without a passphrase** — the
  broker is unattended and cannot answer a prompt.
- **Blender 5.2.0.** The version installed on the instance must match the build
  that assembled the scene; a scene should be rendered by the Blender that
  saved it. The broker installs it on the instance for you.
- **`ffmpeg`**, only if you want to turn a returned frame sequence into a video.
- An assembled `.blend`. This repository does **not** build scenes — assembly is
  an input here, never something the broker produces.

## Credentials — environment variables, never a file in this repository

**Never commit an API key, and never write one into a file inside this tree.**

The vast.ai SDK resolves the key in this order: explicit argument, then
`VAST_API_KEY`, then `~/.config/vastai/vast_api_key`, then `~/.vast_api_key`.
The broker constructs its client without an explicit key, so **setting
`VAST_API_KEY` in the environment takes precedence over every file** — that is
the supported and recommended path:

```bash
export VAST_API_KEY='…'                                # never checked in
export VASTRENDER_SSH_KEY="$HOME/.ssh/id_vast_render"  # default if unset
```

Put those in a shell profile or a secrets manager outside the repository. If
you use the SDK's config file instead, keep it at mode `0600` and outside this
tree; nothing here reads it directly and nothing here should.

`.env.example` in the repository root lists every variable with placeholder
values. Copy it, fill it in, and keep the copy out of git:

```bash
cp .env.example .env      # .env is gitignored; .env.example is not
$EDITOR .env
set -a && . ./.env && set +a
```

**`.env` is ignored, `.env.example` is tracked, and that pair is asserted by a
test** (`tools/publication/check_publication.py`) — because the ignore rule
that covers `.env` is `.env.*`, which silently swallowed `.env.example` too
until it was given an explicit negation. An example file that cannot be
committed is not an example file.

**If a key is ever exposed — pasted into a log, a transcript, a screenshot, or
a commit — rotate it at once in the vast.ai console.** Rotation is the only
remedy; an exposed key is a live billing credential. Scrubbing a repository
does **not** un-expose a key that has already existed somewhere readable.

### What redacts, and what it does not

`redaction.py` at the repository root is the single definition of what a secret
looks like, and everything that can print or store text goes through it:
`broker/remote.py` (`diagnose`), `broker/diagnostics.py` (the traceback hooks),
`broker/db.py` (every `err` written to the database), `broker/seq.py` (the
sequence manifest, which is built to be handed to somebody), `fleetctl` and
`vastctl`. It covers `api_key=…`, a renamed query parameter carrying a
key-shaped value on a `console.vast.ai` URL, `Authorization: Bearer …`,
`X-Api-Key: …`, and `"api_key": "…"` in JSON.

It deliberately does **not** redact a bare 64-hex token with no key-ish context
around it, because that is also the shape of a sha256 and this project's frame
integrity checks and manifests are built on full digests. Blanket-redacting
would turn a security control into a data-integrity bug. This is a chosen
bound, asserted by a test, not an oversight.

None of that is a reason to relax anything above. Redaction is a backstop for
the moment something goes wrong; the environment variable is the control.

## Configuration

Everything else is environment variables too, `VASTRENDER_`-prefixed, read in
`broker/config.py`. That file is the reference: every setting carries the
measurement or the incident that produced its default, and there are a lot of
both. A few that matter on day one:

| variable | meaning |
|---|---|
| `VASTRENDER_SCENE` | the assembled `.blend` a render job gets when it names none |
| `VASTRENDER_SCENE_ROOTS` | colon-separated roots a job may name a scene inside |
| `VASTRENDER_DISK_GB` | volume size requested at create |
| `VASTRENDER_PORT` / `_DB` / `_OUT` | broker port, database and output directory |
| `VASTRENDER_BLENDER_VERSION` | Blender to install on the instance |
| `VASTRENDER_KEEP_ON_EXIT` | adopt the instance on next start instead of destroying it |

`SCENE_ROOTS` is a containment check, not a convenience: a client-supplied
scene path becomes a filesystem path on **both** machines, so it is resolved
through symlinks and `..` first and then required to sit inside a permitted
root. The built-in defaults name the two sibling project trees this broker was
written for (`~/f1-round2/…`, `~/opus5-car-render/…`) and each is used only if
that directory actually exists — so **on a fresh clone the default allowlist is
empty apart from the broker's own `scenes/`, and every other scene path is
refused until you set `VASTRENDER_SCENE_ROOTS`.** That refusal is the intended
behaviour: an allowlist that fails open is not an allowlist.
`VASTRENDER_BUNDLE_ROOTS` behaves the same way for `rq exec` bundles.

## Running it

```bash
# 1. start the broker, pointed at a locally assembled scene
VAST_API_KEY='…' \
VASTRENDER_SCENE=/path/to/scene.blend \
  .venv/bin/python -m broker.app &

# 2. render one frame — rents a GPU on the first job, returns a PNG
./rq render --cam CAM_Hero --res 3840 2160 --samples 220 -o hero.png

# 3. watch queue and spend
./rq status

# 4. stop everything, works even with the broker dead
scripts/panic.sh
```

The projected cost is printed before anything is rented. The instance destroys
itself a few minutes after the last job.

### Frame ranges

A job is a frame **range**, not a frame: the scene uploads once and stays
resident for the whole range, frames land in one directory, and `--name` is the
resume key.

```bash
./rq anim --name beat3 --scene beat3.blend --cam CAM_Oner \
          --res 3840 2160 --samples 512 --frames 620-980
./rq anim --name beat3 ... --frames 701,744-745   # just the holes
./rq seq status beat3     # present / missing / corrupt, by frame number
./rq seq verify beat3     # same, re-hashing and re-measuring every file
./rq budget --set 40      # raise the cumulative spend cap, no restart
```

`--frames` takes `A-B`, `A-BxN`, a bare number, or a comma-separated list —
the same syntax `rq seq status` prints for missing frames, so its output pastes
straight back in.

Re-submitting a name renders only what is absent, and "absent" is re-checked
against the files on disk every pass. A frame counts as delivered only once it
has been fetched, its sha256 matches the digest the worker computed when it
wrote it, **and there is a picture in it**.

### CPU jobs

`rq exec` ships a bundle of code — not a blend — to the same rented box and
runs headless Blender processes on its CPUs, several at once:

```bash
./rq exec --root /path/to/project \
          --include 'world/*.py' --include 'tools/*.py' \
          --entry tools/item_build.py --arg --item --arg kerb_unit \
          --output gate.json --timeout 1800 --wait
```

Measured worth is **3.65x on a 46-CPU box and 1.66x on a 23-CPU one**, against
an adoption bar of 2x — so this lever is worth using on wide hardware and not
worth using on narrow hardware. `docs/operations.md` records both runs.

## Why it verifies pixels

Job `0908e534b1d3` returned an 8,734-byte PNG with a valid signature, an IEND
chunk, exactly the requested dimensions, and a matching sha256. It was entirely
black — mean 0.00000, sd 0.00000. Every check the farm had verified the *file*;
none could see the *image* had nothing in it. In a 3,000-frame single unbroken
take, one such frame passes verification, counts as delivered, survives every
resume as "already done", and is discovered when the finished video is watched.

So every returned PNG is decoded and measured — mean, standard deviation,
range, distinct luminance levels, alpha — and classified `OK`, `SUSPICIOUS`,
`BLACK`, `UNIFORM` or `TRANSPARENT`. The last three fail the job terminally
unless the caller passes `--allow-blank`. `SUSPICIOUS` is reported loudly and
never fails anything, because a dark frame is something artists make on purpose
and a check that refuses legitimate work gets switched off.

A sequence also gets a *relative* check no fixed threshold can make: each frame
is compared against a rolling window of its neighbours, so one dropped frame at
1,600 is flagged while a fade — which walks the whole neighbourhood down
together — is not.

## Safety rails

All of these are mandatory, and each exists because its absence cost money:

1. **`--cancel-unavail` on every create.** Without it a failed schedule silently
   creates a *stopped* instance that still bills storage — an orphan generator.
2. **Destroy, never stop.** Storage bills while an instance *exists*. `stop`
   only ends the GPU meter.
3. **An on-instance watchdog** self-destructs the box via the `CONTAINER_API_KEY`
   vast injects (scoped to that instance alone) if the broker heartbeat goes
   stale. This is the only protection that survives broker crash or local power
   loss — and it only runs while the instance is *running*, so a stopped
   instance depends on the next broker start reaping it.
4. **Every instance is labelled**, and orphans are reaped by label at broker
   startup, *before* anything new is created.
5. **A hard wall-clock cap per instance**, enforced in the broker *and* the
   watchdog.
6. **`scripts/panic.sh`** destroys everything labelled. Idempotent, and runnable
   with the broker dead.
7. **Destroy on every exit path** — `try/finally`, `atexit`, SIGTERM/SIGINT —
   then re-poll until the ID is gone.
8. **One broker per state directory**, enforced with `flock` before anything
   else happens at startup. A second broker used to adopt the running instance
   and then destroy it when its own port bind failed.

Four independent paths destroy an instance, so no single failure strands one.

## Operational hazards

These are documented at length in `docs/`, and they are the parts most likely
to cost you real money or real time. Read `docs/incidents.md` before a long run.

- **A healthy-looking box can be unusable.** One instance passed every probe
  while delivering 14 KB/s downstream against 731 KB/s up — a 7.5 MB frame took
  six minutes to fetch against a 16 second render, and it billed for 68% of a
  rental before anyone noticed. Throughput is now a first-class health signal
  because every other check counted only *failures*, and slow is not a failure.
- **An idle queue is not an idle GPU.** The broker has stopped an instance that
  was at 99% and 420 W on an 8K frame. Refusals to stop are now bounded rather
  than absolute, but the hazard is structural.
- **Scene switching is expensive and asymmetric.** Measured 63 s for a 3 MB
  scene and 1,425 s — 24 minutes — for a 4.5 GB one. A dispatcher that switches
  eagerly can spend more time moving scenes than rendering; the scheduler's
  batching, aging and payback rules all exist to bound that, and all of them are
  tunable in the wrong direction.
- **Compression level is a time trade, not a ratio trade.** `zstd -19` fed a
  4-5 MB/s link at 1.3 MB/s, idling a rented GPU for eight minutes. Do not raise
  it without re-measuring both stages under load.
- **Missing assets change the picture silently.** A `.blend` that was not packed
  stores absolute paths; Blender warns and renders anyway, so frames come back
  lit differently from what you see locally. Likewise a missing physics cache:
  Blender does not fail, it re-simulates, which in a cut-free video changes the
  destruction from one frame to the next.
- **Disk exhaustion is not a clean failure.** Blender writes a short PNG rather
  than refusing, and a short PNG is a class of loss this pipeline has already
  suffered. Hence the measured free-space reserve.
- **Resource conditions must not read as verdicts.** Several bugs here had the
  same shape: an out-of-memory kill, a busy worker, or a lost transfer consuming
  the retry budget and reporting `attempts 3/3`, which reads as "tried and found
  wanting" when the truth was "the box was out of memory three times".
- **Bad hosts are remembered fleet-wide**, in `farm/bad_hosts.json`, with a
  7-day TTL. That file is machine state and is deliberately not tracked.

## Docs

- **`docs/agents.md`** — for clients that just want renders. Start here.
- **`docs/operations.md`** — the runbook for whoever owns the money: safety
  commands, host selection, tuning, and the measured A/B results.
- **`docs/protocol.md`** — job spec reference and wire format.
- **`docs/incidents.md`** — what has gone wrong, and what each fix actually was.
- **`docs/fleet.md`**, **`docs/multi-gpu.md`**, **`docs/linked-libraries.md`** —
  running many brokers, multi-GPU boxes, and Blender library linking.

## Design notes

**The worker is a dumb executor.** This project owns transport and lifecycle,
nothing else. It has no opinion about resolution, samples, cameras, denoiser,
bounces or exposure — every one of those arrives in the job payload, per job,
and is applied explicitly. A job that omits a field is a bug in the client, not
something the worker guesses at. Setting every parameter on every job is also
what makes a warm worker safe: state cannot leak from job N-1 into job N.

**Cameras are discovered, not configured.** At scene load the worker enumerates
every camera in the blend and pre-touches each, so there is no hardcoded list to
drift out of sync. Pre-warm sweeps feature-set variants too — DOF on/off, border
on/off, each denoiser — because a first-camera cost turned out to be an OptiX
pipeline rebuild for a new kernel feature set, not the camera itself.

**Warm worker, not cold-per-job.** `scene.render.use_persistent_data = True`
(note `render`, not `cycles`) collapses per-render fixed cost by a large factor
on a static scene. If geometry moves between frames, every frame pays a BVH
rebuild instead and the gain disappears — `rq anim` projects cost from measured
frame times rather than assuming either case.

**No dedup.** Two clients requesting identical parameters get two renders. A
params-only hash cannot see scene state, so dedup would silently serve stale
frames across a reassembly. Job IDs are broker-minted UUIDs, because a
caller-supplied ID was a path-traversal vector.

**Blender binary, not the pip `bpy` wheel.** The wheel works, but installs no
signal handlers, which means no crash logs.

**On-demand, not interruptible.** Preemption mid-render costs more than the bid
spread saves.

## Status

Working and in production use: this broker rendered a 2,978-frame 4K film as a
multi-day, multi-GPU job. It is nonetheless a single-operator tool that grew
around one project, and it shows — see the hardcoded default paths noted under
Configuration, and the absence of packaging.

Test suites live beside the code they cover (`broker/test_broker.py`,
`worker/test_worker.py`, `worker/test_exec_server.py`, `farm/test_*.py`) and are
the main thing standing between a bad edit and a dead render pipeline.

## Contributing, and one thing to set before your first commit

**Set your git identity to a noreply address before you commit.** This
repository's `.git/config` is preconfigured with

```
user.email = noreply@users.noreply.github.com
```

which keeps a personal address out of future commits. That generic form works,
but GitHub will not attribute the commits to you. Replace it with your own
address — the numeric ID is on `https://api.github.com/users/<username>`:

```bash
git config user.email 'ID+username@users.noreply.github.com'
git config user.name  'Your Name'
```

This is local configuration only. It changes nothing that is already committed:
**the existing history still carries a personal address on 40 of its 61
commits.** Rewriting that history is cheap in this repository — 57-odd commits,
no culture of citing commit SHAs in the docs — so `git filter-repo --mailmap` is
a real option here in a way it is not in the companion repository. That is a
decision for the owner, before publishing.

Tests live beside the code they cover and are the contribution bar: a change to
the broker without a test in `broker/test_broker.py` will not be believed.

## Licence

**Apache-2.0** — see `LICENSE` for the full text and `NOTICE` for the
attribution notice that travels with redistributions.

Chosen over MIT for two reasons specific to this tool rather than by habit.
First, the **express patent grant**: this is infrastructure other people may
deploy commercially, and MIT's silence on patents protects neither side.
Second, the **warranty and liability disclaimers**, which are load-bearing for
software that rents billable hardware on somebody else's cloud account —
sections 7 and 8 exist so that nobody can argue the author warranted their bill.

The `bpy`/GPL question that pushes the companion repository `f1-round2` to
GPL-3.0-or-later barely reaches here: only `worker/server.py` runs inside
Blender, and nothing else imports `bpy`. Apache-2.0 is one-way compatible with
GPL-3.0 anyway, so anyone who wants that file under the GPL can take it there.

**The owner can change this before publishing.** It was applied so the
repository would not go public in the no-licence, all-rights-reserved default
state, which for a broker whose whole value is that other people can run it
would defeat the point. After the first public copy the position is asymmetric:
future versions can be relicensed, copies already released cannot be recalled.
