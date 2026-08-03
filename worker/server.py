#!/usr/bin/env python3
"""Warm Blender render server. Runs *inside* Blender:

    blender -b scene.blend -P worker/server.py -- --port 8799 --out-dir /workspace/out

Holds one scene resident and renders many jobs against it. The reason this
exists, measured on a GTX 1070 with a 208 MB scene:

    first render (sync + BVH + OptiX pipeline)      23-32 s
    subsequent, use_persistent_data off             22-29 s each
    subsequent, use_persistent_data on             0.6-2.2 s

so a cold process per job throws away 15-40x on fixed cost.

Two rules shape the whole design:

**Never thread.** Blender's docs are explicit that Python threads crash it in
hard-to-diagnose ways, specifically during Cycles renders. Accept, decode,
render and reply all happen on the main thread, strictly serially. One render
at a time is also correct on the GPU: concurrent renders timeslice rather than
add throughput.

**Never assume state.** Every knob is set on every job, not just the ones the
request mentions. A warm process makes leaked state a correctness bug — a job
that omits `use_dof` would otherwise silently inherit the previous job's.
Incomplete specs are rejected rather than defaulted, because this server holds
no render policy: the agents that build the models decide everything.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import time
import traceback
from typing import Optional

import bpy

# Every field an agent must supply. There are deliberately no defaults — a
# missing key is a client bug, and guessing at it would make renders depend on
# invisible server state.
REQUIRED = frozenset({
    "job_id", "camera", "resolution", "samples", "engine",
    "denoiser", "denoise_gpu", "use_dof", "film_transparent",
    "border", "zoom", "exposure", "max_bounces", "adaptive_threshold",
    # --- added for frame-sequence rendering ---
    # Which frame to render. `null` means "whatever frame the scene is on",
    # which is what every still job meant before this existed. An integer sets
    # the frame, which is what makes an animation possible at all.
    "frame",
    # Cycles' persistent-data toggle. Explicit rather than assumed because it is
    # the single biggest lever on a long sequence: it keeps BVH and sync results
    # between frames (large win when only a few objects move) at the cost of
    # holding them in memory for the whole run.
    "persistent_data",
    # Refuse the frame if a physics cache does not cover it, instead of letting
    # Blender silently re-simulate. See `cache_report`.
    "require_caches",
})


def log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


# --- render progress ------------------------------------------------------
#
# A 35-minute 8K frame used to be indistinguishable from a wedged one: the
# worker is strictly serial, so `bpy.ops.render.render()` blocks the main
# thread and the socket cannot even answer a ping, and worker.log gained
# nothing until the render finished.
#
# Progress therefore has to leave the process by a route that does not need the
# main thread: a small JSON file the broker reads over SSH.
#
# The source is `bpy.app.handlers.render_stats`, chosen over reading the
# process's stdout after measuring both against Blender 5.2:
#
#   * stdout carries NOTHING when rendering via `bpy.ops.render.render()` from
#     a script — measured 0 occurrences of "Sample" over a 20 s render, total
#     stdout 253 bytes. Blender only prints those `Fra:1 ... Sample N/M` lines
#     from its own command-line render path. Tailing stdout would have captured
#     nothing at all, which is exactly what the frozen 1661-byte worker.log was
#     already telling us.
#   * render_stats fires on the main thread inside the render, so it needs no
#     background thread. This module's first rule is never to thread, because
#     Python threads crash Blender during Cycles renders.
#
# One measured caveat drove the parser: the callback only carries intermediate
# sample counts when **adaptive sampling is on**. With it off, Blender reports
# Sample 0, Sample 1, then silence until the final sample — verified at several
# resolutions and sample counts. `apply_spec` always enables adaptive sampling
# for Cycles, so the production path is the good one, but the stall watchdog
# must stay tolerant rather than assume a steady tick.

PROGRESS: dict = {"state": "idle"}
PROGRESS_PATH: str = ""
_last_write = [0.0]
PROGRESS_MIN_INTERVAL = 0.5     # seconds between writes when nothing changed

_SAMPLE_RE = re.compile(r"Sample\s+(\d+)\s*/\s*(\d+)")
_REMAINING_RE = re.compile(r"Remaining:\s*((?:\d+:)?\d+:\d+(?:\.\d+)?)")
_TIME_RE = re.compile(r"(?<!\w)Time:\s*((?:\d+:)?\d+:\d+(?:\.\d+)?)")
# Big frames are rendered in TILES, and the sample counter restarts at zero for
# every tile: observed live on a 4096-sample frame reporting
# "Rendered 6/12 Tiles, Sample 1824/4096" — the counter had already run 0->4096
# six times. Reporting the raw sample fraction as frame progress showed 94% and
# then dropped back to 15%. Frame progress is tiles plus the current tile's
# fraction, and a per-tile counter that resets is *advancement*, not a stall.
_TILES_RE = re.compile(r"Rendered\s+(\d+)\s*/\s*(\d+)\s+Tiles")


def _clock_to_sec(text: str) -> Optional[float]:
    """`MM:SS.ss` or `HH:MM:SS.ss` -> seconds."""
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts:
        total = total * 60.0 + p
    return total


def _phase_of(text: str) -> str:
    """The human-readable part, minus the numeric fields we parse separately."""
    keep = []
    for seg in text.split("|"):
        seg = seg.strip()
        if not seg or seg.startswith(("Mem:", "Remaining:", "Time:", "Fra:")):
            continue
        keep.append(seg)
    return " | ".join(keep)[:120]


def progress_write(force: bool = False) -> None:
    """Publish atomically. The broker may read this at any instant, and a
    half-written file would look like a corrupt worker rather than a slow one."""
    if not PROGRESS_PATH:
        return
    now = time.time()
    if not force and now - _last_write[0] < PROGRESS_MIN_INTERVAL:
        return
    _last_write[0] = now
    try:
        PROGRESS["updated"] = now
        if PROGRESS.get("started"):
            PROGRESS["elapsed_sec"] = round(now - PROGRESS["started"], 2)
        tmp = PROGRESS_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(PROGRESS, fh)
        os.replace(tmp, PROGRESS_PATH)
    except Exception:
        # Progress reporting must never be able to break a render.
        pass


def on_render_stats(text) -> None:
    """`bpy.app.handlers.render_stats` callback. Must not raise: it runs inside
    the render, and an exception here would surface as a render failure."""
    try:
        text = str(text)
        changed = False

        m = _TILES_RE.search(text)
        if m:
            tile, tiles = int(m.group(1)), int(m.group(2))
            if tile != PROGRESS.get("tile") or tiles != PROGRESS.get("tiles"):
                changed = True
            PROGRESS["tile"] = tile
            PROGRESS["tiles"] = tiles

        m = _SAMPLE_RE.search(text)
        if m:
            sample, total = int(m.group(1)), int(m.group(2))
            if sample != PROGRESS.get("sample") or total != PROGRESS.get("total"):
                changed = True
            PROGRESS["sample"] = sample
            PROGRESS["total"] = total

        # Frame-level percentage, which is the only one worth showing a human.
        sample = PROGRESS.get("sample") or 0
        total = PROGRESS.get("total") or 0
        tiles = PROGRESS.get("tiles") or 0
        within = (sample / total) if total else 0.0
        if tiles:
            frac = ((PROGRESS.get("tile") or 0) + within) / tiles
        else:
            frac = within
        PROGRESS["pct"] = round(100.0 * min(max(frac, 0.0), 1.0), 1) if (total or tiles) else None

        m = _REMAINING_RE.search(text)
        if m:
            PROGRESS["remaining_sec"] = _clock_to_sec(m.group(1))

        m = _TIME_RE.search(text)
        if m:
            PROGRESS["render_time_sec"] = _clock_to_sec(m.group(1))

        phase = _phase_of(text)
        if phase and phase != PROGRESS.get("phase"):
            PROGRESS["phase"] = phase
            changed = True

        progress_write(force=changed)
    except Exception:
        pass


def progress_begin(job_id: str, samples: int, frame: Optional[int] = None) -> None:
    PROGRESS.clear()
    PROGRESS.update({
        "state": "rendering", "job_id": job_id,
        "sample": 0, "total": int(samples), "pct": 0.0,
        "tile": 0, "tiles": 0,
        # Which frame of the sequence this is. The broker shows it while the
        # dispatch thread is blocked inside the render and can say nothing.
        "frame": frame,
        "phase": "starting", "started": time.time(),
        "remaining_sec": None, "elapsed_sec": 0.0,
    })
    progress_write(force=True)


def progress_end(job_id: str, state: str, err: str = "") -> None:
    PROGRESS["state"] = state
    PROGRESS["job_id"] = job_id
    PROGRESS["phase"] = err[:120] if err else state
    PROGRESS["remaining_sec"] = 0.0
    progress_write(force=True)


# --- device ---------------------------------------------------------------


def enable_gpu() -> str:
    """Force GPU compute, preferring OptiX. Never trust saved preferences —
    the config that ships with a container is not the one we want."""
    prefs = bpy.context.preferences.addons["cycles"].preferences
    chosen = None
    for kind in ("OPTIX", "CUDA"):
        try:
            prefs.compute_device_type = kind
        except TypeError:
            continue
        prefs.refresh_devices()
        if any(d.type == kind for d in prefs.devices):
            chosen = kind
            break
    if not chosen:
        raise RuntimeError("no OptiX or CUDA device found — refusing to render on CPU")

    for d in prefs.devices:
        d.use = d.type == chosen
    bpy.context.scene.cycles.device = "GPU"
    names = [d.name for d in prefs.devices if d.use]
    log(f"device={chosen} [{', '.join(names)}]")
    return chosen


# --- job application ------------------------------------------------------


# Scene values captured once at startup. A warm process has no other route back
# to "the scene's own value": once job A sets max_bounces=2, job B asking for
# null would inherit 2 forever. Restoring from this baseline is what makes null
# mean what the protocol says it means.
BASELINE: dict = {}

MAX_PIXELS = 200_000_000   # 200 Mpx; --res 3840 2160 --zoom 7 is 406 Mpx
MAX_REQUEST_BYTES = 1 << 20


class Refused(ValueError):
    """The request is wrong, and rendering it again will not make it right.

    A verdict, not a failure. The broker retries a failed job because most
    failures here are transport — a dropped forward, a reset connection, a
    reaped tunnel — and those genuinely do succeed on the second try. A
    rejection cannot: the same spec produces the same refusal, three times, and
    on this farm each of those attempts drags a scene selection behind it.
    Observed 2026-08-03, verbatim and three times per job:

        job 2e8fa81b1973 requeued: 3840x2160 at zoom 8.0 is 531 Mpx ...
        job 2e8fa81b1973 requeued: 3840x2160 at zoom 8.0 is 531 Mpx ...
        job 2e8fa81b1973 failed:   3840x2160 at zoom 8.0 is 531 Mpx ...

    Worse outside the broker: a client that resubmits on failure turns one bad
    request into unbounded queue depth, and one agent accumulated 27 retry
    processes against an already-contended queue before noticing.

    Replies carrying this are marked `terminal`, and the broker fails them once.
    Retry belongs to transport, never to a verdict.
    """


def capture_baseline(scene: bpy.types.Scene) -> None:
    BASELINE["max_bounces"] = scene.cycles.max_bounces
    BASELINE["exposure"] = scene.view_settings.exposure
    BASELINE["frame"] = scene.frame_current
    BASELINE["persistent_data"] = scene.render.use_persistent_data
    # Per camera, because DOF lives on the camera datablock and the scene may
    # authore a different answer for each one. This is what `use_dof: null`
    # restores, and without it "use the scene's own depth of field" would be
    # unimplementable in a warm process: once one job sets use_dof=False the
    # datablock has no memory of what the artist keyed.
    #
    # Captured BEFORE prewarm, which toggles use_dof on every camera and would
    # otherwise leave the baseline reading whatever prewarm finished on.
    BASELINE["use_dof"] = {
        o.name: bool(o.data.dof.use_dof)
        for o in bpy.data.objects
        if o.type == "CAMERA" and o.data.type != "ORTHO"
    }
    log(f"baseline: max_bounces={BASELINE['max_bounces']} exposure={BASELINE['exposure']} "
        f"frame={BASELINE['frame']} persistent_data={BASELINE['persistent_data']} "
        f"dof={BASELINE['use_dof']}")


def restore_baseline_dof() -> None:
    """Put every camera's DOF back the way the .blend had it."""
    for name, value in (BASELINE.get("use_dof") or {}).items():
        cam = bpy.data.objects.get(name)
        if cam is not None and cam.type == "CAMERA" and cam.data.type != "ORTHO":
            cam.data.dof.use_dof = value


