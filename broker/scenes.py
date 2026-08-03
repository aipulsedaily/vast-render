#!/usr/bin/env python3
"""Which .blend a job renders, and where its external assets live.

A job may name its own scene. That name arrives from a client and becomes a
file path on this machine *and* on the rented instance, which makes it the same
class of vector that forced job ids to be minted broker-side: a client-supplied
id was a path traversal into `~/opus5-car-render`, a project this system must
never write to.

So the rule here is narrow and checked in one place: resolve first, then require
the result to sit under `SCENE_ROOT`. Resolving first is what matters — a
prefix test applied before resolution passes `root/../../etc/shadow` happily,
and symlinks defeat it just as easily.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import config


class SceneError(ValueError):
    """Rejected scene reference. Carries a message safe to hand back over HTTP."""


def resolve_scene(raw: Optional[str]) -> Path:
    """Validate a job's scene reference and return an absolute path.

    `None` or empty means the broker's default scene, so every existing caller
    and every stored job keeps working untouched.

    Relative names resolve against `SCENE_ROOT`, which makes the common case a
    bare filename: `--scene f1_ghost_posed_hq.blend`.
    """
    if raw is None or not str(raw).strip():
        return config.SCENE

    text = str(raw).strip()
    if "\x00" in text:
        raise SceneError("scene path contains a null byte")

    candidate = Path(text)
    if not candidate.is_absolute():
        # A bare name resolves against the first root that actually holds it, so
        # `--scene anim_test.blend` works whichever tree it lives in. Falls back
        # to the primary root, which keeps the error message pointing at the
        # place a user most likely meant.
        for root in config.SCENE_ROOTS:
            if (root / candidate).exists():
                candidate = root / candidate
                break
        else:
            candidate = config.SCENE_ROOT / candidate

    try:
        real = candidate.resolve()
    except OSError as exc:
        raise SceneError(f"cannot resolve scene {text!r}: {exc}") from None

    # Resolved first, then tested — a prefix check before resolution passes
    # `root/../../etc/shadow` happily. `real == root` would name the directory
    # itself, which is not a scene, so `parents` is the right test.
    if not any(root in real.parents for root in config.SCENE_ROOTS):
        raise SceneError(
            f"scene {text!r} resolves to {real}, which is outside every permitted "
            f"scene root: {', '.join(str(r) for r in config.SCENE_ROOTS)}"
        )
    if real.suffix != ".blend":
        raise SceneError(f"scene {text!r} is not a .blend file")
    if not real.is_file():
        raise SceneError(f"scene {text!r} does not exist at {real}")
    return real


def asset_dirs_for(scene: Path) -> list[Path]:
    """Directories to mirror to the instance for this particular scene.

    Per scene, not once per deploy: different variants can sit in different
    trees, and an HDRI that is missing renders a plausible-looking frame lit
    differently from the local one rather than failing. An explicit
    `VASTRENDER_ASSET_DIRS` still wins and applies to every scene.
    """
    if config.ASSET_DIRS:
        return list(config.ASSET_DIRS)
    found: list[Path] = []
    for base in (scene.parent, scene.parent.parent):
        cand = base / "assets"
        if cand.is_dir() and cand not in found:
            found.append(cand)
    return found


def sibling_dirs_for(scene: Path) -> list[Path]:
    """Directories beside the .blend that must travel *with* it, by name.

    Blender resolves a `//`-relative reference against the directory the .blend
    is in, so a physics cache at `//cache/` or `//blendcache_shot/` only exists
    on the instance if a directory of that name sits next to the uploaded copy.
    This is why the broker uploads a scene into a directory of its own under its
    original filename instead of as `<hash>.blend`.

    Missing these is silent and expensive: Blender does not fail on an absent
    physics cache, it **simulates** — and a simulation reached by jumping to a
    frame does not continue the previous frame. In a single unbroken shot that
    is a defect no single-frame inspection can find.

    Matched by name pattern rather than by parsing the .blend because reading
    the blend's path table means loading a 288 MB file in Blender for every
    dispatch. Anything a pattern misses is still caught loudly after the deploy
    by the missing-file check.

    **`blendcache_X` travels only with `X.blend`.** That name is not a generic
    one: Blender derives it from the .blend's own filename, so `blendcache_beat3`
    is beat3's bake and is not readable by anything else. Matching it as a bare
    glob attached every bake in a directory to every blend in it — measured here
    on 2026-08-02, blank_probe.blend was uploaded carrying anim_test's 48-file
    cloth cache — and that is not a cosmetic waste. `f1-round2/render/world/
    assembly/r2/` holds FIVE 4.2 GB assemblies in one directory, and beat 3's
    rigid-body glass bake will sit beside them: under the old rule every one of
    those five scene-cache entries would carry a full copy of a multi-gigabyte
    destruction cache, against an 8 GB cache budget on a 32 GB disk.

    Every other configured name — `cache`, `sim`, `textures` — genuinely is
    shared: Blender resolves `//cache/...` against the blend's directory with no
    reference to its filename, so any blend there may be the one that uses it.
    Those still travel with all of them.
    """
    mine = f"blendcache_{scene.stem}"
    found: list[Path] = []
    for pattern in config.CACHE_DIR_GLOBS:
        for path in sorted(scene.parent.glob(pattern)):
            if not path.is_dir() or path in found:
                continue
            # Someone else's bake, by Blender's own naming rule. Skipping it is
            # the whole point; keeping the one that IS ours is why the test is on
            # the name rather than on the pattern.
            if path.name.startswith("blendcache_") and path.name != mine:
                continue
            found.append(path)
    return found


def label(scene: Path) -> str:
    """Short name for logs and `rq status` — the full path is noise in a table."""
    for root in config.SCENE_ROOTS:
        try:
            return str(scene.relative_to(root))
        except ValueError:
            continue
    return scene.name
