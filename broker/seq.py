#!/usr/bin/env python3
"""Frame sequences: naming, planning, and proving a frame really arrived.

A still is a job. A shot is not — it is thousands of frames rendered over days,
across several rented instances, by several jobs, and it has to end up as one
directory that ffmpeg can read with **no gaps and no wrong frames**. Those are
different problems, and this module holds the parts that only the second one
needs.

Three ideas do all the work here.

**The sequence name, not the job id, is the identity.** Job ids are minted
broker-side and are deliberately unpredictable; a resume two days and one broker
restart later has to find the same frames, so the key it looks under must be
something the caller chose. That makes the name client-supplied, and therefore
validated here — once, narrowly, before it is ever joined to a path on this
machine or on the instance. `[A-Za-z0-9_-]` cannot traverse.

**A frame counts as delivered only when its file verifies.** Not when the worker
says it rendered, not when scp exits 0. This project has already seen scp leave
783 KB of a 1.9 MB PNG behind and exit as if nothing happened, and a truncated
PNG opens fine in most viewers. So the record of "frame 1841 exists" is a record
of a file that was checked after it landed, and the resume set is recomputed
from those checks rather than trusted from a counter.

**Frames rendered with different settings are a defect, not a resume.** The
whole point of a single unbroken shot is that no boundary is visible. Two frames
from the same range with different sample counts, a different camera, or a
different .blend are a seam. So every frame records the hash of the spec and the
scene that produced it, and a resume that would mix hashes stops and says so
instead of quietly filling the gap.

**A blank frame is not delivered.** Everything above verifies the FILE. A
640x480 PNG came back from this farm structurally perfect, sha256-matched,
correctly dimensioned and entirely black, and every check here passed it. In a
sequence that is the worst case of all: the row says done, so a resume skips it
forever, and the hole only appears when the shot is assembled and watched. So
`verify_frame` now also asks what is in the image — from the measurement
recorded at delivery on the cheap pass, and by re-decoding the file on `--deep`.
See `broker/imgstat.py`.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable, Optional

from . import config, imgstat

# Narrow on purpose: this string becomes a directory name here and a filename
# prefix, and job ids are broker-minted precisely because a client-supplied one
# was a traversal vector. No dots, no slashes, so `..` is unrepresentable.
SEQ_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# The fields that decide what the image looks like. `persistent_data` and
# `require_caches` are deliberately absent — the first is a memory/speed
# tradeoff and the second a safety gate, and neither changes a pixel. Including
# them would force a 3,000-frame re-render to change a performance knob.
IMAGE_FIELDS = (
    "camera", "resolution", "samples", "engine", "denoiser", "denoise_gpu",
    "use_dof", "film_transparent", "border", "zoom", "exposure", "max_bounces",
    "adaptive_threshold",
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class SeqError(ValueError):
    """Rejected sequence reference. Message is safe to return over HTTP."""


def valid_name(name: str) -> str:
    if not name or not SEQ_RE.fullmatch(str(name)):
        raise SeqError(
            f"sequence name {name!r} is not usable: it becomes a directory name, so "
            f"it must be 1-64 characters of A-Z a-z 0-9 _ - only"
        )
    return str(name)


def seq_dir(name: str) -> Path:
    return config.SEQ_DIR / valid_name(name)


def frame_path(name: str, frame: int) -> Path:
    """`shot01/shot01_000042.png`.

    Six digits, zero-padded, because ffmpeg's `-i name_%06d.png` needs a fixed
    width and 24 fps × 130 s is four digits already — a shot that grows past
    9,999 frames must not silently change its own naming convention halfway.
    """
    return seq_dir(name) / f"{valid_name(name)}_{int(frame):06d}.png"


def spec_hash(spec: dict, scene_digest: str) -> str:
    """Identity of "what this frame is a render of".

    The scene's content hash is part of it. A reassembled .blend is a different
    shot even when every render parameter is identical, and continuing a
    sequence across one would produce exactly the invisible seam this whole
    design exists to prevent.
    """
    payload = {k: spec.get(k) for k in IMAGE_FIELDS}
    payload["scene"] = scene_digest
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --- ranges ---------------------------------------------------------------


def parse_range(text: str) -> tuple[int, int, int]:
    """`1-240`, `1-240x2`, `57` -> (first, last, step)."""
    raw = str(text).strip().lower().replace(" ", "")
    step = 1
    if "x" in raw:
        raw, _, step_text = raw.partition("x")
        try:
            step = int(step_text)
        except ValueError:
            raise SeqError(f"bad frame step in {text!r}") from None
    if "-" in raw[1:]:
        head, _, tail = raw[1:].partition("-")
        first_text, last_text = raw[0] + head, tail
    else:
        first_text = last_text = raw
    try:
        first, last = int(first_text), int(last_text)
    except ValueError:
        raise SeqError(
            f"bad frame range {text!r} — expected FIRST-LAST, FIRST-LASTxSTEP, or a "
            f"single frame number"
        ) from None
    if step < 1:
        raise SeqError(f"frame step must be >= 1, got {step}")
    if last < first:
        raise SeqError(f"frame range {text!r} ends ({last}) before it starts ({first})")
    return first, last, step


def expand(first: int, last: int, step: int) -> list[int]:
    return list(range(first, last + 1, step))


def parse_frames(text: str) -> list[int]:
    """`620-980`, `620-980x2`, `57`, `1-40,57,90-93x3` -> a sorted frame list.

    Comma-separated because a shot is not always a contiguous run. Re-rendering
    the twelve frames a compositor rejected, or the three holes `rq seq status`
    just named, is one job — and `summarise()` already prints missing frames in
    exactly this syntax, so what the status command says is now what the resume
    command accepts. Copy-paste is a supported workflow; retyping twelve
    separate submissions is how frames get missed.

    De-duplicated and sorted: overlapping parts (`1-10,5-15`) are a range, not
    an error, and rendering frame 5 twice in one job would just cost money.
    """
    raw = str(text).strip()
    if not raw:
        raise SeqError("no frames given — expected e.g. 620-980, 620-980x2, or "
                       "1-40,57,90-93")
    frames: list[int] = []
    for part in raw.split(","):
        if not part.strip():
            # A trailing or doubled comma is a typo in something that decides how
            # much GPU time is spent. Refuse it rather than guessing which of the
            # neighbouring parts was meant.
            raise SeqError(f"empty part in frame list {text!r} — a stray comma")
        first, last, step = parse_range(part)
        frames.extend(expand(first, last, step))
    return sorted(set(frames))


def bounds(frames: list[int]) -> tuple[int, int, int]:
    """(first, last, step) describing `frames`, for display and for the columns.

    `step` is the real one when the list is a plain arithmetic run and 1
    otherwise, because there is no honest single step for `1-40,57,90-93`. The
    authoritative list is stored separately; these three numbers are what a
    human reads in `rq status`.
    """
    if not frames:
        raise SeqError("empty frame list")
    if len(frames) == 1:
        return frames[0], frames[0], 1
    steps = {b - a for a, b in zip(frames, frames[1:])}
    step = steps.pop() if len(steps) == 1 else 1
    return frames[0], frames[-1], step


def is_run(frames: list[int], first: int, last: int, step: int) -> bool:
    """Do (first, last, step) describe `frames` exactly? Then no list is stored."""
    return frames == expand(first, last, step)


def frames_of(job: dict) -> list[int]:
    """Which frames a queue row covers. The ONE place that answers this.

    A row carries both an arithmetic run (three columns) and, when the request
    was not one, an explicit list. Reading the columns directly is how a resume
    of `1-40,57` would quietly become a resume of `1-57` — 16 frames of GPU time
    nobody asked for, filed under a sequence whose spec they match, therefore
    never flagged by anything downstream.
    """
    raw = job.get("frame_list")
    if raw:
        try:
            listed = json.loads(raw)
        except (TypeError, ValueError):
            raise SeqError(
                f"job {job.get('id')} has an unreadable frame_list ({raw!r:.80}) — "
                f"refusing to fall back to {job.get('frame_first')}-"
                f"{job.get('frame_last')}, which would render frames that were "
                f"never requested"
            ) from None
        return sorted({int(f) for f in listed})
    return expand(int(job["frame_first"]), int(job["frame_last"]),
                  int(job["frame_step"] or 1))


def summarise(frames: Iterable[int]) -> str:
    """`1-40, 57, 90-93` — a frame list a human can read and act on.

    Printing 812 individual numbers is how "which frames are missing?" gets
    answered in a way nobody can use.
    """
    nums = sorted(set(int(f) for f in frames))
    if not nums:
        return "none"
    runs: list[tuple[int, int]] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        runs.append((start, prev))
        start = prev = n
    runs.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


# --- verification ---------------------------------------------------------


def inspect_png(path: Path) -> dict:
    """Structure of a PNG on disk, without decoding it.

    Reads the 8-byte signature, the IHDR dimensions, and the last 12 bytes for
    the IEND marker. That combination is what separates a complete frame from
    the failure this project has actually observed — a truncated file that still
    opens, still displays, and is still wrong.
    """
    info: dict = {"exists": False, "bytes": 0, "width": None, "height": None,
                  "complete": False, "reason": "", "mtime": 0.0}
    try:
        stat = path.stat()
    except OSError as exc:
        info["reason"] = f"cannot stat: {type(exc).__name__}"
        return info
    size = stat.st_size
    info["exists"] = True
    info["bytes"] = size
    info["mtime"] = stat.st_mtime
    if size < 45:
        info["reason"] = f"{size} bytes is too small to be a PNG"
        return info
    try:
        with open(path, "rb") as fh:
            head = fh.read(33)
            fh.seek(-12, 2)
            tail = fh.read(12)
    except OSError as exc:
        info["reason"] = f"cannot read: {type(exc).__name__}: {exc}"
        return info
    if head[:8] != PNG_MAGIC:
        info["reason"] = "no PNG signature"
        return info
    if head[12:16] != b"IHDR":
        info["reason"] = "no IHDR chunk"
        return info
    info["width"] = int.from_bytes(head[16:20], "big")
    info["height"] = int.from_bytes(head[20:24], "big")
    if b"IEND" not in tail:
        info["reason"] = "IEND missing — the file is truncated"
        return info
    info["complete"] = True
    return info


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# How far a delivered frame's mtime may sit AFTER the moment it was recorded
# done. The file is written by `tmp.replace(local)` and the row is written a
# measurement later, so a genuine frame's mtime is always the EARLIER of the two
# — the slack only absorbs filesystem timestamp granularity and the one case
# where a frame was recorded within the same tick it landed.
MTIME_SLACK_SEC = 2.0


def verify_frame(path: Path, expect_bytes: Optional[int] = None,
                 expect_wh: Optional[tuple[int, int]] = None,
                 expect_sha: Optional[str] = None,
                 recorded_verdict: Optional[str] = None,
                 measure: bool = False,
                 delivered_at: Optional[float] = None) -> tuple[bool, str]:
    """Is this file the frame it claims to be? Returns (ok, why not).

    Every expectation is optional so the same function serves the cheap resume
    check (structure and recorded size) and the deep audit (`--deep`, which
    re-hashes and re-decodes). What it never does is answer "probably": a frame
    that cannot be confirmed is treated as missing, because re-rendering one
    frame costs seconds and shipping a corrupt one costs the delivery.

    The last gate is the one the file checks cannot see. `recorded_verdict` is
    what the image measured as when it was delivered, so the cheap pass can
    refuse a blank frame without decoding 3,000 PNGs — the size and dimension
    checks above already prove the file has not changed since that measurement.
    `measure=True` decodes it again instead of trusting the record, which is
    what `--deep` does and the only thing that finds a blank frame delivered
    before this check existed.
    """
    info = inspect_png(path)
    if not info["exists"]:
        return False, "missing"
    if not info["complete"]:
        return False, info["reason"] or "not a complete PNG"
    if expect_bytes is not None and info["bytes"] != expect_bytes:
        return False, f"{info['bytes']} bytes on disk, {expect_bytes} recorded"
    if expect_wh and (info["width"], info["height"]) != tuple(expect_wh):
        return False, (f"{info['width']}x{info['height']} on disk, "
                       f"{expect_wh[0]}x{expect_wh[1]} expected")
    # "Is this the file that was delivered?", asked for the price of the stat
    # that already happened. Everything above compares the file against the
    # RECORD, and a file edited in place without changing its length or its
    # dimensions matches the record perfectly — measured here on 2026-08-02, a
    # single flipped byte in a 716,012-byte frame passed size, dimensions and
    # structure and was caught only by the sha256 on `--deep`.
    #
    # That matters because the resume set is computed on the CHEAP pass. Deep
    # verification re-reads every byte of the sequence — 101 GB for a 2,978-frame
    # 4K master — which is not something a resume can afford to do on every pass,
    # so before this the one check that could see such a frame was the one a
    # resume never runs. A frame whose file has been touched since it was
    # recorded is not the frame that was recorded, whatever its length says.
    if delivered_at and info["mtime"] > float(delivered_at) + MTIME_SLACK_SEC:
        return False, (
            f"modified {info['mtime'] - float(delivered_at):.0f}s AFTER it was "
            f"recorded delivered — the file on disk is not the one that was "
            f"fetched and verified"
        )
    if expect_sha:
        got = sha256_of(path)
        if got != expect_sha:
            return False, f"sha256 {got[:12]} != recorded {expect_sha[:12]}"
    if measure:
        stats = imgstat.measure(path)
        if imgstat.is_blank(stats["verdict"]):
            return False, f"{stats['verdict']}: {stats['detail']}"
    elif imgstat.is_blank(recorded_verdict):
        return False, (f"{recorded_verdict}: measured as a blank frame when it was "
                       f"delivered — it was never a deliverable frame")
    return True, ""


# --- planning -------------------------------------------------------------


class Plan:
    """What a submitted range actually amounts to once resume is applied."""

    def __init__(self) -> None:
        self.todo: list[int] = []
        self.have: list[int] = []
        self.stale: list[int] = []          # recorded done, file no longer verifies
        self.conflict: list[int] = []       # done, but by a different spec

    @property
    def total(self) -> int:
        return len(self.todo) + len(self.have) + len(self.conflict)


def plan_range(db, name: str, frames: list[int], want_hash: str,
               deep: bool = False) -> Plan:
    """Split a requested range into what must be rendered and what already is.

    The file is re-checked on every planning pass, not just the row. A row says
    a frame was delivered; only the file says it still is. Deleting a frame from
    the sequence directory is therefore a supported way to force one frame to be
    re-rendered, and a frame corrupted after the fact is caught here rather than
    in the finished video.
    """
    plan = Plan()
    for frame in frames:
        row = db.frame(name, frame)
        if not row or row.get("state") != "done":
            plan.todo.append(frame)
            continue
        if want_hash and row.get("spec_hash") and row["spec_hash"] != want_hash:
            plan.conflict.append(frame)
            continue
        path = Path(row.get("path") or frame_path(name, frame))
        wh = ((row.get("width"), row.get("height"))
              if row.get("width") and row.get("height") else None)
        ok, _ = verify_frame(path, row.get("bytes"), wh,
                             row.get("sha256") if deep else None,
                             recorded_verdict=row.get("blank"), measure=deep,
                             delivered_at=row.get("finished"))
        if ok:
            plan.have.append(frame)
        else:
            plan.stale.append(frame)
            plan.todo.append(frame)
    return plan


def audit(db, name: str, deep: bool = False,
          frames: Optional[list[int]] = None,
          remeasure: Optional[bool] = None) -> dict:
    """Full report on a sequence on disk: what is here, what is not, what is wrong.

    Never returns a bare "ok". Every frame is named in exactly one bucket, and
    the caller is expected to print `missing` and `bad` before believing it has
    a deliverable.

    Two things here are about content rather than files. `blank` counts the
    frames that measured as having nothing in them — they are in `bad` too, so
    they cannot be mistaken for delivered, but they are named separately because
    the fix is a different one from a truncated file. `outliers` is the check
    that a fixed threshold cannot make: frames whose statistics do not belong
    with their neighbours'. Neither is advisory; both exist because a hole in a
    cut-free shot is only visible once the whole thing is assembled.

    `unmeasured` counts frames delivered before this check existed. They are
    still `ok` — nothing says they are bad — but a sequence with any of them has
    not actually been checked for blanks, and `--deep` is what settles it.
    """
    remeasure = deep if remeasure is None else remeasure
    rows = {r["frame"]: r for r in db.frames(name)}
    wanted = frames if frames is not None else sorted(rows)
    good, missing, bad, blank = [], [], [], []
    measured: list[dict] = []
    unmeasured = 0
    total_bytes = 0
    hashes: dict[str, int] = {}
    dims: dict[str, int] = {}
    verdicts: dict[str, int] = {}
    for frame in wanted:
        row = rows.get(frame)
        if not row or row.get("state") != "done":
            missing.append({"frame": frame,
                            "why": (row or {}).get("err") or "never rendered"})
            continue
        path = Path(row.get("path") or frame_path(name, frame))
        wh = ((row.get("width"), row.get("height"))
              if row.get("width") and row.get("height") else None)
        stats = None
        if remeasure and path.exists():
            stats = imgstat.measure(path)
        ok, why = verify_frame(path, row.get("bytes"), wh,
                               row.get("sha256") if deep else None,
                               # A fresh measurement beats a recorded one. A
                               # frame whose file was replaced since it was
                               # measured must be judged on what is on disk now,
                               # or `--deep` could refuse a frame it had just
                               # measured as fine.
                               recorded_verdict=None if stats else row.get("blank"),
                               measure=False,
                               delivered_at=row.get("finished"))
        # Applied here rather than inside verify_frame so the report can name
        # the numbers, not only the verdict. The rule is identical either way.
        if ok and stats is not None and imgstat.is_blank(stats["verdict"]):
            ok, why = False, f"{stats['verdict']}: {stats['detail']}"

        verdict = (stats or {}).get("verdict") or row.get("blank")
        mean = (stats or {}).get("mean", row.get("lum_mean"))
        sd = (stats or {}).get("sd", row.get("lum_sd"))
        if verdict:
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
        else:
            unmeasured += 1
        if mean is not None and sd is not None:
            measured.append({"frame": frame, "mean": mean, "sd": sd,
                             "verdict": verdict})

        if ok:
            good.append(frame)
            total_bytes += row.get("bytes") or 0
            if row.get("spec_hash"):
                hashes[row["spec_hash"]] = hashes.get(row["spec_hash"], 0) + 1
            if row.get("width"):
                key = f"{row['width']}x{row['height']}"
                dims[key] = dims.get(key, 0) + 1
        else:
            bad.append({"frame": frame, "why": why, "path": str(path)})
            if imgstat.is_blank(verdict):
                blank.append({"frame": frame, "verdict": verdict, "why": why})
    return {
        "seq": name,
        "dir": str(seq_dir(name)),
        "checked": len(wanted),
        "ok": len(good),
        "missing": missing,
        "bad": bad,
        "bytes": total_bytes,
        "deep": deep,
        "remeasured": remeasure,
        # More than one entry in either of these means the sequence is not one
        # consistent render, which for a single unbroken shot is a defect even
        # when every individual frame verifies.
        "spec_hashes": hashes,
        "dimensions": dims,
        "contiguous": summarise(good),
        # --- content ---
        "blank": blank,
        "verdicts": verdicts,
        "unmeasured": unmeasured,
        "outliers": imgstat.outliers(measured),
    }


def local_space(name: str, to_render: int, mean_bytes: Optional[float]) -> dict:
    """Will the frames this job is about to render FIT on this machine?

    The instance's disk has been guarded for a while and is not the one that
    runs out. Frames are deleted from it the moment each fetch verifies, so a
    2,978-frame batch never holds more than one frame there — measured on
    2026-08-02, `/workspace/out` was empty mid-batch. Every frame accumulates
    HERE instead, and nothing was looking.

    The arithmetic that motivated this, taken from real numbers on this machine
    the same day: 4K frames of the round-2 assembly return at ~34 MB each, so a
    2,978-frame master is ~101 GB, and `/` had 79 GiB free. The batch fills the
    disk somewhere around frame 2,500 — eighteen days and roughly $155 of GPU
    into a render whose remaining frames then fail on write, one after another,
    for a reason no per-frame error message would name.

    A warning rather than a refusal, deliberately. The caller may be about to
    free space, may be moving frames off as they land, and `SEQ_DIR` may be a
    different mount from the one this process happens to live on. A check that
    refuses a legitimate multi-day batch is a check that gets switched off. What
    it must not do is stay quiet, so this returns the numbers either way and the
    caller prints them before anything is rented.
    """
    import shutil

    directory = seq_dir(name)
    try:
        # The directory has to exist to be measured, and it is where the frames
        # are going anyway. Inside the guard because this runs on the submit
        # path: a disk that cannot be measured must degrade to "I do not know",
        # never to a 500 that stops a legitimate batch being queued.
        directory.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(directory)
    except OSError as exc:
        return {"known": False,
                "note": f"could not measure free space at {directory}: {exc}"}
    if not mean_bytes or to_render <= 0:
        return {"known": False, "free_bytes": usage.free, "dir": str(directory),
                "note": ("no delivered frame of this kind on record, so how much "
                         "disk the batch needs is not yet a measurement — render "
                         "one frame at the batch's resolution and ask again")}
    # Rounded ONCE, then used for everything below. Reporting the mean truncated
    # while dividing by the full float made the printed numbers disagree with
    # each other — `free_bytes / mean_bytes` came to 5 where the same dict said
    # 4 — and a preflight whose own arithmetic does not reproduce is a preflight
    # nobody will believe the day it says something inconvenient.
    mean = max(1, int(round(mean_bytes)))
    need = mean * to_render
    return {
        "known": True,
        "dir": str(directory),
        "frames": to_render,
        "mean_bytes": mean,
        "need_bytes": need,
        "free_bytes": usage.free,
        "fits": need < usage.free,
        # Where it runs out, which is the number that decides whether this is an
        # emergency or a note. "It will not fit" is much less useful than "it
        # stops at frame 2,494 of 2,978".
        "frames_that_fit": int(usage.free // mean),
    }


def write_manifest(name: str, db, extra: Optional[dict] = None) -> Path:
    """A machine-readable record beside the frames themselves.

    The database is authoritative, but the deliverable is a directory, and a
    directory that travels without its database should still be able to say what
    it is and whether it is complete.
    """
    directory = seq_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    rows = db.frames(name)
    doc = {
        "seq": name,
        "frames": [
            {k: r.get(k) for k in
             ("frame", "state", "bytes", "width", "height", "sha256",
              "render_sec", "spec_hash", "job_id",
              # What was IN each frame, not just that the file was intact. A
              # directory that travels without this database should still carry
              # the evidence that its frames had content.
              "blank", "lum_mean", "lum_sd", "lum_min", "lum_max", "lum_levels")}
            for r in rows
        ],
        "summary": db.seq_summary(name),
    }
    if extra:
        doc.update(extra)
    path = directory / "manifest.json"
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True))
    tmp.replace(path)
    return path