# --- physics caches -------------------------------------------------------
#
# The most dangerous thing about rendering an animation on a rented box is that
# a missing simulation cache does not fail. Blender simulates instead — and a
# simulation stepped from a frame jump is not the simulation that was baked, so
# the destruction differs from the frame before it. In a cut-free video that is
# a delivery-blocking defect that no amount of looking at one frame reveals.
#
# So caches are inspected, reported with every frame, and (by default) a frame
# whose caches do not cover it is REFUSED rather than rendered. Nothing here
# ever bakes, frees or re-points a cache: they ship with the scene and are
# read-only on the instance.


def _cache_dir() -> str:
    """Where Blender puts `//blendcache_*` for the loaded file.

    Not exposed by the API, so it is reconstructed the way Blender builds it:
    the blend's own directory plus `blendcache_<name without extension>`. This
    is why the broker uploads the .blend into a directory of its own under its
    original filename — rename it and every relative cache path misses.
    """
    blend = bpy.data.filepath
    if not blend:
        return ""
    base = os.path.basename(blend)
    return os.path.join(os.path.dirname(blend), "blendcache_" + os.path.splitext(base)[0])


def _cache_entry(kind: str, owner: str, pc) -> dict:
    """One point cache, described in the terms that decide whether it is safe."""
    try:
        files = 0
        directory = _cache_dir()
        if pc.use_external and pc.filepath:
            directory = bpy.path.abspath(pc.filepath)
        if directory and os.path.isdir(directory):
            # `.bphys` is the on-disk point-cache format for every physics type
            # that uses one, and the index in the name ties it to this cache.
            files = sum(1 for f in os.listdir(directory) if f.endswith(".bphys"))
        return {
            "kind": kind, "owner": owner, "name": pc.name or "",
            "index": pc.index,
            "is_baked": bool(pc.is_baked),
            "is_outdated": bool(pc.is_outdated),
            "use_disk_cache": bool(pc.use_disk_cache),
            "use_external": bool(pc.use_external),
            "frame_start": int(pc.frame_start), "frame_end": int(pc.frame_end),
            "frame_step": int(pc.frame_step),
            "directory": directory, "bphys_files": files,
            "info": (pc.info or "")[:120],
        }
    except Exception as exc:                       # never let reporting raise
        return {"kind": kind, "owner": owner, "error": f"{type(exc).__name__}: {exc}"}


