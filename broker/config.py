#!/usr/bin/env python3
"""Broker settings. Every value is overridable by environment variable so the
same code runs for a quick local test and an unattended 900-frame batch."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default):
    raw = os.environ.get(f"VASTRENDER_{name}")
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    return type(default)(raw)


# --- what to render -------------------------------------------------------

# The assembled .blend. Assembly happens locally (procedural bpy geometry is
# single-threaded Python with no GPU path, and the remote EPYC is ~1.5x slower
# per core), so this is an input to the broker, never something it produces.
SCENE = Path(_env("SCENE", str(ROOT / "scene.blend")))

# Jobs may name their own scene. Everything they can name must sit under this
# root, resolved — a client-supplied scene becomes a file path on *both*
# machines, which is the same class of vector that made job ids broker-minted:
# a caller-supplied id was a traversal into the read-only scene project. A bare
# prefix comparison is not enough, so validation resolves symlinks and `..`
# first and then requires the real path to be inside this directory.
SCENE_ROOT = Path(_env("SCENE_ROOT", str(SCENE.parent))).resolve()


# The projects this broker renders for. A job may name any .blend inside one of
# these and nothing outside them.
#
# A LIST, and configurable, because it was neither: it was one hard-coded
# round-1 directory, so the moment round 2 moved to its own project root every
# submission came back
#
#     400 scene '...' resolves to ..., which is outside the permitted scene root
#
# for a scene that was perfectly legitimate. One tree per renderer was never
# true — round 1's work directory, round 2's world directory and this repo's own
# test scenes all have to be nameable at once.
#
# What does NOT change is the check. A client-supplied scene path is a traversal
# vector — it becomes a filesystem path on this machine AND on the rented
# instance — so `scenes.resolve_scene` still resolves symlinks and `..` FIRST
# and then requires the real path to sit inside one of these. A prefix-string
# compare would be defeated by `/home/zany/f1-round2-evil`; containment after
# resolution is not.
#
# Override wholesale with `VASTRENDER_SCENE_ROOTS` (colon-separated). Anything
# listed there is trusted whether or not it exists yet; the defaults below are
# included only when the directory is actually present, so this file does not
# grow stale entries as projects come and go.
DEFAULT_SCENE_ROOTS = (
    "/home/zany/opus5-car-render/work",     # round 1 — the F1 showroom stills
    "/home/zany/f1-round2/world",           # round 2 — the one-shot cinematic
    "/home/zany/f1-round2/render",          # round 2 — render-side variants
)


def _scene_roots() -> list[Path]:
    """Every directory a job may name a scene inside, resolved and de-duplicated."""
    roots: list[Path] = [SCENE_ROOT]

    def add(path: Path) -> None:
        real = path.expanduser().resolve()
        if real not in roots:
            roots.append(real)

    raw = os.environ.get("VASTRENDER_SCENE_ROOTS")
    if raw is not None:
        for part in raw.split(":"):
            if part.strip():
                add(Path(part))
    else:
        for part in DEFAULT_SCENE_ROOTS:
            if Path(part).is_dir():
                add(Path(part))
    # Scenes shipped with the broker itself (animation tests, previews).
    local = ROOT / "scenes"
    if local.is_dir():
        add(local)
    return roots


SCENE_ROOTS = _scene_roots()

# Directory names beside a .blend that are mirrored to the instance *next to the
# uploaded scene*, so relative references (`//cache/...`, `//blendcache_x/`)
# resolve there exactly as they do here.
#
# This is the sim-cache path, and getting it wrong is silent: Blender does not
# fail on a missing physics cache, it simulates instead — which in a cut-free
# video changes the destruction from one frame to the next.
CACHE_DIR_GLOBS = [
    p for p in _env("CACHE_DIR_GLOBS",
                    "blendcache_*:cache:caches:sim:sims:textures:tex:anim").split(":")
    if p.strip()
]

# Largest frame range one job may cover. Not a technical limit — the worker
# renders one frame per call and the broker loops — but a blast radius: a typo
# in a range is caught at submit instead of after a day of GPU time.
MAX_FRAMES_PER_JOB = _env("MAX_FRAMES_PER_JOB", 5000)

# Scenes are cached on the instance by content hash, so re-selecting one costs
# nothing. The cache is bounded by TOTAL BYTES and evicted least-recently-used.
#
# 8 GB, not the 12 GB this was, and the difference is the whole point. Measured
# on instance 46133943 after nine hours of a 435-item campaign: 41 cached scenes,
# 8.8 GB, nothing ever evicted — because the cache had never reached 12 GB, so
# the eviction that existed had never once run. At 270 MB per item the campaign
# would have reached 117 GB, and a 12 GB cap plus Blender plus the container
# image does not fit the 16 GB disk this farm is moving to at all.
#
# The sizing WAS: a 16 GB volume carries ~1.7 GB of image + Blender install,
# this reserve of free space, and the cache. 8 GB holds the largest assembly
# seen here (3.9 GB) beside the loaded one with ~4 GB of slack left over.
#
# That constant went stale exactly the way a constant does. Scenes reached
# 4.53-5.22 GB — the largest is now 34 % past the 3.9 GB the 8 GB was sized
# around — so two current scenes cannot both fit BY CONSTRUCTION. Measured
# 2026-08-03 on instance 46712525: a 32.2 GB disk with 20.8 GB free logged
# "scene cache will exceed its 8.0 GB budget (4.77 + 5.22)" while a third of
# the volume sat unused, because the ceiling was written for a 16 GB disk the
# farm may or may not migrate to and applied to a 32 GB one.
#
# So it is DERIVED from the disk actually present, not hardcoded for a disk we
# hope to have. `remote.derived_cache_bytes` takes a fraction of the room left
# after the container image, Blender and the free-space reserve. That room is
# stable as the cache fills — `DiskState.other_bytes` subtracts the cache, so
# the budget does not chase its own tail — and a 16 GB box and a 32 GB box both
# get a correct answer with no constant to go stale. The derived value is
# logged once per instance so it is never a mystery.
#
# Set SCENE_CACHE_GB to pin an explicit ceiling; 0 (the default) means derive.
# Either way `remote.effective_budget` still lowers it whenever the measured
# disk cannot afford the answer.
SCENE_CACHE_GB = _env("SCENE_CACHE_GB", 0.0)

# Fraction of usable room the scene cache may claim when SCENE_CACHE_GB is 0.
# 0.80 of a 16 GB volume is ~9.8 GB and of a 32 GB volume ~23 GB — both hold
# the largest assembly beside the loaded one, which is the property the old
# 8 GB was chosen for and then lost.
SCENE_CACHE_FRACTION = _env("SCENE_CACHE_FRACTION", 0.80)

# Never derive a budget below this. Guards the case where a small or unusually
# full disk would otherwise produce a cache too small to hold one scene beside
# the loaded one, which is thrash by arithmetic rather than by policy. Clamped
# to the room that actually exists — a floor may not conjure disk.
SCENE_CACHE_FLOOR_GB = _env("SCENE_CACHE_FLOOR_GB", 4.0)

# Free space that must survive every scene upload, measured with `df` on the
# instance rather than inferred from DISK_GB (which is what we asked vast.ai
# for, not what the filesystem turned out to be).
#
# Needed because ENOSPC is not a clean failure anywhere in this pipeline:
# Blender writes a short PNG rather than refusing, and a short PNG is the
# failure class this project has already lost frames to. 2 GB covers an 8K
# frame in flight, the OptiX cache, apt's temp files and worker logs with room
# to spare.
DISK_RESERVE_GB = _env("DISK_RESERVE_GB", 2.0)

# How often the heartbeat thread re-measures the instance's disk for `rq status`.
# One SSH command, measured at 0.38 s over 42 cached scenes, so this is about
# not spamming a busy instance rather than about cost. It rides the heartbeat
# thread because that is the one thread still running during a multi-hour
# render — and it runs AFTER the beat is sent, never before it.
DISK_SAMPLE_SEC = _env("DISK_SAMPLE_SEC", 300.0)

# Scene switching costs a worker restart plus a per-camera OptiX prewarm,
# measured at 40-60 s, so the dispatcher drains a scene before switching. These
# two bounds stop draining from becoming starvation. See Broker.next_job.
SCENE_BATCH_MAX = _env("SCENE_BATCH_MAX", 25)
SCENE_STARVE_SEC = _env("SCENE_STARVE_SEC", 300.0)

# SCENE_STARVE_SEC is a FLOOR, not the whole threshold. It was written when a
# scene switch meant "worker restart plus prewarm, 40-60 s", and against that
# cost 300 s is a generous margin. It is not the cost of every switch.
#
# Measured on this farm 2026-08-03, switching between round-2 scenes:
#
#     breach/wit_static.blend   0.003 GB     63 s
#     wavefix/pw_*.blend        0.20  GB    ~100 s
#     spx5.blend                0.68  GB     222 s
#     film6b.blend              4.51  GB    1425 s        <- 24 minutes
#
# With five agents queuing work against five different scenes, *some* scene has
# always been waiting longer than 300 s, so the starvation test fired on every
# single dispatch and the policy degenerated into exactly the job-by-job
# switching `next_job` exists to prevent — 9 consecutive switches, "after 1
# job(s)" every time, one 13 s render bought with 100 s of scene push. It was
# about to abandon a 4.53 GB scene holding SIXTEEN queued jobs to serve one
# 3 MB scene holding one, at ~24 minutes a round trip.
#
# So preemption has to clear the cost of the preemption. A switch is paid
# TWICE — once to leave the loaded scene and once to come back to it — hence
# the factor. Below the floor the old behaviour is unchanged: for a 0.2 GB
# scene 2 x 120 s is under 300 s, so small scenes still interleave freely, and
# only a scene that is genuinely expensive to reload earns patience.
#
# This bounds nothing on its own and is not meant to: SCENE_BATCH_MAX is what
# stops a busy scene deferring the others forever, and it is untouched.
SCENE_SWITCH_PAYBACK = _env("SCENE_SWITCH_PAYBACK", 2.0)

# --- priority, and the fact that it stopped at the scene boundary ----------
#
# `prio` (LOWER is more urgent; 100 is the default) has always ordered jobs
# inside `db.claim`. It did nothing at all for which SCENE gets loaded next —
# that was `ORDER BY created ASC`, pure FIFO on submission time. So a `prio 10`
# job on a freshly submitted scene lost to a `prio 100` job on an older one,
# for as long as the older scene kept work. Measured 2026-08-03: a 13.6 s
# render sat queued 41 minutes behind older scenes with priority set.
#
# A half-working knob is worse than no knob, because agents reasonably believe
# it works and stop looking for the real reason their job is late.
#
# The fix is AGING, not ordering: priority buys a head start in seconds, and a
# scene's own wait keeps growing regardless. So the comparison is
#
#     effective_age = (now - created) + clamp((100 - prio) * BOOST, ±CAP)
#
# and the scene holding the largest effective age is served next.
#
# Aging is chosen over `ORDER BY prio, created` precisely because ordering by
# priority is unbounded: a steady trickle of urgent work would defer everything
# else forever, which is the trap SCENE_BATCH_MAX already fell into once.
# Under aging a deferred scene's age climbs without limit while the head start
# is fixed, so it always wins eventually.
#
# BOOST is per priority POINT, and its scale was set by the case it has to
# fix. At 10 s/point the default-to-urgent gap (100 -> 10, 90 points) is only
# 900 s, so the reported job — 41 minutes behind older scenes — would still
# have waited 26 minutes. That is not a working knob either.
#
# At 20 s/point the same gap is 1800 s, which saturates the clamp below. So
# **`prio 10` is maximum urgency, and anything lower is identical to it**;
# the useful gradient lives between 100 and 10 (prio 50 -> 1000 s, prio 90 ->
# 200 s), and values above 100 deprioritise (prio 150 -> -1000 s).
SCENE_PRIO_BOOST_SEC = _env("SCENE_PRIO_BOOST_SEC", 20.0)

# THE BOUND, and the reason priority cannot become a new way to starve.
#
# The head start is clamped, so no matter what an agent puts in `prio` — 0,
# -1000, a typo — **no scene is ever deferred more than this many seconds
# beyond its FIFO turn.** That is a stated, testable bound rather than a hope,
# and `test_priority_cannot_starve_a_scene` fails if it is exceeded.
SCENE_PRIO_BOOST_MAX_SEC = _env("SCENE_PRIO_BOOST_MAX_SEC", 1800.0)

# The `prio` a job gets when nobody says otherwise; the zero point of the boost.
DEFAULT_PRIO = _env("DEFAULT_PRIO", 100)

# --- download throughput, the health signal that did not exist ------------
#
# Every transport check in this broker counts FAILURES: a reset, a timeout, a
# round that delivered no new bytes. None of them can see *slow*. A link that
# delivers is never a failure, never a stalled round, never a transport
# budget — so an instance that cannot return results still passes every probe,
# reports `ready`, and bills.
#
# Measured 2026-08-03 on instance 46695656 (192.0.2.12), three independent
# ways — a multiplexed fetch, a dedicated no-mux fetch that ruled out our own
# ControlMaster, and a raw `dd` in each direction:
#
#     RTT        265 ms      (these hosts normally run ~69 ms)
#     upload     731 KB/s
#     DOWNLOAD    14 KB/s    <- 52x asymmetric, the wrong way round
#
# A 7.5 MB PNG took over six minutes to fetch against a 16 s render. The farm
# is download-heavy — every frame must come back — so this box was unusable
# while looking perfectly healthy. It went unnoticed for 68% of a rental.
#
# So throughput is a first-class signal now, sampled from the fetches the
# broker already performs. No synthetic probe: a health check that costs
# bandwidth is one that gets disabled.
FETCH_MIN_KBPS = _env("FETCH_MIN_KBPS", 200.0)

# Only fetches at least this big are sampled. At 265 ms RTT a small file is
# nearly all handshake, so tiny transfers report a "rate" that measures
# latency, not bandwidth — the same reason a 3 MB scene push logs 0.4 MB/s on
# a link that does 5 MB/s. Sampling them would condemn healthy hosts.
FETCH_SAMPLE_MIN_BYTES = _env("FETCH_SAMPLE_MIN_BYTES", 1_000_000)

# Consecutive qualifying samples before a verdict, and how many are kept. Two,
# because one slow fetch is a hiccup and this ends in a destroyed instance —
# but only two, because the whole point is catching it in the first minute
# rather than after 68% of a rental.
FETCH_MIN_SAMPLES = _env("FETCH_MIN_SAMPLES", 2)
FETCH_SAMPLE_WINDOW = _env("FETCH_SAMPLE_WINDOW", 8)

# What reloading a scene costs when it has never been measured — the first
# switch away from a big scene must already be protected, or the measurement
# only ever arrives after the mistake. Fitted to the four measurements above
# (60 + 300/GB gives 61 s, 120 s, 264 s, 1413 s against 63/100/222/1425), and
# the slope is the same 300 s/GB the worker readiness budget already uses.
SCENE_RELOAD_BASE_SEC = _env("SCENE_RELOAD_BASE_SEC", 60.0)
SCENE_RELOAD_SEC_PER_GB = _env("SCENE_RELOAD_SEC_PER_GB", 300.0)

# How long a just-drained scene is given to produce more work before the
# dispatcher pays to switch away from it.
#
# The clients here are SERIAL. `tools/r5090` blocks until its render comes
# back, so a five-camera sweep submits camera N+1 only a second or two AFTER
# camera N lands. A dispatcher that switches the instant the queue reads empty
# fires inside that gap — and then pays a full scene switch between every
# single pair of a serial client's jobs.
#
# Measured 2026-08-02, and this is why the value is not zero: a sweep of
# items/spectator_crowd_test.blend drained at 16:10:31, the dispatcher
# committed to verify_world_a6.blend (4.2 GB, uncached) in the same second, and
# the sweep's last camera — submitted moments later — waited behind a
# 20-minute upload. The switch that cost it was decided one second early.
#
# Cheap insurance: no scene switch has ever been measured below 26 s here, so
# the whole grace is a fraction of the thing it avoids. It is also only ever
# spent when a switch is actually imminent (some other scene has work queued),
# and it polls rather than sleeps, so an active client is served as fast as the
# question can be asked. Fairness still outranks it — a scene crossing
# SCENE_STARVE_SEC ends the wait immediately.
SCENE_DRAIN_GRACE_SEC = _env("SCENE_DRAIN_GRACE", 12.0)

# --- scene upload compression ---------------------------------------------
#
# `push_scene` streams the blend through zstd straight into an ssh pipe, so a
# push costs COMPRESSION TIME PLUS WIRE TIME and the level trades one against
# the other. It was hardcoded to 19 — the best ratio — which is the right
# choice only when the link is slow enough that ratio dominates everything.
# It is not. Measured on this farm 2026-08-02:
#
#     link to these hosts     4.0-4.7 MB/s   (see push_blender: 4.02 parallel,
#                                             4.68 single — more streams do NOT
#                                             help, so payload size is the only
#                                             lever the broker has on the wire)
#     local zstd -3  -T6      598 MB/s
#     local zstd -10 -T6      143 MB/s
#     local zstd -19 -T6       20 MB/s
#
# The two stages overlap in the pipe, so what a push costs is whichever stage
# is slower — and at 19 that is the COMPRESSOR, not the link. Caught in the act
# 2026-08-02, 8 minutes into pushing the 4.22 GB verify_world_a6.blend:
#
#     zstd -19 -T6 -c verify_world_a6.blend        190% CPU
#     ssh ... zstd -d -o .../verify_world_a6.part  0.0% CPU     <- starved
#
# That push moved 4216 MB of input in ~490 s = 8.6 MB/s, against a link that
# does 4.0-4.7 MB/s of *compressed* bytes. At 6.55x the compressor was emitting
# only ~1.3 MB/s, so the wire ran at under a third of its capacity for eight
# minutes while a rented GPU sat idle waiting for the scene. Note also the
# 190%: -T6 does not get six threads on a 4-core i7-7700K that is also running
# a Blender assembly, so -19 is even slower in practice than in a clean
# benchmark (20 MB/s isolated, 8.6 MB/s here).
#
# Level 10 moves the bottleneck back onto the wire, which is where it belongs
# when the wire is the thing you cannot make faster: ~830 MB out at 5.08x is
# ~207 s of wire, and the compression that feeds it is no longer the limit.
# Isolated rates on this box, for whoever re-tunes this:
#
#     zstd -3  -T6   598 MB/s   4.05x
#     zstd -10 -T6   143 MB/s   5.08x
#     zstd -19 -T6    20 MB/s   6.55x    (8.6 MB/s under real load)
#
# Do not raise this without re-measuring BOTH stages under load. Ratio is not
# the thing being optimised; time to get the scene onto the GPU is.
SCENE_ZSTD_LEVEL = _env("SCENE_ZSTD_LEVEL", 10)

# The level used once a scene is found to be ALREADY COMPRESSED, where every
# level does the same nothing and the only question is how long it takes to
# discover that. Level 1 is framing and a token search: ~600 MB/s, effectively
# free.
#
# This is not hypothetical. `world/items/spectator_crowd_test.blend` was saved
# with Blender's "Compress File" on, making it a 602 MB zstd frame; -19 spent
# 59 s re-compressing it to a 1.06x ratio, i.e. 59 s of a rented GPU's idle
# time to save 35 MB. The README has always said never to enable that
# preference, but the broker must not be the thing that punishes a scene for
# it — a scene arrives however the artist saved it.
SCENE_ZSTD_LEVEL_PRECOMPRESSED = _env("SCENE_ZSTD_LEVEL_PRECOMPRESSED", 1)

# Below this, skip the probe and just use SCENE_ZSTD_LEVEL. A small scene's
# whole compression is under a second at any level, so measuring which one to
# use costs more than picking wrong.
SCENE_ZSTD_PROBE_MIN_MB = _env("SCENE_ZSTD_PROBE_MIN_MB", 64.0)

# How much of a large scene to test-compress, and the ratio below which the
# payload is called incompressible. 48 MB at level 3 is ~0.08 s. The threshold
# sits well clear of both observed populations: an already-zstd-framed blend
# probes at 1.02-1.06, a normal assembly at 4.05.
SCENE_ZSTD_PROBE_MB = _env("SCENE_ZSTD_PROBE_MB", 48.0)
SCENE_ZSTD_MIN_RATIO = _env("SCENE_ZSTD_MIN_RATIO", 1.15)

# --- blank-frame detection ------------------------------------------------
#
# Thresholds for `broker/imgstat.py`, all on luminance normalised to 0..1.
#
# Derived from the 240 frames this farm had already returned when
# `out/0908e534b1d3.png` came back structurally perfect and entirely black.
# Sorted by standard deviation, that corpus reads:
#
#     sd 0.00000  mean 0.00000   1 level     <- the defect
#     sd 0.00794  mean 0.77401  14 levels    <- a flat grey 4K frame, also wrong
#     sd 0.03494  mean 0.30798  212 levels   <- the flattest LEGITIMATE frame
#     ... 237 more, up to sd 0.34069
#
# There is an empty gap between 0.008 and 0.035, and the thresholds sit in it.
#
# One 8-bit quantisation step is 1/255 = 0.00392, so BLANK_SD_MAX = 0.005 says
# "less than one and a third grey levels of variation across the whole frame" —
# a frame that is flat in the only sense a PNG can express. It is 7x below the
# flattest real frame ever returned here, so a legitimate render reaching it is
# not a tuning question, it is an image with nothing in it.
BLANK_SD_MAX = _env("BLANK_SD_MAX", 0.005)

# Below this mean as well, a flat frame is specifically BLACK rather than merely
# uniform. Same 1.3-quantisation-step reasoning: everything is 0 or 1.
BLACK_MEAN_MAX = _env("BLACK_MEAN_MAX", 0.005)

# Every pixel at or under this alpha means the frame is entirely transparent —
# `film_transparent` with nothing composited behind it. Exact zero would be the
# honest test; a hair above it costs nothing and survives a denoiser that leaves
# a stray 1/255 in the alpha channel.
BLANK_ALPHA_MAX = _env("BLANK_ALPHA_MAX", 0.004)

# Reported loudly, never fatal. 0.02 is roughly five grey levels of spread; it
# sits above the flat-grey 4K frame at 0.00794 and below the flattest genuine
# render at 0.03494, so on the corpus it flags exactly the two frames a human
# should have been shown and none of the other 238.
SUSPECT_SD_MAX = _env("SUSPECT_SD_MAX", 0.02)

# A second, independent way to be featureless: a megapixel image quantised onto
# a handful of luminance levels. Cycles output is noisy even after denoising and
# never lands here; a solid fill or a blown-out plate does.
SUSPECT_LEVELS_MAX = _env("SUSPECT_LEVELS_MAX", 16)
SUSPECT_MIN_PIXELS = _env("SUSPECT_MIN_PIXELS", 4096)

# --- sequence-level, relative --------------------------------------------
#
# For a single unbroken 124-second take the strong signal is not "this frame is
# dark", it is "this frame is nothing like frames 1,590-1,610". A fade walks a
# whole neighbourhood down together and must pass; one dropped frame must not.
SEQ_OUTLIER_WINDOW = _env("SEQ_OUTLIER_WINDOW", 25)      # neighbours compared against
SEQ_OUTLIER_Z = _env("SEQ_OUTLIER_Z", 8.0)               # robust z, in MADs

# Absolute floors, required in ADDITION to the z-score. A stretch of nearly
# identical frames has MAD ~0, where every frame is infinitely many MADs from
# the median — without these floors a perfectly good static shot flags every
# frame in it. 0.02 mean luminance is ~5 grey levels: below that, nobody can see
# the difference and it is not worth a re-render.
SEQ_OUTLIER_MEAN_FLOOR = _env("SEQ_OUTLIER_MEAN_FLOOR", 0.02)
SEQ_OUTLIER_SD_FLOOR = _env("SEQ_OUTLIER_SD_FLOOR", 0.01)
SEQ_OUTLIER_MIN_FRAMES = _env("SEQ_OUTLIER_MIN_FRAMES", 8)

# Whether a blank frame fails its job. Per-job `allow_blank` in the spec
# overrides this for one caller; this is the farm-wide default and exists so the
# check can be switched off in an emergency without editing code. Turning it off
# means a black frame is delivered, recorded done, and skipped by every future
# resume — which is exactly the failure this was built for.
BLANK_FAILS_JOB = _env("BLANK_FAILS_JOB", True)


def _asset_dirs() -> list[Path]:
    """Local directories mirrored to the instance at their ABSOLUTE paths.

    A .blend that was not packed stores external references — HDRIs, textures,
    caches — as absolute paths, and the broker ships only the .blend itself.
    Observed on a live instance: every remote frame rendered with
    `WARNING Image file /home/zany/opus5-car-render/assets/city.exr does not
    exist` followed by `ERROR Failed to load 1 image files`, so the returned
    image was lit differently from the one the artist sees locally — and
    nothing in the broker log said a word about it.

    This is the explicit, global override only. Per-scene discovery lives in
    `scenes.asset_dirs_for`, because jobs choose their own scene and different
    variants can sit in different trees — mirroring once at deploy time from
    one scene's neighbourhood would leave the others short.

    Anything either route misses is caught loudly by the post-deploy
    missing-asset check rather than silently altering the render.
    """
    raw = os.environ.get("VASTRENDER_ASSET_DIRS")
    if raw is None:
        return []
    return [Path(p) for p in raw.split(":") if p.strip()]


ASSET_DIRS = _asset_dirs()

# --- local ----------------------------------------------------------------

BROKER_HOST = _env("HOST", "127.0.0.1")
BROKER_PORT = _env("PORT", 8760)
DB_PATH = Path(_env("DB", str(ROOT / "state" / "broker.db")))
OUT_DIR = Path(_env("OUT", str(ROOT / "out")))
# Frame sequences, one directory per named sequence. Separate from OUT_DIR
# because a 3,000-frame batch would otherwise bury every still ever rendered,
# and because the directory itself is the deliverable ffmpeg reads.
SEQ_DIR = Path(_env("SEQ_DIR", str(OUT_DIR / "seq")))
LOG_DIR = ROOT / "state"

# One broker per state directory, enforced with flock before anything else
# happens at startup. A second broker used to adopt the running instance during
# lifespan startup and then destroy it when its port bind failed, so this is a
# money guard, not hygiene. See broker/lock.py.
LOCK_PATH = Path(_env("LOCK", str(ROOT / "state" / "broker.lock")))

# --- remote ---------------------------------------------------------------

WORKER_PORT = _env("WORKER_PORT", 8799)   # on the instance, reached via tunnel
REMOTE_ROOT = _env("REMOTE_ROOT", "/workspace")
SSH_KEY = Path(_env("SSH_KEY", str(Path.home() / ".ssh" / "id_vast_render")))

# Blender build to install on the instance. Must match the local version that
# assembled the scene, and must be >= the release carrying sm_120 kernels.
BLENDER_VERSION = _env("BLENDER_VERSION", "5.2.0")
# If this bundle exists it is pushed from here instead of fetching from
# blender.org. Build it with scripts/make_blender_bundle.sh.
BLENDER_BUNDLE = Path(_env("BLENDER_BUNDLE", str(ROOT / "state" / "blender-5.2.0.tar.zst")))

BLENDER_URL = _env(
    "BLENDER_URL",
    f"https://download.blender.org/release/Blender{BLENDER_VERSION.rsplit('.', 1)[0]}"
    f"/blender-{BLENDER_VERSION}-linux-x64.tar.xz",
)

# --- lifecycle ------------------------------------------------------------

# Three-stage lifecycle: running -> stopped -> destroyed.
#
# Stopping ends GPU billing immediately while keeping the disk, which costs
# ~$0.014/hr on a 30 GB volume. That 1.4 cents/hour buys back the expensive part
# of a cold start — the image pull, the Blender install, and the 63 MB scene
# upload — none of which have to happen again on resume.
#
# The tradeoff is that a *stopped* container runs no watchdog, so the
# in-container dead-man switch cannot protect this state. HIBERNATE_SEC is the
# broker-side backstop, and the startup reap catches anything a crash leaves.
IDLE_GRACE_SEC = _env("IDLE_GRACE", 300)      # running -> stopped
HIBERNATE_SEC = _env("HIBERNATE", 3600)       # stopped -> destroyed

# How long the idle timer may be unable to ask the instance what it is doing
# before it stops it anyway.
#
# An unanswered progress probe is not permission to stop a GPU — an idle queue
# is not an idle GPU, and this broker has already stopped an instance that was
# at 99% and 420 W on an 8K frame. But an instance that can never be reached
# must not bill forever either, so the refusal is bounded.
#
# 2700 s is deliberately longer than the in-container watchdog's 30-minute
# heartbeat deadline (HEARTBEAT_STALE_SEC in vastctl): the heartbeat rides the
# same SSH channel, so a genuinely unreachable instance destroys itself before
# this bound is ever reached, and this branch only fires for the strange middle
# case where SSH answers the heartbeat but not the progress probe. When the
# watchdog deadline moves, this must move with it and stay LONGER — at 1800/1800
# the invariant silently stopped holding and the broker could blind-stop a box
# the watchdog was about to handle properly.
IDLE_UNKNOWN_MAX_SEC = _env("IDLE_UNKNOWN_MAX", 2700.0)

POLL_INTERVAL = _env("POLL_INTERVAL", 10.0)   # vast rate-limits per IP; do not lower
HEARTBEAT_INTERVAL = _env("HEARTBEAT_INTERVAL", 60.0)

# How often to read the worker's progress file off the instance. One cheap SSH
# command over the existing ControlMaster; 15 s is invisible next to a
# multi-minute frame and still gives a live-looking counter.
PROGRESS_INTERVAL = _env("PROGRESS_INTERVAL", 15.0)

# Warn — never kill — when a running job's sample counter has not advanced for
# this long. Generous on purpose, and for two independent reasons.
#
# Cycles reports at adaptive-sampling checkpoints, not per sample, so the gap
# between updates is however long a batch takes; measured locally, one batch
# covered 191 samples in a single 22 s jump with nothing in between. On a
# 7680x4320 frame the initial scene sync and the first batch can each run for
# minutes with the counter legitimately frozen.
#
# And killing is the expensive mistake. This session has repeatedly destroyed
# healthy work by concluding it was wedged — a 481 MB upload, a rented GPU, and
# a fully pre-warmed worker. So this only ever logs, and a human decides.
STALL_WARN_SEC = _env("STALL_WARN_SEC", 600.0)

# How long to keep reattaching to a render whose job socket dropped.
#
# The worker writes its PNG to disk independently of the connection that asked
# for it, so a dead tunnel costs a connection, not the frame. Generous because
# the frame it is protecting is the expensive one: a 7680x4320 @ 8192-sample
# render measured 2425 s, and re-running it because a forwarded port blinked is
# the single most expensive mistake this broker can make.
REATTACH_SEC = _env("REATTACH_SEC", 5400.0)
JOB_LEASE_SEC = _env("JOB_LEASE", 3600.0)
MAX_ATTEMPTS = _env("MAX_ATTEMPTS", 3)

# Volume size requested from vast.ai at `create`. NOT what the filesystem turns
# out to be — every disk decision downstream measures with `df` on the instance
# instead (see DISK_RESERVE_GB), because the two have disagreed.
#
# Raised 30 -> 60 on 2026-08-04, and the reason is the working set, not a guess:
#
#     largest scene on disk        5.22 GB   (render/film9_breach.blend)
#     film-scene family            ~5.0 GB   each, eight of them
#     measured live on a 32.2 GB volume:  22.3 GB used (69%),
#         cache 20.84 GB in 13 scenes against a derived 23.0 GB budget
#
# So the 30 GB volume was already running at 69 % with the eviction loop
# working continuously to stay there. 60 GB doubles the headroom, and the
# supply cost of asking for it is ZERO: measured across the full production
# filter on 2026-08-04, `disk_space>45`, `>75` and `>135` all returned the
# same 19 shared / 8 exclusive machines. Nothing is excluded by asking.
#
# The money is negligible at this size — storage runs $0.13-0.40/GB/month, so
# 60 GB is roughly $0.008-0.033/hr against a ~$0.42/hr box.
#
# **This is sized for ONE resident scene, not eight.** A multi-worker instance
# pins one scene per worker, so eight workers is 8 x 5.2 GB = 41.6 GB of
# unevictable cache before Blender, the image, frames in flight and the
# reserve — and `protected_scenes()` would make every one of them unevictable
# at once. That build needs ~300 GB and it needs to raise this deliberately;
# do not discover it mid-master. See docs/multi-gpu.md.
DISK_GB = _env("DISK_GB", 60)

# Deploy retries, per dispatch pass and per instance.
#
# A dropped 481 MB upload is a transport problem, not broken hardware, so the
# broker retries it on the same GPU: DEPLOY_ATTEMPTS times immediately, then
# again on later dispatch passes up to MAX_TRANSPORT_ROUNDS rounds. Only once
# that budget is spent — or the moment a failure looks host-level rather than
# transport-level — is the instance destroyed and replaced. Getting this wrong
# costs a fresh rental plus an image pull plus another half-gigabyte push, which
# is exactly what a single failed bundle push used to trigger.
DEPLOY_ATTEMPTS = _env("DEPLOY_ATTEMPTS", 3)
MAX_TRANSPORT_ROUNDS = _env("MAX_TRANSPORT_ROUNDS", 3)

# How many concurrent SSH connections a bulk push opens.
#
# Eight is NOT a throughput setting. Measured: 4.02 MB/s across 8 streams
# against 4.68 MB/s on one, because the ceiling is the local line's real upload
# capacity and not a per-connection window. Eight is there for robustness — a
# transfer that lost one stream kept the other seven — and 8 is also the most
# that fits comfortably under sshd's default MaxStartups of 10:30:100 alongside
# the broker's own ControlMaster connection.
PUSH_STREAMS = _env("PUSH_STREAMS", 8)

# Consecutive "every stream was reset by the far end" failures before the push
# drops to a single stream.
#
# Two, not one. One such failure is a coincidence worth retrying at full width;
# two in a row is a pattern, and the single-stream retry is the experiment that
# settles what caused it — one connection cannot trip a connection-rate or
# MaxStartups limit, so if it is reset the same way the host is at fault and
# not our concurrency. Cheap to be wrong in this direction: the fallback costs
# ~14% of throughput on a link that is not the bottleneck anyway.
PUSH_SERIAL_AFTER = _env("PUSH_SERIAL_AFTER", 2)

# Deploy rounds that may fail on transport WITHOUT DELIVERING A SINGLE NEW BYTE
# before the instance is condemned, its offer and machine blacklisted, and a
# different one rented.
#
# This is the exit the retry policy did not have. The skill's rule — "a failed
# transfer is a TRANSPORT problem and must not by itself condemn an instance" —
# is right, and it assumes the retry can eventually succeed. On 2026-08-02 it
# could not: machine 55313 reset every connection it was given, and the broker
# spent 80 minutes and $0.41 of GPU on instance 46579745 relearning that, then
# rented the same offer again and started over.
#
# The counter is NOT "rounds failed". It is "rounds failed having moved nothing",
# and that distinction is the whole design. Pushes resume, so a link that is
# merely flaky *must* show its high-water mark climbing — it keeps whatever
# bytes it lands. A round that ends with the instance holding no more of the
# bundle than it did before is a round that achieved literally nothing, and no
# number of further rounds will differ. So a genuinely transient failure keeps
# its full MAX_TRANSPORT_ROUNDS budget and more, because any progress at all
# resets this to zero, while a host that cannot deliver is dropped after two.
#
# Two rather than one because one bad round is exactly the hiccup the skill is
# protecting: a container whose sshd is still settling, a momentary drop, a
# reset while our own key was being installed. Two rather than three because
# each round here is up to DEPLOY_ATTEMPTS x 4 push attempts — the third round
# costs another ~25 minutes of billing to confirm what the second already
# established.
MAX_STALLED_ROUNDS = _env("MAX_STALLED_ROUNDS", 2)

# Consecutive heartbeat failures before the broker asks vast.ai whether the
# instance it is beating still EXISTS.
#
# Nothing used to ask. An instance destroyed out of band — by a human, by
# `vastctl reap`, or by a bid being preempted, which is what this looks like in
# the mode a spot fleet would run in — left the broker waiting on an endpoint
# that could not answer, through a 900 s ssh timeout and then a full deploy
# budget, before anything questioned the premise. Three failures is ~3 minutes
# at HEARTBEAT_INTERVAL and is comfortably inside the in-container watchdog's
# 30-minute deadline, so a live instance is never abandoned over a blip.
RECONCILE_AFTER_HEARTBEATS = _env("RECONCILE_AFTER_HEARTBEATS", 3)

# How long a freshly started worker may take to answer its first ping.
#
# "Ready" here means the .blend is open AND the warm-up sweep has run, so this
# budget pays for scene load plus prewarm — both of which scale with the scene,
# and neither of which is bounded by anything the broker controls. It was a
# flat 1800 s, and a flat number is exactly the wrong shape: uselessly generous
# for a 1 MB probe scene, and too tight for a big one.
#
# Measured on `verify_world.blend`, 4.17 GB / 28,391 objects / ~13 billion
# instanced triangles: 12 s to read the blend, then 65-68 s per warm-up render,
# ten cameras x two DOF states = ~23 min before the first ping could possibly
# be answered. The worker was healthy the whole time — load average 99, 22 GB
# resident, the GPU busy — and the probe condemned it anyway, so the broker
# redeployed, which restarted the sweep, forever. The worker-side half of this
# fix bounds the sweep (WORKER_PREWARM_BUDGET in worker/server.py); this half
# accepts that a 4 GB scene genuinely takes minutes to open and that no probe
# should call that a failure.
#
# 1800 s + 300 s/GB gives the 293 MB showroom scene 1888 s (it uses 50) and the
# 4.17 GB circuit 3051 s. Being wrong in this direction costs GPU billing at
# $0.336/hr — 51 minutes is 29 cents — against a redeploy loop that bills the
# same money and never terminates.
WORKER_READY_SEC = _env("WORKER_READY", 1800.0)
WORKER_READY_PER_GB_SEC = _env("WORKER_READY_PER_GB", 300.0)


def worker_ready_budget(scene_bytes: int) -> float:
    """Seconds to allow a worker to load `scene_bytes` and warm up."""
    return WORKER_READY_SEC + WORKER_READY_PER_GB_SEC * (max(scene_bytes, 0) / 1e9)


# Admission control. Dedup is deliberately absent — two agents asking for the
# same frame get two renders, because a params hash cannot see scene state and
# would silently serve stale frames across a reassembly.
MAX_QUEUE_DEPTH = _env("MAX_QUEUE_DEPTH", 200)
MAX_PER_AGENT_QUEUED = _env("MAX_PER_AGENT", 25)

# Hard ceiling for one batch. Prepaid credit is the only cap vast.ai itself
# offers, so this is the software one.
MAX_BATCH_USD = _env("MAX_BATCH_USD", 20.0)

# Normally the broker destroys its instance on exit — an instance must not
# outlive the process responsible for it. Set this when restarting the broker
# to pick up new code while agents are waiting on a warm GPU: the next start
# adopts the instance instead of renting a fresh one.
#
# The safety net is the in-container watchdog, which destroys the box if the
# heartbeat goes stale. So a broker that never comes back still cannot bill
# forever — but only while the instance is *running*; a stopped one has no
# watchdog, and relies on the next broker start to reap it.
KEEP_ON_EXIT = _env("KEEP_ON_EXIT", False)


def ensure_dirs() -> None:
    for d in (DB_PATH.parent, OUT_DIR, SEQ_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
