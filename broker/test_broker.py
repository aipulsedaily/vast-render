#!/usr/bin/env python3
"""Broker tests.

Queue semantics, frame-sequence planning and output verification are checked
directly against temporary files — no GPU, no network, no money. The HTTP
section needs a broker running; point it at one with a deliberately missing
scene so dispatch fails before it can rent anything:

    VASTRENDER_SCENE=/tmp/nope.blend .venv/bin/python -m broker.app &
    .venv/bin/python -m broker.test_broker

**Do not run the HTTP section against the live broker.** It submits jobs to
whatever is listening on port 8760, so against a working broker it queues junk
renders on a real GPU, and its last two checks ("long-poll returns on terminal
state", "no GPU rented during tests") fail by observing the live batch rather
than anything the tests did. Run the offline sections alone with:

    .venv/bin/python -c "from broker import test_broker as t; \
        raise SystemExit(t.run_offline())"

The focus is the failure modes that cost real money or silently lose work:
leases surviving a crash, retries terminating, and admission control holding.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import app, config, execservice, remote
from .db import DB
from .fleet import Fleet

BASE = "http://127.0.0.1:8760"
results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name, detail))


def spec(cam: str = "CAM_A") -> dict:
    return {
        "camera": cam, "resolution": [64, 64], "samples": 1, "engine": "CYCLES",
        "denoiser": "NONE", "denoise_gpu": False, "use_dof": False,
        "film_transparent": False, "border": None, "zoom": 1.0, "exposure": None,
        "max_bounces": None, "adaptive_threshold": 0.01,
        "frame": None, "persistent_data": True, "require_caches": True,
    }


# --- frame sequences ------------------------------------------------------
#
# All of this runs against temporary files: the properties being checked are
# about bookkeeping and verification, and none of them need a GPU. The point is
# that a resume must never skip a frame whose file is not actually good, because
# the cost of being wrong is a hole in a video that nobody sees until delivery.


def _png(path: Path, width: int = 4, height: int = 3, trailer: bytes = b"IEND\xaeB`\x82",
         body: bytes = b"") -> Path:
    """A minimal file with a real PNG signature, IHDR and (optionally) IEND."""
    ihdr = (b"\x00\x00\x00\x0dIHDR"
            + width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + b"\x08\x06\x00\x00\x00")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + ihdr + (body or b"\x00" * 40) + trailer)
    return path


def test_seq_verification() -> None:
    from . import seq

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good = _png(root / "good.png")
        ok, why = seq.verify_frame(good, good.stat().st_size, (4, 3))
        check("complete PNG verifies", ok, why)

        # The observed corruption: a file that is the right shape at the front
        # and simply stops. It opens in most viewers, showing a partial image.
        truncated = _png(root / "cut.png", trailer=b"")
        ok, why = seq.verify_frame(truncated, truncated.stat().st_size, (4, 3))
        check("truncated PNG rejected", not ok and "IEND" in why, why)

        # Right length, wrong content — only a digest can tell.
        flipped = _png(root / "flip.png", body=b"\xff" * 40)
        ok, why = seq.verify_frame(flipped, flipped.stat().st_size, (4, 3),
                                   expect_sha=seq.sha256_of(good))
        check("corrupted-but-plausible PNG rejected by digest",
              not ok and "sha256" in why, why)

        ok, why = seq.verify_frame(good, good.stat().st_size, (999, 999))
        check("wrong dimensions rejected", not ok and "expected" in why, why)

        ok, why = seq.verify_frame(root / "nope.png")
        check("missing frame rejected", not ok and why == "missing", why)


def _real_png(path: Path, width: int = 32, height: int = 24,
              pixels=None, fill=(0, 0, 0), alpha: Optional[int] = None) -> Path:
    """A PNG that actually decodes, written with nothing but zlib.

    `_png` above is a structural fixture — right signature, right IHDR, junk for
    image data — and it is the right tool for "is this file intact". It is the
    wrong tool for every check in this section, because the whole point here is
    what the PIXELS are. Written by hand rather than with Pillow so these tests
    have the same dependencies as `rq` itself.

    `pixels(x, y) -> (r, g, b)` paints; `fill` is a solid colour; `alpha` adds an
    alpha channel at that constant value.
    """
    import struct
    import zlib as _zlib

    channels = 4 if alpha is not None else 3
    ctype = 6 if alpha is not None else 2
    rows = bytearray()
    for y in range(height):
        rows.append(0)                                  # filter: None
        for x in range(width):
            r, g, b = pixels(x, y) if pixels else fill
            rows += bytes((r & 255, g & 255, b & 255))
            if alpha is not None:
                rows.append(alpha & 255)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", _zlib.crc32(kind + body)))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0))
        + chunk(b"IDAT", _zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )
    return path


def _busy(x: int, y: int) -> tuple[int, int, int]:
    """Pixels with real structure in them — a stand-in for a rendered frame."""
    v = (x * 37 + y * 91) % 256
    return v, (v * 3) % 256, (v * 7 + 40) % 256


def _faint(x: int, y: int) -> tuple[int, int, int]:
    """Seven grey levels spread over six steps: sd ~2/255 = 0.008.

    Deliberately BETWEEN the two thresholds — above BLANK_SD_MAX (0.005), below
    SUSPECT_SD_MAX (0.02) — because that band is the whole reason there is a
    classification rather than a threshold. This is what the flat 4K frame
    `out/f36725c40f08.png` measured at: sd 0.00794, 14 levels, mean 0.774.
    """
    v = 200 + (x * 3 + y * 5) % 7
    return v, v, v


def test_imgstat_classifies_what_is_in_the_image() -> None:
    """The gap every other check in this file leaves open.

    A structurally perfect, sha256-matched, correctly dimensioned 640x480 PNG
    came back from this farm entirely black — `out/0908e534b1d3.png`, 8,734
    bytes, mean 0.00000, sd 0.00000 — and was recorded `done`. Nothing here
    verifies files; it verifies pictures.
    """
    from . import imgstat

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        black = _real_png(root / "black.png", fill=(0, 0, 0))
        st = imgstat.measure(black)
        check("an all-black frame is BLACK",
              st["verdict"] == imgstat.BLACK and st["mean"] == 0.0 and st["sd"] == 0.0,
              imgstat.summary(st))

        grey = _real_png(root / "grey.png", fill=(128, 128, 128))
        st = imgstat.measure(grey)
        check("a flat GREY frame is UNIFORM, not OK — flat at any brightness "
              "is just as broken", st["verdict"] == imgstat.UNIFORM,
              imgstat.summary(st))

        white = _real_png(root / "white.png", fill=(255, 255, 255))
        check("a flat WHITE frame is UNIFORM too",
              imgstat.measure(white)["verdict"] == imgstat.UNIFORM)

        clear = _real_png(root / "clear.png", pixels=_busy, alpha=0)
        st = imgstat.measure(clear)
        check("a fully transparent frame is TRANSPARENT even with RGB content",
              st["verdict"] == imgstat.TRANSPARENT, imgstat.summary(st))

        # Genuine content: this must NOT be flagged, or the check gets switched
        # off and protects nothing.
        good = _real_png(root / "good.png", pixels=_busy)
        st = imgstat.measure(good)
        check("a frame with content is OK",
              st["verdict"] == imgstat.OK and st["sd"] > 0.02, imgstat.summary(st))

        # A few grey levels apart: nothing anyone would call an image, but far
        # enough from flat that calling it empty would be a lie. Reported loudly,
        # never fatal — this is the band the real frame f36725c40f08 sits in.
        faint = _real_png(root / "faint.png", pixels=_faint)
        st = imgstat.measure(faint)
        check("an almost-flat frame is SUSPICIOUS but not blank",
              st["verdict"] == imgstat.SUSPICIOUS and not imgstat.is_blank(st["verdict"]),
              imgstat.summary(st))

        # Two levels one step apart IS flat, and is caught as such.
        near = _real_png(root / "near.png",
                         pixels=lambda x, y: ((200,) * 3 if (x + y) % 2 else (201,) * 3))
        check("two adjacent grey levels is still UNIFORM",
              imgstat.measure(near)["verdict"] == imgstat.UNIFORM,
              imgstat.summary(imgstat.measure(near)))

        # A dark frame with real content is legitimate — a night shot, a fade.
        # This is the false positive that would make the check unusable.
        dark = _real_png(root / "dark.png",
                         pixels=lambda x, y: (v := (x * 13 + y * 7) % 40, v, v))
        st = imgstat.measure(dark)
        check("a DARK frame with content is OK, not BLACK",
              st["verdict"] == imgstat.OK, imgstat.summary(st))

        st = imgstat.measure(root / "does-not-exist.png")
        check("a missing file is UNREADABLE rather than an exception",
              st["verdict"] == imgstat.UNREADABLE and not imgstat.is_blank(st["verdict"]),
              st["detail"][:50])

        # The actual artefact, when it is still on disk. Not a synthetic
        # reconstruction of the bug — the bug.
        real = Path(__file__).resolve().parent.parent / "out" / "0908e534b1d3.png"
        if real.exists():
            st = imgstat.measure(real)
            check("the real frame that started this is caught",
                  st["verdict"] == imgstat.BLACK, imgstat.summary(st))


def test_imgstat_decoders_agree() -> None:
    """Pillow is not a declared dependency of this project — it arrives
    transitively — so a pure-stdlib decoder sits behind it. Two decoders that
    disagree are worse than one slow decoder, so they are checked against each
    other on every colour type the worker can emit."""
    from . import imgstat

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cases = {
            "rgb": _real_png(root / "a.png", pixels=_busy),
            "rgba": _real_png(root / "b.png", pixels=_busy, alpha=128),
            "black": _real_png(root / "c.png", fill=(0, 0, 0)),
            "clear": _real_png(root / "d.png", pixels=_busy, alpha=0),
        }
        real = imgstat._pillow
        worst = 0.0
        verdicts_agree = True
        for name, path in cases.items():
            with_pil = imgstat.measure(path)
            imgstat._pillow = lambda: None
            try:
                without = imgstat.measure(path)
            finally:
                imgstat._pillow = real
            verdicts_agree &= with_pil["verdict"] == without["verdict"]
            worst = max(worst, abs(with_pil["mean"] - without["mean"]),
                        abs(with_pil["sd"] - without["sd"]))
        # Sub-quantisation agreement. One 8-bit level is 1/255 = 0.0039 and the
        # tightest threshold in play is 0.005, so anything at 1e-4 cannot move a
        # verdict.
        check("stdlib and Pillow decoders reach the same verdict and the same "
              "numbers", verdicts_agree and worst < 1e-4,
              f"largest disagreement {worst:.2e}")

        check("Pillow is skipped for 16-bit greyscale, where its own convert('L') "
              "clips at 255", "I;16" in imgstat.PILLOW_LOSSY_MODES)


def test_blank_frame_is_never_a_delivered_frame() -> None:
    """The resume-poisoning case, and the most dangerous one.

    A black frame that verifies is recorded `done`, so every later resume skips
    it — "already delivered" — and it survives to the assembly. There is nothing
    to cut around it in a single unbroken take.

    THIS TEST FAILS AGAINST THE OLD CODE. Every call below uses the signature
    the old `verify_frame` and `frame_done` had, and the old versions answer
    "this frame is fine" to all of them.
    """
    from . import imgstat, seq

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = DB(root / "t.db")
        name, want = "shot", seq.spec_hash(spec(), "scene-digest")

        black = _real_png(root / "shot_000007.png", fill=(0, 0, 0))
        good = _real_png(root / "shot_000008.png", pixels=_busy)
        for frame, path in ((7, black), (8, good)):
            db.frame_done(name, frame, "job1", str(path), path.stat().st_size,
                          32, 24, seq.sha256_of(path), 1.0, want)

        # Everything the farm checked before this existed, on the black frame.
        ok, why = seq.verify_frame(black, black.stat().st_size, (32, 24),
                                   seq.sha256_of(black))
        check("THE BUG: file checks alone pass a black frame", ok, why)

        # And what it answers now that it is asked about the picture.
        ok, why = seq.verify_frame(black, black.stat().st_size, (32, 24),
                                   seq.sha256_of(black), measure=True)
        check("measuring the frame rejects it", not ok and "BLACK" in why, why)

        ok, why = seq.verify_frame(good, good.stat().st_size, (32, 24),
                                   seq.sha256_of(good), measure=True)
        check("and still accepts a frame with content in it", ok, why)

        plan = seq.plan_range(db, name, [7, 8], want, deep=True)
        check("`rq seq verify` re-renders the blank frame instead of counting "
              "it delivered",
              plan.todo == [7] and plan.have == [8] and plan.stale == [7],
              f"todo={plan.todo} have={plan.have} stale={plan.stale}")

        report = seq.audit(db, name, deep=True)
        check("the audit names it BLANK, separately from CORRUPT",
              report["ok"] == 1 and [b["frame"] for b in report["blank"]] == [7]
              and report["verdicts"].get(imgstat.BLACK) == 1,
              f"ok={report['ok']} blank={report['blank']} verdicts={report['verdicts']}")


def test_blank_verdict_recorded_at_delivery_blocks_the_cheap_resume() -> None:
    """Re-decoding 2,978 4K PNGs on every planning pass would cost ~25 minutes,
    so the cheap pass reads the verdict recorded when the frame was delivered.
    The size and dimension checks alongside it are what prove the file has not
    changed since that measurement."""
    from . import imgstat, seq

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = DB(root / "t.db")
        name, want = "shot", seq.spec_hash(spec(), "d")

        black = _real_png(root / "b.png", fill=(0, 0, 0))
        db.frame_done(name, 1, "job1", str(black), black.stat().st_size, 32, 24,
                      seq.sha256_of(black), 1.0, want, imgstat.measure(black))
        row = db.frame(name, 1)
        check("the measurement is stored on the frame row",
              row["blank"] == imgstat.BLACK and row["lum_mean"] == 0.0
              and row["lum_levels"] == 1, str(dict(row))[:80])

        plan = seq.plan_range(db, name, [1], want, deep=False)
        check("the CHEAP resume pass refuses it too, without decoding anything",
              plan.todo == [1] and plan.stale == [1], f"todo={plan.todo}")

        good = _real_png(root / "g.png", pixels=_busy)
        db.frame_done(name, 2, "job1", str(good), good.stat().st_size, 32, 24,
                      seq.sha256_of(good), 1.0, want, imgstat.measure(good))
        plan = seq.plan_range(db, name, [1, 2], want, deep=False)
        check("and leaves a measured good frame alone",
              plan.have == [2], f"have={plan.have}")

        # A frame delivered before this check existed has NULL here. It is not
        # rejected — nothing says it is bad — but it must not read as cleared.
        db.frame_done(name, 3, "job1", str(good), good.stat().st_size, 32, 24,
                      seq.sha256_of(good), 1.0, want)
        report = seq.audit(db, name, deep=False)
        check("frames delivered before the check are counted as UNMEASURED, "
              "not as OK", report["unmeasured"] == 1, str(report["verdicts"]))

        # A frame whose file has been REPLACED since it was measured must be
        # judged on what is on disk now. Otherwise a stale row could refuse a
        # frame that `--deep` had just measured as fine.
        replaced = seq.frame_path(name, 1)
        replaced.parent.mkdir(parents=True, exist_ok=True)
        _real_png(replaced, pixels=_busy)
        db.frame_done(name, 1, "job2", str(replaced), replaced.stat().st_size,
                      32, 24, seq.sha256_of(replaced), 1.0, want)
        db.conn.execute("UPDATE frames SET blank='BLACK' WHERE seq=? AND frame=1",
                        (name,))
        db.conn.commit()
        report = seq.audit(db, name, deep=True, frames=[1])
        check("a re-measurement beats a stale recorded verdict",
              report["ok"] == 1 and not report["blank"],
              f"ok={report['ok']} blank={report['blank']}")


def test_sequence_outliers_find_the_buried_frame() -> None:
    """The check a fixed threshold cannot make.

    One dead frame at 1,600 of 2,978 is caught by comparing it with frames
    1,590-1,610, not by comparing it with a constant — and a fade to black walks
    a whole neighbourhood down together and must NOT be flagged, or the check is
    useless on a film.
    """
    from . import imgstat

    # A shot that drifts, the way a 124-second continuous take does, with one
    # frame dropped in the middle of it.
    rows = [{"frame": f, "mean": 0.42 + 0.00005 * f + (0.004 if f % 3 else -0.003),
             "sd": 0.18 + 0.00002 * f} for f in range(1500, 1700)]
    rows[100] = {"frame": 1600, "mean": 0.0, "sd": 0.0}
    found = imgstat.outliers(rows)
    check("a single black frame buried in a drifting shot is flagged",
          [o["frame"] for o in found] == [1600],
          f"flagged {[o['frame'] for o in found]}")
    check("and the reason says what is wrong with it",
          found and "darker" in found[0]["why"], found[0]["why"] if found else "")

    # A legitimate fade: every frame darker than the last, all the way to black.
    fade = [{"frame": f, "mean": max(0.0, 0.6 - 0.006 * (f - 1500)),
             "sd": max(0.0, 0.20 - 0.002 * (f - 1500))} for f in range(1500, 1600)]
    check("a fade to black flags nothing — the neighbourhood moves together",
          imgstat.outliers(fade) == [],
          f"flagged {[o['frame'] for o in imgstat.outliers(fade)]}")

    # A dead-still locked-off shot: MAD is zero, every frame is infinitely many
    # MADs from the median, and without an absolute floor everything flags.
    static = [{"frame": f, "mean": 0.5, "sd": 0.2} for f in range(200)]
    check("an absolutely static shot flags nothing",
          imgstat.outliers(static) == [], str(imgstat.outliers(static))[:60])

    # ... but a dropped frame inside that static shot is unmissable.
    static[77] = {"frame": 77, "mean": 0.0, "sd": 0.0}
    check("a dropped frame inside a static shot is still caught",
          [o["frame"] for o in imgstat.outliers(static)] == [77],
          str([o["frame"] for o in imgstat.outliers(static)]))

    check("too few frames to have neighbours yields no opinion",
          imgstat.outliers([{"frame": 1, "mean": 0.0, "sd": 0.0}]) == [])


def test_blank_still_fails_terminally_unless_allowed() -> None:
    """A blank still must fail — and must not be retried.

    MAX_ATTEMPTS is 3. A camera aimed at empty space renders black three times
    for three times the money and reaches the same verdict, so this is the one
    failure in the broker that spends no further GPU. The escape hatch has to
    exist too: a black plate or a fade is a thing a caller can legitimately want.
    """
    from . import imgstat

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        black = imgstat.measure(_real_png(root / "b.png", fill=(0, 0, 0)))
        good = imgstat.measure(_real_png(root / "g.png", pixels=_busy))
        faint = imgstat.measure(_real_png(root / "f.png", pixels=_faint))

        try:
            app.Broker.blank_gate(spec(), black, "job x", root / "b.png")
            check("a blank still is refused", False, "the gate accepted it")
        except app.BlankOutput as exc:
            check("a blank still is refused, and the message says how to override",
                  "--allow-blank" in str(exc) and "BLACK" in str(exc), str(exc)[:60])

        allowed = dict(spec(), allow_blank=True)
        app.Broker.blank_gate(allowed, black, "job x", root / "b.png")
        check("--allow-blank delivers it anyway", True)

        app.Broker.blank_gate(spec(), good, "job y", root / "g.png")
        check("a frame with content passes untouched", True)

        # SUSPICIOUS is reported loudly and never fatal. A check that refuses
        # legitimate work gets switched off, and then it protects nothing.
        app.Broker.blank_gate(spec(), faint, "job z", root / "f.png")
        check("a SUSPICIOUS frame is reported, not failed",
              faint["verdict"] == imgstat.SUSPICIOUS, imgstat.summary(faint))

        db = DB(root / "t.db")
        job_id = db.submit(spec(), agent="t")
        db.claim(60)
        state = db.fail_terminal(job_id, "blank")
        row = db.get(job_id)
        check("a blank job is failed for good, not requeued for two more renders",
              state == "failed" and row["state"] == "failed" and row["attempts"] == 1,
              f"state={row['state']} attempts={row['attempts']}")


def test_seq_resume() -> None:
    """A resume must render exactly the frames that are not already good."""
    from . import seq

    with tempfile.TemporaryDirectory() as tmp:
        db = DB(Path(tmp) / "t.db")
        name = "shot"
        frames = list(range(1, 11))
        digest = "abc123"
        want = seq.spec_hash(spec(), digest)

        plan = seq.plan_range(db, name, frames, want)
        check("nothing recorded -> render everything", plan.todo == frames,
              f"{len(plan.todo)} todo")

        # Deliver 1-5 for real, so the files exist and verify.
        for f in frames[:5]:
            path = Path(tmp) / f"{name}_{f:06d}.png"
            _png(path)
            db.frame_done(name, f, "job1", str(path), path.stat().st_size,
                          4, 3, seq.sha256_of(path), 1.0, want)

        plan = seq.plan_range(db, name, frames, want)
        check("resume renders only what is missing",
              plan.todo == [6, 7, 8, 9, 10] and plan.have == [1, 2, 3, 4, 5],
              f"todo={plan.todo} have={plan.have}")

        # Corrupt a delivered frame. The row still says done; the file does not.
        victim = Path(db.frame(name, 3)["path"])
        victim.write_bytes(victim.read_bytes()[:20])
        plan = seq.plan_range(db, name, frames, want)
        check("a corrupted delivered frame is re-rendered, not trusted",
              3 in plan.todo and 3 in plan.stale and 2 in plan.have,
              f"todo={plan.todo} stale={plan.stale}")

        # Delete one. Same treatment — the directory is the source of truth.
        Path(db.frame(name, 4)["path"]).unlink()
        plan = seq.plan_range(db, name, frames, want)
        check("a deleted delivered frame is re-rendered",
              4 in plan.todo, f"todo={plan.todo}")

        # A different spec over the same frames is a seam, not a resume. Every
        # frame recorded under the old spec is a conflict, including the two
        # whose files are now broken: the hash is checked before the file
        # because "this sequence was rendered differently" is both cheaper to
        # answer and the more serious of the two conditions.
        other = seq.spec_hash(dict(spec(), samples=999), digest)
        plan = seq.plan_range(db, name, frames, other)
        check("frames from a different spec are a conflict, never silently reused",
              set(plan.conflict) == {1, 2, 3, 4, 5} and not plan.have,
              f"conflict={plan.conflict} have={plan.have}")

        # And a reassembled .blend must invalidate it too, even with identical
        # render parameters — otherwise a resume splices two different scenes.
        rebuilt = seq.spec_hash(spec(), "different-scene-hash")
        check("a different scene hash is a different spec", rebuilt != want,
              f"{rebuilt} vs {want}")

        report = seq.audit(db, name, deep=True, frames=frames)
        check("audit names every missing and bad frame, never a bare ok",
              report["ok"] == 3 and len(report["missing"]) == 5
              and len(report["bad"]) == 2,
              f"ok={report['ok']} missing={len(report['missing'])} "
              f"bad={len(report['bad'])}")


def test_keep_on_exit_decides_whether_shutdown_destroys() -> None:
    """Both directions of KEEP_ON_EXIT, because only one of them is safe to try live.

    Turning it ON and shutting down is testable against the real farm: if it is
    honoured the instance survives, and if it is not, the worst case is a
    rental. Turning it OFF and shutting down is not — a wrong answer there means
    a healthy GPU with somebody else's 4 GB scene on it is destroyed to prove a
    point. So the destroy direction is proved here, against a fleet that only
    records what it was asked to do.

    The `started` guard is checked in the same breath because it is the third
    state and the one that cost money: a broker whose startup ABORTED must
    destroy nothing at all, even with KEEP_ON_EXIT off — that is the bug where a
    second broker adopted the live instance, failed to bind the port, and tore
    down the first broker's GPU on the way out.
    """
    from . import config

    def shutdown(keep: bool, started: bool = True) -> list[str]:
        torn: list[str] = []
        b = app.Broker.__new__(app.Broker)
        b.running = True
        b.started = started
        b.thread = None
        b.fleet = Fleet.__new__(Fleet)
        b.fleet.instance_id = 4242
        b.fleet.ep = None            # no endpoint: nothing to ask, nothing to protect
        b.fleet.teardown = lambda why: torn.append(why)
        b.db = type("D", (), {"close": lambda self: None})()
        b.execsvc = type("E", (), {"stop": lambda self: None})()
        was = config.KEEP_ON_EXIT
        config.KEEP_ON_EXIT = keep
        try:
            b.stop()
        finally:
            config.KEEP_ON_EXIT = was
        return torn

    check("KEEP_ON_EXIT ON: shutdown leaves the instance alone",
          shutdown(keep=True) == [], str(shutdown(keep=True)))
    # THE control. A check that has never seen the other answer has not been
    # shown to work: with the flag off the same shutdown MUST tear down, or
    # "KEEP_ON_EXIT worked" would be indistinguishable from "stop() does nothing".
    check("CONTROL: KEEP_ON_EXIT OFF: the same shutdown DOES destroy",
          shutdown(keep=False) == ["broker shutdown"], str(shutdown(keep=False)))
    check("CONTROL: an ABORTED startup destroys nothing, flag or no flag",
          shutdown(keep=False, started=False) == [],
          str(shutdown(keep=False, started=False)))


def test_local_disk_preflight_says_where_the_batch_stops() -> None:
    """The disk that a 2,978-frame master actually fills is THIS one.

    The instance's disk is guarded and cannot fill — `collect` deletes each
    frame there once its fetch verifies — so the guard that existed was on the
    wrong machine. The number that matters is not "does it fit" but "which
    frame does it stop at", because that is what turns a silent write failure
    eighteen days in into a decision made before renting anything.
    """
    from . import config, seq

    with tempfile.TemporaryDirectory() as tmp:
        old = config.SEQ_DIR
        config.SEQ_DIR = Path(tmp)
        try:
            free = seq.local_space("shot", 10, 1.0)["free_bytes"]
            check("CONTROL: a batch that plainly fits reports that it fits",
                  seq.local_space("shot", 10, 1024)["fits"] is True)

            # Ten frames of a fifth of the disk each: twice what is there.
            # Checked against the reading in the SAME dict rather than against
            # `free` above — /tmp is a live filesystem and two readings a
            # millisecond apart are not obliged to agree.
            huge = seq.local_space("shot", 10, free / 5)
            check("a batch that cannot fit says so", huge["fits"] is False, str(huge))
            check("...and names the frame it stops at, not just 'too big'",
                  huge["frames_that_fit"] == int(huge["free_bytes"] // huge["mean_bytes"])
                  and 0 < huge["frames_that_fit"] < huge["frames"],
                  str(huge["frames_that_fit"]))

            check("CONTROL: with no measurement it says so, it does not guess",
                  seq.local_space("shot", 10, None)["known"] is False)
            check("CONTROL: an empty resume needs no disk and raises nothing",
                  seq.local_space("shot", 0, 1e6)["known"] is False)
        finally:
            config.SEQ_DIR = old


def test_a_bake_travels_only_with_the_blend_that_owns_it() -> None:
    """`blendcache_X` is X's bake. Every other sibling name is shared.

    The positive control is the one that was actually happening: a second .blend
    in the same directory picked up the first one's cache. The negative controls
    are what stop the fix from being "ship nothing" — the owning blend must still
    get its own bake, and the generically-named directories must still travel
    with every blend, because Blender resolves `//cache/...` with no reference to
    the .blend's filename and any of them could be the one that uses it.
    """
    from . import scenes

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for blend in ("beat3.blend", "beat1.blend"):
            (d / blend).write_bytes(b"x")
        for sub in ("blendcache_beat3", "blendcache_beat1", "cache", "textures"):
            (d / sub).mkdir()

        beat3 = {p.name for p in scenes.sibling_dirs_for(d / "beat3.blend")}
        beat1 = {p.name for p in scenes.sibling_dirs_for(d / "beat1.blend")}

        check("a blend carries its OWN bake", "blendcache_beat3" in beat3, str(beat3))
        check("REFUSED: another blend's bake travelling with it",
              "blendcache_beat1" not in beat3, str(beat3))
        check("...and symmetrically", "blendcache_beat1" in beat1
              and "blendcache_beat3" not in beat1, str(beat1))
        check("CONTROL: generically-named siblings still travel with both",
              {"cache", "textures"} <= beat3 and {"cache", "textures"} <= beat1,
              f"{beat3} / {beat1}")


def test_edited_in_place_frame_is_not_a_delivered_frame() -> None:
    """A frame edited without changing its LENGTH must still fail the cheap pass.

    This is the hole the deep pass was covering alone, and the resume never runs
    the deep pass — re-hashing a 2,978-frame 4K master is 101 GB of reads per
    planning pass. Measured against the live farm on 2026-08-02: one flipped byte
    in a 716,012-byte delivered frame passed size, dimensions and PNG structure
    and was caught only by `rq seq verify`'s sha256.

    Both controls are here and both matter. The negative one is the reason this
    check can be trusted at all: an UNTOUCHED frame beside the edited one must
    keep verifying, or the rule is just "re-render everything".
    """
    from . import seq

    with tempfile.TemporaryDirectory() as tmp:
        db = DB(Path(tmp) / "t.db")
        name, want = "shot", seq.spec_hash(spec(), "d")
        paths = {}
        for f in (1, 2):
            paths[f] = Path(tmp) / f"{name}_{f:06d}.png"
            _png(paths[f])
            db.frame_done(name, f, "job1", str(paths[f]), paths[f].stat().st_size,
                          4, 3, seq.sha256_of(paths[f]), 1.0, want)

        plan = seq.plan_range(db, name, [1, 2], want)
        check("CONTROL: two untouched frames both verify on the cheap pass",
              plan.have == [1, 2] and not plan.todo, f"have={plan.have}")

        # Same length, same dimensions, same PNG structure — one different byte,
        # and a timestamp that says the file moved after it was recorded.
        blob = bytearray(paths[1].read_bytes())
        # Inside the body, deliberately clear of the IHDR at the front and the
        # IEND the structural check reads from the last 12 bytes — the point is a
        # file that every OTHER check still passes.
        blob[40] ^= 0xFF
        was = len(blob)
        paths[1].write_bytes(bytes(blob))
        os.utime(paths[1], (time.time() + 60, time.time() + 60))
        check("the edit did NOT change the file's length",
              paths[1].stat().st_size == was, f"{paths[1].stat().st_size} vs {was}")

        plan = seq.plan_range(db, name, [1, 2], want)
        check("a frame edited in place is stale on the CHEAP pass, not just --deep",
              plan.todo == [1] and plan.stale == [1] and plan.have == [2],
              f"todo={plan.todo} stale={plan.stale} have={plan.have}")

        ok, why = seq.verify_frame(paths[1], was, (4, 3),
                                   delivered_at=db.frame(name, 1)["finished"])
        check("...and it says WHY, in terms someone can act on",
              not ok and "AFTER it was recorded" in why, why)

        # The other direction: a frame whose mtime is EARLIER than its row — the
        # normal case, since the file lands before the row is written — must not
        # be condemned by this check.
        os.utime(paths[2], (100.0, 100.0))
        plan = seq.plan_range(db, name, [1, 2], want)
        check("CONTROL: an OLDER file than its row is normal, not stale",
              2 in plan.have, f"have={plan.have}")
        db.close()


def test_remote_call_signatures() -> None:
    """Every `remote.*` call in fleet.py must match the real signature.

    Python does not check this until the line runs, and the line that mattered
    ran only after a GPU had been rented, 481 MB of Blender pushed and the
    instance provisioned:

        deploy attempt 1/3 failed: TypeError: scene_cached() missing 1 required
        positional argument: 'name'

    `scene_cached` had gained a parameter and one of its two call sites was not
    updated. Seven minutes and a few cents to learn something that is sitting in
    the AST for free.
    """
    import ast
    import inspect
    from . import remote as remote_mod

    source = (Path(__file__).parent / "fleet.py").read_text()
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id == "remote"):
            continue
        target = getattr(remote_mod, func.attr, None)
        if target is None:
            bad.append(f"fleet.py:{node.lineno} remote.{func.attr} does not exist")
            continue
        if isinstance(target, type):            # exception classes
            continue
        try:
            sig = inspect.signature(target)
        except (ValueError, TypeError):
            continue
        supplied = len(node.args) + len({k.arg for k in node.keywords if k.arg})
        required = sum(1 for p in sig.parameters.values()
                       if p.default is inspect.Parameter.empty
                       and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
        if supplied < required:
            bad.append(f"fleet.py:{node.lineno} remote.{func.attr} got {supplied}, "
                       f"needs {required} {sig}")
    check("every remote.* call in fleet.py matches its signature", not bad,
          "; ".join(bad[:3]))


class StubDisk:
    """A pretend instance disk, so eviction can be tested without renting one.

    Answers the two commands the preflight issues — the df+du measurement and
    `rm -rf` — from a model of the filesystem that actually shrinks when
    something is deleted. That is the point: a stub that always reports the same
    numbers would let a broken eviction pass, which is the exact defect class
    (a check that measures nothing) this preflight exists to avoid.
    """

    def __init__(self, total: int, other: int, scenes: dict[str, tuple[int, float]],
                 measurable: bool = True, deletes_work: bool = True) -> None:
        self.total = total
        self.other = other                      # image + Blender + output
        self.scenes = dict(scenes)              # digest -> (bytes, mtime)
        self.measurable = measurable
        self.deletes_work = deletes_work
        self.removed: list[str] = []
        self.measurements = 0

    @property
    def used(self) -> int:
        return self.other + sum(b for b, _ in self.scenes.values())

    def probe(self, ep, command: str, timeout: float = 600):
        if "df -kP" in command:
            self.measurements += 1
            if not self.measurable:
                return remote.Ran(command, 255, "", "ssh: connect timed out", 20.0, "stub")
            lines = [f"DF {self.total // 1024} {self.used // 1024} "
                     f"{(self.total - self.used) // 1024}"]
            lines += [f"S {int(mtime)} {size} {digest}"
                      for digest, (size, mtime) in self.scenes.items()]
            lines.append("END")
            return remote.Ran(command, 0, "\n".join(lines) + "\n", "", 0.4, "stub")
        if command.startswith("rm -rf "):
            # Removals arrive BATCHED — one SSH command for up to 50 scenes,
            # because one connection per directory measured 67.6 s for 26 of
            # them against a live instance. The stub has to model that or it
            # would only ever see the last one.
            for part in command.split(";"):
                part = part.strip()
                if not part.startswith("rm -rf "):
                    continue
                digest = part.rsplit("/", 1)[-1].strip("'\" ")
                self.removed.append(digest)
                if self.deletes_work:
                    self.scenes.pop(digest, None)
            return remote.Ran(command, 0, "", "", 0.1, "stub")
        return remote.Ran(command, 0, "", "", 0.1, "stub")


def test_scene_cache_is_evicted_by_use_and_verified() -> None:
    """The cache must be bounded by BYTES, evicted least-recently-USED, and
    proven to have shrunk.

    Measured live on instance 46133943 after nine hours of a 435-item campaign:
    41 cached scenes, 8.8 GB, and not one eviction — the budget was 12 GB, so
    the code that existed had never once run. At 270 MB an item the campaign
    reaches 117 GB, and the 16 GB disk this farm is moving to cannot hold even
    the old budget beside Blender.
    """
    ep = remote.Endpoint(host="stub", port=1, instance_id=1)
    GB = 10 ** 9
    real_probe, real_run = remote.probe, remote.run

    def install(disk: StubDisk):
        remote.probe = disk.probe
        remote.run = lambda ep, cmd, timeout=600, check=True: disk.probe(ep, cmd).out

    try:
        # Four scenes, 2 GB each, on a 16 GB disk with 2 GB of image+Blender.
        # `old_but_used` was uploaded first and touched most recently; a
        # creation-time LRU throws away exactly the wrong one.
        disk = StubDisk(total=16 * GB, other=2 * GB, scenes={
            "old_but_used": (2 * GB, 1000),     # uploaded first...
            "stale_a": (2 * GB, 2000),
            "stale_b": (2 * GB, 3000),
            "loaded": (2 * GB, 4000),
        })
        disk.scenes["old_but_used"] = (2 * GB, 9000)    # ...used most recently
        install(disk)

        report = remote.evict_to_fit(
            ep, {"loaded", "incoming"}, incoming=3 * GB,
            budget=8 * GB, reserve=2 * GB)
        check("evicts least-recently-USED first, not oldest-uploaded",
              disk.removed and disk.removed[0] == "stale_a", str(disk.removed))
        check("never evicts the loaded scene", "loaded" not in disk.removed,
              str(disk.removed))
        check("stops as soon as the incoming scene fits",
              disk.removed == ["stale_a", "stale_b"], str(disk.removed))
        check("the scene used all day survives the ones merely uploaded later",
              "old_but_used" not in disk.removed, str(disk.removed))
        check("re-measures after evicting rather than assuming",
              disk.measurements == 2, f"{disk.measurements} measurement(s)")
        check("the report carries before/after numbers",
              report.before.cache_bytes == 8 * GB and report.after.cache_bytes == 4 * GB,
              report.describe())

        # A disk that cannot be measured must REFUSE, never pass. Two gates in
        # this project reported a green verdict on an empty measurement
        # (f1-round2 R2-018); a preflight is the worst possible place to repeat
        # that, because the thing it protects is written before anyone looks.
        blind = StubDisk(total=16 * GB, other=2 * GB, scenes={}, measurable=False)
        install(blind)
        try:
            remote.evict_to_fit(ep, set(), incoming=GB, budget=8 * GB, reserve=2 * GB)
            check("an unmeasurable disk fails the preflight", False, "it passed")
        except remote.DiskFull as exc:
            check("an unmeasurable disk fails the preflight",
                  "could not be measured" in str(exc), str(exc)[:120])

        # Removals that silently do nothing must not be reported as success.
        # `rm -rf` runs with check=False, so "we sent the command" is not
        # evidence; only the second measurement is.
        lying = StubDisk(total=16 * GB, other=2 * GB, scenes={
            "a": (6 * GB, 1000), "b": (6 * GB, 2000)}, deletes_work=False)
        install(lying)
        try:
            remote.evict_to_fit(ep, set(), incoming=3 * GB, budget=8 * GB, reserve=2 * GB)
            check("an eviction that freed nothing is caught", False, "it passed")
        except remote.DiskFull as exc:
            check("an eviction that freed nothing is caught",
                  "NOT ENOUGH DISK" in str(exc), str(exc)[:120])

        # Genuinely too big: every unpinned scene gone and it still will not
        # fit. Loud, with the numbers, and no silent success.
        tight = StubDisk(total=16 * GB, other=2 * GB, scenes={"loaded": (9 * GB, 1000)})
        install(tight)
        try:
            remote.evict_to_fit(ep, {"loaded"}, incoming=6 * GB,
                                budget=8 * GB, reserve=2 * GB)
            check("a scene that cannot fit fails loudly", False, "it passed")
        except remote.DiskFull as exc:
            msg = str(exc)
            check("a scene that cannot fit fails loudly",
                  "NOT ENOUGH DISK" in msg and "6.00G" in msg and "loaded" in msg,
                  msg[:160])

        # The configured ceiling is not the only bound: on a small disk the
        # budget is whatever is left after everything that is not the cache.
        small = remote.DiskState(ok=True, total=16 * GB, used=3 * GB, free=13 * GB,
                                 scenes=(remote.SceneEntry("x", 1 * GB, 1000),))
        check("the budget is clamped by the disk, not just by config",
              remote.effective_budget(small, 12 * GB, 2 * GB) == 12 * GB
              and remote.effective_budget(small, 20 * GB, 2 * GB) == 12 * GB,
              str(remote.effective_budget(small, 20 * GB, 2 * GB)))
    finally:
        remote.probe, remote.run = real_probe, real_run


def test_disk_state_refuses_to_invent_numbers() -> None:
    """An unreadable disk reports UNKNOWN, never zero.

    Zero used bytes reads to every caller — and to a human scanning `rq status`
    — as an empty disk with room to spare. The one thing a disk check must
    never do is answer 'fine' when it did not look.
    """
    ep = remote.Endpoint(host="stub", port=1, instance_id=1)
    real_probe = remote.probe

    def canned(out: str, rc: int = 0):
        return lambda ep, cmd, timeout=600: remote.Ran(cmd, rc, out, "", 0.1, "stub")

    try:
        remote.probe = canned("DF 1000 400 600\nS 1700 1024 abc\nEND\n")
        state = remote.disk_state(ep)
        check("a good measurement parses df in bytes",
              state.ok and state.total == 1000 * 1024 and state.free == 600 * 1024,
              state.describe())
        check("and counts the cache from du",
              state.cache_bytes == 1024 and state.scene_count == 1
              and state.other_bytes == 400 * 1024 - 1024, state.describe())

        # Truncated output — the marker never arrived. This is the shape an
        # interrupted read takes, and it is indistinguishable from "no scenes
        # cached" without the marker.
        remote.probe = canned("DF 1000 400 600\nS 1700 1024 abc\n")
        check("a truncated read is UNKNOWN, not an empty cache",
              not remote.disk_state(ep).ok, remote.disk_state(ep).describe())

        # df said nothing usable.
        remote.probe = canned("END\n")
        check("df with no numbers is UNKNOWN", not remote.disk_state(ep).ok)

        # `du` failed for one scene, leaving a hole. Skipping it would
        # under-count the cache, which is the direction that fills a disk.
        remote.probe = canned("DF 1000 400 600\nS 1700  abc\nEND\n")
        check("an unmeasurable scene fails the whole measurement",
              not remote.disk_state(ep).ok, remote.disk_state(ep).describe())

        remote.probe = canned("", rc=255)
        check("an ssh failure is UNKNOWN with the reason attached",
              not remote.disk_state(ep).ok and "exit 255" in remote.disk_state(ep).detail)

        # ...and none of that may reach `rq status` as a number. The operator
        # line is the whole point of measuring: it has to be able to say "I do
        # not know", because a missing line reads as "fine".
        fleet = Fleet.__new__(Fleet)
        fleet.ep = None
        fleet.disk = None
        never = fleet.disk_report()
        check("an unsampled disk reports measured:false, not zeroes",
              never["measured"] is False and "used_gb" not in never, str(never))

        fleet.ep = ep
        fleet.disk = remote.DiskState(ok=False, detail="ssh went away",
                                      measured_at=time.time())
        failed = fleet.disk_report()
        check("a failed measurement carries its reason to the operator",
              failed["measured"] is False and failed["detail"] == "ssh went away",
              str(failed))

        fleet.disk = remote.DiskState(ok=True, total=16 * 10 ** 9, used=6 * 10 ** 9,
                                      free=10 * 10 ** 9, measured_at=time.time(),
                                      scenes=(remote.SceneEntry("a", 4 * 10 ** 9, 1),))
        good = fleet.disk_report()
        check("a good measurement reports totals, cache and budget",
              good["measured"] and good["cache_gb"] == 4.0 and good["scene_count"] == 1
              and good["other_gb"] == 2.0 and good["budget_gb"] == 8.0, str(good))
    finally:
        remote.probe = real_probe


def test_blender_bundle_is_dropped_only_once_the_install_works() -> None:
    """460 MB of archive that nothing reads — but deleted only on proof.

    A truncated extract leaves an executable at exactly the path the resume
    check tests for, so `test -x` is not proof of a working install. The version
    banner is. Getting this backwards would throw away the only copy of Blender
    on a box whose install is broken, and the next deploy would re-push half a
    gigabyte to fix a problem this created.
    """
    ep = remote.Endpoint(host="stub", port=1, instance_id=1)
    real_probe = remote.probe
    sent: list[str] = []

    def fake(out: str):
        def probe(ep, cmd, timeout=600):
            sent.append(cmd)
            if cmd.startswith("rm -f"):
                return remote.Ran(cmd, 0, "GONE", "", 0.1, "stub")
            return remote.Ran(cmd, 0, out, "", 0.2, "stub")
        return probe

    try:
        sent.clear()
        remote.probe = fake("482344960\nBlender 5.2.0\n")
        freed = remote.drop_blender_bundle(ep)
        check("a verified install frees the bundle",
              freed == 482344960 and any(c.startswith("rm -f") for c in sent),
              f"freed={freed} sent={len(sent)}")

        sent.clear()
        remote.probe = fake("482344960\n")          # no version banner
        check("an unproven install keeps the bundle",
              remote.drop_blender_bundle(ep) == 0
              and not any(c.startswith("rm -f") for c in sent), str(sent))

        sent.clear()
        remote.probe = fake("NOBUNDLE\n")
        check("no bundle is not an error",
              remote.drop_blender_bundle(ep) == 0
              and not any(c.startswith("rm -f") for c in sent), str(sent))
    finally:
        remote.probe = real_probe


def test_disk_full_fails_the_job_and_never_the_gpu() -> None:
    """A full disk is neither transport nor broken hardware.

    Both of the broker's reflexes are wrong here. Retrying the upload measures
    the same disk three more times; replacing the instance rents an identically
    sized volume, re-pushes Blender and re-pushes the scene to reach the same
    verdict. The job fails, terminally, with the numbers — and the GPU is left
    exactly where it was.
    """
    full = remote.DiskFull("NOT ENOUGH DISK on stub for 6.00G of scene ...")

    fleet = Fleet.__new__(Fleet)
    fleet.instance_id = 5
    fleet.ep = remote.Endpoint(host="stub", port=1, instance_id=5)
    fleet.stopped_at = None
    fleet.deploy_failures = app.config.MAX_TRANSPORT_ROUNDS - 1
    fleet.status = "ready"
    fleet.may_hold_render = True
    fleet.machine_id = 0
    fleet.bad_machines = set()
    fleet.activity = lambda attempts=3: idle_worker()
    fleet.torn = []
    fleet._teardown_locked = lambda reason="": fleet.torn.append(reason)

    def deploy(scene):
        raise full
    fleet._deploy = deploy

    raised = None
    try:
        fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
    except remote.DiskFull as exc:
        raised = exc
    check("DiskFull escapes the deploy retry loop on the first attempt",
          raised is full, repr(raised))
    check("a full disk never destroys the instance", not fleet.torn, str(fleet.torn))
    check("and does not burn the transport budget",
          fleet.deploy_failures == app.config.MAX_TRANSPORT_ROUNDS - 1,
          str(fleet.deploy_failures))

    # ...and at the queue it is terminal, not a retry. Retrying cannot create
    # space, and each retry buries the one message that names the sizes.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        b = stub_broker(tmp, StubFleet([idle_worker()]))
        b.pass_delivered = 0
        b.current_key = None
        b.fleet.pin_scene = lambda d: None
        b.fleet.unpin_scene = lambda d: None
        job_id = b.db.submit(spec(), agent="agent")
        row = b.db.claim(60.0)
        b.run_sequence = b.run_still = lambda job: (_ for _ in ()).throw(full)
        b.run_job(row or {"id": job_id})
        got = b.db.get(job_id) or {}
        check("a DiskFull job is failed terminally, not requeued",
              got.get("state") == "failed", str(got.get("state")))
        check("and its error names the disk",
              "NOT ENOUGH DISK" in (got.get("err") or ""), str(got.get("err"))[:120])


def test_seq_names_and_ranges() -> None:
    """The sequence name becomes a directory, so it is validated like one."""
    from . import seq

    for bad in ("../etc", "a/b", "", "x" * 65, "a b", "sh.ot"):
        try:
            seq.valid_name(bad)
            check(f"rejects sequence name {bad!r}", False, "accepted it")
        except seq.SeqError:
            check(f"rejects sequence name {bad!r}", True)

    check("parses ranges", seq.parse_range("1-240") == (1, 240, 1))
    check("parses stepped ranges", seq.parse_range("1-240x3") == (1, 240, 3))
    check("parses a single frame", seq.parse_range("57") == (57, 57, 1))
    for bad in ("240-1", "abc", "1-10x0", ""):
        try:
            seq.parse_range(bad)
            check(f"rejects range {bad!r}", False, "accepted it")
        except seq.SeqError:
            check(f"rejects range {bad!r}", True)
    check("summarises frame lists for humans",
          seq.summarise([1, 2, 3, 7, 20, 21]) == "1-3, 7, 20-21",
          seq.summarise([1, 2, 3, 7, 20, 21]))


def test_frame_lists_round_trip() -> None:
    """Comma forms, and the guarantee that one can never widen into a range.

    `1-40,57` read as `1-57` is 16 frames of GPU nobody asked for, rendered from
    a matching spec into a matching sequence — so nothing downstream would ever
    flag them. The whole point of the explicit list is that the widening is
    unrepresentable, so both directions are checked here: the list survives a
    round trip through the database, and a row that does NOT carry one still
    means exactly the arithmetic run it always did.
    """
    from . import seq

    check("parses a comma list", seq.parse_frames("1-3,7,20-21") == [1, 2, 3, 7, 20, 21],
          str(seq.parse_frames("1-3,7,20-21")))
    check("parses a stepped part inside a list",
          seq.parse_frames("1-9x4,100") == [1, 5, 9, 100],
          str(seq.parse_frames("1-9x4,100")))
    check("de-duplicates overlapping parts",
          seq.parse_frames("1-5,3-7") == [1, 2, 3, 4, 5, 6, 7],
          str(seq.parse_frames("1-5,3-7")))
    check("a plain range still parses", seq.parse_frames("620-980")[:2] == [620, 621])
    # summarise -> parse_frames is the copy-paste loop `rq seq status` promises.
    holes = [701, 744, 745, 1600]
    check("what `rq seq status` PRINTS is what `rq anim --frames` ACCEPTS",
          seq.parse_frames(seq.summarise(holes)) == holes,
          f"{seq.summarise(holes)!r} -> {seq.parse_frames(seq.summarise(holes))}")

    for bad in ("1-3,,7", "1-3,", ",7", "1-3,abc", "1-3,10-2", ""):
        try:
            seq.parse_frames(bad)
            check(f"rejects frame list {bad!r}", False, "accepted it")
        except seq.SeqError:
            check(f"rejects frame list {bad!r}", True)

    check("bounds of a run keep its step", seq.bounds([1, 5, 9]) == (1, 9, 4),
          str(seq.bounds([1, 5, 9])))
    check("bounds of a non-run do not invent a step",
          seq.bounds([1, 2, 3, 7]) == (1, 7, 1), str(seq.bounds([1, 2, 3, 7])))
    check("a run is recognised as one", seq.is_run([1, 5, 9], 1, 9, 4))
    check("a non-run is NOT recognised as one", not seq.is_run([1, 2, 3, 7], 1, 7, 1))

    with tempfile.TemporaryDirectory() as tmp:
        db = DB(Path(tmp) / "b.db")
        frames = seq.parse_frames("10-12,40")
        first, last, step = seq.bounds(frames)
        listed = db.submit_range(spec(), seq="holes", first=first, last=last,
                                 step=step, frames_total=len(frames),
                                 spec_hash="h", frame_list=frames)
        row = db.get(listed)
        check("a non-contiguous job stores its frame list",
              seq.frames_of(row) == [10, 11, 12, 40], str(seq.frames_of(row)))
        # THE control that matters: without the list, those same three columns
        # describe 31 frames. If frames_of ever ignored frame_list this passes 10-40.
        check("...and the columns alone would have widened it to 31 frames",
              len(seq.expand(row["frame_first"], row["frame_last"],
                             row["frame_step"])) == 31)

        plain = db.submit_range(spec(), seq="run", first=1, last=9, step=4,
                                frames_total=3, spec_hash="h", frame_list=None)
        prow = db.get(plain)
        check("a contiguous job stores NO list and still expands correctly",
              prow["frame_list"] is None and seq.frames_of(prow) == [1, 5, 9],
              f"{prow['frame_list']!r} {seq.frames_of(prow)}")

        # A corrupt list must never silently fall back to the columns, for the
        # same reason: the fallback renders frames that were never requested.
        db.conn.execute("UPDATE jobs SET frame_list='{not json' WHERE id=?", (listed,))
        db.conn.commit()
        try:
            seq.frames_of(db.get(listed))
            check("an unreadable frame_list refuses rather than widening", False,
                  "it fell back to the columns")
        except seq.SeqError as exc:
            check("an unreadable frame_list refuses rather than widening",
                  "never requested" in str(exc), str(exc)[:80])
        db.close()


# --- queue semantics ------------------------------------------------------


def test_db() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = DB(Path(tmp) / "t.db")

        # No dedup: identical specs must produce distinct jobs. Collapsing them
        # would serve a stale frame after the scene is reassembled.
        a = db.submit(spec(), agent="x")
        b = db.submit(spec(), agent="x")
        check("identical specs create distinct jobs", a != b, f"{a} vs {b}")

        # Fair share: an agent with recent service sorts behind an idle one, so
        # one chatty agent cannot monopolise a single GPU.
        db2 = DB(Path(tmp) / "fair.db")
        for _ in range(3):
            db2.submit(spec(), agent="greedy")
        db2.submit(spec(), agent="quiet")
        first = db2.claim(60)
        db2.finish(first["id"], "/dev/null", 1.0)
        second = db2.claim(60)
        check("fair-share favours the idle agent", second["agent"] == "quiet",
              f"served {first['agent']} then {second['agent']}")

        # A claimed job is not handed out twice.
        db3 = DB(Path(tmp) / "claim.db")
        db3.submit(spec(), agent="x")
        got = [db3.claim(60), db3.claim(60)]
        check("claim is exclusive", got[0] is not None and got[1] is None)

        # Crash recovery: a lapsed lease returns the job to the queue. A lock
        # would have stranded it instead.
        db4 = DB(Path(tmp) / "lease.db")
        jid = db4.submit(spec(), agent="x")
        db4.claim(-1)  # already expired
        reclaimed = db4.requeue_expired()
        check("expired lease is requeued", reclaimed == 1 and db4.get(jid)["state"] == "queued",
              f"requeued={reclaimed}")

        # Retries must terminate rather than loop forever on a poison job.
        db5 = DB(Path(tmp) / "retry.db")
        jid = db5.submit(spec(), agent="x")
        states = []
        for _ in range(3):
            db5.claim(60)
            states.append(db5.fail(jid, "boom", max_attempts=2))
        check("retries stop at the cap", states == ["queued", "failed", "failed"], str(states))

        # Failed rows are kept, so failures show up in status instead of vanishing.
        check("failed jobs remain visible", db5.counts().get("failed") == 1, str(db5.counts()))

        # Cancel is terminal and does not resurrect.
        db6 = DB(Path(tmp) / "cancel.db")
        jid = db6.submit(spec(), agent="x")
        check("cancel works once", db6.cancel(jid) and not db6.cancel(jid))


# --- busy-worker dispatch -------------------------------------------------
#
# The most expensive bug this project has had is the broker recording a verdict
# about a render it never observed. These run against a stub fleet — no GPU, no
# network, no money — because the live path only exercises them when something
# is already going wrong, and the previous version of this logic shipped
# unverified and failed the first 8K frame it met.


class StubFleet:
    """A fleet whose instance does exactly what the test says it does."""

    spend = 0.0
    disk_spend = 0.0

    def __init__(self, script: list) -> None:
        self.script = list(script)       # Activity per ensure_ready/activity call
        self.ep = remote.Endpoint(host="stub", port=1, instance_id=1)
        self.instance_id = 1
        self.stopped_at = None
        self.local_port = 0
        self.ensure_calls = 0
        self.hibernated = False
        self.torn_down = False
        self.png: dict[str, int] = {}
        self.awaited: list[str] = []
        # What the dispatcher thinks reloading the loaded scene would cost.
        # Zero unless a test says otherwise, so `starve_threshold` collapses to
        # the plain SCENE_STARVE_SEC floor and every pre-existing scheduling
        # test keeps measuring what it was written to measure.
        self.scene_path: Optional[Path] = None
        self.reload_cost = 0.0
        self.scene_demand = lambda: set()

    @property
    def hibernated_for(self) -> float:
        return time.time() - self.stopped_at if self.stopped_at else 0.0

    def _now(self) -> "remote.Activity":
        return self.script[0] if len(self.script) == 1 else self.script.pop(0)

    def activity(self, attempts: int = 3) -> "remote.Activity":
        return self._now()

    def ensure_ready(self, scene):
        self.ensure_calls += 1
        act = self._now()
        if act.rendering:
            raise remote.WorkerBusy(f"busy: {act.describe()}", job_id=act.job_id,
                                    progress=act.progress)
        return self.ep

    def collect_finished(self, job_id: str, render_sec: float = 0.0):
        size = self.png.get(job_id, 0)
        if size <= 0:
            return None
        return {"ok": True, "job_id": job_id, "path": f"/workspace/out/{job_id}.png",
                "bytes": size, "render_sec": render_sec, "recovered": True}

    def await_render(self, job_id, deadline_sec, on_poll=None, **kw):
        self.awaited.append(job_id)
        if on_poll:
            on_poll(None)
        return self.collect_finished(job_id, 123.0)

    def reload_cost_sec(self, scene: "Optional[Path]" = None) -> float:
        return self.reload_cost

    def hibernate(self, force: bool = False) -> None:
        self.hibernated = True

    def teardown(self, reason: str = "idle") -> None:
        self.torn_down = True


def rendering(job_id: str) -> "remote.Activity":
    return remote.Activity(reachable=True, age=1.0, progress={
        "state": "rendering", "job_id": job_id, "sample": 100, "total": 8192,
        "tile": 1, "tiles": 12, "elapsed_sec": 600.0})


def idle_worker() -> "remote.Activity":
    return remote.Activity(reachable=True, age=1.0,
                           progress={"state": "idle", "phase": "waiting for work"})


def unreachable() -> "remote.Activity":
    return remote.Activity(reachable=False, detail="ssh: connect refused")


def stub_broker(tmp: Path, fleet: StubFleet):
    b = app.Broker.__new__(app.Broker)
    b.db = DB(tmp / "busy.db")
    b.fleet = fleet
    b.current_job = None
    b._stall_warned = {}
    b.idle_unknown_since = None
    b.last_work = time.time() - 100_000     # long past the idle grace
    b.running = True
    b.paused = None
    b.scene_batch = 0
    # The exec dispatcher, which the idle timer consults before stopping an
    # instance. A stub broker built with __new__ never ran __init__, so this has
    # to be attached explicitly — and it is attached for real rather than
    # mocked, because `busy()` reading the SAME database is the property under
    # test: an exec queue must hold the instance open exactly as a render queue
    # does.
    b.execsvc = execservice.ExecService(b)
    return b


def test_busy_dispatch() -> None:
    slow, app.config.PROGRESS_INTERVAL = app.config.PROGRESS_INTERVAL, 0.01
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # 1. Busy with MY job -> reattach and collect it. This is the case
            #    that was written `failed` while the GPU rendered it.
            fleet = StubFleet([rendering("mine")])
            fleet.png["mine"] = 91_000_000
            b = stub_broker(tmp, fleet)
            reply = b.acquire_worker("mine", Path("/tmp/s.blend"))
            check("busy with MY job -> reattaches, returns the frame",
                  reply is not None and reply.get("bytes") == 91_000_000
                  and fleet.awaited == ["mine"], str(reply)[:70])

            # 2. Busy with SOMEONE ELSE's job -> queue behind it, never kill it,
            #    and above all never fail my job for their frame.
            fleet = StubFleet([rendering("theirs"), rendering("theirs"), idle_worker()])
            b = stub_broker(tmp, fleet)
            reply = b.acquire_worker("mine", Path("/tmp/s.blend"))
            check("busy with ANOTHER job -> waits, then proceeds",
                  reply is None and fleet.ensure_calls == 3 and fleet.awaited == [],
                  f"ensure_ready x{fleet.ensure_calls}, awaited={fleet.awaited}")

            # 3. Not rendering -> ordinary path, no waiting at all.
            fleet = StubFleet([idle_worker()])
            b = stub_broker(tmp, fleet)
            check("idle worker -> deploys normally",
                  b.acquire_worker("mine", Path("/tmp/s.blend")) is None
                  and fleet.ensure_calls == 1)

            # 4. The guarantee: never `failed` while the instance renders it.
            fleet = StubFleet([rendering("mine")])
            b = stub_broker(tmp, fleet)
            jid = b.db.submit(spec(), agent="x")
            b.db.claim(60)
            blocked = b.must_not_fail(jid)
            fleet.script = [rendering(jid)]
            check("must_not_fail blocks while rendering that job",
                  bool(b.must_not_fail(jid)), b.must_not_fail(jid)[:60])

            # ... and the requeue that replaces the failure costs no attempt.
            before = b.db.get(jid)["attempts"]
            b.db.requeue(jid, "transport blew up")
            row = b.db.get(jid)
            check("requeue instead of fail costs no attempt",
                  row["state"] == "queued" and row["attempts"] == before - 1,
                  f"{row['state']} attempts {before}->{row['attempts']}")

            # 5. An unreachable instance is not a rendering one either — it must
            #    not block a failure forever, only a *demonstrated* render may.
            fleet = StubFleet([unreachable()])
            b2 = stub_broker(tmp, fleet)
            check("must_not_fail does not block on an unanswered probe",
                  b2.must_not_fail("whatever") == "")

            # 6. Idle timer: an idle QUEUE is not an idle GPU.
            fleet = StubFleet([rendering("someones-frame")])
            b3 = stub_broker(tmp, fleet)
            b3.maybe_idle_down()
            check("idle timer will not stop a rendering GPU", not fleet.hibernated)

            fleet = StubFleet([unreachable()])
            b4 = stub_broker(tmp, fleet)
            b4.maybe_idle_down()
            check("idle timer will not stop on an unanswered probe",
                  not fleet.hibernated and b4.idle_unknown_since is not None)

            # ... but it is bounded, so an unreachable box cannot bill forever.
            b4.idle_unknown_since = time.time() - app.config.IDLE_UNKNOWN_MAX_SEC - 1
            fleet.script = [unreachable()]
            b4.maybe_idle_down()
            check("idle timer stops eventually when it can never ask",
                  fleet.hibernated)

            fleet = StubFleet([idle_worker()])
            b5 = stub_broker(tmp, fleet)
            b5.maybe_idle_down()
            check("idle timer still stops a genuinely idle GPU", fleet.hibernated)
    finally:
        app.config.PROGRESS_INTERVAL = slow


# --- retries, recovery, and the money paths --------------------------------
#
# Two independent audits found the bugs these pin: an attempts count that was
# off by one (so the collect-a-finished-frame recovery never opened and whole
# frames re-rendered), fetches that were trusted on size alone, and four
# distinct ways an instance could be destroyed blind or leaked billing. All
# offline: stub fleets, temp databases, monkeypatched remotes.


def test_claim_reports_true_attempts() -> None:
    """`claim()` selects the row BEFORE adding 1 to attempts, so returning the
    raw row understates reality by one — and every `attempts > 1` retry gate in
    the dispatcher opened one attempt late, or never."""
    with tempfile.TemporaryDirectory() as tmp:
        db = DB(Path(tmp) / "att.db")
        jid = db.submit(spec(), agent="x")

        job = db.claim(60)
        check("first claim reports attempts == 1, matching the database",
              job["attempts"] == 1 == db.get(jid)["attempts"],
              f"claim said {job['attempts']}, db says {db.get(jid)['attempts']}")

        # A refunding requeue (must_not_fail, or a pass that delivered frames)
        # gives the attempt back; the next claim takes it again. The count must
        # keep matching the database on the way back through.
        db.requeue(jid, "the instance is rendering it")
        job = db.claim(60)
        check("claim after a refunding requeue still reports the written count",
              job["attempts"] == db.get(jid)["attempts"],
              f"claim said {job['attempts']}, db says {db.get(jid)['attempts']}")


def test_retry_gate_survives_refunding_requeue() -> None:
    """The collect-finished recovery must open on every genuine retry.

    The gate used to be `attempts > 1`, which the off-by-one made mean
    "attempt 3", and which a refunding requeue kept at 1 forever — so a job
    whose local fetch kept failing re-rendered its ENTIRE frame on every pass,
    with the finished PNG sitting on the instance the whole time."""
    with tempfile.TemporaryDirectory() as tmp:
        db = DB(Path(tmp) / "gate.db")
        jid = db.submit(spec(), agent="x")

        job = db.claim(60)
        check("a first-ever dispatch is not a retry", not app.Broker.is_retry(job),
              f"attempts={job['attempts']} err={job['err']!r}")

        # The audited loop: must_not_fail requeues (refunding the attempt)
        # because the finished PNG is on the instance. The NEXT pass must look
        # for that PNG instead of re-rendering it.
        db.requeue(jid, "fetch failed [not failed: the finished PNG is on the instance]")
        job = db.claim(60)
        check("a refunded requeue still opens the collect-finished gate",
              app.Broker.is_retry(job),
              f"attempts={job['attempts']} err={job['err']!r}")

        # And the ordinary spent-attempt path opens it too.
        db2 = DB(Path(tmp) / "gate2.db")
        jid2 = db2.submit(spec(), agent="x")
        db2.claim(60)
        db2.fail(jid2, "boom", max_attempts=3)
        job2 = db2.claim(60)
        check("a failed-and-requeued job is a retry",
              app.Broker.is_retry(job2),
              f"attempts={job2['attempts']} err={job2['err']!r}")


def test_retry_collects_or_reattaches_never_refetches_midwrite() -> None:
    """A retry first looks for the finished PNG — unless the worker is at this
    moment rendering that very frame, in which case its PNG on disk is
    half-written and collecting it would fetch garbage and blame the network.
    The right move there is the reattach path, which waits for the finish."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Idle instance, finished PNG present: collect, do not render.
        fleet = StubFleet([idle_worker()])
        fleet.png["k1"] = 5_000_000
        b = stub_broker(tmp, fleet)
        reply = b.render_one("k1", spec(), Path("/tmp/s.blend"), "row1", retry=True)
        check("retry collects a finished frame without touching the worker",
              reply.get("recovered") and fleet.ensure_calls == 0 and fleet.awaited == [],
              str(reply)[:60])

        # Same retry, but the instance is STILL RENDERING that key: the PNG on
        # disk is unfinished, so the pre-collect must step aside and let the
        # busy path reattach.
        fleet = StubFleet([rendering("k2"), rendering("k2")])
        fleet.png["k2"] = 5_000_000
        b = stub_broker(tmp, fleet)
        reply = b.render_one("k2", spec(), Path("/tmp/s.blend"), "row2", retry=True)
        check("retry mid-render reattaches instead of collecting a half-written PNG",
              reply is not None and fleet.awaited == ["k2"],
              f"awaited={fleet.awaited}")