def cache_report(scene: bpy.types.Scene) -> list[dict]:
    """Every physics cache in the scene. Empty list means there are none."""
    out: list[dict] = []
    rbw = getattr(scene, "rigidbody_world", None)
    if rbw is not None and getattr(rbw, "point_cache", None) is not None:
        out.append(_cache_entry("RIGIDBODY", "scene.rigidbody_world", rbw.point_cache))
    for ob in scene.objects:
        for mod in getattr(ob, "modifiers", []):
            pc = None
            if mod.type in ("CLOTH", "SOFT_BODY"):
                pc = getattr(mod, "point_cache", None)
            elif mod.type == "DYNAMIC_PAINT":
                canvas = getattr(mod, "canvas_settings", None)
                for surface in (getattr(canvas, "canvas_surfaces", []) if canvas else []):
                    out.append(_cache_entry("DYNAMIC_PAINT", ob.name,
                                            surface.point_cache))
                continue
            elif mod.type == "FLUID":
                dom = getattr(mod, "domain_settings", None)
                if dom is not None:
                    out.append({
                        "kind": "FLUID", "owner": ob.name, "name": "mantaflow",
                        "index": 0,
                        # Mantaflow keeps its own cache, with its own flags and
                        # its own directory — a PointCache it is not.
                        "is_baked": bool(getattr(dom, "has_cache_baked_data", False)),
                        "is_outdated": False,
                        "use_disk_cache": True, "use_external": False,
                        "frame_start": int(getattr(dom, "cache_frame_start", 0)),
                        "frame_end": int(getattr(dom, "cache_frame_end", 0)),
                        "frame_step": 1,
                        "directory": bpy.path.abspath(getattr(dom, "cache_directory", "") or ""),
                        "bphys_files": -1,
                        "info": "",
                    })
                continue
            if pc is not None:
                out.append(_cache_entry(mod.type, ob.name, pc))
        for psys in getattr(ob, "particle_systems", []):
            pc = getattr(psys, "point_cache", None)
            if pc is not None:
                out.append(_cache_entry("PARTICLES", f"{ob.name}/{psys.name}", pc))
    return out


