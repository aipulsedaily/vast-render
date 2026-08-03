#!/usr/bin/env python3
"""What is actually IN the image — not whether the file is intact.

Every check this project had before this module verified the FILE: the PNG
signature, the IHDR dimensions, the IEND marker, the sha256 the worker computed
against the sha256 of the bytes that landed. All four passed on
`out/0908e534b1d3.png`, an 8,734-byte 640x480 PNG that is **entirely black** —
mean 0.00000, sd 0.00000, one distinct luminance level across 307,200 pixels.
The broker logged it as `done — render 33.2s, total 799.8s, 0.0 MB` and counted
it as delivered. The neighbouring frames from the same instance, the same
worker session and the same .blend measured 27-38 MB and mean 0.30-0.68.

Round 1 of this project already learned this in a different tool, and wrote it
down in `f1-site/tools/drive.mjs`:

    A frame whose pixels are effectively uniform is reported BLANK. A black
    canvas from a dead GL context is the single most common headless-3D failure
    and it passes every 'file exists' check ever written.

The render farm never got that lesson. This is it.

**Why a classification and not a threshold.** A near-black frame can be
correct — a fade, a night interior, a shot into shadow — so a bare "reject if
dark" would refuse legitimate work, and once refused legitimate work it would be
switched off. The distinctions that matter are:

    BLACK        mean and sd both ~0. Nothing was rendered.
    TRANSPARENT  an alpha channel that is zero everywhere. `film_transparent`
                 with nothing composited over it looks exactly like BLACK once
                 flattened, but the fix is different, so it is named differently.
    UNIFORM      sd ~0 at any brightness. A flat grey or flat white frame is
                 just as broken as a black one and passes every existing check
                 just as completely.
    SUSPICIOUS   almost no variation, or almost no distinct levels. Not proof of
                 anything; a reason for a human to look.
    OK           everything else.

**Why the sequence check is relative.** For one still, a fixed threshold is all
there is. For a 2,978-frame single unbroken take, the far stronger signal is
that frame 1,600 is nothing like frames 1,590-1,610 — a fixed threshold cannot
see that, and a fade legitimately walks a whole neighbourhood down to black
together. `outliers()` compares each frame against a rolling window of its
neighbours using median/MAD, so a gradual fade passes and a single dropped frame
does not.

**Luminance** is ITU-R 601-2 luma over the stored (sRGB-encoded) bytes,
`(299R + 587G + 114B) / 1000`, which is exactly what PIL's `convert("L")`
computes. Perceptual accuracy is not the point; agreeing with the number a human
gets from any other tool is.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import config

# --- verdicts -------------------------------------------------------------

OK = "OK"
BLACK = "BLACK"
TRANSPARENT = "TRANSPARENT"
UNIFORM = "UNIFORM"
SUSPICIOUS = "SUSPICIOUS"
UNREADABLE = "UNREADABLE"

#: Verdicts that mean "this frame has no content". These fail a job by default.
BLANK = (BLACK, TRANSPARENT, UNIFORM)

#: Verdicts worth shouting about. UNREADABLE is here because a PNG that passes
#: the structural check and still will not decode is a defect either way.
NOT_OK = (BLACK, TRANSPARENT, UNIFORM, SUSPICIOUS, UNREADABLE)


def is_blank(verdict: Optional[str]) -> bool:
    return verdict in BLANK


# --- decoding -------------------------------------------------------------
#
# Two paths. Pillow is present in this venv and does the whole job in C —
# ~0.5 s for a 3840x2160 frame and ~2.1 s for 7680x4320, against render times of
# minutes. It is NOT a declared dependency of this project though (it arrives
# transitively), so a pure-stdlib decoder sits behind it: slower by a large
# factor, but this must not become a check that quietly stops running the day
# someone rebuilds the virtualenv.

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _pillow():
    try:
        from PIL import Image
    except ImportError:
        return None
    # A 7680x4320 frame is 33 Mpx, over Pillow's 89 Mpx bomb limit only for
    # much larger images, but a future 16K plate would trip it and be reported
    # as unreadable rather than measured. These files are written by our own
    # worker, so the decompression-bomb guard protects nothing here.
    Image.MAX_IMAGE_PIXELS = None
    return Image


#: Modes where Pillow's own `convert("L")` cannot be trusted. A 16-bit
#: GREYSCALE PNG opens as `I;16`, and converting that to `L` CLIPS at 255
#: instead of scaling — measured here on an 8x4 ramp spanning 0-62000, where 31
#: of 32 pixels came back as pure white. That would classify a perfectly good
#: 16-bit render as UNIFORM and fail the job. 16-bit *colour* is fine: Pillow
#: opens it as `RGB` with the high byte taken, and the two decoders agree
#: exactly. So only these modes fall back.
PILLOW_LOSSY_MODES = {"I", "I;16", "I;16B", "I;16L", "I;16N", "F"}


def _hist_pillow(path: Path):
    """(luma histogram, alpha histogram or None, mode, width, height), or None
    when Pillow cannot measure this image faithfully."""
    Image = _pillow()
    with Image.open(path) as im:
        if im.mode in PILLOW_LOSSY_MODES:
            return None
        im.load()
        mode, width, height = im.mode, im.width, im.height
        alpha = None
        if "A" in im.getbands():
            alpha = im.getchannel("A").histogram()
        elif mode == "P" and "transparency" in im.info:
            alpha = im.convert("RGBA").getchannel("A").histogram()
        luma = im.convert("L").histogram()
    return luma[:256], (alpha[:256] if alpha else None), mode, width, height


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _hist_stdlib(path: Path) -> tuple[list[int], Optional[list[int]], str, int, int]:
    """Decode a PNG with nothing but zlib, and histogram it.

    Correct for the colour types Blender writes (8- and 16-bit grey, grey+alpha,
    RGB, RGBA) and for palette images, which Blender does not write but other
    tools do. Interlaced PNGs are refused rather than mis-decoded — Blender does
    not produce them and a wrong answer here is worse than no answer.

    Slow: unfiltering is inherently sequential (every filter but None reads the
    row above), so this is a per-byte Python loop and a 4K frame costs tens of
    seconds. It exists so the check still runs without Pillow, not to be the
    normal path.
    """
    raw = path.read_bytes()
    if raw[:8] != PNG_MAGIC:
        raise ValueError("no PNG signature")
    pos = 8
    idat = bytearray()
    width = height = depth = ctype = interlace = 0
    palette = b""
    trns = b""
    seen_ihdr = False
    while pos + 8 <= len(raw):
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        kind = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            seen_ihdr = True
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if not seen_ihdr:
        raise ValueError("no IHDR chunk")
    if interlace:
        raise ValueError("interlaced PNG (Adam7) is not decoded here")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        raise ValueError(f"unknown PNG colour type {ctype}")
    if depth not in (1, 2, 4, 8, 16):
        raise ValueError(f"unsupported bit depth {depth}")

    data = zlib.decompress(bytes(idat))
    bpp = max(1, (channels * depth) // 8)          # filter offset, in bytes
    stride = (width * channels * depth + 7) // 8

    luma = [0] * 256
    # Only where an alpha value is actually accumulated below. Creating it for
    # an RGB image carrying a `tRNS` chunk would leave an all-zero histogram
    # that reads as "max alpha 0" — and classify a perfectly opaque frame as
    # TRANSPARENT.
    alpha_hist: Optional[list[int]] = (
        [0] * 256 if ctype in (4, 6) or (ctype == 3 and trns) else None)
    prev = bytearray(stride)
    row = bytearray(stride)
    at = 0
    for _ in range(height):
        if at + 1 + stride > len(data):
            raise ValueError("PNG image data is short — the file is truncated")
        ftype = data[at]
        row[:] = data[at + 1:at + 1 + stride]
        at += 1 + stride
        if ftype == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                upleft = prev[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif ftype != 0:
            raise ValueError(f"unknown PNG row filter {ftype}")

        _accumulate(row, luma, alpha_hist, width, channels, depth, ctype, palette, trns)
        prev[:] = row

    mode = {0: "L", 2: "RGB", 3: "P", 4: "LA", 6: "RGBA"}[ctype]
    return luma, alpha_hist, mode, width, height


def _accumulate(row: bytearray, luma: list[int], alpha: Optional[list[int]],
                width: int, channels: int, depth: int, ctype: int,
                palette: bytes, trns: bytes) -> None:
    """Fold one unfiltered scanline into the luminance (and alpha) histograms."""
    if depth == 16:
        # Take the high byte: the classification works on 8-bit luminance and
        # nothing downstream needs more precision than that.
        samples = row[0::2]
    elif depth == 8:
        samples = row
    else:
        samples = bytearray()
        per = 8 // depth
        mask = (1 << depth) - 1
        scale = 255 // mask
        for byte in row:
            for k in range(per):
                samples.append(((byte >> (8 - depth * (k + 1))) & mask) * scale)
        samples = samples[:width * channels]

    # `+ 500` before the divide, not truncation: Pillow's Convert.c rounds
    # half-up here, and without it this path disagrees with the Pillow path by
    # one grey level on about half the pixels — enough to shift a mean by 0.004,
    # which is the same order as BLANK_SD_MAX. Two decoders that answer
    # differently are worse than one slow decoder.
    if ctype == 3:                                   # palette
        for x in range(width):
            idx = samples[x]
            r, g, b = palette[idx * 3:idx * 3 + 3] or (0, 0, 0)
            luma[(299 * r + 587 * g + 114 * b + 500) // 1000] += 1
            if alpha is not None:
                alpha[trns[idx] if idx < len(trns) else 255] += 1
        return
    if channels == 1:                                # grey
        for x in range(width):
            luma[samples[x]] += 1
        return
    if channels == 2:                                # grey + alpha
        for x in range(width):
            luma[samples[2 * x]] += 1
            alpha[samples[2 * x + 1]] += 1           # type: ignore[index]
        return
    for x in range(width):                           # RGB / RGBA
        off = channels * x
        r, g, b = samples[off], samples[off + 1], samples[off + 2]
        luma[(299 * r + 587 * g + 114 * b + 500) // 1000] += 1
        if channels == 4:
            alpha[samples[off + 3]] += 1             # type: ignore[index]


def decoder_name() -> str:
    return "pillow" if _pillow() is not None else "stdlib"


# --- statistics -----------------------------------------------------------


def _from_hist(hist: Sequence[int]) -> dict:
    """Exact mean/sd/min/max/percentiles from a 256-bin 8-bit histogram.

    Exact, not estimated: the histogram *is* the whole population, so summing
    over 256 bins costs nothing and loses nothing. All values are reported on
    0..1 so a threshold reads the same whatever the bit depth.
    """
    total = sum(hist)
    if not total:
        return {"pixels": 0, "mean": 0.0, "sd": 0.0, "min": 0.0, "max": 0.0,
                "p01": 0.0, "p50": 0.0, "p99": 0.0, "levels": 0, "black_frac": 0.0}
    s = sum(i * c for i, c in enumerate(hist) if c)
    mean = s / total
    var = sum((i - mean) ** 2 * c for i, c in enumerate(hist) if c) / total
    present = [i for i, c in enumerate(hist) if c]

    def pct(q: float) -> int:
        want = q * total
        run = 0
        for i, c in enumerate(hist):
            run += c
            if run >= want:
                return i
        return 255

    return {
        "pixels": total,
        "mean": round(mean / 255.0, 6),
        "sd": round(var ** 0.5 / 255.0, 6),
        "min": round(present[0] / 255.0, 6),
        "max": round(present[-1] / 255.0, 6),
        "p01": round(pct(0.01) / 255.0, 6),
        "p50": round(pct(0.50) / 255.0, 6),
        "p99": round(pct(0.99) / 255.0, 6),
        "levels": len(present),
        "black_frac": round(hist[0] / total, 6),
    }


def measure(path) -> dict:
    """Decode one PNG and describe what is in it.

    Never raises. A file that cannot be decoded comes back with
    `verdict=UNREADABLE` and a reason, because this runs inside the delivery
    path of a render that already cost money and an exception here must not be
    the thing that loses it.
    """
    path = Path(path)
    stats: dict = {"verdict": UNREADABLE, "detail": "", "decoder": "",
                   "pixels": 0, "mean": None, "sd": None, "min": None, "max": None,
                   "p01": None, "p50": None, "p99": None, "levels": None,
                   "black_frac": None, "alpha_mean": None, "alpha_max": None,
                   "mode": "", "width": None, "height": None, "hist16": None}
    try:
        got = _hist_pillow(path) if _pillow() is not None else None
        if got is not None:
            luma, alpha, mode, width, height = got
            stats["decoder"] = "pillow"
        else:
            luma, alpha, mode, width, height = _hist_stdlib(path)
            stats["decoder"] = "stdlib"
    except Exception as exc:                      # noqa: BLE001 - see docstring
        stats["detail"] = f"cannot decode: {type(exc).__name__}: {exc}"
        return stats

    stats.update(_from_hist(luma))
    stats["mode"], stats["width"], stats["height"] = mode, width, height
    # Sixteen buckets, not 256: enough to see "everything is in one bucket" or
    # "this is bimodal" in a terminal, small enough to store per frame for 3,000
    # frames without the row becoming a blob nobody reads.
    stats["hist16"] = [sum(luma[i * 16:(i + 1) * 16]) for i in range(16)]
    # `sum(alpha)` and not just `alpha`: an empty histogram would measure as
    # "max alpha 0.0" and every frame carrying it would be called TRANSPARENT.
    # A verdict derived from no data at all is the one kind of false positive
    # this must never produce.
    if alpha and sum(alpha):
        a = _from_hist(alpha)
        stats["alpha_mean"] = a["mean"]
        stats["alpha_max"] = a["max"]
    verdict, detail = classify(stats)
    stats["verdict"], stats["detail"] = verdict, detail
    return stats


# --- classification -------------------------------------------------------


def classify(stats: dict) -> tuple[str, str]:
    """Turn measurements into one of the verdicts, with a reason a human can act on.

    The numbers come from `config` and were chosen against the 240 frames this
    farm had already returned when the black frame was found. See
    `config.BLANK_SD_MAX` for the derivation; the short version is that one
    8-bit quantisation step is 1/255 = 0.0039, the flattest genuine render in
    that corpus had sd 0.035, and the two thresholds sit either side of the
    empty gap between 0.0 and 0.035.
    """
    if stats.get("mean") is None:
        return UNREADABLE, stats.get("detail") or "not measured"

    mean, sd = stats["mean"], stats["sd"]
    levels, pixels = stats.get("levels") or 0, stats.get("pixels") or 0
    amax = stats.get("alpha_max")

    if amax is not None and amax <= config.BLANK_ALPHA_MAX:
        return TRANSPARENT, (
            f"every pixel is fully transparent (max alpha {amax:.4f}). Flattened "
            f"onto anything this frame contributes nothing — film_transparent is on "
            f"and nothing was composited behind it"
        )
    if sd <= config.BLANK_SD_MAX and mean <= config.BLACK_MEAN_MAX:
        return BLACK, (
            f"the image is black: mean {mean:.5f}, sd {sd:.5f}, "
            f"{levels} distinct luminance level(s) across {pixels:,} pixels"
        )
    if sd <= config.BLANK_SD_MAX:
        return UNIFORM, (
            f"the image is one flat colour: mean {mean:.5f}, sd {sd:.5f}, "
            f"{levels} distinct luminance level(s) across {pixels:,} pixels — "
            f"a flat frame at any brightness has no content in it"
        )
    if sd <= config.SUSPECT_SD_MAX:
        return SUSPICIOUS, (
            f"almost no variation: sd {sd:.5f} (mean {mean:.5f}, range "
            f"{stats['min']:.3f}-{stats['max']:.3f}). Real frames from this farm "
            f"measure sd 0.035-0.34"
        )
    if pixels >= config.SUSPECT_MIN_PIXELS and levels <= config.SUSPECT_LEVELS_MAX:
        return SUSPICIOUS, (
            f"only {levels} distinct luminance levels across {pixels:,} pixels "
            f"(sd {sd:.5f}) — a rendered image is never this quantised"
        )
    return OK, ""


def summary(stats: Optional[dict]) -> str:
    """One line, for a log or a terminal. Always says the verdict."""
    if not stats:
        return "not measured"
    if stats.get("mean") is None:
        return f"{stats.get('verdict', UNREADABLE)}  {stats.get('detail', '')}"
    line = (f"mean {stats['mean']:.4f}  sd {stats['sd']:.4f}  "
            f"range {stats['min']:.3f}-{stats['max']:.3f}  "
            f"{stats['levels']} levels")
    if stats.get("alpha_mean") is not None:
        line += f"  alpha {stats['alpha_mean']:.3f}"
    return f"{stats['verdict']:<11} {line}"


# --- sequence-level, relative --------------------------------------------


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def outliers(rows: Iterable[dict], window: Optional[int] = None,
             z: Optional[float] = None) -> list[dict]:
    """Frames whose statistics do not belong with their neighbours'.

    This is the check that catches the case a fixed threshold cannot: one dead
    frame buried at 1,600 in a shot that is otherwise fine. It is also the check
    that must NOT fire on a fade, a headlight sweep or a tunnel — all of which
    move the whole neighbourhood together.

    Median and MAD over a rolling window of neighbours, rather than mean and
    standard deviation over the whole sequence, because a 124-second continuous
    take legitimately drifts from one end to the other and because the very
    defect being looked for would inflate a global standard deviation enough to
    hide itself.

    Two conditions, both required:

      * a robust z-score past `z` — the frame is unlike its neighbourhood; and
      * an ABSOLUTE deviation past a floor — because a stretch of near-identical
        frames has MAD ~0, where every frame is infinitely many MADs from the
        median and everything would be flagged.

    `rows` are dicts with `frame`, `mean` and `sd`; anything without both
    measurements is skipped rather than guessed at.
    """
    window = int(window or config.SEQ_OUTLIER_WINDOW)
    z = float(z if z is not None else config.SEQ_OUTLIER_Z)
    pts = [r for r in rows
           if r.get("mean") is not None and r.get("sd") is not None]
    pts.sort(key=lambda r: r["frame"])
    if len(pts) < config.SEQ_OUTLIER_MIN_FRAMES:
        return []

    found = []
    half = max(2, window // 2)
    for i, row in enumerate(pts):
        lo, hi = max(0, i - half), min(len(pts), i + half + 1)
        neigh = [p for j, p in enumerate(pts[lo:hi], start=lo) if j != i]
        if len(neigh) < 4:
            continue
        why = []
        for field, floor in (("mean", config.SEQ_OUTLIER_MEAN_FLOOR),
                             ("sd", config.SEQ_OUTLIER_SD_FLOOR)):
            values = [p[field] for p in neigh]
            med = _median(values)
            mad = _median([abs(v - med) for v in values]) * 1.4826
            delta = row[field] - med
            if abs(delta) < floor:
                continue
            score = abs(delta) / mad if mad > 1e-9 else float("inf")
            if score >= z:
                direction = {
                    ("mean", True): "much brighter", ("mean", False): "much darker",
                    ("sd", True): "far busier", ("sd", False): "far flatter",
                }[(field, delta > 0)]
                why.append(
                    f"{direction} than its neighbours: {field} {row[field]:.4f} "
                    f"vs {med:.4f} median over {len(neigh)} frames"
                    + (f" ({score:.0f} MADs)" if score != float("inf")
                       else " (neighbours are identical)")
                )
        if why:
            found.append({"frame": row["frame"], "mean": row["mean"], "sd": row["sd"],
                          "verdict": row.get("verdict"), "why": "; ".join(why)})
    return found


# --- measuring files that were never a job -------------------------------


def _main(argv: list[str]) -> int:
    """`python -m broker.imgstat FILE...` — measure PNGs the broker never saw.

    The thresholds in `config` were derived by pointing this at the 240 frames
    this farm had already returned, and finding that exactly one of them was
    black and exactly one more was a flat grey. That is a thing worth being able
    to do again — against a delivery directory, against somebody else's frames,
    against a sequence rendered before any of this existed.
    """
    sort_key = ""
    paths: list[Path] = []
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--sort":
            sort_key = rest.pop(0) if rest else "sd"
        elif arg.startswith("-"):
            print(f"unknown option {arg}")
            return 2
        else:
            paths.append(Path(arg))
    if not paths:
        print("usage: python -m broker.imgstat [--sort sd|mean] FILE...")
        return 2
    rows = [(p, measure(p)) for p in paths]
    if sort_key:
        rows.sort(key=lambda r: (r[1].get(sort_key) is None, r[1].get(sort_key) or 0))
    counts: dict[str, int] = {}
    for path, st in rows:
        counts[st["verdict"]] = counts.get(st["verdict"], 0) + 1
        print(f"{path.name:<30} {summary(st)}")
        if st["verdict"] in NOT_OK and st.get("detail"):
            print(f"{'':<30} !! {st['detail']}")
    print(f"\n{len(rows)} file(s): " + "  ".join(f"{v} {n}" for v, n in sorted(counts.items())))
    return 1 if any(is_blank(st["verdict"]) for _, st in rows) else 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