def test_collect_verifies_before_deleting_remote() -> None:
    """The single-frame fetch path must prove the file it fetched is the file
    that was rendered — sha256, dimensions, PNG structure — and delete the
    remote copy only after ALL of it passes. It used to trust a size match,
    mark the job done, and `rm -f` the only good copy."""
    from . import seq

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        good = _png(tmp / "src_good.png")
        bad = _png(tmp / "src_bad.png", body=b"\xff" * 40)   # same length, wrong bytes
        cut = _png(tmp / "src_cut.png", trailer=b"")
        want_sha = seq.sha256_of(good)

        deleted: list[str] = []
        source = {"path": good}

        def fake_fetch(ep, remote_path, local, attempts=4):
            data = Path(source["path"]).read_bytes()
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            return len(data)

        def fake_run(ep, cmd, timeout=60, check=True):
            deleted.append(cmd)
            return ""

        real_fetch, real_run = remote.fetch_file, remote.run
        remote.fetch_file, remote.run = fake_fetch, fake_run
        try:
            fleet = StubFleet([idle_worker()])
            b = stub_broker(tmp, fleet)
            reply = {"path": "/workspace/out/j.png",
                     "png": {"width": 4, "height": 3, "sha256": want_sha}}

            local = tmp / "out" / "j.png"
            size, stats = b.collect(dict(reply), local)
            check("verified fetch returns the size and deletes the remote copy",
                  size == good.stat().st_size and len(deleted) == 1
                  and "rm -f" in deleted[0] and local.exists(),
                  f"deleted={deleted}")
            # `_png` is a structural fixture carrying no decodable image data,
            # which is exactly the case that must NOT take the farm down: a
            # measurement that fails is not evidence that the frame is empty.
            check("an unmeasurable frame is reported, not rejected",
                  stats["verdict"] == "UNREADABLE" and local.exists(),
                  str(stats.get("detail", ""))[:60])

            # Corrupted-but-plausible: right length, wrong bytes. Only the
            # digest can tell — and the remote copy must survive the failure.
            deleted.clear()
            source["path"] = bad
            local2 = tmp / "out" / "j2.png"
            try:
                b.collect(dict(reply), local2)
                check("corrupt fetch raises", False, "collect accepted it")
            except RuntimeError as exc:
                check("corrupt fetch raises, names the digest, keeps the remote copy",
                      "sha256" in str(exc) and not deleted and not local2.exists(),
                      str(exc)[:70])

            # Truncated: structure check, same guarantees.
            deleted.clear()
            source["path"] = cut
            local3 = tmp / "out" / "j3.png"
            try:
                b.collect(dict(reply), local3)
                check("truncated fetch raises", False, "collect accepted it")
            except RuntimeError as exc:
                check("truncated fetch raises and keeps the remote copy",
                      "PNG" in str(exc) and not deleted and not local3.exists(),
                      str(exc)[:70])
        finally:
            remote.fetch_file, remote.run = real_fetch, real_run