def cache_problems(caches: list[dict], frame: int) -> list[str]:
    """Why this frame cannot be trusted, given these caches. Empty = safe.

    Deliberately strict about the one thing that matters: a cache that does not
    cover this frame means Blender will *simulate* it, and a simulated frame
    reached by a jump does not continue the previous frame's motion.
    """
    problems = []
    for c in caches:
        if "error" in c:
            problems.append(f"{c['kind']} {c['owner']}: could not be inspected ({c['error']})")
            continue
        who = f"{c['kind']} {c['owner']}"
        if not c["is_baked"]:
            problems.append(
                f"{who}: NOT BAKED — Blender will simulate this frame instead of "
                f"reading it, and a simulation reached by jumping to frame {frame} "
                f"does not continue the previous frame"
            )
            continue
        if c["is_outdated"]:
            problems.append(f"{who}: cache is marked OUTDATED (the scene changed since the bake)")
        if not (c["frame_start"] <= frame <= c["frame_end"]):
            problems.append(
                f"{who}: baked range {c['frame_start']}-{c['frame_end']} does not "
                f"cover frame {frame}"
            )
        if not c["use_disk_cache"] and c["kind"] != "FLUID":
            problems.append(
                f"{who}: is a MEMORY cache, not a disk cache — nothing was uploaded "
                f"with the scene and a freshly opened file has it empty"
            )
        elif c["bphys_files"] == 0:
            problems.append(
                f"{who}: disk cache directory {c['directory'] or '(unset)'} holds no "
                f".bphys files on this machine — the bake did not travel with the scene"
            )
    return problems


def apply_spec(scene: bpy.types.Scene, spec: dict) -> None:
    """Set every controlled property from the spec. No property is left alone."""
    cam = bpy.data.objects.get(spec["camera"])
    if cam is None or cam.type != "CAMERA":
        raise ValueError(f"no camera named {spec['camera']!r}")
    scene.camera = cam

    # Validate by assigning, not by inspecting enum_items: Cycles is an addon
    # render engine and does not always appear in the static enum, so checking
    # the enum first rejects the one engine we use most.
    try:
        scene.render.engine = spec["engine"]
    except TypeError as exc:
        known = sorted(e.identifier for e in
                       scene.render.bl_rna.properties["engine"].enum_items)
        raise ValueError(
            f"unknown engine {spec['engine']!r}; this build reports {known}"
        ) from exc

    # Zoom multiplies pixel density before cropping, so a border render is real
    # extra detail rather than an upscale.
    width, height = spec["resolution"]
    zoom = float(spec["zoom"])
    if zoom <= 0:
        raise Refused(f"zoom must be > 0, got {zoom}")
    if width <= 0 or height <= 0:
        raise Refused(f"resolution must be positive, got {width}x{height}")

    border = spec["border"]
    if border:
        min_x, max_x, min_y, max_y = border
        if not all(0.0 <= v <= 1.0 for v in border):
            raise Refused(f"border values must be within 0..1, got {border}")
        if min_x >= max_x or min_y >= max_y:
            raise Refused(
                f"border must be (min_x, max_x, min_y, max_y) with min < max, "
                f"got {border}"
            )

    # The cap exists to stop a job exhausting the GPU's memory, so it has to
    # count the pixels that are actually RENDERED — and with
    # `use_crop_to_border` Blender renders and returns the crop alone, not the
    # frame it was cut from.
    #
    # Counting the full frame made high-density crops, which is what zoom is
    # FOR, the one thing zoom could not do. Measured 2026-08-03: a 0.16 x 0.16
    # border at zoom 8 on a 3840x2160 frame is 4915x2765 = 13.6 Mpx of real
    # work, and it was refused as "531 Mpx, over the 200 Mpx limit" — the size
    # of a frame nobody asked to render. Three agents hit it; one worked around
    # it with a local crop rather than get a 13 Mpx render out of a 5090.
    full_w, full_h = int(width * zoom), int(height * zoom)
    if border:
        min_x, max_x, min_y, max_y = border
        px_w = max(1, int(full_w * (max_x - min_x)))
        px_h = max(1, int(full_h * (max_y - min_y)))
    else:
        px_w, px_h = full_w, full_h
    px = px_w * px_h
    if px > MAX_PIXELS:
        crop = (f" cropped to {px_w}x{px_h}" if border else "")
        raise Refused(
            f"{width}x{height} at zoom {zoom}{crop} is {px / 1e6:.0f} Mpx, over "
            f"the {MAX_PIXELS / 1e6:.0f} Mpx limit"
        )
    scene.render.resolution_x = full_w
    scene.render.resolution_y = full_h
    scene.render.resolution_percentage = 100

    if border:
        min_x, max_x, min_y, max_y = border
        scene.render.use_border = True
        scene.render.use_crop_to_border = True
        scene.render.border_min_x = min_x
        scene.render.border_max_x = max_x
        scene.render.border_min_y = min_y
        scene.render.border_max_y = max_y
    else:
        scene.render.use_border = False
        scene.render.use_crop_to_border = False

    transparent = bool(spec["film_transparent"])
    scene.render.film_transparent = transparent
    # A transparent film still bakes out opaque unless the output format
    # actually carries alpha, and Blender's default is RGB.
    scene.render.image_settings.color_mode = "RGBA" if transparent else "RGB"

    if spec["engine"] == "CYCLES":
        scene.cycles.samples = int(spec["samples"])
        scene.cycles.use_adaptive_sampling = True
        scene.cycles.adaptive_threshold = float(spec["adaptive_threshold"])

        denoiser = spec["denoiser"]
        scene.cycles.use_denoising = denoiser != "NONE"
        if denoiser != "NONE":
            scene.cycles.denoiser = denoiser
            # Without this OpenImageDenoise runs on the CPU even on a 5090.
            if hasattr(scene.cycles, "denoising_use_gpu"):
                scene.cycles.denoising_use_gpu = bool(spec["denoise_gpu"])

        # Restore rather than skip when null: in a warm process, "leave it
        # alone" means "inherit the previous job", which is not what null means.
        scene.cycles.max_bounces = (
            int(spec["max_bounces"]) if spec["max_bounces"] is not None
            else BASELINE["max_bounces"]
        )
    else:
        # EEVEE ignores cycles.samples entirely; without this, --samples is
        # silently dropped and every EEVEE render uses the scene default.
        scene.eevee.taa_render_samples = int(spec["samples"])

    scene.view_settings.exposure = (
        float(spec["exposure"]) if spec["exposure"] is not None
        else BASELINE["exposure"]
    )

    # Cycles keeps sync + BVH between renders when this is on. It is the single
    # biggest lever on a long sequence and the caller owns it, like everything
    # else here. `null` restores what the .blend asked for.
    scene.render.use_persistent_data = (
        bool(spec["persistent_data"]) if spec["persistent_data"] is not None
        else BASELINE["persistent_data"]
    )

    # DOF lives on the camera, so it persists across jobs unless reset here.
    #
    # THREE values, not two. `null` means "use the depth of field the .blend
    # authors, including its animation" and restores the captured baseline;
    # true/false override it. Round 1 lost a render to the two-valued version:
    # a submission that simply omitted `--nodof` got DOF forced ON and came back
    # blurred. For an animation whose focus is keyed on the camera, an override
    # is almost always the wrong answer, so `null` is what the animation client
    # sends by default.
    if cam.data.type != "ORTHO":
        if spec["use_dof"] is None:
            restore_baseline_dof()
        else:
            cam.data.dof.use_dof = bool(spec["use_dof"])


