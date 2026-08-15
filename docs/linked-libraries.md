# Linked libraries: the silent-empty-render defect

**Defect log block R2-351 .. R2-365.** Staged here rather than in
`opus5-car-render/docs/DEFECT-LOG-R2.md`, which lives in the other tree.

---

## R2-351 — the defect

A scene that links datablocks out of another `.blend` renders **empty** on a
rented instance and the job is reported `done`.

Probe job `82ebdd064292` (agent `crowd-r2296`, 2026-08-04 04:41:59, scene
`f1-round2/render/r2296_before.blend`) returned a strip of sky over pure black
in **0.829 s**, recorded `blank: OK`, `lum_mean 0.0899`, `lum_sd 0.2322`,
`state: done`. Its grandstands are linked from
`f1-round2/render/world/assembly/r2/assembly9.blend` by **absolute path**.

The broker uploads the `.blend` (`remote.push_scene`) and a name-matched list of
sibling directories (`scenes.sibling_dirs_for` → `remote.push_scene_siblings`).
It has never uploaded a linked library and nothing in it ever read one. Blender
does not fail on an unresolved library: it substitutes placeholders, drops the
geometry, and renders the empty world fast.

## R2-352 — why every existing gate passed it

* **The blank gate.** Sky over black is not uniform, so `blank` read `OK` for a
  frame containing none of its subject. A metric that reads the same whether
  the thing is present or absent is not a measurement.
* **`remote.missing_assets`.** Its own docstring named the failure — *"the
  broker returns a subtly wrong frame and logs nothing"* — and then grepped for
  **one string**, `Image file <path> does not exist`. That is what a missing
  *image* prints. A missing *library* prints `Cannot find lib '<path>'`. The
  check named the right class of failure and tested for one instance of it.
* **Render time.** 0.83 s looked wrong to a human who knew the scene. It is not
  an anomaly in any corpus-relative sense: see R2-357.

## R2-353 — blast radius: one render, and it is the known one

Swept `jobs.scene` for every job in `state/broker.db` (2004 jobs, 1792 `done`,
344 distinct scenes) and every `.blend` in both project trees plus
`vast-render/scenes` (464 files, 226 GB), reading each file's `Library`
datablocks directly (`broker.blendlibs`).

| | scenes | delivered renders |
|---|---|---|
| link nothing | 291 | 1713 |
| link libraries | 3 | 5 |
| `.blend` no longer on disk | 50 | 74 |

The three that link:

* `f1-round2/render/r2296_before.blend` → `assembly9.blend`, absolute. **1
  render: job `82ebdd064292`, the black frame that started this.** Already
  discarded.
* `opus5-car-render/work/f1_exploded_posed_hq.blend` (3 renders) and
  `f1_ghost_posed_hq.blend` (1 render), both round 1, 2026-07-26. Both link
  Blender's **own bundled** `geometry_nodes_essentials.blend` — the "Smooth by
  Angle" modifier, applied to `Platform_Dais`, `Turntable_Deck` and six
  `SpotCan_*`. **Not affected**: see R2-355.

Across the whole corpus only 15 files link at all: those 13 round-1 car scenes
(all bundled essentials) and `r2296_before.blend` / `r2296_after.blend`.

**No delivered result other than the already-known black frame came through an
unresolved library. No wrong conclusion is left standing.**

## R2-354 — what the sweep cannot answer

Stated because the answer above is only as good as its coverage.

* **74 renders from 27 scenes whose `.blend` no longer exists.** Unanswerable
  from the file.
* **600 renders from 64 scenes whose `.blend` was modified after their last
  job.** Today's verdict describes today's file, not the revision that
  rendered.
* No `.blend1` backup sweep was run; 163 exist and might narrow the second
  group.

## R2-355 — Blender remaps its own bundled assets; the round-1 scenes are clean

`work/f1_exploded_posed_hq.blend` stores
`//../../../../usr/share/blender/5.2/datafiles/assets/nodes/geometry_nodes_essentials.blend`.
The instance has no `/usr/share/blender` (verified on a rented box) and the scene
is uploaded to `/workspace/scenes/<digest>/`, from which that relative path
resolves to nothing. It still renders correctly.

Measured, because three earlier attempts to simulate the instance were wrong:

1. Copying the scene elsewhere did not reproduce the failure — Blender falls
   back to the path **recorded in the file at save time**, which pointed at the
   real `/usr/share`.
2. Deleting that path's directory did not reproduce it either.
3. Running `/opt/blender-5.2.0-linux-x64/blender` in a mount namespace with
   `/usr/share/blender` bind-mounted empty — no component of the stored
   reference existing anywhere — gave `is_missing: False`, and Blender
   **rewrote** the sibling brushes library to
   `/opt/blender-5.2.0-linux-x64/5.2/datafiles/assets/...`. It remaps bundled
   assets onto the running installation.

