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

from collections import OrderedDict
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


# Reading the block chain of a 5 GB scene costs 5.5 s — measured on
# film14_breach.blend, 2026-08-04 — because the walk is one small read per
# block and those files have on the order of a million. Cheap enough to do once
# per revision, far too expensive to do once per frame of a 500-frame sequence
# submit. Keyed the same way `remote.scene_hash` memoises: identity plus mtime
# plus size, so a re-saved scene is re-read and a renamed one is not confused
# with its predecessor. Bounded so a long-lived broker cannot grow it without
# limit.
_LIB_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_LIB_CACHE_MAX = 256


def _library_closure_cached(scene: Path) -> list:
    from . import blendlibs

    try:
        st = scene.stat()
        key = (str(scene), st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return blendlibs.library_closure(scene)
    hit = _LIB_CACHE.get(key)
    if hit is not None:
        _LIB_CACHE.move_to_end(key)
        return hit
    refs = blendlibs.library_closure(scene)
    _LIB_CACHE[key] = refs
    while len(_LIB_CACHE) > _LIB_CACHE_MAX:
        _LIB_CACHE.popitem(last=False)
    return refs


# Blender's own bundled asset libraries — the Essentials node groups, brushes
# and shaders that ship inside the installation. A scene that uses "Smooth by
# Angle" links one of these, and thirteen of the round-1 car scenes do.
#
# These are carried, and it is not a guess. MEASURED 2026-08-04 on
# `work/f1_exploded_posed_hq.blend`, whose stored path is
# `//../../../../usr/share/blender/5.2/datafiles/assets/nodes/geometry_nodes_
# essentials.blend`: run under `/opt/blender-5.2.0-linux-x64` in a mount
# namespace with `/usr/share/blender` bind-mounted empty — no path in that
# reference exists, and the .blend is not where it was saved — Blender reports
# `is_missing: False` and rewrites the sibling brushes library to
# `/opt/blender-5.2.0-linux-x64/5.2/datafiles/assets/...`. It remaps bundled
# assets onto the RUNNING installation. The instance carries
# `/workspace/blender/5.2/datafiles/assets/`, verified on 46712525.
#
# Matching on the tail rather than the prefix is therefore correct: the prefix
# is exactly the part Blender replaces. It is also why this is a narrow test —
# `datafiles/assets/` under a `blender`-shaped directory — rather than "any path
# containing essentials".
#
# Getting this wrong in the other direction has a cost worth stating: a gate
# that refused these would reject thirteen scenes that render correctly, and a
# gate with false refusals is a gate somebody switches off.
_BUNDLED_MARKER = ("datafiles", "assets")


def is_bundled_essentials(ref: "blendlibs.LibRef") -> bool:
    parts = ref.path.parts
    for i in range(len(parts) - 2):
        if (parts[i], parts[i + 1]) == _BUNDLED_MARKER:
            # ".../<something>/<version>/datafiles/assets/..." — require the
            # version-shaped component so an ordinary project directory called
            # `datafiles/assets` is not silently waved through.
            return i >= 1 and parts[i - 1][:1].isdigit()
    return False


class UnresolvedLibraries(SceneError):
    """The scene links .blend libraries the instance will not have.

    A `SceneError` because it is the same class of answer `resolve_scene` gives:
    a verdict about the reference, decided before a GPU exists, and safe to hand
    back over HTTP.
    """

    def __init__(self, scene: Path, refs: list["blendlibs.LibRef"]) -> None:
        self.scene = scene
        self.refs = refs
        detail = "; ".join(
            f"{r.stored}" + ("" if r.stored == str(r.path) else f" -> {r.path}")
            + ("" if r.exists else " [missing locally too]")
            for r in refs
        )
        super().__init__(
            f"scene {label(scene)} links {len(refs)} library file(s) that the "
            f"broker does not upload, so on the instance they resolve to nothing "
            f"and Blender renders the scene WITHOUT them — silently, quickly, and "
            f"reported done. Unresolved: {detail}. "
            f"Fix by making the scene self-contained (File > External Data > Make "
            f"Local, or `bpy.ops.file.make_paths_absolute` then Make Local) and "
            f"saving it under a scene root; a linked scene cannot be rendered on "
            f"this farm."
        )


def library_status(scene: Path) -> tuple[list["blendlibs.LibRef"],
                                         list["blendlibs.LibRef"]]:
    """Split this scene's linked libraries into (carried, unresolved).

    "Carried" means the file will genuinely be readable at the path the .blend
    asks for, ON THE INSTANCE. That is a narrower question than "does it exist
    here", and the difference is the whole defect: every library in the probe
    scene that produced the black frame existed locally.

    Only two arrangements survive the upload, and both are consequences of how
    `remote.push_*` works rather than guesses:

      * A `//`-relative reference that stays INSIDE the scene's own directory
        and whose first component is a directory `sibling_dirs_for` uploads.
        Those land beside the .blend under their own names, which is the one
        arrangement in which `//` resolves remotely.

      * Any reference — relative or absolute — whose resolved path sits under a
        directory `asset_dirs_for` mirrors, because `push_assets` recreates
        those at IDENTICAL ABSOLUTE PATHS on the instance.

    Everything else is unresolved. In particular a bare absolute path into
    `/home/zany/...` cannot resolve: nothing creates that tree on a rented box.
    A `//`-relative path that climbs out of the scene directory cannot either —
    the instance's directory layout above the scene is not this machine's.
    """
    from . import blendlibs

    refs = _library_closure_cached(scene)
    if not refs:
        return [], []

    scene_dir = scene.parent.resolve()
    sibling_names = {d.name for d in sibling_dirs_for(scene)}
    asset_roots = [d.resolve() for d in asset_dirs_for(scene) if d.is_dir()]

    carried: list[blendlibs.LibRef] = []
    unresolved: list[blendlibs.LibRef] = []
    for ref in refs:
        ok = False
        if is_bundled_essentials(ref):
            ok = True
        elif any(root == ref.path or root in ref.path.parents for root in asset_roots):
            ok = True
        elif ref.relative and ref.linker.parent.resolve() == scene_dir:
            try:
                inside = ref.path.relative_to(scene_dir)
            except ValueError:
                inside = None
            if inside is not None and inside.parts and inside.parts[0] in sibling_names:
                ok = True
        (carried if ok else unresolved).append(ref)
    return carried, unresolved


def require_resolvable_libraries(scene: Path) -> list["blendlibs.LibRef"]:
    """Raise unless every library this scene links will exist on the instance.

    Called at submit, not at dispatch: the point is that the answer costs a few
    hundred milliseconds of file reading and is available before any GPU is
    rented, whereas the failure it prevents costs a 4K render, is returned as a
    finished frame, and is read as evidence.

    Returns the libraries that ARE carried, so a caller can log that a linked
    scene was accepted and on what grounds — an empty list is the ordinary case.
    """
    carried, unresolved = library_status(scene)
    if unresolved:
        raise UnresolvedLibraries(scene, unresolved)
    return carried


def label(scene: Path) -> str:
    """Short name for logs and `rq status` — the full path is noise in a table."""
    for root in config.SCENE_ROOTS:
        try:
            return str(scene.relative_to(root))
        except ValueError:
            continue
    return scene.name