def reassert_overrides(scene: bpy.types.Scene, spec: dict) -> None:
    """Re-apply only the values the job stated explicitly, after a frame change.

    `scene.frame_set()` evaluates the animation system, which overwrites any
    property the scene keyframes — exposure and DOF among them. That is exactly
    what we want for a field the job left as `null` ("use the scene's own"), and
    exactly what we do NOT want for a field the job set: a job asking for
    exposure 0.5 must get 0.5 on every frame, not on the frames that happen not
    to be keyed.

    So: apply everything, set the frame, then re-assert the explicit subset.
    Nothing here restores a baseline — a `null` field is left with whatever the
    animation just evaluated to, which is the whole point.
    """
    if spec["exposure"] is not None:
        scene.view_settings.exposure = float(spec["exposure"])
    if spec["max_bounces"] is not None and spec["engine"] == "CYCLES":
        scene.cycles.max_bounces = int(spec["max_bounces"])
    if spec["use_dof"] is not None:
        cam = bpy.data.objects.get(spec["camera"])
        if cam is not None and cam.type == "CAMERA" and cam.data.type != "ORTHO":
            cam.data.dof.use_dof = bool(spec["use_dof"])
    # Resolution and samples are animatable properties in Blender, and a scene
    # that keys them would silently override the job's numbers on a frame set.
    width, height = spec["resolution"]
    zoom = float(spec["zoom"])
    scene.render.resolution_x = int(width * zoom)
    scene.render.resolution_y = int(height * zoom)
    scene.render.resolution_percentage = 100
    if spec["engine"] == "CYCLES":
        scene.cycles.samples = int(spec["samples"])


# --- output verification --------------------------------------------------


def png_info(path: str) -> dict:
    """Dimensions, size and digest of a PNG this worker just wrote.

    Returned with every frame so the broker can prove the file it fetched is
    byte-for-byte the file that was rendered. A size check alone is not enough:
    the observed corruption in this project (scp leaving 783 KB of a 1.9 MB PNG)
    is caught by size, but a flipped bit in transit is not, and one bad frame in
    a cut-free video is a delivery-blocking defect that survives every visual
    check nobody does on 3,000 frames.
    """
    info: dict = {"bytes": 0, "width": None, "height": None,
                  "sha256": "", "complete": False}
    try:
        size = os.path.getsize(path)
        info["bytes"] = size
        digest = hashlib.sha256()
        head = b""
        tail = b""
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                if not head:
                    head = chunk[:33]
                digest.update(chunk)
                tail = (tail + chunk)[-12:]
        info["sha256"] = digest.hexdigest()
        if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            info["width"] = int.from_bytes(head[16:20], "big")
            info["height"] = int.from_bytes(head[20:24], "big")
        info["complete"] = bool(info["width"]) and b"IEND" in tail
    except OSError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def render_to(path: str) -> None:
    scene = bpy.context.scene
    scene.render.filepath = path
    scene.render.image_settings.file_format = "PNG"
    bpy.ops.render.render(write_still=True)