Pixel confirmation: `mean|Δ|` between the two 640×360 renders is **0.000000**.
The metric is not blind — its positive control (a genuinely dropped linked
collection) reads `mean|Δ| 0.0178`, `max 0.52`, 3.8 % of pixels changed, and
its null (an image against itself) reads 0.

`scenes.is_bundled_essentials` therefore exempts these. A gate that refused
them would reject thirteen scenes that render correctly, and a gate with false
refusals gets switched off.

## R2-356 — `blendlibs`: reading the library table without opening Blender

`scenes.sibling_dirs_for` chose name-pattern matching over reading the blend's
path table, on the stated grounds that "reading the blend's path table means
loading a 288 MB file in Blender for every dispatch". True of
`bpy.data.libraries`; **not true of the file format**. `broker/blendlibs.py`
walks the block chain and reads only `LI` and `DNA1`:

* 4.99 GB `film14_breach.blend` — 5.5 s
* 4.21 GB `assembly9.blend` — 2.9 s
* 3 MB `r2296_before.blend` — instant

Memoised on `(path, mtime, size, inode)` in `scenes._library_closure_cached`,
so a 500-frame sequence submit pays it once.

The `Library.filepath` offset is **computed from the file's own `DNA1` block**,
not hardcoded, so it survives the field being renamed (`name` → `filepath` at
3.0) or moved. The block header layout covers all three encodings including the
Blender 4.4+ `file_format_version 1` header, whose field order is
`code, sdna, old, len, count` — *not* the legacy order with wider fields.

## R2-357 — the screen that does not work, recorded so nobody rebuilds it

"An empty render comes back suspiciously fast" is not a usable detector.
Modelling every done job's throughput as `pixels × samples ÷ render_sec`, the
known-bad job `82ebdd064292` ranks **1645th of 1788** — among the *slowest*,
because it was a 1280×720×4-sample probe. It was caught because a human knew
what that scene should cost, not because 0.83 s is anomalous.

## R2-358 — an unreadable file must never be reported as a clean one

The first sweep returned 207 of 344 files "unparseable": Blender has saved
zstd-compressed by default since 3.0 and `zstandard` is installed in neither
the system nor the broker's Python. `blendlibs` now falls back to the `zstd`
binary, which `remote.push_scene` already requires.

The second sweep returned 22 more: the `zstd` CLI **silently refuses any input
that is not a regular file** and exits 0 having written nothing, so a `.blend`
reached through a symlink decoded to zero bytes. All 22 were symlinks under
`f1-round2/work/dr_relief/before_root/`, every one a real 47 MB scene. Fixed
with `-f`.

Both were found only because an unreadable file is an **error** here, never
"no libraries". Had this module guessed "clean" on a read failure, 229 of 464
files would have been cleared by a sweep that never opened them.

## R2-359 — the gate, at submit

`scenes.require_resolvable_libraries` raises `UnresolvedLibraries` (a
`SceneError`, so it becomes a 400) from `POST /jobs` and `POST /sequences`,
before a GPU exists. A library counts as **carried** only if it will genuinely
be readable *on the instance*:

* a bundled Blender essentials asset (R2-355), or
* a resolved path under a directory `asset_dirs_for` mirrors — `push_assets`
  recreates those at identical absolute paths, or
* a `//`-relative reference that stays inside the scene's own directory and
  whose first component is a directory `sibling_dirs_for` uploads.

Everything else is refused, naming each path. An absolute `/home/<user>/...`
library can never resolve: nothing creates that tree on a rented box.

## R2-360 — the gate, at load

`worker/server.py` refuses terminally when any `bpy.data.libraries` entry has
`is_missing` — Blender's own verdict, on the machine that actually loaded the
file, rather than a log parse. Unconditional: unlike a physics cache there is
no reading under which a render missing its linked geometry is the render that
was asked for.

It also logs at load **in both directions** — `LIBRARIES: none`, `LIBRARIES: N
linked, all resolved`, or `MISSING LIBRARY: <path>` — so "no libraries" and
"the check did not run" cannot look identical from outside. That is the hole
the blank gate fell into.

Blender's own bundled libraries are not special-cased in the worker and do not
need to be: they resolve against the running installation, and the rented image
carries `/workspace/blender/5.2/datafiles/assets/`.

## R2-361 — `missing_assets` now matches every way Blender says "not found"