def test_finished_png_info_requires_stable_size() -> None:
    """`finished_png` was one bare `stat`, so a PNG still being written could be
    'collected'. The probe now reads size, hashes, reads size again — any
    disagreement means in-flight, answered with None."""
    ep = remote.Endpoint(host="stub", port=1, instance_id=1)
    sha = "ab" * 32

    def probe_returning(out: str, rc: int = 0):
        return lambda *a, **k: remote.Ran(cmd="stub", rc=rc, out=out, err="",
                                          elapsed=0.1, where="stub")

    real = remote.probe
    try:
        remote.probe = probe_returning(f"100 100 {sha}")
        info = remote.finished_png_info(ep, "job1")
        check("stable finished PNG reports size and sha",
              info == {"bytes": 100, "sha256": sha, "path": f"{app.config.REMOTE_ROOT}/out/job1.png"},
              str(info))

        remote.probe = probe_returning(f"100 224 {sha}")
        check("a growing file is not a finished frame",
              remote.finished_png_info(ep, "job1") is None)

        remote.probe = probe_returning("0 0")
        check("an absent file is not a finished frame",
              remote.finished_png_info(ep, "job1") is None)

        remote.probe = probe_returning("", rc=255)
        check("an unreachable probe is not evidence either way",
              remote.finished_png_info(ep, "job1") is None)
    finally:
        remote.probe = real

    # And the fleet's rebuilt reply must carry the digest so the fetch of a
    # RECOVERED frame is verifiable — it used to be the one fetch that was not.
    fleet = Fleet.__new__(Fleet)
    fleet.ep = ep
    real_info = remote.finished_png_info
    try:
        remote.finished_png_info = lambda e, j: {"bytes": 7, "sha256": sha,
                                                 "path": "/workspace/out/x.png"}
        reply = fleet.collect_finished("x")
        check("a recovered reply carries the instance-side sha256",
              reply is not None and reply.get("png", {}).get("sha256") == sha,
              str(reply))
    finally:
        remote.finished_png_info = real_info