# --- warm-up --------------------------------------------------------------


# How long the whole warm-up sweep may run before it stops early.
#
# Prewarm exists to move a fixed cost off the first job. On every scene this
# farm had rendered until now that cost was seconds, so sweeping every camera
# in every feature-set variant was free and the sweep was unbounded.
#
# It is not free on a big one. `verify_world.blend` — 4.17 GB, 28,391 objects,
# ~13 billion instanced triangles — costs **65-68 s per prewarm render** even at
# 64x64 and 1 sample, because the time is scene synchronisation, not shading.
# Ten cameras x two DOF states is 20 of them: **~23 minutes** of a rented GPU
# before the worker will answer a single ping, against a readiness budget that
# was 1800 s. The readiness probe then condemned a perfectly healthy instance,
# the "repair" was a redeploy, and the redeploy restarted the same 23-minute
# sweep from zero — forever.
#
# So the sweep is now bounded, and the bound is wall-clock rather than a render
# count, because the whole problem is that one render's cost varies by three
# orders of magnitude between scenes. Small scenes are unaffected: their entire
# sweep finishes inside a couple of seconds and never reaches the bound.
PREWARM_BUDGET_SEC = float(os.environ.get("WORKER_PREWARM_BUDGET", "180"))


def _prewarm_plan(scene, cams: list[str]) -> list[tuple[str, bool]]:
    """(camera, use_dof) steps, cheapest ordering first.

    Two properties, in priority order:

    **The scene's own active camera is covered first, in both DOF states.**
    It is the one the next job is most likely to ask for, and it is the only
    part of the sweep that runs even when the budget is already blown.

    **Everything after it is grouped by DOF, not by camera.** DOF on/off is a
    different Cycles *kernel feature set*: flipping it resets the session and
    forces a full re-sync, which is what actually costs the 65 s. The old
    `for cam: for dof in (True, False)` ordering flipped it on every single
    step — 20 full re-syncs for 10 cameras. Grouping flips it once, so the
    remaining cameras pay only the (measured 0.97 s) camera-switch cost.
    """
    active = scene.camera.name if scene.camera is not None and scene.camera.name in cams else None
    ordered = ([active] if active else []) + [c for c in cams if c != active]

    def steps(name: str, dofs) -> list[tuple[str, bool]]:
        # An ortho camera has no DOF to vary, so it gets exactly one step.
        if bpy.data.objects[name].data.type == "ORTHO":
            return [(name, False)]
        return [(name, d) for d in dofs]

    plan: list[tuple[str, bool]] = []
    if active:
        plan += steps(active, (False, True))
    rest = [c for c in ordered if c != active]
    # Start at True to continue from where the active camera's pair ended, so
    # the whole tail costs one feature-set flip rather than one per camera.
    for dof in (True, False):
        for name in rest:
            step = steps(name, (dof,))
            if step and step[0] not in plan:
                plan += step
    return plan


def prewarm(out_dir: str) -> dict:
    """Pay the one-time costs before the first real job arrives.

    Two distinct costs hide here. OptiX ships PTX and JITs per driver and GPU
    (~9 s, cached on disk). Separately, the first use of a camera cost 9.5 s
    versus 0.97 s on repeat — an OptiX pipeline rebuild for a new kernel
    feature set. Cameras are discovered from the scene rather than configured,
    so this stays correct no matter what the agents build.

    Bounded by PREWARM_BUDGET_SEC. A skipped camera is not a broken one: it
    pays its own first-use cost inside the first job that asks for it, which
    is a slow frame — where an unbounded sweep is an instance that never comes
    up at all. Whatever is skipped is named in the log, so a slow first frame
    on camera X has an explanation sitting right above it.
    """
    scene = bpy.context.scene
    cams = [o.name for o in bpy.data.objects if o.type == "CAMERA"]
    plan = _prewarm_plan(scene, cams)
    active = scene.camera.name if scene.camera is not None else None
    log(f"prewarm: {len(cams)} cameras {cams} — active={active}, "
        f"{len(plan)} step(s), budget {PREWARM_BUDGET_SEC:.0f}s")

    saved = (scene.render.resolution_x, scene.render.resolution_y, scene.cycles.samples)
    saved_cam = scene.camera
    scene.render.resolution_x = scene.render.resolution_y = 64
    scene.cycles.samples = 1
    scrap = os.path.join(out_dir, ".prewarm.png")

    # The active camera's own pair is mandatory: a worker that pre-warmed
    # nothing at all would hand the entire sync cost to the first job while
    # still having spent the time deciding not to.
    mandatory = sum(1 for name, _ in plan if name == active) if active else 0

    timings: dict = {}
    began = time.time()
    skipped: list[str] = []
    for i, (name, dof) in enumerate(plan):
        spent = time.time() - began
        if i >= mandatory and spent >= PREWARM_BUDGET_SEC:
            skipped = [f"{n}{'+dof' if d else ''}" for n, d in plan[i:]]
            log(f"prewarm: stopping after {i}/{len(plan)} step(s) — {spent:.0f}s spent "
                f"of a {PREWARM_BUDGET_SEC:.0f}s budget. This scene costs "
                f"{spent / max(i, 1):.1f}s per warm-up render, so the full sweep would "
                f"take ~{spent / max(i, 1) * len(plan) / 60:.0f} min and the readiness "
                f"probe would condemn a healthy instance. Not warmed: {skipped}. "
                f"Those cameras pay their first-use cost inside their first job.")
            break
        cam = bpy.data.objects[name]
        if cam.data.type != "ORTHO":
            cam.data.dof.use_dof = dof
        scene.camera = cam
        t0 = time.time()
        render_to(scrap)
        timings[f"{name}{'+dof' if dof else ''}"] = round(time.time() - t0, 2)

    scene.render.resolution_x, scene.render.resolution_y, scene.cycles.samples = saved
    # Leaving the scene pointed at whichever camera the sweep happened to end on
    # would silently change what `camera: null` means to the first job.
    scene.camera = saved_cam
    # This loop leaves every camera with use_dof=False. Harmless while every job
    # states use_dof explicitly; a correctness bug the moment one says "use the
    # scene's own", which animation jobs do by default. Put them back.
    restore_baseline_dof()
    if os.path.exists(scrap):
        os.remove(scrap)
    log(f"prewarm done in {time.time() - began:.1f}s: {timings}"
        + (f" (skipped {len(skipped)})" if skipped else ""))
    return timings