`ASSET_MISS_PATTERNS` is a list, and it is a list *because* the version that
shipped was one string. Captured verbatim from Blender 5.2 on 2026-08-04:

    Warning: Unable to open '<path>': No such file or directory
    Info: Cannot find lib '<path>'
    Info: LIB: Collection: 'X' missing from '<path>', parent '<direct>'
    Warning: N libraries and M linked data-blocks are missing (...)
    Warning: Image file <path> does not exist

`missing_libraries` splits out the subset that means geometry is absent, and
the two are reported at different severities: a missing image is a warning,
a missing library is an error.

`missing_assets` now returns the matched **text** rather than a bare path. The
old version stripped its one pattern down to a path, which read well in a log
line and discarded the only thing distinguishing a missing HDRI from a missing
`.blend`.

## R2-362 — why the broker warns and the worker refuses

`fleet._report_missing` logs; it does not raise. Raising from a scene switch
lands in `except Exception` and is read as "this instance failed to switch
scenes", which starts a redeploy — and every farm outage on this project has
been a healthy 5090 thrown away over something that was not the hardware's
fault. A scene that links libraries is wrong wherever it runs. The box is fine.

## R2-363 — worker code now refreshes on a scene switch

`_deploy` and the resume path pushed `worker/server.py`; `_switch_scene` did
not, so a worker-side fix took effect only at the next **full redeploy** —
which, on an instance deliberately held warm for hours, may be never. The push
is guarded: a failure logs and continues with the worker already on the box,
because "could not copy a file" must never become "this instance cannot switch
scenes".

## R2-364 — controls

`broker/test_broker.py`, 35 checks, all offline:

* **Positive** — a linked scene is refused, and the refusal names the path.
* **Negative** — `appended.blend`: the *same collection* from the *same source
  `.blend`*, appended instead of linked. Identical geometry, and its datablock
  names still carry the source's name. A detector keyed on "mentions another
  `.blend`" passes the positive and fails here.
* **Compressed positive** — the same scene saved zstd. The instance object in
  the fixture is load-bearing: a linked collection nothing references is orphan
  data, Blender drops it on save, and the fixture silently becomes a second
  copy of the clean one. That happened on the first attempt.
* **Carried positive** — a `//cache/` library is accepted, so the gate is not
  "refuse everything that links" wearing a policy's clothes.
* **Pattern controls** — the four library lines match, the old single pattern
  is asserted **blind** to all four, and lines from a *successful* load of a
  linking scene (`Info: Read library: '<same path>'`) must not match.
* Fixtures are written by Blender, never by the test. A `.blend` synthesised by
  the test would be a file shaped like whatever the reader expects, which is
  the round-trip-against-its-own-constant this project has shipped before. If
  Blender is unavailable the test **fails**; it does not skip.

## R2-365 — open

* The 74 + 600 renders in R2-354 remain unattributable.
* `POST /exec` is not gated. Exec jobs run arbitrary Blender scripts and may
  open scenes the broker never validated.
* The submit gate models what the upload *will* do. If `push_*` changes,
  `scenes.library_status` must change with it; the worker-side refusal is the
  backstop that does not depend on that model being right.
* **Recommendation on linked-scene support: refuse them.** See below.

---

## Recommendation: refuse linked scenes; do not pack, do not resolve at push

Three options were on the table.

**Refuse (chosen).** A linked scene is rejected at submit with a message naming
the paths and the fix. Cost: an artist who works with linked assemblies must
run *File > External Data > Make Local: All* and save a self-contained copy
under a `SCENE_ROOT` before rendering. For `r2296_before.blend` that is 62
datablocks and 769 k faces — a few hundred MB, not the 4.5 GB the linked
assembly implies. The step is manual and has to be redone whenever the
assembly changes.

**Pack at submit** (broker makes the scene local and uploads the result).
Cost: the broker would write `.blend` files, which it currently never does; it
would need somewhere to put them that is inside a scene root and outside the
project trees; every submit of a large linked scene pays a make-local pass;
and the content-addressed scene cache keys on the *source* file's hash, so a
packed copy needs a second identity or the cache silently serves a stale pack.
That last one is the same shape as the defect being fixed.

**Resolve libraries at push** (upload each library to the absolute path the
`.blend` asks for, the way `push_assets` already does for asset dirs).
Cost: it works, and it is the most transparent option — but it uploads the
*whole* library file. `assembly9.blend` is 4.2 GB against a 23 GB scene-cache
budget on a 32 GB disk, and five 4.2 GB assemblies sit in that one directory.
It also has to handle nested libraries, eviction of libraries independently of
scenes, and the case where two scenes want different revisions of the same
absolute path. That is a scene-cache redesign, not a fix.

Refusing is the only one of the three that cannot itself fail silently: the
scene either has no unresolved libraries or the job does not exist. If linked
workflows become common, revisit **resolve at push** — but only with the
per-library cache accounting the current one does not have.