def test_transport_failure_never_destroys_blind() -> None:
    """Exhausting the transport-retry budget used to call the destroy
    unconditionally — the broker's own log shows an instance destroyed 90 s
    after 'This is a TRANSPORT failure, not a statement about the render'.
    Only a reachable, definitely-idle answer may license that destroy."""
    def make_fleet(activity, boom, may_hold_render=True):
        fleet = Fleet.__new__(Fleet)
        fleet.instance_id = 5
        fleet.ep = remote.Endpoint(host="stub", port=1, instance_id=5)
        fleet.stopped_at = None
        fleet.deploy_failures = app.config.MAX_TRANSPORT_ROUNDS - 1
        fleet.status = "ready"
        # Every instance in this test has been talked to at least once, so an
        # unanswered probe is a genuine unknown and must block the destroy. The
        # never-started case is a *different fact* about the same probe
        # result and has its own test below.
        fleet.may_hold_render = may_hold_render
        fleet.machine_id = 0
        fleet.bad_machines = set()
        fleet.activity = lambda attempts=3: activity
        fleet.torn = []
        fleet._teardown_locked = lambda reason="": fleet.torn.append(reason)

        def deploy(scene):
            raise boom
        fleet._deploy = deploy
        return fleet

    dropped = remote.TransferError("bundle push", "stub", "connection reset", 2.0)
    slow_sleep, time.sleep = time.sleep, lambda s: None
    try:
        # Transport budget exhausted + probe unanswered -> KEEP the GPU.
        fleet = make_fleet(unreachable(), dropped)
        ok = fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("transport exhaustion + unknown activity keeps the instance",
              ok is False and not fleet.torn and fleet.instance_id == 5,
              f"torn={fleet.torn}")

        # ... + a demonstrated render -> KEEP the GPU.
        fleet = make_fleet(rendering("someones-8k-frame"), dropped)
        fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("transport exhaustion + rendering keeps the instance",
              not fleet.torn, f"torn={fleet.torn}")

        # Host-level failure but the box cannot be asked -> still no destroy.
        fleet = make_fleet(unreachable(), RuntimeError("provision exploded"))
        fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("host-level failure + unknown activity keeps the instance",
              not fleet.torn, f"torn={fleet.torn}")

        # Reachable and demonstrably idle -> the destroy is licensed.
        fleet = make_fleet(idle_worker(), dropped)
        fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("a demonstrably idle instance may still be replaced",
              fleet.torn == ["deploy failed"], f"torn={fleet.torn}")

        # An instance we RENTED but never once ran a command on cannot be
        # rendering: the worker arrives over ssh and every frame is dispatched
        # over ssh. `unknown` there is a known-empty, and blocking the destroy
        # on it is what wedged instance 46118513 for sixteen billed minutes.
        fleet = make_fleet(unreachable(), dropped, may_hold_render=False)
        fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("an unreachable instance with no worker started IS replaced",
              fleet.torn == ["deploy failed"], f"torn={fleet.torn}")

        # ...but a demonstrated render still outranks it, even if the flag says
        # we have never spoken to the box. The reading that keeps the frame
        # always wins over the reading that frees the GPU.
        fleet = make_fleet(rendering("a-frame-somehow"), dropped, may_hold_render=False)
        fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("rendering outranks the flag and still blocks the destroy",
              not fleet.torn, f"torn={fleet.torn}")
    finally:
        time.sleep = slow_sleep