# --- protocol -------------------------------------------------------------


def handle(spec: dict, out_dir: str) -> dict:
    missing = REQUIRED - spec.keys()
    if missing:
        raise ValueError(f"incomplete spec, missing: {sorted(missing)}")

    # Defence in depth: the broker mints ids, but the worker builds a path from
    # this and must not trust an upstream that might not have.
    job_id = str(spec["job_id"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        raise ValueError(f"unsafe job_id {job_id!r}")

    scene = bpy.context.scene
    t0 = time.time()
    apply_spec(scene, spec)

    # The frame comes AFTER the spec and the overrides come after the frame.
    # Order is load-bearing and each step undoes part of the last on purpose:
    #
    #   apply_spec   — everything set, `null` fields restored to the .blend's
    #                  own values so nothing leaks in from the previous job
    #   frame_set    — the animation system overwrites every keyed property,
    #                  which is what "use the scene's own DOF/exposure" means
    #   reassert     — the job's explicit values win back over the animation
    frame = spec["frame"]
    if frame is not None:
        try:
            frame = int(frame)
        except (TypeError, ValueError):
            raise ValueError(f"frame must be an integer or null, got {spec['frame']!r}") from None
    else:
        # The BASELINE frame, never `scene.frame_current`. In a warm process
        # frame_current is whatever the previous job left behind — after a
        # 500-frame sequence it is frame 500 — so reading it here made every
        # still that said `frame: null` silently render the last frame anybody
        # else asked for, pass every transport check, and reply ok. `null`
        # means "the scene's own frame", and like every other null field that
        # is the value captured at load, restored explicitly.
        frame = int(BASELINE.get("frame") or scene.frame_current)
    scene.frame_set(frame)
    reassert_overrides(scene, spec)
    t_apply = time.time() - t0

    # Asked after the frame is set, because a cache's coverage is a question
    # about this frame. Never bakes, never frees, never re-points anything.
    caches = cache_report(scene)
    problems = cache_problems(caches, frame) if caches else []
    if problems and spec["require_caches"]:
        # A verdict about the scene, not a failure of this attempt: the same
        # blend at the same frame has no cache on the second try either.
        raise Refused(
            f"frame {frame} refused: physics caches do not cover it, so Blender "
            f"would SIMULATE rather than read them and this frame would not "
            f"continue the previous one — "
            + " ; ".join(problems)
            + ". Bake and ship the caches with the scene, or resubmit with "
              "require_caches=false if the difference is genuinely acceptable."
        )

    out = os.path.join(out_dir, f"{job_id}.png")
    t1 = time.time()
    # Published before the render starts, so a job that dies during scene sync
    # is still visible as "this job, in this phase" rather than as nothing.
    progress_begin(job_id, spec["samples"], frame)
    try:
        render_to(out)
    except BaseException as exc:
        progress_end(job_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    t_render = time.time() - t1

    png = png_info(out)
    if not png["bytes"]:
        progress_end(job_id, "failed", "render produced no output")
        raise RuntimeError(f"render produced no output at {out}")
    if not png["complete"]:
        # Caught here rather than after the fetch: a truncated PNG on the
        # instance is the worker's fault, and reporting it as a transfer problem
        # would send the broker chasing the network.
        progress_end(job_id, "failed", "render wrote an incomplete PNG")
        raise RuntimeError(
            f"render at {out} is not a complete PNG ({png['bytes']} bytes, "
            f"{png.get('width')}x{png.get('height')}, IEND "
            f"{'present' if png['complete'] else 'MISSING'})"
        )
    progress_end(job_id, "done")

    cam = bpy.data.objects.get(spec["camera"])
    return {
        "ok": True,
        "job_id": job_id,
        "path": out,
        "bytes": png["bytes"],
        "apply_sec": round(t_apply, 3),
        "render_sec": round(t_render, 3),
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        # What was ACTUALLY rendered, so a caller can verify a frame rather than
        # trust that the request was honoured. Temporal continuity across a
        # batch boundary is a defect category in its own right, and it is
        # checkable only if every frame says what state produced it.
        "frame": frame,
        "png": {"width": png["width"], "height": png["height"],
                "sha256": png["sha256"]},
        "effective": {
            "dof": bool(cam.data.dof.use_dof)
            if cam is not None and cam.data.type != "ORTHO" else None,
            "focus_distance": round(float(cam.data.dof.focus_distance), 6)
            if cam is not None and cam.data.type != "ORTHO" else None,
            "lens": round(float(cam.data.lens), 4) if cam is not None else None,
            "exposure": round(float(scene.view_settings.exposure), 6),
            "motion_blur": bool(scene.render.use_motion_blur),
            "shutter": round(float(getattr(scene.render, "motion_blur_shutter", 0.0)), 4),
            "fps": scene.render.fps / max(scene.render.fps_base, 1e-9),
            "persistent_data": bool(scene.render.use_persistent_data),
        },
        "caches": caches,
        "cache_problems": problems,
    }


def serve(port: int, out_dir: str, host: str = "127.0.0.1", progress_path: str = "") -> None:
    global PROGRESS_PATH
    os.makedirs(out_dir, exist_ok=True)
    PROGRESS_PATH = progress_path or os.path.join(out_dir, ".progress.json")
    # Registered once for the life of the process. The handler is a no-op until
    # progress_begin names a job, so prewarm's throwaway renders do not publish.
    bpy.app.handlers.render_stats.append(on_render_stats)
    scene = bpy.context.scene

    # The entire reason this server exists: keep sync/BVH results between jobs.
    # It is on scene.render, not scene.cycles.
    scene.render.use_persistent_data = True
    enable_gpu()
    # Must run before prewarm, which mutates samples and resolution.
    capture_baseline(scene)
    prewarm(out_dir)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(64)
    PROGRESS.update({"state": "idle", "phase": "waiting for work"})
    progress_write(force=True)
    log(f"ready on {host}:{port} — newline-delimited JSON, serial "
        f"(progress -> {PROGRESS_PATH})")

    jobs = 0
    started = time.time()
    while True:
        try:
            conn, _ = srv.accept()
        except OSError as exc:
            log(f"accept failed: {exc}")
            continue

        # Every per-connection failure is contained here. Without this, a client
        # that stalls (socket.timeout), vanishes mid-render (ConnectionReset),
        # or gives up before the reply (BrokenPipe) raises straight out of the
        # loop and takes Blender with it — losing the resident scene and every
        # queued job. Over an SSH tunnel those are routine, not exotic.
        try:
            conn.settimeout(120)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_REQUEST_BYTES:
                    raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
            if not buf.strip():
                continue

            try:
                spec = json.loads(buf)
            except json.JSONDecodeError as exc:
                conn.sendall(json.dumps({"ok": False, "error": f"bad json: {exc}"}).encode() + b"\n")
                continue

            if spec.get("cmd") == "ping":
                # started_at lets the broker tell a freshly launched worker from
                # a stale one that survived a restart and still holds an old
                # scene.
                conn.sendall(json.dumps(
                    {"ok": True, "jobs": jobs, "started_at": started}
                ).encode() + b"\n")
                continue
            if spec.get("cmd") == "scan":
                # Everything about the loaded scene a caller needs to plan a
                # sequence: cameras, the frame range the .blend declares, fps,
                # and the state of every physics cache. Cheap, and it answers
                # "will this batch silently re-simulate?" before any GPU time is
                # spent on it rather than after 3,000 frames.
                sc = bpy.context.scene
                caches = cache_report(sc)
                conn.sendall(json.dumps({
                    "ok": True,
                    "blend": bpy.data.filepath,
                    "scene": sc.name,
                    "cameras": [o.name for o in bpy.data.objects if o.type == "CAMERA"],
                    "frame_start": sc.frame_start, "frame_end": sc.frame_end,
                    "frame_step": sc.frame_step, "frame_current": sc.frame_current,
                    # What `frame: null` renders: the frame captured at load,
                    # not frame_current, which is merely the last job's leftover.
                    "frame_baseline": BASELINE.get("frame"),
                    "fps": sc.render.fps / max(sc.render.fps_base, 1e-9),
                    "resolution": [sc.render.resolution_x, sc.render.resolution_y],
                    "motion_blur": bool(sc.render.use_motion_blur),
                    "shutter": round(float(getattr(sc.render, "motion_blur_shutter", 0.0)), 4),
                    "cache_dir": _cache_dir(),
                    "caches": caches,
                    "baseline_dof": BASELINE.get("use_dof") or {},
                }).encode() + b"\n")
                continue
            if spec.get("cmd") == "shutdown":
                conn.sendall(json.dumps({"ok": True, "bye": True}).encode() + b"\n")
                log("shutdown requested")
                return

            try:
                reply = handle(spec, out_dir)
                jobs += 1
                log(f"job {spec.get('job_id')} frame={reply.get('frame')} "
                    f"render={reply['render_sec']}s {reply['bytes']}B "
                    f"sha={reply.get('png', {}).get('sha256', '')[:12]}")
            except Exception as exc:
                traceback.print_exc()
                reply = {"ok": False, "job_id": spec.get("job_id"),
                         "error": str(exc), "terminal": isinstance(exc, Refused)}

            conn.sendall(json.dumps(reply).encode() + b"\n")
        except Exception as exc:
            log(f"connection dropped: {type(exc).__name__}: {exc}")
        finally:
            try:
                conn.close()
            except OSError:
                pass


def main() -> None:
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8799)
    p.add_argument("--out-dir", default="/tmp/render_out")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--progress", default="",
                   help="JSON file this worker publishes render progress to; "
                        "the broker polls it over SSH while the socket is "
                        "blocked inside a render")
    args = p.parse_args(argv)
    serve(args.port, args.out_dir, args.host, args.progress)


if __name__ == "__main__":
    main()