def test_ssh_contact_is_not_evidence_of_a_render() -> None:
    """Running `true` over ssh cannot start a render, so it must not protect
    the instance from replacement.

    The first version of the escape hatch keyed on "has any ssh command
    succeeded". That sounds equivalent to "might this be rendering" and is not.
    Instance 46124078 proved the gap: ssh worked well enough to provision, the
    481 MB Blender push then failed at 3.5% on every one of four retries, and
    the flag insisted a box that had never had Blender on it at all might be
    mid-frame — re-wedging the broker the flag existed to unwedge.

    A render exists only where a WORKER was started. That is the fact."""
    fleet = Fleet.__new__(Fleet)
    fleet.ep = remote.Endpoint(host="stub", port=1, instance_id=7)
    fleet.stopped_at = None
    real_activity = remote.activity
    try:
        # A reachable probe must NOT quietly mark the instance as render-bearing.
        remote.activity = lambda ep, attempts=3: idle_worker()
        fleet.may_hold_render = False
        fleet.activity()
        check("an answered probe does not imply a worker exists",
              fleet.may_hold_render is False, str(fleet.may_hold_render))
    finally:
        remote.activity = real_activity

    # ...and the deploy gate therefore still replaces such an instance when it
    # later goes silent, instead of holding a rented GPU forever.
    slow_sleep, time.sleep = time.sleep, lambda s: None
    try:
        f2 = Fleet.__new__(Fleet)
        f2.instance_id = 46124078
        f2.local_port = 8798
        f2.ep = remote.Endpoint(host="stub", port=1, instance_id=46124078)
        f2.stopped_at = None
        f2.deploy_failures = app.config.MAX_TRANSPORT_ROUNDS - 1
        f2.status = "ready"
        f2.may_hold_render = False      # ssh worked, but no worker ever started
        f2.machine_id = 53217
        f2.bad_machines = set()
        f2.activity = lambda attempts=3: unreachable()
        f2.torn = []
        f2._teardown_locked = lambda reason="": f2.torn.append(reason)
        f2._deploy = lambda scene: (_ for _ in ()).throw(
            remote.TransferError("blender bundle push", "stub",
                                 "broken pipe at 3.5%", 208.1))
        f2._try_deploy(Path("/tmp/s.blend"), "freshly rented instance")
        check("ssh contact alone does not protect a worker-less instance",
              f2.torn == ["deploy failed"], f"torn={f2.torn}")
    finally:
        time.sleep = slow_sleep


def test_local_tunnel_failure_never_condemns_the_host() -> None:
    """A failure on THIS machine must not destroy a GPU or blacklist a machine.

    Regression, caught live and costing two healthy instances plus a wrongly
    condemned machine (96679). The worker-not-ready path raised a bare
    `RuntimeError` whose text said *"this is a transport failure, not a worker
    failure"* while its type said host-level — and `is_transport()` matches on
    type. It was masked for as long as the activity probe returned `unknown`
    for every never-rendered instance, because `unknown` blocked the destroy.
    Fixing the probe removed the mask and the fleet immediately acted on the
    misclassification, on this:

        bind [127.0.0.1]:8798: Address already in use
        channel_setup_fwd_listener_tcpip: cannot listen to port: 8798

    `kill -9` is the only sanctioned restart, so an orphaned `ssh -L` holding
    the local port is the documented procedure's guaranteed side effect."""
    bind_fail = remote.WaitResult(
        False, 0.0, 1,
        "the SSH tunnel exited 255 after 0s, so these pings went nowhere — this "
        "is a transport failure, not a worker failure: bind [127.0.0.1]:8798: "
        "Address already in use | channel_setup_fwd_listener_tcpip: cannot "
        "listen to port: 8798 | Could not request local forwarding.",
        tunnel_died=True)
    remote_drop = remote.WaitResult(
        False, 66.0, 21,
        "the SSH tunnel exited 255 after 66s, so these pings went nowhere — this "
        "is a transport failure, not a worker failure: Connection to 1.2.3.4 "
        "closed by remote host.", tunnel_died=True)
    check("a local bind conflict is recognised as OUR fault",
          bind_fail.local_bind_failed, bind_fail.last_error[:60])
    check("a remote-side drop is NOT called a local fault",
          not remote_drop.local_bind_failed and remote_drop.tunnel_died, "")

    reaped = []
    real_reap = remote.reap_stale_tunnels
    slow_sleep, time.sleep = time.sleep, lambda s: None
    try:
        remote.reap_stale_tunnels = lambda port: reaped.append(port) or 1

        def make(exc):
            fleet = Fleet.__new__(Fleet)
            fleet.instance_id = 5
            fleet.local_port = 8798
            fleet.ep = remote.Endpoint(host="stub", port=1, instance_id=5)
            fleet.stopped_at = None
            fleet.deploy_failures = 0
            fleet.status = "ready"
            fleet.may_hold_render = True
            fleet.machine_id = 96679
            fleet.bad_machines = set()
            fleet.activity = lambda attempts=3: idle_worker()
            fleet.torn = []
            fleet._teardown_locked = lambda reason="": fleet.torn.append(reason)
            fleet._deploy = lambda scene: (_ for _ in ()).throw(exc)
            return fleet

        fleet = make(remote.WorkerUnreachable(
            "worker on stub " + bind_fail.describe(),
            tunnel_died=True, local=True))
        ok = fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("a local bind conflict does NOT destroy the instance",
              ok is False and not fleet.torn, f"torn={fleet.torn}")
        check("a local bind conflict does NOT blacklist the machine",
              fleet.bad_machines == set(), str(fleet.bad_machines))
        check("the stale local tunnel is reaped instead", reaped == [8798], str(reaped))

        # A tunnel the REMOTE dropped is transport: keep the GPU and retry it,
        # rather than treating a dead forward as broken hardware.
        fleet = make(remote.WorkerUnreachable(
            "worker on stub " + remote_drop.describe(), tunnel_died=True))
        fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("a remotely dropped tunnel is transport, so the GPU is kept",
              not fleet.torn and not fleet.bad_machines,
              f"torn={fleet.torn} bad={fleet.bad_machines}")

        # Healthy tunnel, healthy bind, worker still never ready -> the SCENE.
        # Replacing hardware in a loop costs a rental + a 481 MB Blender push +
        # a 291 MB scene push per attempt for a fault that travels with it.
        fleet = make(remote.WorkerUnreachable(
            "worker on stub not ready after 1800s and 599 pings: "
            "ping replied not-ok"))
        fleet._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("a scene that will not load keeps the GPU instead of cycling hosts",
              not fleet.torn and not fleet.bad_machines,
              f"torn={fleet.torn} bad={fleet.bad_machines}")
    finally:
        remote.reap_stale_tunnels = real_reap
        time.sleep = slow_sleep


def test_cost_projection_names_its_basis() -> None:
    """A measured number can still be the wrong measurement.

    The projection is arithmetic on real durations, which is right — but a
    2,978-frame 4K batch of a circuit priced off 1080p previews of a DIFFERENT
    .blend is precise and wrong, and reads as authoritative precisely because
    it is not a model. Stills also stored no spec_hash, so the estimator's own
    advice ("render one frame first and ask again") could not work: the frame
    came back as an anonymous duration averaged in with every other still."""
    with tempfile.TemporaryDirectory() as tmp:
        db = DB(Path(tmp) / "t.db")

        # A still carrying the spec it was rendered at is findable...
        jid = db.submit({"cam": "C"}, agent="a", scene="/s.blend", spec_hash="DELIVERY")
        db.claim(60)
        db.finish(jid, "/out.png", 412.0)
        mean, n, basis = db.mean_sec_for_spec("DELIVERY")
        check("a still records the spec it was rendered at",
              mean == 412.0 and n == 1 and "exact spec" in basis, f"{mean} {n} {basis}")

        # ...and a different spec must NOT match it.
        mean2, n2, _ = db.mean_sec_for_spec("SOMETHING_ELSE")
        check("a different spec finds no basis", mean2 is None and n2 == 0, str(mean2))

    # The estimate must label an inexact basis loudly rather than quietly
    # presenting it as a projection worth budgeting against.
    broker = app.Broker.__new__(app.Broker)
    broker.fleet = type("F", (), {"dph": 0.35})()
    exact = broker.cost_estimate(2978, 412.0, basis="single frame(s) rendered at "
                                 "this exact spec and .blend", samples=1, exact=True)
    loose = broker.cost_estimate(2978, 9.4, basis="single frames of OTHER "
                                 "scenes/specs", samples=118, exact=False)
    check("an exact basis is reported as exact",
          exact["exact"] and "WARNING" not in exact["note"], exact["note"][:60])
    check("a mismatched basis is flagged in the note",
          not loose["exact"] and "NOT a render of this batch's spec" in loose["note"],
          loose["note"][:80])
    check("the two bases give wildly different money, which is the whole point",
          exact["usd"] > 20 * loose["usd"],
          f"exact ${exact['usd']:.2f} vs loose ${loose['usd']:.2f}")


def test_absent_progress_json_is_idle_not_unknown() -> None:
    """A reachable instance that has never rendered was reported `unknown`.

    The probe is `stat ...; date +%s; cat progress.json`, so its exit status is
    `cat`'s — and on an instance that has not rendered yet there is no
    progress.json, so it exits 1. `activity()` scored any non-zero exit as
    unreachable, which meant EVERY fresh instance was permanently `unknown`:

        exit 1 after 0.6s on 192.0.2.13:23972 [stat -c %Y ...]: 1785254527

    ssh ran the command and `date` printed the answer. The branch that says
    "the file is absent, so it is not rendering" existed but could not be
    reached in the absent case. Consequence: the deploy gate could never call a
    fresh instance idle, so it could never replace one, and instance 46121112 —
    whose Blender died of SIGBUS — wedged the broker exactly like the host that
    refused our key did."""
    from . import remote as r

    real_probe = r.probe
    try:
        # ssh ran it; no progress.json, so `cat` exited 1 and only `date` printed.
        r.probe = lambda ep, cmd, timeout=45: remote.Ran(
            cmd=cmd, rc=1, out="1785254527\n", err="", elapsed=0.6, where="stub")
        act = r.activity(remote.Endpoint(host="stub", port=1, instance_id=1))
        check("a never-rendered instance is IDLE, not unknown",
              act.reachable and act.idle and not act.unknown and not act.rendering,
              act.detail)

        # ssh could not run the command at all -> still unknown.
        r.probe = lambda ep, cmd, timeout=45: remote.Ran(
            cmd=cmd, rc=255, out="", err="Connection timed out",
            elapsed=20.0, where="stub")
        act = r.activity(remote.Endpoint(host="stub", port=1, instance_id=1), attempts=1)
        check("an ssh transport failure is still unknown",
              act.unknown and not act.idle, act.detail)

        # Ran, but produced nothing at all -> we did not get an answer.
        r.probe = lambda ep, cmd, timeout=45: remote.Ran(
            cmd=cmd, rc=1, out="   \n", err="", elapsed=0.5, where="stub")
        act = r.activity(remote.Endpoint(host="stub", port=1, instance_id=1), attempts=1)
        check("a silent probe is unknown, not idle", act.unknown, act.detail)

        # A live render still reads as rendering, and still outranks everything.
        now = int(time.time())
        payload = json.dumps({"state": "rendering", "job_id": "j1"})
        r.probe = lambda ep, cmd, timeout=45: remote.Ran(
            cmd=cmd, rc=0, out=f"{now}\n{now}\n{payload}\n", err="",
            elapsed=0.6, where="stub")
        act = r.activity(remote.Endpoint(host="stub", port=1, instance_id=1))
        check("a live render is still detected as rendering",
              act.rendering and act.job_id == "j1" and not act.idle, act.detail)
    finally:
        r.probe = real_probe


def test_ssh_auth_rejection_is_not_transport() -> None:
    """`Permission denied (publickey)` and `connection timed out` are both
    ssh exit 255, and treating them alike wedged the broker on a rented 5090.

    The host had failed to write our key into the container. sshd was up and
    answering — it completed the handshake and denied the auth — so the box was
    never going to work, but the failure was classified as transport, retried
    for three rounds, and then blocked from replacement because the activity
    probe (also ssh) could not answer. Every job failed while the GPU billed."""
    auth = remote.Ran(
        cmd="true", rc=255, out="",
        err="root@192.0.2.11: Permission denied (publickey).",
        elapsed=2.5, where="192.0.2.11:29502",
    )
    timeout = remote.Ran(
        cmd="true", rc=255, out="",
        err="ssh: connect to host 1.2.3.4 port 22: Connection timed out",
        elapsed=20.0, where="1.2.3.4:22",
    )
    check("a publickey denial is recognised as an auth rejection",
          auth.auth_rejected, auth.err)
    check("a connect timeout is NOT an auth rejection",
          not timeout.auth_rejected, timeout.err)
    check("both are still 'the command did not run'",
          auth.transport_failed and timeout.transport_failed, "rc 255")
    check("SshNeverReady carries the classification through",
          remote.SshNeverReady("x", ran=auth).auth
          and not remote.SshNeverReady("x", ran=timeout).auth, "")
    check("SshNeverReady without a Ran does not claim auth",
          not remote.SshNeverReady("x").auth, "")

    # End to end through the deploy path: a fresh rental whose host never
    # installed our key must be replaced on the FIRST round, not the third,
    # and its machine must be blacklisted so the next rent avoids it.
    fleet = Fleet.__new__(Fleet)
    fleet.instance_id = 46118513
    fleet.ep = remote.Endpoint(host="stub", port=1, instance_id=46118513)
    fleet.stopped_at = None
    fleet.deploy_failures = 0
    fleet.status = "ready"
    fleet.may_hold_render = False          # never ran a command on it
    fleet.machine_id = 42763
    fleet.bad_machines = set()
    fleet.activity = lambda attempts=3: unreachable()
    fleet.torn = []
    fleet._teardown_locked = lambda reason="": fleet.torn.append(reason)

    def deploy(scene):
        raise remote.SshNeverReady("never accepted a command", ran=auth)
    fleet._deploy = deploy

    slow_sleep, time.sleep = time.sleep, lambda s: None
    try:
        ok = fleet._try_deploy(Path("/tmp/s.blend"), "freshly rented instance")
    finally:
        time.sleep = slow_sleep
    # deploy_failures started at 0, so a teardown here proves the replacement
    # happened on the first round rather than after MAX_TRANSPORT_ROUNDS of
    # re-asking a host that had already given its final answer. The counter is
    # then reset, because the replacement instance starts with a clean budget.
    check("an auth-rejecting fresh rental is replaced on the first round",
          ok is False and fleet.torn == ["deploy failed"] and fleet.deploy_failures == 0,
          f"torn={fleet.torn} rounds={fleet.deploy_failures}")
    check("the host that failed to install the key is blacklisted",
          fleet.bad_machines == {42763}, str(fleet.bad_machines))


def test_hibernate_refuses_unknown() -> None:
    """`hibernate()`'s under-lock re-check raised only on `rendering`; an
    unanswered probe fell straight through to stop_instance — the exact
    two-state collapse the tri-state rule exists to prevent. A stop kills any
    frame in flight, so `unknown` must refuse; `force=True` is the caller
    saying it has already applied a bounded blind-stop policy."""
    stops: list[int] = []
    fleet = Fleet.__new__(Fleet)
    fleet.lock = threading.Lock()
    fleet.instance_id = 9
    fleet.stopped_at = None
    fleet.tunnel = None
    fleet.ep = None                       # activity() -> unknown ("no endpoint")
    fleet.dph = 0.3
    fleet.started_at = time.time() - 60
    fleet.gpu_seconds = 0.0
    fleet.last_ready = True
    fleet.status = "ready"

    class Client:
        def stop_instance(self, iid):
            stops.append(iid)
    fleet.client = Client()

    try:
        fleet.hibernate()
        check("hibernate refuses on an unanswered probe", False, "it stopped anyway")
    except remote.WorkerBusy as exc:
        check("hibernate refuses on an unanswered probe", not stops, str(exc)[:70])
    check("the refusal never reached vast", stops == [], str(stops))

    fleet.hibernate(force=True)
    check("a deliberate forced stop still works", stops == [9] and fleet.stopped_at,
          str(stops))


def test_resume_abandon_destroys_stopped_instance() -> None:
    """Giving up on an unwakeable hibernated instance used to set
    `instance_id = None` and promise 'the hibernation deadline will reap it' —
    but that deadline lives in maybe_idle_down, which returns immediately once
    instance_id is falsy, so the promise could never be kept and a stopped
    container (which runs no watchdog) billed storage until a human noticed.
    A stopped instance runs nothing, so destroying it at abandonment is safe."""
    destroyed: list[int] = []
    from . import fleet as fleet_mod

    fleet = Fleet.__new__(Fleet)
    fleet.lock = threading.Lock()
    fleet.instance_id = 77
    fleet.ep = None
    fleet.tunnel = None
    fleet.stopped_at = time.time() - 100
    fleet.started_at = None
    fleet.dph = 0.3
    fleet.gpu_seconds = 0.0
    fleet.scene_hash = None
    fleet.scene_path = None
    fleet.mirrored_assets = set()
    fleet.last_ready = False
    fleet.status = "stopped"
    fleet.resume_failures = fleet_mod.RESUME_ATTEMPTS - 1
    fleet.deploy_failures = 0
    fleet.on_teardown = None
    fleet.doomed = {}
    fleet.client = None

    def failing_resume(scene):
        raise RuntimeError("actual=exited, intended=stopped")
    fleet._resume = failing_resume

    def no_rent():
        raise RuntimeError("rent-sentinel")
    fleet._rent = no_rent

    real_destroy = fleet_mod.vastctl.destroy
    fleet_mod.vastctl.destroy = lambda client, iid: destroyed.append(iid) or True
    try:
        try:
            fleet.ensure_ready(Path("/tmp/s.blend"))
        except RuntimeError as exc:
            if "rent-sentinel" not in str(exc):
                raise
        check("an unwakeable stopped instance is destroyed, not forgotten",
              destroyed == [77] and fleet.instance_id is None,
              f"destroyed={destroyed} instance_id={fleet.instance_id}")
    finally:
        fleet_mod.vastctl.destroy = real_destroy


def test_unconfirmed_destroy_is_reaped() -> None:
    """`destroy()` returning False used to be ignored inside `_rent`'s cleanup;
    the instance then billed on, untracked, until the next restart. Unconfirmed
    destroys are now remembered and retried from the heartbeat thread."""
    from . import fleet as fleet_mod

    fleet = Fleet.__new__(Fleet)
    fleet.doomed = {}
    fleet.client = None
    answers = {"ok": False}

    real_destroy = fleet_mod.vastctl.destroy
    fleet_mod.vastctl.destroy = lambda client, iid: answers["ok"]
    try:
        got = fleet._destroy_confirmed(11, "test")
        check("an unconfirmed destroy is remembered, not swallowed",
              got is False and 11 in fleet.doomed, str(fleet.doomed))

        fleet.reap_doomed()
        check("the retry respects the rate-limit interval", 11 in fleet.doomed)

        fleet.doomed[11] = 0.0             # pretend the interval has passed
        answers["ok"] = True
        fleet.reap_doomed()
        check("a confirmed retry clears the reap list", 11 not in fleet.doomed,
              str(fleet.doomed))

        # And an exploding destroy is a failure too, never a silent pass.
        def explode(client, iid):
            raise RuntimeError("vast 429")
        fleet_mod.vastctl.destroy = explode
        got = fleet._destroy_confirmed(12, "test")
        check("an exception from destroy is remembered as unconfirmed",
              got is False and 12 in fleet.doomed, str(fleet.doomed))
    finally:
        fleet_mod.vastctl.destroy = real_destroy


def test_paused_broker_still_winds_down() -> None:
    """`if self.paused: continue` sat in front of maybe_idle_down, so a broker
    paused over the spend cap could never destroy an instance it had adopted
    *stopped* (which has no endpoint, so pause() itself does not tear it down,
    and no watchdog, so nothing else ever would)."""
    slow_sleep, time.sleep = time.sleep, lambda s: None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fleet = StubFleet([idle_worker()])
            fleet.ep = None                       # adopted stopped: no endpoint
            fleet.stopped_at = time.time() - app.config.HIBERNATE_SEC - 5
            b = stub_broker(tmp, fleet)
            b.paused = "cumulative spend hit the cap"
            b.dispatch_once()
            check("a paused broker still reaps a hibernation-expired instance",
                  fleet.torn_down, f"torn_down={fleet.torn_down}")
    finally:
        time.sleep = slow_sleep


# --- surviving the process, and the thread ---------------------------------
#
# The failure family here is different from the one above: not a wrong decision
# about a render, but the broker *ceasing to exist* mid-batch. Two multi-hour
# batches were lost that way. None of this needs a GPU either.


def test_wait_does_not_hold_the_fleet_lock() -> None:
    """A reattach may run for the length of an 8K frame — up to REATTACH_SEC,
    5400 s. Holding `fleet.lock` across it would block `/teardown`, `hibernate`
    and every other fleet call for the whole render, which is wrong regardless
    of whether anything crashes: the operator's escape hatch stops working
    exactly when a long render is what they want to escape from."""
    fleet = Fleet.__new__(Fleet)
    fleet.lock = threading.Lock()
    fleet.ep = remote.Endpoint(host="stub", port=1, instance_id=1)
    fleet.instance_id = 1
    fleet.stopped_at = None

    entered = threading.Event()
    release = threading.Event()

    def slow_activity(ep, attempts=3, max_age=None):
        entered.set()
        release.wait(10)                 # stand in for a frame that takes an hour
        return rendering("mine")

    real = remote.activity
    remote.activity = slow_activity
    try:
        t = threading.Thread(
            target=lambda: fleet.await_render("mine", 5.0), daemon=True)
        t.start()
        check("await_render actually starts waiting", entered.wait(5))
        # THE ASSERTION: the lock is free while the wait is in flight.
        got = fleet.lock.acquire(timeout=2)
        check("await_render does NOT hold fleet.lock while waiting", got,
              "teardown and hibernate would block for the whole render")
        if got:
            fleet.lock.release()
    finally:
        release.set()
        remote.activity = real
        t.join(timeout=5)

    # And the guard that replaced the old blocking wait raises instead of
    # blocking, so the *dispatcher* decides whose frame it is — the fleet lock
    # is held only for the length of that check.
    fleet.activity = lambda attempts=3: rendering("someone-else")
    try:
        fleet._refuse_if_rendering()
        check("_refuse_if_rendering raises instead of blocking", False, "returned")
    except remote.WorkerBusy as exc:
        check("_refuse_if_rendering raises instead of blocking",
              exc.job_id == "someone-else", f"job_id={exc.job_id}")


def test_thread_supervision() -> None:
    """An exception anywhere in the dispatch path must fail that job, never take
    down the process — and if a whole loop thread does die, it must come back.
    A dead dispatch thread is silent: HTTP keeps answering while nothing claims
    a job, and the queue simply stops moving."""
    b = app.Broker.__new__(app.Broker)
    b.running = True
    calls = []

    def flaky() -> None:
        calls.append(len(calls))
        if len(calls) < 3:
            raise RuntimeError("boom")
        b.running = False               # third call: stop cleanly

    slow_sleep, time.sleep = time.sleep, lambda s: None
    try:
        b.supervised("test", flaky)
    finally:
        time.sleep = slow_sleep
    check("a thread body that raises is restarted, not lost", len(calls) == 3,
          f"{len(calls)} call(s)")

    # BaseException too — `except Exception` in each loop does not cover it, and
    # a MemoryError or a stray SystemExit would otherwise kill the loop silently.
    b2 = app.Broker.__new__(app.Broker)
    b2.running = True
    hits = []

    def fatal() -> None:
        hits.append(1)
        if len(hits) < 2:
            raise SystemExit(1)
        b2.running = False

    slow_sleep, time.sleep = time.sleep, lambda s: None
    try:
        b2.supervised("test", fatal)
    finally:
        time.sleep = slow_sleep
    check("SystemExit in a loop thread is caught and restarted", len(hits) == 2,
          f"{len(hits)} call(s)")


def test_jobs_survive_a_restart() -> None:
    """Queued work must outlive the process, and a `running` row left by a
    broker that no longer exists must go back on the queue rather than wait out
    an hour-long lease while a paid GPU idles."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "restart.db"
        db = DB(path)
        queued = db.submit(spec(), agent="a")
        claimed = db.claim(3600)
        db.close()

        # A new process opening the same file is exactly a broker restart.
        b = app.Broker.__new__(app.Broker)
        b.db = DB(path)
        reclaimed = b.reclaim_orphans()
        counts = b.db.counts()
        check("a queued job survives a broker restart",
              b.db.get(queued)["state"] == "queued", str(counts))
        check("a running job is reclaimed at startup, not stranded on its lease",
              reclaimed == 1 and b.db.get(claimed["id"])["state"] == "queued",
              f"reclaimed={reclaimed}")
        b.db.close()


# --- HTTP surface ---------------------------------------------------------


def http(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_http() -> bool:
    try:
        urllib.request.urlopen(BASE + "/health", timeout=5)
    except Exception:
        print(f"  (skipping HTTP tests — no broker at {BASE})")
        return False

    # An incomplete spec is refused rather than defaulted, matching the worker.
    bad = spec()
    del bad["samples"]
    code, body = http("POST", "/jobs", {"spec": bad, "agent": "test"})
    check("incomplete spec -> 400", code == 400 and "samples" in str(body), f"{code}")

    code, _ = http("GET", "/jobs/nosuchjob")
    check("unknown job -> 404", code == 404, str(code))

    code, body = http("POST", "/jobs", {"spec": spec(), "agent": "test"})
    check("submit accepted", code == 200 and "job_id" in body, str(body)[:60])
    jid = body.get("job_id", "")

    # Long-poll must return promptly once the job reaches a terminal state
    # rather than burning the full wait window.
    started = time.time()
    code, job = http("GET", f"/jobs/{jid}?wait=30")
    took = time.time() - started
    check("long-poll returns on terminal state", job.get("state") in ("done", "failed", "canceled")
          and took < 25, f"state={job.get('state')} in {took:.1f}s")

    code, body = http("GET", "/queue")
    check("queue reports fleet state", code == 200 and "fleet" in body and "counts" in body)

    check("no GPU rented during tests", body.get("fleet", {}).get("instance_id") is None,
          str(body.get("fleet", {}).get("instance_id")))
    return True


def test_exec_queue_and_bundles() -> None:
    """The EXEC job type: kind separation, bundle containment, digest sensitivity.

    Every check here is a failure this codebase has already paid for in the
    render path, re-asked of the new one:

      * a client-supplied name used as a path (job ids are broker-minted because
        one was a traversal into a read-only project),
      * two dispatchers reaching for the same row,
      * an input that changed under a queued job,
      * a schema restated in two places and allowed to drift.
    """
    from . import execremote, execservice

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db = DB(tmp / "exec.db")

        # --- kind separation -------------------------------------------
        render_id = db.submit({"camera": "CAM"}, agent="a", scene="/s.blend")
        exec_id = db.submit({"entry": "tools/x.py"}, agent="a", kind="exec",
                            bundle="0123456789abcdef")
        got = db.claim(600, scene="/s.blend")
        check("the render dispatcher claims only render rows",
              got is not None and got["id"] == render_id, str(got and got["id"]))
        check("the render dispatcher cannot claim an exec row",
              db.claim(600) is None)
        got = db.claim_exec(600)
        check("the exec dispatcher claims the exec row",
              got is not None and got["id"] == exec_id, str(got and got["id"]))
        check("the exec dispatcher cannot claim a render row",
              db.claim_exec(600) is None)
        check("a scene-batching query never sees an exec row",
              db.oldest_waiting_scene() == (None, 0.0) or
              db.oldest_waiting_scene()[0] != None,
              str(db.oldest_waiting_scene()))

        # An exec row waiting is what stops the idle timer from stopping the
        # instance under a full exec queue.
        db.requeue(exec_id)
        check("a queued exec row is visible to the idle timer",
              db.exec_waiting() == 1, str(db.exec_waiting()))

        # --- bundle roots ----------------------------------------------
        root = tmp / "project"
        (root / "world" / "items").mkdir(parents=True)
        (root / "tools").mkdir()
        (root / "world" / "items" / "a.py").write_text("A = 1\n")
        (root / "world" / "items" / "b.py").write_text("B = 2\n")
        (root / "tools" / "build.py").write_text("pass\n")
        outside = tmp / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("SECRET\n")
        (root / "escape").symlink_to(outside)

        os.environ["VASTRENDER_BUNDLE_ROOTS"] = str(root)
        try:
            for name, raw in (
                ("a bundle root outside every permitted root", str(outside)),
                ("a bundle root reached with ..", str(root / ".." / "outside")),
                ("a bundle root reached through a symlink", str(root / "escape")),
                ("an empty bundle root", ""),
            ):
                try:
                    execservice.resolve_bundle_root(raw)
                    check(f"REFUSED: {name}", False, "accepted")
                except execservice.ExecError as exc:
                    check(f"REFUSED: {name}", True, str(exc)[:60])
            check("the permitted root itself resolves",
                  execservice.resolve_bundle_root(str(root)) == root.resolve())

            for name, patterns in (
                ("an absolute bundle pattern", ["/etc/*"]),
                ("a bundle pattern matching nothing", ["nope/*.py"]),
                ("a bundle pattern that is not a list", "world/*.py"),
                ("an empty bundle pattern list", []),
            ):
                try:
                    execservice.plan_bundle(str(root), patterns)
                    check(f"REFUSED: {name}", False, "accepted")
                except execservice.ExecError as exc:
                    check(f"REFUSED: {name}", True, str(exc)[:60])

            # A glob through the symlink resolves outside the root and is
            # refused — and, as with the traversal tests, the point is that the
            # file is not in the bundle rather than merely that an error was
            # raised.
            try:
                execservice.plan_bundle(str(root), ["escape/*.py"])
                check("REFUSED: a bundle pattern reaching through a symlink",
                      False, "accepted")
            except execservice.ExecError as exc:
                check("REFUSED: a bundle pattern reaching through a symlink",
                      True, str(exc)[:60])

            # --- the digest is the point ------------------------------
            b1 = execservice.plan_bundle(str(root), ["world/items/*.py", "tools/*.py"])
            b2 = execservice.plan_bundle(str(root), ["world/items/*.py", "tools/*.py"])
            check("the same tree hashes the same twice",
                  b1.digest == b2.digest, b1.digest)
            check("the bundle holds exactly the files the globs matched",
                  sorted(b1.rel) == ["tools/build.py", "world/items/a.py",
                                     "world/items/b.py"], str(sorted(b1.rel)))
            (root / "world" / "items" / "b.py").write_text("B = 3\n")
            b3 = execservice.plan_bundle(str(root), ["world/items/*.py", "tools/*.py"])
            check("editing ONE module moves the digest — which is what makes a "
                  "job refuse to build code it was not queued against",
                  b3.digest != b1.digest, f"{b1.digest} -> {b3.digest}")

            # Same bytes, different name, must not collide: two modules with
            # identical content import differently.
            (root / "world" / "items" / "c.py").write_text("A = 1\n")
            b4 = execservice.plan_bundle(str(root), ["world/items/*.py", "tools/*.py"])
            check("a file's PATH is part of the content address",
                  b4.digest != b3.digest)
        finally:
            os.environ.pop("VASTRENDER_BUNDLE_ROOTS", None)

        # --- the schema is read from the worker, never restated --------
        src = Path(app.__file__).resolve().parent.parent / "worker" / "exec_server.py"
        declared = set(re.findall(r'^\s+"(\w+)",', src.read_text(), re.M))
        check("the broker's required set comes from the worker's own frozenset",
              execservice.CALLER_REQUIRED ==
              execservice.WORKER_FIELDS - {"job_id", "bundle"} and
              execservice.WORKER_FIELDS <= declared,
              str(sorted(execservice.CALLER_REQUIRED)))

        # --- the remote path names the REMOTE command line -------------
        cmd = execremote.exec_launch_cmd("/workspace", 8800, 12)
        check("the exec launch line runs the script at its REMOTE path",
              "-P /workspace/exec_server.py" in cmd, cmd[:80])
        check("the exec pid pattern would not match the render worker",
              "/workspace/server.py" not in "/workspace/exec_server.py")
        check("the exec launch detaches with setsid --fork and no trailing &",
              "setsid --fork" in cmd and not cmd.rstrip().endswith("&"))
        check("the exec launch redirects to its own log, not the worker's",
              "/workspace/exec.log" in cmd and "/workspace/worker.log" not in cmd)

        for bad in ("../scenes", "zzzz", "", "a/b"):
            try:
                execremote.bundle_dir(bad)
                check(f"REFUSED: bundle_dir({bad!r})", False, "accepted")
            except ValueError:
                check(f"REFUSED: bundle_dir({bad!r})", True)


def report() -> int:
    width = max(len(n) for _, n, _ in results) + 2
    print()
    for ok, name, detail in results:
        print(f"  [{'ok' if ok else 'FAIL':>4}] {name:<{width}} {detail}")
    failed = sum(1 for ok, _, _ in results if not ok)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


def _stall_fleet(boom, moved_per_round=(), reconcile="running"):
    """A Fleet whose deploy always fails, with a scripted transport history."""
    fleet = Fleet.__new__(Fleet)
    fleet.instance_id = 42
    fleet.ep = remote.Endpoint(host="stub", port=1, instance_id=42)
    fleet.stopped_at = None
    fleet.status = "ready"
    fleet.deploy_failures = 0
    fleet.transport_bytes = 0
    fleet.stalled_rounds = 0
    # Never talked to, so `unknown` is a known-empty and the destroy is allowed
    # to happen — otherwise the tri-state gate, not the new policy, is what
    # decides, and the test would be measuring the wrong thing.
    fleet.may_hold_render = False
    fleet.machine_id = 55313
    fleet.offer_id = 43856614
    fleet.bad_machines = set()
    fleet.bad_offers = set()
    fleet.activity = lambda attempts=3: unreachable()
    fleet.reconcile = lambda why: reconcile
    fleet.torn = []
    fleet._teardown_locked = lambda reason="": fleet.torn.append(reason)
    fleet.rounds = 0

    def deploy(scene):
        raise boom(fleet.rounds)
    fleet._deploy = deploy
    return fleet


def test_a_stalled_transport_is_condemned_and_a_progressing_one_is_not() -> None:
    """The retry loop needed an EXIT, and the exit must not be a plain count.

    On 2026-08-02 machine 55313 reset every ssh connection it was given. The
    broker spent 3 rounds x 3 attempts x 4 push attempts — 80 minutes and $0.41
    of GPU across two instances — relearning that, because the only bound on
    "retry a dropped upload rather than condemn the GPU" was a count of rounds,
    and a host that resets everything spends a count of rounds as happily as a
    host that is merely flaky.

    What separates them is not how often a push failed but whether the failures
    are getting anywhere. Pushes resume, so a flaky link keeps whatever bytes it
    lands and its high-water mark climbs; a host that hangs up on everything
    ends every round exactly where it started."""
    slow_sleep, time.sleep = time.sleep, lambda s: None
    try:
        # A link that is dropping but DELIVERING: each round leaves more of the
        # bundle on the box. This must never be condemned, however long it goes
        # on — that is the skill's "retry before replacing a GPU" rule, intact.
        climbing = _stall_fleet(
            lambda r: remote.TransferError("bundle push", "stub", "reset", 2.0,
                                           sent=40_000_000 * r,
                                           expected=481_485_662)
        )
        for _ in range(2):
            climbing.rounds += 1
            climbing._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("a transport failure that keeps DELIVERING BYTES is not condemned",
              not climbing.torn and climbing.stalled_rounds == 0,
              f"torn={climbing.torn} stalled={climbing.stalled_rounds} "
              f"high-water={climbing.transport_bytes}")

        # The same number of rounds, the same error type, the same host — and
        # the opposite verdict, decided purely by whether it got anywhere.
        stuck = _stall_fleet(
            lambda r: remote.TransferError("bundle push", "stub", "reset", 2.0,
                                           sent=80_000_000, expected=481_485_662,
                                           chronic=True)
        )
        stuck._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("ONE hopeless round still retries — a hiccup is not a verdict",
              not stuck.torn and stuck.stalled_rounds == 1,
              f"torn={stuck.torn} stalled={stuck.stalled_rounds}")

        stuck._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("the SECOND hopeless round is condemned, not looped",
              stuck.torn == ["deploy failed"], f"torn={stuck.torn}")
        check("condemning blacklists the OFFER, or the next rent buys it back",
              43856614 in stuck.bad_offers, str(stuck.bad_offers))
        check("...and the MACHINE, which re-lists under a new offer in seconds",
              55313 in stuck.bad_machines, str(stuck.bad_machines))

        # Delivering bytes does NOT buy an unbounded loop: the existing
        # MAX_TRANSPORT_ROUNDS ceiling is untouched and still ends it.
        for _ in range(3):
            climbing.rounds += 1
            climbing._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("a progressing link is still bounded by MAX_TRANSPORT_ROUNDS",
              climbing.torn == ["deploy failed"], f"torn={climbing.torn}")
    finally:
        time.sleep = slow_sleep


def test_a_vanished_instance_is_forgotten_not_retried() -> None:
    """An instance destroyed out of band — or a preempted bid — must not be
    waited on, retried, condemned or destroyed. Nothing used to ask vast.ai
    whether the box still existed, so `waiting-for-ssh instance=46585570`
    survived the instance itself."""
    slow_sleep, time.sleep = time.sleep, lambda s: None
    try:
        gone = _stall_fleet(
            lambda r: remote.TransferError("bundle push", "stub", "reset", 2.0,
                                           sent=0, expected=481_485_662),
            reconcile="gone",
        )
        for _ in range(4):
            gone._try_deploy(Path("/tmp/s.blend"), "existing instance")
        check("a vanished instance is never destroyed (there is nothing there)",
              not gone.torn, f"torn={gone.torn}")
        check("...and its offer is NOT condemned — the offer did nothing wrong",
              not gone.bad_offers and not gone.bad_machines,
              f"offers={gone.bad_offers} machines={gone.bad_machines}")

        # And the forget path itself: no destroy call, no instance left behind.
        fleet = Fleet.__new__(Fleet)
        fleet.instance_id = 46585570
        fleet.ep = remote.Endpoint(host="stub", port=1, instance_id=46585570)
        fleet.tunnel = None
        fleet.on_teardown = None
        real_close, remote.close_master = remote.close_master, lambda ep: None
        try:
            fleet._forget_vanished()
        finally:
            remote.close_master = real_close
        check("forgetting a vanished instance clears it without a destroy",
              fleet.instance_id is None and fleet.ep is None
              and fleet.status == "down", str(fleet.status))
    finally:
        time.sleep = slow_sleep


def test_reconcile_never_stalls_the_heartbeat_thread() -> None:
    """`ensure_ready` holds the fleet lock across the WHOLE deploy, including a
    481 MB push. The heartbeat runs on its own thread precisely so liveness
    signalling is not gated on "deploy finished" — the in-container watchdog
    destroys an instance whose heartbeat lapses for 30 minutes, and it does not
    care that we were busy. So the reconcile the heartbeat performs may take the
    lock only if it is free, and must otherwise report and move on."""
    import threading

    fleet = Fleet.__new__(Fleet)
    fleet.instance_id = 46585570
    fleet.ep = remote.Endpoint(host="stub", port=1, instance_id=46585570)
    fleet.lock = threading.Lock()
    fleet.tunnel = None
    fleet.on_teardown = None

    class Client:
        def show_instances(self):
            return []          # our instance is gone
    fleet.client = Client()

    real_our, real_close = None, remote.close_master
    import vastctl
    real_our = vastctl.our_instances
    vastctl.our_instances = lambda client: []
    remote.close_master = lambda ep: None
    try:
        # Lock HELD, as it is throughout a deploy: must return promptly and
        # must not have mutated the instance out from under the deploy.
        fleet.lock.acquire()
        done = threading.Event()
        verdict = []

        def beat():
            verdict.append(fleet.reconcile("heartbeat", locked=False))
            done.set()

        threading.Thread(target=beat, daemon=True).start()
        finished = done.wait(timeout=5.0)
        check("reconcile on the heartbeat thread never blocks on a held lock",
              finished, "it blocked — the watchdog clock would be running")
        check("...and leaves the instance for the lock holder to clean up",
              verdict == ["gone"] and fleet.instance_id == 46585570,
              f"verdict={verdict} instance={fleet.instance_id}")
        fleet.lock.release()

        # Lock FREE: now it may do the cleanup itself.
        check("with the lock free it forgets the vanished instance",
              fleet.reconcile("heartbeat", locked=False) == "gone"
              and fleet.instance_id is None, str(fleet.instance_id))
    finally:
        vastctl.our_instances = real_our
        remote.close_master = real_close


def test_push_falls_back_to_one_stream_and_reports_chronic() -> None:
    """Parallelism buys robustness, not speed — 4.02 MB/s across 8 streams
    against 4.68 MB/s on one. So when every stream is reset together, dropping
    to a single connection costs nothing worth having and settles the question
    eight streams cannot ask: one connection cannot trip a connection-rate or
    MaxStartups limit, so if it is reset the same way, the host is the problem
    and not our concurrency."""
    widths: list[int] = []
    splits: list[int] = []

    def always_reset(ep, local, remote_path, streams=8, concurrency=None):
        widths.append(concurrency)
        splits.append(streams)
        raise remote.TransferError("parallel push", "stub", "reset", 1.0,
                                   sent=8_000_000, expected=481_485_662,
                                   streams=streams, reset_all=True)

    class Stat:
        ok, transport_failed, out = True, False, "0"

    real_probe, real_push = remote.probe, remote.push_parallel
    slow_sleep, time.sleep = time.sleep, lambda s: None
    bundle = Path(tempfile.mkstemp()[1])
    try:
        bundle.write_bytes(b"x" * 1024)
        remote.probe = lambda ep, cmd, timeout=120: Stat()
        remote.push_parallel = always_reset
        ep = remote.Endpoint(host="stub", port=1, instance_id=1)
        try:
            remote.push_blender(ep, bundle, attempts=4)
            failure = None
        except remote.TransferError as exc:
            failure = exc
        check("the push falls back to one connection at a time after two resets",
              widths == [8, 8, 1, 1], str(widths))
        check("...WITHOUT renarrowing the split, which would void every "
              "resumable byte already on the instance",
              splits == [8, 8, 8, 8], str(splits))
        check("a lone connection reset the same way is reported as CHRONIC",
              failure is not None and failure.chronic, str(failure))
        check("the high-water mark travels with the failure, not this attempt's bytes",
              failure is not None and failure.sent == 8_000_000, str(failure.sent))

        # A stall or a partial failure is an ordinary bad link. Halving our own
        # throughput for that would make a recoverable transfer worse.
        widths.clear()
        def stalled(ep, local, remote_path, streams=8, concurrency=None):
            widths.append(concurrency)
            raise remote.TransferError("parallel push", "stub", "no bytes moved",
                                       1.0, sent=1, expected=2,
                                       streams=streams, reset_all=False)
        remote.push_parallel = stalled
        try:
            remote.push_blender(ep, bundle, attempts=4)
        except remote.TransferError as exc:
            check("a STALL never triggers the fallback and is not chronic",
                  widths == [8, 8, 8, 8] and not exc.chronic, str(widths))
    finally:
        remote.probe, remote.push_parallel = real_probe, real_push
        time.sleep = slow_sleep
        bundle.unlink(missing_ok=True)


def test_unreadable_resume_state_never_deletes_the_parts() -> None:
    """`have` empty means two very different things: "the instance holds no
    parts" and "we could not ask". The second used to fall into the branch that
    deletes every `.partN` on the box — so the one condition under which the
    surviving bytes matter most is the one that threw them away, and a link
    that could move 80 MB before dropping could never deliver 481 MB."""
    ran: list[str] = []

    class Dead:
        ok, transport_failed = False, True
        out = err = ""
        rc = 255
        def describe(self) -> str:
            return "ssh: exit 255"

    real_probe, real_run = remote.probe, remote.run
    try:
        remote.probe = lambda ep, cmd, timeout=120: Dead()
        remote.run = lambda ep, cmd, check=True, **kw: ran.append(cmd)
        bundle = Path(tempfile.mkstemp()[1])
        bundle.write_bytes(b"x" * 4096)
        ep = remote.Endpoint(host="stub", port=1, instance_id=1)
        try:
            remote.push_parallel(ep, bundle, "/workspace/blender.tar.zst")
            raised = None
        except Exception as exc:
            raised = exc
        check("an unreadable resume state fails the attempt",
              isinstance(raised, remote.SshError), repr(raised))
        check("...and deletes NOTHING — the surviving bytes are the whole point",
              not any("rm -f" in c for c in ran), str(ran))
        bundle.unlink(missing_ok=True)
    finally:
        remote.probe, remote.run = real_probe, real_run


def test_the_pixel_cap_counts_what_is_actually_rendered() -> None:
    """A crop is capped on the crop, not on the frame it was cut from.

    `use_crop_to_border` makes Blender render and return the border alone, so
    counting the full frame charged a job for pixels nobody asked for — and
    made high-density crops, which is what `--zoom` is FOR, the one thing zoom
    could not do. Measured 2026-08-03: a 0.16 x 0.16 border at zoom 8 on a
    3840x2160 frame is 4915x2765 = 13.6 Mpx of real work and was refused as
    "531 Mpx, over the 200 Mpx limit".

    Checked against the arithmetic rather than against Blender, so it runs with
    no GPU and no bpy — the numbers are the whole of the bug.
    """
    def rendered_px(w: int, h: int, zoom: float, border=None) -> int:
        full_w, full_h = int(w * zoom), int(h * zoom)
        if not border:
            return full_w * full_h
        min_x, max_x, min_y, max_y = border
        return (max(1, int(full_w * (max_x - min_x)))
                * max(1, int(full_h * (max_y - min_y))))

    cap = 200_000_000
    crop = rendered_px(3840, 2160, 8.0, (0.42, 0.58, 0.42, 0.58))
    check("a 0.16x0.16 crop at zoom 8 is counted as the crop, and fits",
          crop < cap and 13_000_000 < crop < 14_000_000, f"{crop / 1e6:.1f} Mpx")

    full = rendered_px(3840, 2160, 8.0)
    check("the same job without a border is still refused",
          full > cap, f"{full / 1e6:.0f} Mpx")

    # The cap must still bite: a crop can be large too, and this is a memory
    # ceiling rather than a formality.
    big = rendered_px(3840, 2160, 8.0, (0.0, 1.0, 0.0, 0.9))
    check("a crop big enough to blow the budget is still refused",
          big > cap, f"{big / 1e6:.0f} Mpx")

    # And an unzoomed full frame is nowhere near it — no accidental narrowing.
    check("an ordinary 4K frame is unaffected",
          rendered_px(3840, 2160, 1.0) < cap, "")


def test_a_refusal_is_never_retried() -> None:
    """A verdict fails once. Retry belongs to transport.

    The worker marks a rejected spec `terminal`; the broker must fail it rather
    than spend attempts on it. Measured 2026-08-03: four jobs each logged the
    same refusal three times before failing, and each attempt dragged a scene
    selection behind it — on a farm where a scene selection can cost 24 minutes.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        b = stub_broker(tmp, StubFleet([idle_worker()]))

        job_id = b.db.submit(spec(), agent="wavefix", scene="/scenes/a.blend")
        b.db.claim(60.0)
        b.db.fail_terminal(job_id, "531 Mpx, over the 200 Mpx limit")
        row = b.db.get(job_id)
        check("a refused job is failed, not requeued for two more renders",
              row["state"] == "failed", f"state={row['state']}")

        # And the distinction is carried by the reply, not guessed from the
        # text of the message — a worker that does not set the flag still gets
        # the old retrying behaviour, so this cannot silently swallow anything.
        check("a terminal reply raises JobRefused, a plain one does not",
              issubclass(app.JobRefused, RuntimeError)
              and not issubclass(RuntimeError, app.JobRefused), "")


def test_preemption_must_beat_the_switch_it_costs() -> None:
    """A costly scene is not abandoned for a wait shorter than reloading it.

    The 2026-08-03 outage: five agents held work against five scenes, so some
    scene had ALWAYS waited longer than the flat 300 s starvation line. The
    test fired on every dispatch and `next_job` — which exists to avoid a
    switch per job — performed nine consecutive switches, each logged "after 1
    job(s)", buying 13 s renders with 100 s scene pushes. It was one job from
    dropping a 4.53 GB scene holding sixteen queued jobs, at ~24 minutes a
    round trip, to serve a 3 MB scene holding one.

    The rule: preemption has to clear the cost of the preemption, paid twice —
    once to leave and once to come back.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        big, small = "/scenes/film7.blend", "/scenes/wit_static.blend"

        fleet = StubFleet([idle_worker()])
        fleet.scene_path = Path(big)
        b = stub_broker(tmp, fleet)

        # A cheap scene keeps the old behaviour exactly: 2 x 60 s is under the
        # 300 s floor, so small scenes still interleave as freely as before.
        fleet.reload_cost = 60.0
        check("a cheap scene still preempts at the plain floor",
              b.starve_threshold() == app.config.SCENE_STARVE_SEC,
              f"{b.starve_threshold():.0f}s")

        # A 4.5 GB scene measured at 1425 s must be worth ~2x that to leave.
        fleet.reload_cost = 1425.0
        check("an expensive scene raises the bar to the round trip it costs",
              b.starve_threshold() == 2850.0, f"{b.starve_threshold():.0f}s")

        # Sixteen jobs for the loaded scene; one job for another that has
        # already waited 26 minutes — the exact live state. 1584 s is under the
        # 2850 s the switch would cost, so the loaded scene must DRAIN.
        for _ in range(16):
            b.db.submit(spec(), agent="showlight", scene=big)
        old = b.db.submit(spec(), agent="crowd", scene=small)
        b.db.conn.execute("UPDATE jobs SET created=? WHERE id=?",
                          (time.time() - 1584, old))
        b.db.conn.commit()

        served = []
        for _ in range(16):
            job = b.next_job()
            if job is None:
                break
            served.append(job["scene"])
        check("the loaded scene drains instead of being preempted per job",
              served == [big] * 16, f"{len(served)} job(s), {set(served)}")

        # And the waiting scene is not starved — it is served the moment the
        # expensive one has nothing left, which is when the switch is free.
        job = b.next_job()
        check("the waiting scene is served as soon as draining is done",
              job is not None and job["scene"] == small, str(job and job["scene"]))

        # A wait that genuinely exceeds the round trip still preempts: this is
        # a cost test, not a licence to hold the GPU forever. Many jobs queued,
        # so finishing is NOT the cheaper option.
        for _ in range(400):
            b.db.submit(spec(), agent="showlight", scene=big)
        older = b.db.submit(spec(), agent="crowd", scene=small)
        b.db.conn.execute("UPDATE jobs SET created=? WHERE id=?",
                          (time.time() - 4000, older))
        b.db.conn.commit()
        fleet.scene_path = Path(big)
        job = b.next_job()
        check("a wait longer than the round trip still preempts",
              job is not None and job["scene"] == small, str(job and job["scene"]))


def test_a_scene_you_can_finish_is_not_yielded() -> None:
    """Round-robin one job at a time is starvation-avoidance eating itself.

    `starve_threshold` compares a WAIT against a COST. In a contended queue
    every scene eventually waits longer than any switch costs, so every scene
    reads as starving, the comparison stops discriminating, and the dispatcher
    trades the worker back and forth a job at a time — the exact behaviour it
    exists to prevent, reached from the other side.

    Measured 2026-08-03 with the threshold fix already live: two 292 MB scenes,
    7 and 6 jobs queued, both waiting ~2400 s, alternating every job. 07:51:29
    to 07:52:23 is 54 s to switch scenes and render one 6.1 s frame.

    So when the loaded scene can be finished in less time than leaving and
    returning would cost, finish it.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        a, other = "/scenes/probe_lit.blend", "/scenes/verify_exposure.blend"

        fleet = StubFleet([idle_worker()])
        fleet.scene_path = Path(a)
        fleet.reload_cost = 90.0          # round trip 180 s
        b = stub_broker(tmp, fleet)

        # Seed a mean render time: 6 s stills, like the measured pair.
        for _ in range(3):
            jid = b.db.submit(spec(), agent="showlight", scene=a)
            b.db.claim(60.0)
            b.db.finish(jid, str(tmp / "x.png"), 6.0, size=1000)

        # 7 jobs x 6 s = 42 s to drain, against a 180 s round trip. Both
        # competing jobs have waited far past the 300 s floor.
        for _ in range(7):
            b.db.submit(spec(), agent="showlight", scene=a)
        old = b.db.submit(spec(), agent="showlight", scene=other)
        b.db.conn.execute("UPDATE jobs SET created=? WHERE id=?",
                          (time.time() - 2400, old))
        b.db.conn.commit()

        served = []
        for _ in range(7):
            job = b.next_job()
            if job is None:
                break
            served.append(job["scene"])
            fleet.scene_path = Path(job["scene"])
        check("a scene finishable inside the round trip is drained, not traded",
              served == [a] * 7, f"{len(served)} job(s), {set(served)}")

        job = b.next_job()
        check("and the waiting scene is served the moment it is done",
              job is not None and job["scene"] == other, str(job and job["scene"]))

        # The escape hatch stays open: work too big to finish inside the round
        # trip must still yield, or this becomes the starvation it replaced.
        fleet.scene_path = Path(a)
        for _ in range(200):
            b.db.submit(spec(), agent="showlight", scene=a)
        older = b.db.submit(spec(), agent="crowd", scene=other)
        b.db.conn.execute("UPDATE jobs SET created=? WHERE id=?",
                          (time.time() - 3000, older))
        b.db.conn.commit()
        job = b.next_job()
        check("a scene too big to finish inside the round trip still yields",
              job is not None and job["scene"] == other, str(job and job["scene"]))


def test_queued_scenes_are_evicted_last_not_first() -> None:
    """Eviction must not delete the scene the queue is about to need.

    LRU gets this backwards on its own: a scene's stamp is written when it is
    SELECTED, so one with sixteen jobs merely waiting still carries the oldest
    possible timestamp and sorts ahead of an idle scene finished with an hour
    ago. A 602 MB scene survived an eight-scene eviction by luck once already.

    Deferring, not pinning: "has queued work" can cover the whole cache, and an
    unevictable cache turns a policy ceiling into a refused job. Physics wins.
    """
    calls: list[str] = []

    def fake_run(ep, cmd, **kw):
        calls.append(cmd)
        return remote.Ran(cmd=cmd, rc=0, out="", err="", elapsed=0.0, where="stub")

    def entry(digest: str, used_at: float, gb: float):
        return remote.SceneEntry(digest=digest, bytes=int(gb * 1e9), used_at=used_at)

    # `wanted` is the LEAST recently used — exactly the case LRU gets wrong.
    before = remote.DiskState(
        ok=True, total=int(32e9), free=int(24e9), used=int(8e9),
        scenes=(entry("wanted", 100.0, 4.5), entry("idle_a", 200.0, 0.8),
                entry("idle_b", 300.0, 0.7)))

    saved_run, saved_disk = remote.run, remote.disk_state
    remote.run = fake_run
    remote.disk_state = lambda ep, **kw: before
    try:
        ep = remote.Endpoint(host="stub", port=1, instance_id=1)
        # 3.0 G incoming against an 8 G budget and 6 G cached needs 1 G freed —
        # enough that something must go, not enough that everything must.
        asked = []

        def demand() -> set[str]:
            asked.append(1)
            return {"wanted"}

        report = remote.evict_to_fit(ep, keep=set(), incoming=int(3.0e9),
                                     budget=int(8e9), reserve=int(2e9),
                                     state=before, defer=demand)
        gone = {e.digest for e in report.evicted}
        check("a scene with jobs queued is not evicted ahead of idle ones",
              gone == {"idle_a", "idle_b"}, f"evicted {sorted(gone)}")

        # Physics still outranks the preference: when the idle scenes are not
        # enough, the wanted one goes rather than the disk filling.
        report = remote.evict_to_fit(ep, keep=set(), incoming=int(5.5e9),
                                     budget=int(8e9), reserve=int(2e9),
                                     state=before, defer=demand)
        check("a deferred scene is still evictable when nothing else fits",
              "wanted" in {e.digest for e in report.evicted},
              f"evicted {sorted(e.digest for e in report.evicted)}")

        # Answering the question costs a content hash per queued scene — 31.3 s
        # over 9.67 GB, measured on the live queue. A preflight that evicts
        # nothing must not pay it.
        asked.clear()
        report = remote.evict_to_fit(ep, keep=set(), incoming=int(1.0e9),
                                     budget=int(8e9), reserve=int(2e9),
                                     state=before, defer=demand)
        check("a preflight that evicts nothing never asks what is queued",
              not report.evicted and not asked,
              f"evicted {len(report.evicted)}, asked {len(asked)}x")
    finally:
        remote.run, remote.disk_state = saved_run, saved_disk


def test_drain_grace_holds_a_scene_for_a_serial_client() -> None:
    """A scene that drains for a second must not cost a scene switch.

    Every client here is serial — `r5090` blocks until its render returns — so
    the next camera of a sweep is submitted just AFTER the previous one lands.
    The dispatcher used to give the scene up inside that gap; on 2026-08-02 that
    put a sweep's last camera behind a 4.2 GB upload it had no need to wait for.
    """
    slow, app.config.SCENE_DRAIN_GRACE_SEC = app.config.SCENE_DRAIN_GRACE_SEC, 3.0
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            mine, theirs = "/scenes/big.blend", "/scenes/other.blend"

            # 1. Nothing else is queued, so no switch is coming and there is
            #    nothing to buy by waiting. Must return at once, not in 3 s.
            fleet = StubFleet([idle_worker()])
            fleet.scene_path = Path(mine)
            b = stub_broker(tmp, fleet)
            began = time.time()
            held = b.settle(mine, 60.0)
            check("no competing scene -> settle returns immediately",
                  held is None and time.time() - began < 1.0,
                  f"{time.time() - began:.2f}s")

            # 2. Another scene is waiting, and a job for the LOADED scene lands
            #    mid-grace. That is the serial client, and it must be served
            #    without paying for a switch.
            b.db.submit(spec(), agent="other", scene=theirs)
            late = threading.Thread(
                target=lambda: (time.sleep(0.6),
                                b.db.submit(spec("CAM_LATE"), agent="me", scene=mine)))
            late.start()
            began = time.time()
            held = b.settle(mine, 60.0)
            late.join()
            check("a job arriving mid-grace is served, no scene switch paid",
                  held is not None and held["scene"] == mine,
                  f"{held and held['scene']} after {time.time() - began:.2f}s")

            # 3. The loaded scene really is finished. The grace must EXPIRE and
            #    hand the other scene its turn — a hold that never lets go is
            #    just starvation with a nicer name.
            began = time.time()
            held = b.settle(mine, 60.0)
            waited = time.time() - began
            check("a genuinely drained scene is given up when the grace ends",
                  held is None and waited >= 2.5, f"waited {waited:.2f}s")

            # 4. Fairness outranks the grace: a scene already past the
            #    starvation line ends the wait rather than extending its wait.
            app.config.SCENE_STARVE_SEC, keep = -1.0, app.config.SCENE_STARVE_SEC
            try:
                began = time.time()
                held = b.settle(mine, 60.0)
                check("a starving scene cuts the grace short",
                      held is None and time.time() - began < 1.5,
                      f"{time.time() - began:.2f}s")
            finally:
                app.config.SCENE_STARVE_SEC = keep
    finally:
        app.config.SCENE_DRAIN_GRACE_SEC = slow


def test_scene_zstd_level_never_recompresses_a_compressed_scene() -> None:
    """The level is chosen per scene, and an already-compressed one gets 1.

    `world/items/spectator_crowd_test.blend` was saved with Blender's "Compress
    File" on, so it arrived as a 602 MB zstd frame. At the old hardcoded -19
    that cost 59 s of a 4-core box to achieve 1.06x — while the GPU it was
    bound for sat idle. The broker cannot stop a scene arriving that way; it
    can stop paying for it.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        body = bytes(range(256)) * 8192          # 2 MB, highly compressible

        plain = tmp / "plain.blend"
        plain.write_bytes(b"BLENDER-v502" + body)
        level, why = remote.scene_zstd_level(plain)
        check("a small uncompressed scene gets the configured level",
              level == config.SCENE_ZSTD_LEVEL, f"-{level} ({why})")

        squashed = tmp / "squashed.blend"
        squashed.write_bytes(subprocess.run(
            ["zstd", "-3", "-c"], input=body, stdout=subprocess.PIPE).stdout)
        level, why = remote.scene_zstd_level(squashed)
        check("a zstd-framed .blend is not re-compressed",
              level == config.SCENE_ZSTD_LEVEL_PRECOMPRESSED
              and "Compress File" in why, f"-{level} ({why})")

        # Detected by CONTENT, not just by the magic number: a .blend whose
        # bulk is packed EXR/PNG is an uncompressed container full of
        # incompressible bytes, and -19 buys nothing there either.
        packed = tmp / "packed.blend"
        packed.write_bytes(b"BLENDER-v502" + os.urandom(
            int(config.SCENE_ZSTD_PROBE_MIN_MB * 1e6) + (8 << 20)))
        level, why = remote.scene_zstd_level(packed)
        check("a large scene of incompressible bytes probes down to level 1",
              level == config.SCENE_ZSTD_LEVEL_PRECOMPRESSED and "probes" in why,
              f"-{level} ({why})")

        # A level is a performance choice. Nothing here may ever fail a push.
        level, why = remote.scene_zstd_level(tmp / "does-not-exist.blend")
        check("an unreadable scene falls back instead of raising",
              level == config.SCENE_ZSTD_LEVEL, f"-{level} ({why})")


OFFLINE_TESTS = (
    "test_the_pixel_cap_counts_what_is_actually_rendered",
    "test_a_refusal_is_never_retried",
    "test_preemption_must_beat_the_switch_it_costs",
    "test_a_scene_you_can_finish_is_not_yielded",
    "test_queued_scenes_are_evicted_last_not_first",
    "test_drain_grace_holds_a_scene_for_a_serial_client",
    "test_scene_zstd_level_never_recompresses_a_compressed_scene",
    "test_a_stalled_transport_is_condemned_and_a_progressing_one_is_not",
    "test_a_vanished_instance_is_forgotten_not_retried",
    "test_reconcile_never_stalls_the_heartbeat_thread",
    "test_push_falls_back_to_one_stream_and_reports_chronic",
    "test_unreadable_resume_state_never_deletes_the_parts",
    "test_db", "test_seq_verification",
    "test_imgstat_classifies_what_is_in_the_image",
    "test_imgstat_decoders_agree",
    "test_blank_frame_is_never_a_delivered_frame",
    "test_blank_verdict_recorded_at_delivery_blocks_the_cheap_resume",
    "test_sequence_outliers_find_the_buried_frame",
    "test_blank_still_fails_terminally_unless_allowed",
    "test_seq_resume", "test_edited_in_place_frame_is_not_a_delivered_frame",
    "test_a_bake_travels_only_with_the_blend_that_owns_it",
    "test_local_disk_preflight_says_where_the_batch_stops",
    "test_keep_on_exit_decides_whether_shutdown_destroys",
    "test_seq_names_and_ranges", "test_remote_call_signatures",
    "test_scene_cache_is_evicted_by_use_and_verified",
    "test_disk_state_refuses_to_invent_numbers",
    "test_blender_bundle_is_dropped_only_once_the_install_works",
    "test_disk_full_fails_the_job_and_never_the_gpu",
    "test_busy_dispatch",
    "test_claim_reports_true_attempts",
    "test_retry_gate_survives_refunding_requeue",
    "test_retry_collects_or_reattaches_never_refetches_midwrite",
    "test_collect_verifies_before_deleting_remote",
    "test_finished_png_info_requires_stable_size",
    "test_transport_failure_never_destroys_blind",
    "test_ssh_contact_is_not_evidence_of_a_render",
    "test_local_tunnel_failure_never_condemns_the_host",
    "test_frame_lists_round_trip",
    "test_cost_projection_names_its_basis",
    "test_absent_progress_json_is_idle_not_unknown",
    "test_ssh_auth_rejection_is_not_transport",
    "test_hibernate_refuses_unknown",
    "test_resume_abandon_destroys_stopped_instance",
    "test_unconfirmed_destroy_is_reaped",
    "test_paused_broker_still_winds_down",
    "test_wait_does_not_hold_the_fleet_lock",
    "test_thread_supervision", "test_jobs_survive_a_restart",
    "test_exec_queue_and_bundles",
)


def run_offline() -> int:
    """Every test that needs no broker, no GPU and no network. Safe to run
    while a live broker is serving on 8760."""
    for name in OFFLINE_TESTS:
        globals()[name]()
    return report()


def main() -> int:
    for name in OFFLINE_TESTS:
        globals()[name]()
    test_http()
    return report()


if __name__ == "__main__":
    sys.exit(main())
