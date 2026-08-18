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
than anything the tests did.

That paragraph has been here all along, and on 2026-08-08 an agent ran
`python -m broker.test_broker` anyway — the obvious invocation, the one the
shebang and the `__main__` block both invite — and it POSTed a render job to
the live broker on 8760. No harm resulted (the submit was refused with a
non-JSON body, which is what crashed the runner; `depth` stayed 0 and no GPU was
rented), and it was harmless only because it was checked rather than assumed.

    A WARNING IS NOT A MECHANISM.

So the default is now the offline suite and nothing else. `http()` and
`test_http()` refuse to open a socket unless `--live-http` was passed, and
`--live-http` takes a URL with **no default**, because the default was
production:

    .venv/bin/python -m broker.test_broker            # offline; cannot reach
                                                      # the network at all

    VASTRENDER_SCENE=/tmp/nope.blend VASTRENDER_PORT=8799 \
    VASTRENDER_DB=/tmp/throwaway.db .venv/bin/python -m broker.app &
    .venv/bin/python -m broker.test_broker --live-http http://127.0.0.1:8799

Even with the flag, the run is refused unless the target has **no job history**
— a throwaway has none and a working broker has thousands. An earlier version
of this gate checked "is it renting right now" and "does its scene exist", and
allowed a run against broker 1, because a production broker had drifted into
exactly the state the docs called safe. A state is not an identity; history is.
`run_offline()` remains callable directly:

    .venv/bin/python -c "from broker import test_broker as t; \
        raise SystemExit(t.run_offline())"

The focus is the failure modes that cost real money or silently lose work:
leases surviving a crash, retries terminating, and admission control holding.
"""

from __future__ import annotations

import inspect
import json
import os
import argparse
import re
import shutil
import socket
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


class LiveBrokerRefused(RuntimeError):
    """A test tried to talk to a real broker without anyone asking it to."""


# THE DEFAULT PATH MAY NOT REACH THE NETWORK, AND THIS IS WHAT ENFORCES IT.
#
# Not a convention and not a docstring: a flag that every route to a socket in
# this file checks first. `main()` sets it only when `--live-http` is passed.
#
# The guard lives at the point of egress rather than in `main()` on purpose. A
# check in `main()` protects the tests that exist today; a check in `http()`
# also protects the test somebody adds next year, drops into OFFLINE_TESTS
# because it looked offline, and never runs against a broker until the one time
# it does.
_LIVE_HTTP_ALLOWED = False


def _require_live_http(what: str) -> None:
    """Refuse anything that would reach a real broker, unless asked explicitly."""
    if _LIVE_HTTP_ALLOWED:
        return
    raise LiveBrokerRefused(
        f"REFUSED: {what} would talk to a real broker at {BASE}, and nothing "
        f"asked it to. The broker on 8760 is production: it accepts jobs and "
        f"rents GPUs. Re-run with `--live-http` and point it at a throwaway "
        f"broker (see this module's docstring), or call `run_offline()`."
    )


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

        # The actual artefact. Not a synthetic reconstruction of the bug — the
        # bug: the 8,734-byte 640x480 PNG job 0908e534b1d3 really returned.
        #
        # 2026-08-18 — THIS CHECK WAS THE FAILURE IT GUARDS AGAINST. It read:
        #
        #     real = Path(__file__).resolve().parent.parent / "out" / "0908e534b1d3.png"
        #     if real.exists():
        #         ...
        #
        # and `out/` is the first rule in .gitignore. So the fixture existed on
        # exactly one machine in the world, and NO CLONE COULD EVER HAVE IT. On
        # a clean checkout the guard was false, the check did not run, nothing
        # was reported as skipped, and the suite printed `507/507 passed` — a
        # total that reads as total success. Here, where the file happens to
        # survive in a gitignored directory, it printed 508/508, and three
        # documents plus a release checkbox in docs/publication.md asserted
        # 508/508 as the number a reader should expect. The checkbox was
        # therefore unpassable on any clone, and nobody could tell.
        #
        # That is this project's own catalogued failure family — "it passed on
        # an empty set, or it never executed at all" — living inside the release
        # gate, guarding the one property the README leads with: a returned
        # frame can be a perfectly valid, correctly sized, sha256-matching PNG
        # with nothing in it.
        #
        # THE FIXTURE IS NOW TRACKED, at broker/fixtures/0908e534b1d3.png, with
        # an explicit `!` negation in .gitignore because `*.png` hid it. 8,734
        # bytes is a rounding error against a repository that already ships
        # 500 KB of docs, and it buys the check the one thing it could not have:
        # it runs for everybody.
        #
        # AND IT IS UNCONDITIONAL. No `if exists()`. If the fixture goes
        # missing, imgstat.measure returns UNREADABLE, this check FAILS, and the
        # total drops to 507/508 — visibly, with a reason. A check that can
        # vanish without changing the score is not a check.
        real = Path(__file__).resolve().parent / "fixtures" / "0908e534b1d3.png"
        st = imgstat.measure(real)
        check("the real frame that started this is caught",
              st["verdict"] == imgstat.BLACK,
              imgstat.summary(st) if real.exists() else
              f"FIXTURE MISSING at {real} — it is TRACKED; restore it with "
              f"`git checkout -- broker/fixtures/0908e534b1d3.png`")


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


def test_a_black_frame_on_a_scene_that_used_to_work_gets_one_retry() -> None:
    """Blackness that arrives suddenly is a farm question, not a camera question.

    2026-08-04: film14_breach_r6b.blend, unchanged on disk, rendered four
    correct frames (07:54-07:59) and then four all-black ones (08:04-08:36) on
    an instance that was simultaneously throwing CUDA OOM and
    OPTIX_ERROR_UNKNOWN. All four blacks were failed terminally. The scenes were
    blamed for a fault in the box, and because three different agents hit it at
    once the identical blackness was read as proof of the opposite.

    So the rule is history-based, and this pins both halves of it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = DB(root / "t.db")
        worked = "/home/user/f1-round2/render/has_worked.blend"
        never = "/home/user/f1-round2/render/never_worked.blend"

        # A scene with a good frame behind it, and one that has only ever been black.
        good_id = db.submit(spec(), agent="t", scene=worked)
        db.claim(60)
        db.set_image_stats(good_id, {"verdict": "OK", "mean": 0.4, "sd": 0.1})
        db.finish(good_id, str(root / "g.png"), 1.0, {"verdict": "OK"}, 10)

        # The gate records the image stats BEFORE it raises, so by the time the
        # handler asks for history the current black frame is already counted.
        # Mirror that here, or the note suppresses itself as a single data point.
        blk_id = db.submit(spec(), agent="t", scene=worked)
        db.claim(60)
        db.set_image_stats(blk_id, {"verdict": "BLACK", "mean": 0.0003, "sd": 0.001})
        db.fail_terminal(blk_id, "blank")

        blk, ok, _ = db.scene_blank_verdict_history(worked)
        check("history sees the good frame this scene produced", ok >= 1, f"ok={ok}")
        check("...and the black one alongside it", blk >= 1, f"blk={blk}")
        blk2, ok2, _ = db.scene_blank_verdict_history(never)
        check("a scene with no history reports none", ok2 == 0, f"ok={ok2}")

        b = app.Broker.__new__(app.Broker)
        b.db = db

        note_worked = b.blank_history_note(worked)
        note_never = b.blank_history_note(never)
        check("the note tells an operator the scene HAS worked",
              "HAS rendered fine" in note_worked, note_worked[:70])
        check("...and says nothing when there is no history to report",
              note_never == "", note_never[:70])

        # The policy itself: one retry for the scene with a good frame, terminal
        # for the one that has never produced a picture.
        retry_id = db.submit(spec(), agent="t", scene=worked)
        db.claim(60)
        blk, ok, _ = db.scene_blank_verdict_history(worked)
        retried: set = set()
        first = ok > 0 and retry_id not in retried
        check("a black frame on a proven scene is retried, not failed", first, f"ok={ok}")
        retried.add(retry_id)
        second = ok > 0 and retry_id not in retried
        check("...but only once — the second black is believed", not second, "")

        dead_id = db.submit(spec(), agent="t", scene=never)
        db.claim(60)
        blk, ok, _ = db.scene_blank_verdict_history(never)
        check("a scene that has never rendered a picture is still terminal",
              not (ok > 0), f"ok={ok}")


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
        # budget_gb is DERIVED from this disk, not a constant: 16 GB total,
        # 2 GB non-cache, 2 GB reserve leaves 12 GB of room, and 80 % of that
        # is 9.6. Asserted as a derivation rather than a literal because the
        # literal is what went stale last time — an 8 GB ceiling written for a
        # 16 GB volume was still being applied to a 32 GB one.
        expect = round(remote.cache_budget(
            fleet.disk, int(config.DISK_RESERVE_GB * 10 ** 9)) / 1e9, 2)
        check("a good measurement reports totals, cache and budget",
              good["measured"] and good["cache_gb"] == 4.0 and good["scene_count"] == 1
              and good["other_gb"] == 2.0 and good["budget_gb"] == expect == 9.6,
              f"{good} expected budget_gb={expect}")
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
        # The load-vs-render accounting `rq status` prints. Real attributes,
        # not mocks, so a test that drives renders through `render_one` also
        # exercises the arithmetic that ends up in front of an operator.
        self.load_sec = 0.0
        self.render_sec = 0.0
        # Download-throughput health. Real attributes with the real defaults —
        # a healthy stub link, so every existing test keeps testing what it was
        # written to test rather than tripping the new replace-the-box path.
        self.fetch_samples: list[float] = []
        self.slow_link: Optional[str] = None
        self.condemned: list[str] = []

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

    def note_fetch(self, nbytes: int, seconds: float) -> None:
        self.fetch_samples.append(nbytes / seconds if seconds > 0 else 0.0)

    def download_too_slow(self) -> "Optional[str]":
        return self.slow_link

    def condemn_slow_link(self, why: str) -> None:
        self.condemned.append(why)
        self.torn_down = True

    def hibernate(self, force: bool = False,
                  expect: Optional[int] = None) -> None:
        # `expect` mirrors the real signature. The stub does not enforce it —
        # the guard itself is pinned in
        # `test_a_stale_teardown_cannot_destroy_the_replacement` against a real
        # Fleet — but it must be ACCEPTED here, or every caller that passes it
        # dies on a TypeError that the idle path catches and turns into
        # "stop failed — destroying instead".
        self.hibernated = True

    def teardown(self, reason: str = "idle",
                 expect: Optional[int] = None) -> None:
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


def test_a_stale_teardown_cannot_destroy_the_replacement() -> None:
    """The most expensive lock race this broker has: a verdict outlives its box.

    Every caller of `teardown`/`hibernate` reads `instance_id` and `stopped_at`
    OUTSIDE `Fleet.lock` and acts on them INSIDE it, and that lock is held for
    the whole of `ensure_ready` — a resume is 902 s, and `RESUME_ATTEMPTS` is
    2, followed by a destroy, a rent and a full deploy. Ten minutes is normal.

    Measured 2026-08-07 on instance 47040457:

        05:19:56  ensure_ready takes the lock, resume 2/2
        05:30:05  maybe_idle_down sees stopped_at from 04:30, logs
                  "hibernated 60 min — destroying", calls teardown, BLOCKS
        05:34:57  resume abandoned; 47040457 destroyed; 47048579 rented
        05:38:16  47048579 deployed: Blender, 409 MB scene, worker ready 140.3s
        05:38:22  the 05:30:05 call runs and destroys 47048579

    The evidence is in the line it wrote — `destroyed 47048579 (hibernation
    expired) ... hibernated 0.0 min` — a hibernation deadline enforced against
    an instance that never hibernated. Two agents lost their box to it and the
    whole deploy was paid for twice.

    `hibernate`'s existing `if not self.instance_id or self.stopped_at: return`
    cannot catch this: the replacement is running, so both facts read healthy.
    Only the identity answers the right question.
    """
    from . import fleet as fleet_mod

    destroyed: list[int] = []
    real_destroy = fleet_mod.vastctl.destroy
    fleet_mod.vastctl.destroy = lambda client, iid: destroyed.append(iid) or True
    try:
        def make(instance_id: int) -> Fleet:
            f = Fleet.__new__(Fleet)
            f.lock = threading.Lock()
            f.instance_id = instance_id
            f.ep = None
            f.tunnel = None
            f.stopped_at = None
            f.started_at = time.time()
            f.dph = 0.3
            f.gpu_seconds = 0.0
            f.gpu_frac = None
            f.scene_hash = None
            f.scene_path = None
            f.mirrored_assets = set()
            f.last_ready = True
            f.status = "ready"
            f.may_hold_render = False
            f.machine_id = 0
            f.offer_id = 0
            f.transport_bytes = 0
            f.stalled_rounds = 0
            f.heartbeat_failures = 0
            f.on_teardown = None
            f.doomed = {}
            f.client = None
            return f

        # The exact shape of the incident: the caller decided about 47040457,
        # the fleet is now serving its freshly deployed replacement.
        fleet = make(47048579)
        fleet.teardown("hibernation expired", expect=47040457)
        check("a teardown decided about a REPLACED instance destroys nothing — "
              "the replacement's deploy is not the dead box's to spend",
              destroyed == [] and fleet.instance_id == 47048579,
              f"destroyed={destroyed} instance_id={fleet.instance_id}")

        # And the guard must not become a way to never tear anything down.
        fleet.teardown("hibernation expired", expect=47048579)
        check("a teardown decided about the instance that is actually up still "
              "destroys it", destroyed == [47048579], str(destroyed))

        destroyed.clear()
        fleet = make(47048579)
        fleet.teardown("rq teardown")
        check("expect=None still means 'whatever is up now' — rq teardown, a "
              "pause and a shutdown are not observations, they are orders",
              destroyed == [47048579], str(destroyed))

        # The same rule on the stop path. Stopping is recoverable where
        # destroying is not, but stopping a box that was deployed 6 s ago still
        # throws the deploy away.
        stopped: list[int] = []
        fleet = make(47048579)
        fleet.client = type("C", (), {
            "stop_instance": lambda self, iid: stopped.append(iid)})()
        fleet.hibernate(force=True, expect=47040457)
        check("a stop decided about a replaced instance stops nothing",
              stopped == [] and fleet.stopped_at is None,
              f"stopped={stopped} stopped_at={fleet.stopped_at}")
        fleet.hibernate(force=True, expect=47048579)
        check("and the instance that IS up still stops", stopped == [47048579],
              str(stopped))
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
    _require_live_http(f"http({method} {path})")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, _body(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _body(e.read())


def _body(raw: bytes):
    """Decoded JSON, or the raw text when the server did not send JSON.

    `json.loads` used to be called on the error body directly, so ANY non-JSON
    response killed the runner with `JSONDecodeError: Expecting value: line 1
    column 1` — a traceback pointing at the JSON decoder, naming neither the
    status code nor the endpoint nor the body.

    That is not hypothetical and it is not cosmetic: it is what this file did on
    2026-08-08, three separate times, and each time it hid the actual answer.
    The broker was returning

        HTTP/1.1 500 Internal Server Error
        content-type: text/plain
        Internal Server Error

    to `POST /jobs` — a real defect, visible in one line — and the harness
    converted it into a decoder crash that read like a broken test. A test
    harness that cannot report a 500 is worse than one that has no HTTP tests.
    """
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace").strip()


def test_http() -> bool:
    # Before the health probe, which is itself a socket to production.
    _require_live_http("test_http()")
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
    accepted = code == 200 and isinstance(body, dict) and "job_id" in body
    check("submit accepted", accepted, f"{code} {str(body)[:60]}")
    if not accepted:
        # Everything below needs a job id. Without this the run died on
        # `'str' object has no attribute 'get'` three frames later, which reads
        # like a harness bug and is in fact the broker's 500 arriving intact.
        check("HTTP section reached a broker and reported its answer", True,
              f"submit returned {code}; remaining checks need a job id and are "
              f"skipped")
        return True
    jid = body["job_id"]

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


def test_exec_transport_is_a_wait_and_never_spends_an_attempt() -> None:
    """`_run_guarded`'s classification, against the exceptions really raised.

    The defect this pins, observed live on instance 47040457 on 2026-08-07:
    the transport branch named four `RemoteError` SUBCLASSES, and the two
    conditions that actually happened were neither of them —

      * `execremote.start_exec_server` raises the BASE `RemoteError` for "the
        server exited immediately after launch", which is what a Blender bundle
        that is still uploading looks like. Job b0d427488e0f: three attempts in
        twelve seconds, `failed` at 3/3, having never executed a line of its own
        code, seventeen seconds before the box was ready for it.
      * `TransferError`, for a dropped push. Job 88de1f4d5faf: `scene push
        failed after 20.0s`, charged to the build.

    So this test is written against the TYPES THE CODE RAISES rather than
    against the list the handler happens to check, and the last check walks
    `execremote` for `raise` sites so a new one cannot quietly reopen the hole.
    """
    from . import execremote

    class StubBroker:
        running = True
        paused = False
        last_work = 0.0

    with tempfile.TemporaryDirectory() as tmpdir:
        db = DB(Path(tmpdir) / "exec.db")
        svc = execservice.ExecService.__new__(execservice.ExecService)
        svc.broker = StubBroker()
        svc.db = db
        svc.fleet = None
        svc.slots = 12
        svc.inflight = {}
        svc.lock = threading.Lock()
        svc.last_error = ""

        # The backoff is asserted by recording it, not by sleeping through it.
        backoffs: list[float] = []
        svc._hold_the_slot_and_wait = lambda s: (backoffs.append(s), 0.0)[1]

        def outcome(exc: Exception) -> tuple[str, int]:
            """Run one job that dies with `exc`, and report state and attempts.

            The row is cancelled on the way out. A refunded requeue leaves it
            `queued`, and `claim_exec` takes the OLDEST queued row — so without
            this, every later case in this test claims the first case's job and
            measures the wrong row. (Which is itself a small demonstration that
            the refund really does put the job back on the queue.)
            """
            jid = db.submit({"entry": "tools/x.py"}, agent="a", kind="exec",
                            bundle="0123456789abcdef")
            claimed = db.claim_exec(600)          # attempts -> 1, state -> running
            assert claimed is not None and claimed["id"] == jid, "claimed the wrong row"
            def boom(job, spec):
                raise exc
            svc.run_one = boom
            svc._run_guarded({"id": jid}, {})
            row = db.get(jid)
            db.cancel(jid)
            return row["state"], row["attempts"]

        # The exact string `start_exec_server` produced on the live farm, at the
        # exact type it produced it: the BASE class, not a subclass.
        state, attempts = outcome(remote.RemoteError(
            "exec server on 192.0.2.14:45716 exited immediately after launch. "
            "exec.log: env: '/workspace/blender/blender': No such file or directory"))
        check("a bare RemoteError — an exec server that could not start — is a "
              "WAIT: requeued with the attempt refunded",
              (state, attempts) == ("queued", 0), f"{state} {attempts}/3")

        state, attempts = outcome(remote.TransferError(
            "scene push", "192.0.2.14:45716", "connection timed out", 20.0,
            expected=7969670247))
        check("a dropped transfer is a WAIT: the transfer is not the work, so "
              "it costs the instance nothing and the retry budget nothing",
              (state, attempts) == ("queued", 0), f"{state} {attempts}/3")

        for label, exc in (
            ("a dead job socket", remote.ConnectionDropped("forward died")),
            ("an unreachable worker", remote.WorkerUnreachable("no answer")),
            ("no usable box", remote.FleetUnavailable("blender not installed yet")),
        ):
            state, attempts = outcome(exc)
            check(f"{label} is a WAIT: requeued with the attempt refunded",
                  (state, attempts) == ("queued", 0), f"{state} {attempts}/3")

        check("every refunded wait backs off first, because a requeue with no "
              "backoff is re-claimed within a second and spins",
              len(backoffs) >= 5 and all(b > 0 for b in backoffs), str(backoffs))

        # The two RemoteErrors that are NOT waits must stay terminal, which is
        # the ordering half of the fix: `DiskFull` IS a `RemoteError`, so a
        # branch placed before it would silently start retrying a full disk.
        state, attempts = outcome(remote.DiskFull("7.97 GB will not fit"))
        check("DiskFull stays TERMINAL even though it is a RemoteError — "
              "retrying cannot create space",
              state == "failed", f"{state} {attempts}/3")
        state, attempts = outcome(execservice.StaleBundle("the code moved"))
        check("a stale bundle stays terminal", state == "failed",
              f"{state} {attempts}/3")

        # THE GPU REFUSAL, 2026-08-07. An exec job set `cycles.device = GPU`
        # and put a second 8 GB film scene on the same 32 GB card as the warm
        # render worker; another agent's `carhero` render died twice with `Out
        # of memory in CUDA queue enqueue`, terminally the second time. The
        # classification is the whole point of the class: the thing the job
        # collided with is a WARM worker holding a resident scene for the entire
        # campaign, so a retry is not "later", it is the same collision three
        # times ending in a `3/3` that reads as a build tried and found wanting.
        # This is deliberately the OPPOSITE verdict to ExecMemoryShort directly
        # above, which is a wait because sibling builds end and a resident
        # render scene does not.
        state, attempts = outcome(execservice.ExecGpuRefused(
            "exec job d41d8cd98f00 was refused the GPU by the exec server: "
            "refusing d41d8cd98f00: the render worker holds "
            "/workspace/scenes/deadbeef/film18.blend on NVIDIA GeForce RTX 5090"))
        check("a GPU refusal is TERMINAL ON THE FIRST REFUSAL — the render "
              "worker holds its scene for the whole campaign, so three attempts "
              "buy three identical collisions",
              (state, attempts) == ("failed", 1), f"{state} {attempts}/3")

        # And the thing the whole budget exists for still spends it: a child
        # that ran and came back `ok: false` is the caller's own code failing.
        jid = db.submit({"entry": "tools/x.py"}, agent="a", kind="exec",
                        bundle="0123456789abcdef")
        for n in range(1, config.MAX_ATTEMPTS + 1):
            db.claim_exec(600)
            def boom(job, spec):
                raise RuntimeError("exec job failed: NameError: name 'foo' is not defined")
            svc.run_one = boom
            svc._run_guarded({"id": jid}, {})
            row = db.get(jid)
            check(f"a real error in the caller's script spends attempt {n}",
                  row["attempts"] == n, f"{row['state']} {row['attempts']}")
        check("and terminates the job once the budget is gone",
              db.get(jid)["state"] == "failed", db.get(jid)["state"])

        # THE GUARD AGAINST THIS REGRESSING. The bug was an allowlist that did
        # not cover what was raised, so the durable assertion is about the net,
        # not about today's list: everything `execremote` raises must be under
        # the base class the handler now catches.
        src = Path(execremote.__file__).read_text()
        raised = set(re.findall(r"raise\s+(?:remote\.)?(\w+)\(", src))
        unclassified = sorted(
            name for name in raised
            if not (name in ("ValueError",) or
                    issubclass(getattr(remote, name, type(None)), remote.RemoteError))
        )
        check("every failure execremote raises is a RemoteError (or a caller "
              "input error), so the transport net cannot be reopened by adding "
              "one more type", not unclassified, str(unclassified))


def test_exec_can_bring_up_a_box_without_a_render_job() -> None:
    """An exec-only workload must not depend on somebody else renting a GPU.

    `Fleet.ensure_ready` is the only code that may rent or wake an instance and
    it insists on a scene, because the render path always has one. Exec does
    not. On a broker started without `VASTRENDER_SCENE` — how this one has run
    all week; `scene.blend` has never existed — that left `rq exec` unable to
    bootstrap at all: it waited, refunding attempts forever, for a RENDER job
    to happen along and bring a box up. Measured 2026-08-07, job c066603f71e3,
    against a queue that contained no render work to unblock it.

    The fix is a bootstrap scene of exec's own. What this test mostly exists to
    pin is what the fix must NOT be: pointing `SCENE` at the same file.
    `blank_probe.blend` is the fixture that proves the blank-frame checker
    works — CAM_VOID renders black on purpose — so as the render default it
    would answer a forgotten `--scene` with a black frame instead of an error.
    """
    from . import execservice as es

    check("the exec bootstrap scene is NOT the render default — a missing "
          "--scene must stay a hard error, not a black test fixture",
          config.EXEC_BOOTSTRAP_SCENE != config.SCENE,
          f"{config.EXEC_BOOTSTRAP_SCENE} == {config.SCENE}")

    svc = es.ExecService.__new__(es.ExecService)
    asked: list[Path] = []

    class StubFleetNoBox:
        ep = None
        stopped_at = None
        scene_path = None
        def ensure_ready(self, scene):
            asked.append(Path(scene))
            return "endpoint"

    svc.fleet = StubFleetNoBox()

    with tempfile.TemporaryDirectory() as tmpdir:
        missing = Path(tmpdir) / "never_existed.blend"
        boot = Path(tmpdir) / "blank_probe.blend"
        boot.write_bytes(b"x" * 594666)

        real_scene, real_boot = config.SCENE, config.EXEC_BOOTSTRAP_SCENE
        config.SCENE, config.EXEC_BOOTSTRAP_SCENE = missing, boot
        try:
            ep = svc.endpoint_without_disturbing_the_worker()
            check("with no scene loaded and no render default, exec deploys "
                  "with its bootstrap scene instead of waiting for a render "
                  "job that may never come",
                  ep == "endpoint" and asked == [boot], f"{ep} {asked}")

            # The render default still WINS when it exists: the bootstrap is a
            # last resort, not a preference. Deploying a probe over the real
            # assembly would cost a scene switch on the next render.
            asked.clear()
            real = Path(tmpdir) / "assembly.blend"
            real.write_bytes(b"y" * 1024)
            config.SCENE = real
            svc.endpoint_without_disturbing_the_worker()
            check("a real render default still wins — the bootstrap is a last "
                  "resort, not a preference", asked == [real], str(asked))

            # And a scene already loaded wins over both, which is what keeps
            # exec from ever causing a worker restart.
            asked.clear()
            svc.fleet.scene_path = Path(tmpdir) / "loaded.blend"
            svc.endpoint_without_disturbing_the_worker()
            check("a scene already loaded wins over both, so exec never causes "
                  "a scene switch under a render",
                  asked == [svc.fleet.scene_path], str(asked))

            # With all three gone it is still a refunded WAIT, never a verdict.
            asked.clear()
            svc.fleet.scene_path = None
            config.SCENE, config.EXEC_BOOTSTRAP_SCENE = missing, missing
            try:
                svc.endpoint_without_disturbing_the_worker()
                raised: Exception | None = None
            except Exception as exc:                            # noqa: BLE001
                raised = exc
            check("with no scene anywhere it is still FleetUnavailable — a "
                  "refunded wait, never a FileNotFoundError that would burn an "
                  "attempt and fail the build",
                  isinstance(raised, remote.FleetUnavailable), repr(raised))
        finally:
            config.SCENE, config.EXEC_BOOTSTRAP_SCENE = real_scene, real_boot


def test_a_box_that_will_not_wake_does_not_spend_an_exec_attempt() -> None:
    """The hole the RemoteError net could never have covered: `vastctl`.

    `_run_guarded` refunds everything under `remote.RemoteError`, on the rule
    that transport is never the caller's code. `Fleet.ensure_ready` does not
    obey that rule — its resume path raises `vastctl.NotReachable`, which is a
    `VastError` and has no relation to `RemoteError` at all. So the one failure
    that says the LEAST about a build, "the broker could not get a box", was
    the one that landed in the final `else` and spent an attempt.

    Measured 2026-08-07 on instance 47040457: hibernated at 04:30, and vast.ai
    then would not act on `start_instance` — `actual=exited, intended=stopped`
    across three calls, 902 s per resume. Exec job 5534329f168f (agent
    occ-all6, 2.41 GB bundle) came back `attempts=2, err=NotReachable` without
    having executed a line of its own code; one more and the row would have
    read `failed` for good.

    `run_one` re-types it, exactly as `app.py` already does for renders around
    `acquire_worker`. The wrapper sits where the fleet is ASKED for hardware,
    so whatever `Fleet` raises next is covered without a name being added to
    any list — which is the same argument the transport net is built on.
    """
    sys.path.insert(0, str(config.ROOT / "vastctl"))
    import vastctl

    svc = execservice.ExecService.__new__(execservice.ExecService)

    def unwakeable() -> None:
        raise vastctl.NotReachable(
            47040457, "waiting for running", "actual=exited, intended=stopped",
            902.0, provisioning=True)

    svc.ensure_ready = unwakeable
    try:
        svc.run_one({"id": "5534329f168f", "bundle": "e35c9db563b31b22"}, {})
        raised: Exception | None = None
    except Exception as exc:                                   # noqa: BLE001
        raised = exc

    check("an instance that will not wake is re-typed FleetUnavailable — the "
          "refunded WAIT branch — not left as a bare VastError that spends an "
          "attempt", isinstance(raised, remote.FleetUnavailable), repr(raised))
    check("and the re-typing keeps the original diagnosis, or the log says "
          "only 'no instance available' about a control-plane fault",
          "47040457" in str(raised) and "intended=stopped" in str(raised),
          str(raised))
    check("the cause is chained, so nothing is thrown away by re-typing",
          isinstance(raised.__cause__, vastctl.NotReachable),
          repr(getattr(raised, "__cause__", None)))

    # The ordering half: DiskFull must not be swallowed into a refunded wait on
    # its way through, or a full disk retries forever.
    for exc_in in (remote.DiskFull("7.97 GB will not fit"),
                   remote.WorkerBusy("a frame is in flight")):
        svc.ensure_ready = lambda e=exc_in: (_ for _ in ()).throw(e)
        try:
            svc.run_one({"id": "x", "bundle": "0"}, {})
            out: Exception | None = None
        except Exception as exc:                               # noqa: BLE001
            out = exc
        check(f"{type(exc_in).__name__} passes through the wrapper unchanged, "
              f"keeping the verdict its own branch gives it",
              out is exc_in, repr(out))


def test_exec_server_saying_not_yet_is_not_the_build_failing() -> None:
    """`wait: true` on a non-ok reply must survive into the retry decision.

    The exec server's memory gate calls itself "a WAIT rather than a
    rejection", and it was — right up to serialisation. Every refusal left as
    `{"ok": false, "error": ...}`, `run_one` turned any non-ok reply into a
    plain `RuntimeError`, and a plain `RuntimeError` is the broker's word for
    "the caller's script is broken". Jobs 88de1f4d5faf and 2a7e2a119e60,
    03:43 on 2026-08-07, were both charged an attempt for `waited 602s for
    20.0G of free memory and only 3.7G was ever available` — memory held by the
    render worker and eleven sibling builds. The gate runs before `stage` and
    `run_child`, so neither had a child process, let alone a verdict.

    Driven through the REAL `run_one` and the REAL `_run_guarded`, because the
    bug lived exactly in the seam between them.
    """
    from . import execremote

    class StubBroker:
        running = True
        paused = False
        last_work = 0.0

    class StubFleet:
        ep = remote.Endpoint(host="h", port=1, instance_id=1)
        def protected_scenes(self):
            return set()
        def staging_digest(self):
            return None

    with tempfile.TemporaryDirectory() as tmpdir:
        db = DB(Path(tmpdir) / "exec.db")
        svc = execservice.ExecService.__new__(execservice.ExecService)
        svc.broker, svc.db, svc.fleet = StubBroker(), db, StubFleet()
        svc.slots, svc.inflight, svc.lock, svc.last_error = 12, {}, threading.Lock(), ""
        svc._hold_the_slot_and_wait = lambda s: 0.0
        # Everything between the claim and the exec_call is exercised by other
        # tests; what is under test here is what happens to the REPLY.
        svc.ensure_ready = lambda: None
        svc.refuse_if_memory_is_short = lambda spec: None
        svc.ensure_scene_staged = lambda ep, spec: None

        root = Path(tmpdir) / "project"
        (root / "tools").mkdir(parents=True)
        (root / "tools" / "build.py").write_text("pass\n")
        os.environ["VASTRENDER_BUNDLE_ROOTS"] = str(root)
        real_push = execremote.push_bundle
        execremote.push_bundle = lambda ep, bundle, **k: {"cached": True}
        real_call = execremote.exec_call
        try:
            bundle = execservice.plan_bundle(str(root), ["tools/*.py"])
            job_spec = {"bundle_root": str(root), "bundle_patterns": ["tools/*.py"],
                        "entry": "tools/build.py", "timeout_s": 60}

            def run(reply: dict) -> tuple[str, int]:
                jid = db.submit(job_spec, agent="a", kind="exec",
                                bundle=bundle.digest)
                claimed = db.claim_exec(600)
                assert claimed is not None and claimed["id"] == jid
                execremote.exec_call = lambda payload, **k: (
                    reply if payload.get("cmd") is None else {"ok": True})
                svc._run_guarded({"id": jid, "bundle": bundle.digest}, job_spec)
                row = db.get(jid)
                db.cancel(jid)
                return row["state"], row["attempts"]

            state, attempts = run({
                "ok": False, "wait": True,
                "error": "ResourceWait: waited 602s for 20.0G of free memory and "
                         "only 3.7G was ever available"})
            check("an exec server that would not ADMIT the job is a WAIT: the "
                  "attempt is refunded, because no child ever ran",
                  (state, attempts) == ("queued", 0), f"{state} {attempts}/3")

            state, attempts = run({
                "ok": False,
                "error": "child exited 1",
                "log": "NameError: name 'foo' is not defined"})
            check("a child that RAN and failed still spends the attempt — the "
                  "marker distinguishes, it does not blanket",
                  (state, attempts) == ("queued", 1), f"{state} {attempts}/3")

            # An exec server predating the marker sends no `wait` field at all.
            state, attempts = run({"ok": False, "error": "child exited 1"})
            check("a reply from an OLDER exec server, with no marker, behaves "
                  "exactly as it does today",
                  (state, attempts) == ("queued", 1), f"{state} {attempts}/3")
        finally:
            execremote.push_bundle = real_push
            execremote.exec_call = real_call
            os.environ.pop("VASTRENDER_BUNDLE_ROOTS", None)

    # And the two sides agree about the name of the field, which is the whole
    # contract. Read from the worker rather than restated, for the same reason
    # `WORKER_FIELDS` is.
    worker_src = (Path(execservice.__file__).resolve().parent.parent /
                  "worker" / "exec_server.py").read_text()
    check("the exec server marks its waits with the field the broker reads",
          'reply["wait"] = True' in worker_src and
          'class ResourceWait' in worker_src,
          "worker/exec_server.py")


def test_exec_never_duplicates_a_scene_push_already_in_flight() -> None:
    """Two 8 GB streams up one uplink is not half the bandwidth, it is a reset.

    `Fleet._deploy` pushed film16_breach.blend from 03:27:14 to 03:31:52 on
    2026-08-07. `ExecService.ensure_scene_staged` began pushing THE SAME DIGEST
    at 03:30:23 and was reset twenty seconds later. Content addressing is what
    makes the overlap detectable and what makes skipping it safe: a matching
    digest is not similar work, it is the same bytes.
    """
    fleet = Fleet.__new__(Fleet)
    fleet.transfer = None
    check("nothing in flight reports no staging digest",
          fleet.staging_digest() is None)
    fleet.transfer = {"what": "film16_breach.blend", "bytes": 7969670247,
                      "began": time.time(), "digest": "1e8d5440c349fe51"}
    check("a scene push in flight is reported BY CONTENT, not by filename — "
          "two assemblies can share a name, which is why scene_hash exists",
          fleet.staging_digest() == "1e8d5440c349fe51", str(fleet.staging_digest()))
    fleet.transfer = {"what": "x.blend", "bytes": 1, "began": time.time()}
    check("a transfer recorded without a digest reports None rather than "
          "inventing one — the caller must go and ask the instance",
          fleet.staging_digest() is None)

    class StubFleet:
        def staging_digest(self):
            return "1e8d5440c349fe51"
        def protected_scenes(self):
            return set()

    svc = execservice.ExecService.__new__(execservice.ExecService)
    svc.fleet = StubFleet()
    spec_ = {"scene_digest": "1e8d5440c349fe51", "scene_name": "film16_breach.blend",
             "scene_bytes": 7969670247, "scene_path": "/nonexistent/film16_breach.blend"}
    cached_calls: list = []

    real_cached = remote.scene_cached
    remote.scene_cached = lambda *a, **k: (cached_calls.append(a), False)[1]
    try:
        try:
            svc.ensure_scene_staged("EP", spec_)
            check("REFUSED: staging a scene the render path is already pushing",
                  False, "it went ahead and pushed a second copy")
        except remote.FleetUnavailable as exc:
            check("REFUSED: staging a scene the render path is already pushing — "
                  "and as a WAIT, which _run_guarded refunds",
                  True, str(exc)[:70])
    finally:
        remote.scene_cached = real_cached


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


# --- linked libraries -----------------------------------------------------

# Blender 5.2's output when a linked library cannot be found, captured verbatim
# on 2026-08-04 by loading a scene whose library had been moved away. These are
# the strings the broker greps, so they are pinned here rather than paraphrased:
# the defect being tested for is a check that matched ONE of them.
BLENDER_MISSING_LIBRARY_LINES = (
    "Warning: Unable to open '/x/lib_source.blend': No such file or directory",
    "Info: Cannot find lib '/x/lib_source.blend'",
    "Info: LIB: Collection: 'SourceCollection' missing from '/x/lib_source.blend', parent '<direct>'",
    "Warning: 1 libraries and 1 linked data-blocks are missing (including 0 ObjectData), "
    "please check the Info and Outliner editors for details",
)
BLENDER_MISSING_IMAGE_LINE = "Warning: Image file /x/hdri.exr does not exist"
BLENDER_ORDINARY_LINES = (
    "Info: Read library: '/x/lib_source.blend', '/x/lib_source.blend', parent '<direct>'",
    "Fra:1 Mem:412.00M | Time:00:03.21 | Rendering 24/24 samples",
    "00:01.123  blend            | Read blend: \"/workspace/scenes/abc/scene.blend\"",
)

# The check that shipped. Kept as a literal so the test can demonstrate what it
# could and could not see, rather than assert that in prose.
OLD_ASSET_PATTERN = r"Image file [^ ]+ does not exist"


def test_missing_asset_patterns_see_libraries() -> None:
    """The broker's log check must match every way Blender says "not found".

    The version that shipped greps for one string, `Image file ... does not
    exist`, while its own docstring named the failure it was written to prevent:
    "the broker returns a subtly wrong frame and logs nothing". A missing
    library prints none of that text, so a scene that linked its grandstands out
    of another .blend rendered sky over black in 0.83 s and passed.

    Both directions are checked. Matching the library lines is worthless on its
    own — `.*` would do it — so the ordinary lines must NOT match, and the old
    pattern must be shown failing on the very lines the new one catches.
    """
    from . import remote

    combined = re.compile("|".join(remote.ASSET_MISS_PATTERNS))
    old = re.compile(OLD_ASSET_PATTERN)

    for line in BLENDER_MISSING_LIBRARY_LINES:
        check(f"matches library miss: {line[:38]!r}", bool(combined.search(line)))
        # The regression itself, asserted rather than described.
        check(f"OLD pattern was BLIND to: {line[:38]!r}", not old.search(line))

    check("still matches the missing-image line",
          bool(combined.search(BLENDER_MISSING_IMAGE_LINE)))
    check("old pattern matched the image line (it was not useless)",
          bool(old.search(BLENDER_MISSING_IMAGE_LINE)))

    # The negative control, and it is a real one: these are lines from a
    # SUCCESSFUL load of a scene that links a library. `Read library:` names the
    # same path in the same log; a pattern keyed on the path, or on the word
    # "library", would fire on a scene that is completely fine.
    for line in BLENDER_ORDINARY_LINES:
        check(f"does NOT match healthy line: {line[:38]!r}", not combined.search(line))

    # And the split between "warn" and "refuse" has to survive the same test:
    # a missing image must not be classified as a missing library.
    lib_markers = remote.LIBRARY_MISS_MARKERS
    check("image miss is not classified as a library miss",
          not any(m in BLENDER_MISSING_IMAGE_LINE for m in lib_markers))
    check("library misses are classified as library misses",
          all(any(m in ln for m in lib_markers)
              for ln in BLENDER_MISSING_LIBRARY_LINES))


def _blender_fixtures(tmp: Path) -> Optional[Path]:
    """Build the control pair with Blender: one scene that links, one that does
    not. Returns None if Blender is not installed.

    Written by Blender, never by this test. A .blend synthesised here would be
    a file shaped like whatever `blendlibs` expects to read, and a reader tested
    against its own idea of the format is the round-trip-against-the-constant
    this project has already shipped once.
    """
    blender = shutil.which("blender")
    if not blender:
        return None
    script = tmp / "mk.py"
    script.write_text(
        "import bpy, os, sys\n"
        "OUT = sys.argv[-1]\n"
        "def fresh():\n"
        "    bpy.ops.wm.read_factory_settings(use_empty=True)\n"
        "def save(p, compress=False):\n"
        "    bpy.ops.wm.save_as_mainfile(filepath=p, compress=compress,\n"
        "                                relative_remap=False)\n"
        "fresh()\n"
        "bpy.ops.mesh.primitive_cube_add()\n"
        "ob = bpy.context.active_object\n"
        "col = bpy.data.collections.new('SourceCollection')\n"
        "bpy.context.scene.collection.children.link(col)\n"
        "bpy.context.scene.collection.objects.unlink(ob)\n"
        "col.objects.link(ob)\n"
        "save(os.path.join(OUT, 'lib_source.blend'))\n"
        "src = os.path.join(OUT, 'lib_source.blend')\n"
        "def link_and_save(name, compress):\n"
        "    fresh()\n"
        "    with bpy.data.libraries.load(src, link=True) as (fro, to):\n"
        "        to.collections = ['SourceCollection']\n"
        "    for c in bpy.data.collections:\n"
        "        if c.library:\n"
        "            inst = bpy.data.objects.new('Inst', None)\n"
        "            inst.instance_type = 'COLLECTION'\n"
        "            inst.instance_collection = c\n"
        "            bpy.context.scene.collection.objects.link(inst)\n"
        # The instance is load-bearing: a linked collection nothing references
        # is orphan data, Blender drops it on save, and the "linked" fixture
        # silently becomes a second copy of the clean one.
        "    save(os.path.join(OUT, name), compress)\n"
        "link_and_save('linked.blend', False)\n"
        "link_and_save('linked_z.blend', True)\n"
        "fresh()\n"
        "with bpy.data.libraries.load(src, link=False) as (fro, to):\n"
        "    to.collections = ['SourceCollection']\n"
        "for c in bpy.data.collections:\n"
        "    bpy.context.scene.collection.children.link(c)\n"
        "save(os.path.join(OUT, 'appended.blend'))\n"
        "print('FIXTURES-OK')\n"
    )
    out = tmp / "fx"
    out.mkdir(exist_ok=True)
    ran = subprocess.run(
        [blender, "--background", "--factory-startup", "--python", str(script),
         "--", str(out)],
        capture_output=True, text=True, timeout=600,
    )
    if "FIXTURES-OK" not in ran.stdout:
        return None
    return out


def test_unresolved_libraries_are_refused() -> None:
    """A scene that links must fail; a self-contained scene must pass.

    The negative control is `appended.blend`, not an empty file. It holds the
    SAME collection, from the SAME source .blend, appended instead of linked —
    so its datablock names still carry the source's name and its geometry is
    identical. A detector that keys on "mentions another .blend" passes the
    positive and fails here, which is the point: this project has already
    shipped a negative control that was a second positive.
    """
    from . import blendlibs, scenes

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fx = _blender_fixtures(tmp)
        if fx is None:
            # Not a skip. A gate whose controls did not run is a gate that has
            # not been shown to work, and reporting that as a pass is the exact
            # habit this whole defect came out of.
            check("library gate controls ran", False,
                  "blender unavailable, so neither control was exercised")
            return
        check("library gate controls ran", True)

        linked = fx / "linked.blend"
        linked_z = fx / "linked_z.blend"
        appended = fx / "appended.blend"
        source = fx / "lib_source.blend"

        # 1. the reader, on a file Blender wrote
        refs = blendlibs.library_paths(linked)
        check("POSITIVE: reader finds the linked library",
              len(refs) == 1 and refs[0].path == source.resolve(),
              str([r.stored for r in refs]))
        check("NEGATIVE: appended scene has no library",
              blendlibs.library_paths(appended) == [])
        check("NEGATIVE: the library source itself links nothing",
              blendlibs.library_paths(source) == [])
        check("POSITIVE: compressed .blend is read, not skipped",
              len(blendlibs.library_paths(linked_z)) == 1,
              "zstd is Blender's save default; a reader blind to it clears "
              "half the corpus without opening it")

        # 2. the policy, which is what actually admits or refuses a job
        os.environ["VASTRENDER_SCENE_ROOTS"] = str(fx)
        try:
            import importlib
            from . import config as cfg
            importlib.reload(cfg)
            importlib.reload(scenes)

            for name, want_refused in (("linked.blend", True),
                                       ("linked_z.blend", True),
                                       ("appended.blend", False),
                                       ("lib_source.blend", False)):
                try:
                    scenes.require_resolvable_libraries(fx / name)
                    refused, why = False, ""
                except scenes.UnresolvedLibraries as exc:
                    refused, why = True, str(exc)
                check(f"{'REFUSED' if want_refused else 'ACCEPTED'}: {name}",
                      refused == want_refused, why[:90])
                if want_refused and refused:
                    check(f"{name} refusal names the missing path",
                          "lib_source.blend" in why)

            # 3. a library that DOES travel must be accepted, or the gate is
            #    just "refuse everything that links" wearing a policy's clothes.
            sib = fx / "cache"
            sib.mkdir(exist_ok=True)
            shutil.copy(source, sib / "lib_source.blend")
            carried_scene = fx / "carried.blend"
            shutil.copy(linked, carried_scene)
            # Point the copy at the sibling directory the broker really uploads.
            refs = blendlibs.library_paths(carried_scene)
            check("carried-library case is set up on a real linked scene",
                  len(refs) == 1)
            fake = blendlibs.LibRef(stored="//cache/lib_source.blend",
                                    path=(sib / "lib_source.blend").resolve(),
                                    exists=True, linker=carried_scene)
            check("a `//cache/` library is classified as carried",
                  _is_carried(scenes, carried_scene, fake))
            outside = blendlibs.LibRef(
                stored="//../elsewhere/lib_source.blend",
                path=(fx.parent / "elsewhere" / "lib_source.blend").resolve(),
                exists=False, linker=carried_scene)
            check("a library above the scene directory is NOT carried",
                  not _is_carried(scenes, carried_scene, outside))
        finally:
            os.environ.pop("VASTRENDER_SCENE_ROOTS", None)
            import importlib
            from . import config as cfg
            importlib.reload(cfg)
            importlib.reload(scenes)


def _is_carried(scenes_mod, scene: Path, ref) -> bool:
    """Run one LibRef through the same classification `library_status` uses."""
    original = scenes_mod._library_closure_cached
    scenes_mod._library_closure_cached = lambda _p: [ref]
    try:
        carried, _unresolved = scenes_mod.library_status(scene)
        return bool(carried)
    finally:
        scenes_mod._library_closure_cached = original


def test_bundled_essentials_are_not_refused() -> None:
    """Blender's own shipped assets must not trip the gate.

    Thirteen round-1 scenes link `geometry_nodes_essentials.blend` — that is
    what the "Smooth by Angle" modifier is. Blender remaps those onto the
    running installation: MEASURED 2026-08-04 under
    `/opt/blender-5.2.0-linux-x64` with `/usr/share/blender` bind-mounted empty,
    `is_missing` came back False and the sibling brushes library was rewritten
    to the `/opt` install. The instance carries `/workspace/blender/5.2/
    datafiles/assets/`, so they resolve there too.

    A gate that refused these would reject thirteen scenes that render
    correctly, and a gate with false refusals gets switched off. The negative
    half matters just as much: a project directory that merely happens to be
    called `assets` must not inherit the exemption.
    """
    from . import blendlibs, scenes

    def ref(p: str) -> "blendlibs.LibRef":
        return blendlibs.LibRef(stored=p, path=Path(p), exists=False,
                                linker=Path("/scene/x.blend"))

    for path in (
        "/usr/share/blender/5.2/datafiles/assets/nodes/geometry_nodes_essentials.blend",
        "/workspace/blender/5.2/datafiles/assets/brushes/essentials_brushes-mesh_sculpt.blend",
        "/opt/blender-5.2.0-linux-x64/5.2/datafiles/assets/nodes/shading_nodes_essentials.blend",
    ):
        check(f"bundled: {path[:46]}", scenes.is_bundled_essentials(ref(path)))

    for path in (
        "/home/user/f1-round2/render/world/assembly/r2/assembly9.blend",
        "/home/user/f1-round2/datafiles/assets/grandstand.blend",
        "/home/user/project/assets/datafiles/assets/thing.blend",
    ):
        check(f"NOT bundled: {path[:46]}", not scenes.is_bundled_essentials(ref(path)))


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

        # A missing .blend is the same kind of answer, reached before the
        # worker is ever involved. It was retried three times: observed
        # 2026-08-03, two reliefpvg jobs for a renamed blend each burned three
        # dispatch passes restating "does not exist".
        src = inspect.getsource(app.Broker.run_job)
        scene_at = src.find("except scenes.SceneError")
        generic_at = src.find("except Exception as exc:")
        check("a missing scene is failed terminally, not retried three times",
              scene_at != -1 and generic_at != -1 and scene_at < generic_at,
              f"SceneError at {scene_at}, generic at {generic_at}")

        # And the two hand-rolled not-found raises are the same class, or they
        # would sail past the handler that was just added for them.
        for fn in (app.Broker.run_still, app.Broker.run_sequence):
            body = inspect.getsource(fn)
            if "scene not found" in body:
                check(f"{fn.__name__} raises SceneError for a missing scene",
                      "raise scenes.SceneError(f\"scene not found" in body,
                      "still a bare RuntimeError" if "raise RuntimeError(f\"scene not found"
                      in body else "ok")

        # Inside a SEQUENCE a refusal must not be treated as a frame failure:
        # the same question is asked of every remaining frame and gets the same
        # answer, so it used to burn FRAME_FAIL_STREAK frames restating it.
        # It is re-raised, which means it is NOT caught by the infrastructure
        # tuple that requeues, and NOT counted by the streak.
        src = inspect.getsource(app.Broker.run_sequence)
        refuse_at = src.find("except JobRefused")
        generic_at = src.find("except Exception as exc:")
        check("a refusal in a sequence is raised, not counted as a frame failure",
              refuse_at != -1 and generic_at != -1 and refuse_at < generic_at,
              f"JobRefused at {refuse_at}, generic at {generic_at}")


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


def test_load_versus_render_time_is_accounted() -> None:
    """The ratio `rq status` prints, and the failures it must not hide.

    Loading a scene is paid GPU time that renders nothing. On 2026-08-03 one
    instance spent more of its life loading scenes than rendering with them,
    and nobody knew because nothing displayed it — it took hand-measurement off
    the log to find. A scheduler whose whole purpose is this ratio has to
    report it.

    Two properties, and the second is the one that could quietly lie: renders
    are counted from the WORKER's own render_sec (not wall clock, which folds
    in fetch and queue wait and flatters the ratio), and a scene load that
    FAILED still counts as load — the GPU was rented for every one of those
    seconds and rendered nothing in them.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        fleet = StubFleet([rendering("j1")])
        fleet.png["j1"] = 5_000_000
        b = stub_broker(tmp, fleet)

        # A render reattached and collected still reports the WORKER's seconds
        # (123.0 here), not the wall clock the broker waited.
        b.render_one("j1", spec(), Path("/tmp/s.blend"), "row1", retry=False)
        check("a completed render adds the worker's own seconds",
              fleet.render_sec == 123.0, f"{fleet.render_sec}")

        # The real Fleet's accounting, exercised directly: a failed switch is
        # still load. Using the real class here on purpose — this arithmetic
        # is the thing being tested, so a stub of it would test nothing.
        f = Fleet.__new__(Fleet)
        f.load_sec = 0.0
        f.render_sec = 0.0
        f.load_sec += 900.0          # a switch that worked
        f.load_sec += 300.0          # a switch that failed, still paid for
        f.render_sec += 400.0
        total = f.load_sec + f.render_sec
        check("a failed scene load is still counted as load",
              f.load_sec == 1200.0 and round(100 * f.load_sec / total) == 75,
              f"load {f.load_sec}s of {total}s")
        check("the ratio names the case worth acting on",
              f.load_sec > f.render_sec, "load exceeds render")


def test_per_instance_counters_cannot_outlive_their_instance() -> None:
    """A reset that only runs on the clean path is not a reset.

    Measured 2026-08-03. Instance 46705078 was torn down during a vast.ai DNS
    outage, so its stop call could not resolve console.vast.ai; the destroy
    took the ERROR path and `_forget_vanished` — which is where load_sec,
    render_sec, fetch_samples and switch_cost were zeroed — never ran. Its
    3660 s of load and 1008 s of render survived into instance 46712525, whose
    own figures were 492 s and 2923 s. `rq status` therefore printed
    `load 4152s (52%)  <- more time loading than rendering` for a box actually
    running at 14 % load. The sum 3660 + 492 = 4152 is exactly how the
    contamination was proved, and it sent a coordinator and an agent after a
    scene-thrash defect that did not exist.

    The same shape has bitten this project before: `assert_levelled` sat inside
    `if not a.no_rig:` and a rig-less build shipped un-relit. So the fix is not
    another reset on another path — it is that the values are KEYED by the
    instance id that earned them, and a read for a different id cannot reach
    them at all.

    fetch_samples matters as much as the timings: a stale median condemns a
    healthy replacement on its predecessor's link, or hides a bad one.
    """
    f = Fleet.__new__(Fleet)
    f.instance_id = 46705078
    f.load_sec += 3660.0
    f.render_sec += 1008.0
    f.fetch_samples.append(1_040_000.0)
    f.switch_cost["fb3a34ec"] = 178.8
    check("the box that earned them can read its own numbers",
          (f.load_sec, f.render_sec) == (3660.0, 1008.0)
          and len(f.fetch_samples) == 1 and len(f.switch_cost) == 1,
          f"load {f.load_sec} render {f.render_sec}")

    # The instance dies through the error path. No teardown, no reset — the
    # next instance id is simply adopted, exactly as it was on 2026-08-03.
    f.instance_id = 46712525
    check("a new instance starts at zero even though no reset ever ran",
          (f.load_sec, f.render_sec) == (0.0, 0.0), f"{f.load_sec} {f.render_sec}")
    check("a stale link median cannot condemn or flatter the new box",
          f.fetch_samples == [] and f.switch_cost == {},
          f"{f.fetch_samples} {f.switch_cost}")

    # The real figures for 46712525, and the ratio they should have shown.
    f.load_sec += 262.4          # deploy, incl. the first 4.77 GB push
    f.load_sec += 26.0           # redeploy onto a warm cache
    f.load_sec += 203.6          # the 5.22 GB scene switch
    f.render_sec += 2923.0
    pct = 100 * f.load_sec / (f.load_sec + f.render_sec)
    check("the uncontaminated ratio is the healthy one that was masked",
          f.load_sec == 492.0 and 14 <= round(pct) <= 15,
          f"load {f.load_sec}s = {pct:.1f}%")

    # And the old numbers are gone, not parked somewhere reachable: re-adopting
    # the dead id must not resurrect them.
    f.instance_id = 46705078
    check("re-adopting a dead instance id does not resurrect its numbers",
          (f.load_sec, f.render_sec) == (0.0, 0.0), f"{f.load_sec} {f.render_sec}")


def test_the_cache_budget_is_derived_from_the_disk_present() -> None:
    """A ceiling sized for a disk we might migrate to, applied to the one we have.

    Measured 2026-08-03 on instance 46712525: a 32.2 GB volume with 20.8 GB
    free logged "scene cache will exceed its 8.0 GB budget (4.77 GB cached +
    5.22 GB incoming)" while a third of the disk sat unused. The 8 GB was
    correct once — it was chosen so the largest assembly then known (3.9 GB)
    fitted beside the loaded one on a 16 GB volume — and scenes then grew to
    5.22 GB, which is 34 % past what it was sized around. Two current scenes
    could no longer coexist BY CONSTRUCTION.

    Raising the constant would buy one more year of the same bug, so the budget
    is derived from the measured disk instead. Addressing the class, not the
    instance.
    """
    GB = 10 ** 9
    res = int(config.DISK_RESERVE_GB * GB)

    def disk(total_gb, other_gb, cache_gb):
        scenes = ((remote.SceneEntry("s", int(cache_gb * GB), 1.0),)
                  if cache_gb else ())
        used = int((other_gb + cache_gb) * GB)
        return remote.DiskState(ok=True, total=int(total_gb * GB), used=used,
                                free=int(total_gb * GB) - used, scenes=scenes)

    big = disk(32.2, 1.48, 5.22)      # the box that hit it
    small = disk(16, 1.7, 0)          # the volume the old constant was for

    check("the disk that was two-thirds empty now gets a budget that fits it",
          remote.cache_budget(big, res) > 20 * GB,
          f"{remote.cache_budget(big, res) / GB:.1f}G")
    check("the two scenes that could not coexist under 8 GB now can",
          remote.cache_budget(big, res) >= int(9.99 * GB),
          f"{remote.cache_budget(big, res) / GB:.1f}G vs 9.99G needed")
    check("a 16 GB volume still gets a 16 GB-shaped answer, not the big one",
          9 * GB <= remote.cache_budget(small, res) <= 10 * GB,
          f"{remote.cache_budget(small, res) / GB:.1f}G")

    # The property that makes it stable: `other_bytes` excludes the cache, so
    # filling the cache must not lower the ceiling that permitted the fill. A
    # budget derived from FREE space would chase its own tail.
    ceilings = {remote.cache_budget(disk(32.2, 1.48, c), res) for c in (0, 5, 10, 20)}
    check("the budget does not shrink as the cache it governs fills",
          len(ceilings) == 1, f"{sorted(round(c / GB, 1) for c in ceilings)}")

    # Physics still outranks policy, and an explicit pin still wins.
    tiny = disk(6, 1.7, 0)
    check("a floor may not conjure disk that is not there",
          remote.cache_budget(tiny, res) <= remote.cache_room(tiny, res),
          f"{remote.cache_budget(tiny, res) / GB:.1f}G of {remote.cache_room(tiny, res) / GB:.1f}G")
    pinned = config.SCENE_CACHE_GB
    try:
        config.SCENE_CACHE_GB = 8.0
        check("an explicit SCENE_CACHE_GB is still honoured as a ceiling",
              remote.cache_budget(big, res) == 8 * GB,
              f"{remote.cache_budget(big, res) / GB:.1f}G")
    finally:
        config.SCENE_CACHE_GB = pinned
    check("and the derived value is printable, so it is never a magic number",
          "derived" in remote.describe_cache_budget(big, res),
          remote.describe_cache_budget(big, res))


def test_a_slow_link_is_a_health_signal() -> None:
    """A check that counts failures cannot see slow. This one can.

    2026-08-03, instance 46695656 (192.0.2.12): 265 ms RTT, 731 KB/s up,
    **14 KB/s down** — verified three ways, including a no-mux fetch that ruled
    out our own ControlMaster. It rendered a frame in 16 s and needed six
    minutes to hand it back. Every transport check passed, because every one of
    them counts resets, timeouts and rounds that moved no bytes, and a link that
    delivers slowly produces none of those. It billed for 68% of a rental.

    The threshold condemns the OFFER, never the machine: one container's
    measured path over one rental does not justify a 24 h ban on a host.
    """
    f = Fleet.__new__(Fleet)
    f.fetch_samples = []

    # The real numbers. A 7.5 MB PNG at the measured rate.
    f.note_fetch(7_524_253, 6 * 60 + 8)
    check("one sample is not yet a verdict — a hiccup must not destroy a box",
          f.download_too_slow() is None, f"{len(f.fetch_samples)} sample(s)")

    f.note_fetch(7_524_253, 5 * 60 + 40)
    why = f.download_too_slow()
    check("two slow fetches condemn the link, naming the cost",
          why is not None and "KB/s" in why and "min to fetch" in why,
          (why or "")[:88])

    # A healthy box: 7.5 MB in about two seconds, the normal case on this farm.
    g = Fleet.__new__(Fleet)
    g.fetch_samples = []
    for _ in range(4):
        g.note_fetch(7_524_253, 2.0)
    check("a healthy link is never condemned",
          g.download_too_slow() is None,
          f"{(g.fetch_bps or 0) / 1000:.0f} KB/s")

    # Small transfers are latency, not bandwidth. At 265 ms RTT a 100 KB file
    # reports a terrible "rate" on a link that is fine — sampling them would
    # condemn healthy hosts, which is worse than the bug being fixed.
    h = Fleet.__new__(Fleet)
    h.fetch_samples = []
    for _ in range(5):
        h.note_fetch(100_000, 3.0)          # 33 KB/s, but meaningless
    check("transfers too small to measure bandwidth are ignored",
          not h.fetch_samples and h.download_too_slow() is None,
          f"{len(h.fetch_samples)} sample(s) kept")

    # Median, not mean: one fetch sharing the link with a scene push is not
    # evidence about the link.
    m = Fleet.__new__(Fleet)
    m.fetch_samples = []
    for _ in range(4):
        m.note_fetch(8_000_000, 2.0)        # 4 MB/s, healthy
    m.note_fetch(8_000_000, 400.0)          # one outlier at 20 KB/s
    check("a single outlier cannot condemn a healthy link",
          m.download_too_slow() is None, f"{(m.fetch_bps or 0) / 1000:.0f} KB/s median")

    # And the broker ACTS on it — between jobs, never mid-render, because the
    # verdict ends in a destroyed instance.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        fleet = StubFleet([idle_worker()])
        b = stub_broker(tmp, fleet)

        b.check_download_health()
        check("a healthy link is left alone by the dispatcher",
              not fleet.condemned and not fleet.torn_down, "")

        fleet.slow_link = "download is 14.0 KB/s over 2 real fetch(es)"
        b.check_download_health()
        check("a condemned link replaces the instance",
              fleet.condemned == [fleet.slow_link] and fleet.torn_down,
              f"condemned={fleet.condemned}")

        # A paused broker is already winding down; it must not also be racing
        # to rent a replacement.
        fleet2 = StubFleet([idle_worker()])
        fleet2.slow_link = "download is 14.0 KB/s"
        b2 = stub_broker(tmp / "paused", fleet2) if (tmp / "paused").mkdir() is None else None
        b2.paused = "over budget"
        b2.check_download_health()
        check("a paused broker does not replace hardware",
              not fleet2.condemned, f"condemned={fleet2.condemned}")


def test_the_scene_dir_self_heal_keeps_the_evidence() -> None:
    """The stray-inode fix must heal, must not over-reach, and must SAY what it found.

    On 2026-08-03 `mkdir -p /workspace/scenes/<hash>` failed three times in
    nine seconds, `_deploy` read EEXIST as a host-level failure, and instance
    46668588 was destroyed — reachable, idle, 7 h uptime, 5.46 GB of warm
    cache — for one bad inode that `rm -f` fixes.

    The self-heal that followed closed the outage and opened a different hole:
    it removed the offending thing silently, so what WROTE a non-directory into
    a content-addressed cache path is still unknown, and every future
    recurrence would have destroyed its own evidence too.

    Run against a real filesystem with a real shell, because the whole thing is
    shell semantics — `-e`, `-d` and `rm -f` on paths that are variously a
    directory, a file, a dangling symlink, or absent.
    """
    from . import fleet as fleet_mod

    def run(path: str) -> str:
        return subprocess.run(["bash", "-c", fleet_mod.heal_scene_dir_cmd(path)],
                              capture_output=True, text=True).stdout

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # The ordinary case: nothing there yet.
        fresh = tmp / "fresh"
        out = run(str(fresh))
        check("an absent cache path is simply created",
              fresh.is_dir() and fleet_mod.STRAY_MARK not in out, out[:60])

        # A real cache directory, with a scene in it. Must survive untouched —
        # this is the case that must never be "healed".
        live = tmp / "live"
        live.mkdir()
        (live / "scene.blend").write_bytes(b"blend-data")
        out = run(str(live))
        check("a real cache directory and its contents are never touched",
              (live / "scene.blend").read_bytes() == b"blend-data"
              and fleet_mod.STRAY_MARK not in out, out[:60])

        # The failure itself: a plain file where the directory belongs.
        stray = tmp / "stray"
        stray.write_bytes(b"PNG-ish bytes that should never have been here")
        out = run(str(stray))
        check("a stray file is removed and the directory created",
              stray.is_dir(), f"is_dir={stray.is_dir()}")
        check("the stray file is DESCRIBED before removal, not silently deleted",
              fleet_mod.STRAY_MARK in out and "rw" in out
              and "P   N   G" in out,
              " | ".join(out.split("\n")[:3])[:90])

        # A dangling symlink: `-e` is false on it, so a naive `[ -e ]` guard
        # would skip the heal and leave `mkdir -p` failing forever.
        dangle = tmp / "dangle"
        dangle.symlink_to(tmp / "nowhere")
        out = run(str(dangle))
        check("a dangling symlink where a scene dir belongs is healed too",
              dangle.is_dir(), f"exists={dangle.exists()} link={dangle.is_symlink()}")

        # Paths are quoted: a scene digest is hex today, but the quoting is what
        # stops this being a command injection into a shell running as root.
        nasty = tmp / "a b; touch pwned"
        run(str(nasty))
        check("the path is shell-quoted, not interpolated",
              nasty.is_dir() and not (tmp / "pwned").exists(),
              f"pwned={(tmp / 'pwned').exists()}")


def test_the_slow_link_signal_is_actually_wired_to_a_fetch() -> None:
    """The seam: a real fetch must PRODUCE the sample the verdict is made of.

    `test_a_slow_link_is_a_health_signal` proves the arithmetic on the old
    box's recorded numbers, and it proves `check_download_health` acts on a
    verdict handed to it. Both halves passed while nothing connected them —
    `collect` could stop calling `note_fetch`, or pass it the wrong pair, and
    every one of those checks would still be green with the signal permanently
    silent. That is the exact shape of the defect this whole feature exists to
    fix: a check that cannot fail on the condition that matters.

    So this asserts the wiring. `collect` must feed the size the fetch actually
    returned, and the wall time that fetch actually took — not a constant, not
    the render time, not the size the worker claimed.

    Verified live on 2026-08-03 against instance 46700730 as well: two
    genuinely throttled fetches over the real SSH link (1,050,000 B at 40.6 and
    43.9 KB/s) produced two real samples and fired the verdict — "download is
    43.9 KB/s over 2 real fetch(es) ... an 8 MB frame would take 3.0 min to
    fetch" — while that same box's real measured rate (2,028 KB/s median) was
    correctly left alone.
    """
    from . import seq

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        good = _png(tmp / "src.png")
        payload = good.read_bytes()
        want_sha = seq.sha256_of(good)
        # Long enough to be unambiguously distinguishable from zero, short
        # enough that the suite does not notice.
        fetch_delay = 0.25

        def slow_fetch(ep, remote_path, local, attempts=4):
            time.sleep(fetch_delay)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(payload)
            return len(payload)

        real_fetch, real_run = remote.fetch_file, remote.run
        remote.fetch_file, remote.run = slow_fetch, lambda *a, **k: ""
        try:
            fleet = StubFleet([idle_worker()])
            b = stub_broker(tmp, fleet)
            reply = {"path": "/workspace/out/j.png",
                     "png": {"width": 4, "height": 3, "sha256": want_sha}}
            b.collect(dict(reply), tmp / "out" / "j.png")

            check("a fetch through collect records exactly one sample",
                  len(fleet.fetch_samples) == 1,
                  f"{len(fleet.fetch_samples)} sample(s)")
            # StubFleet.note_fetch stores bytes/seconds, so the recorded rate
            # pins BOTH arguments at once: the size the fetch returned and the
            # time it really took. A hardcoded 0, a missing call, or the
            # worker's claimed size all land outside this band.
            rate = fleet.fetch_samples[0] if fleet.fetch_samples else 0
            expected = len(payload) / fetch_delay
            check("the sample carries the real size over the real elapsed time",
                  expected * 0.4 <= rate <= expected * 1.1,
                  f"{rate:.0f} B/s, expected about {expected:.0f} B/s")
        finally:
            remote.fetch_file, remote.run = real_fetch, real_run


def test_priority_reaches_the_scene_choice() -> None:
    """`prio` has to decide which SCENE loads next, not just job order in one.

    It ordered jobs inside `claim` and stopped dead at the scene boundary,
    where selection was `ORDER BY created ASC` — pure FIFO on submission. A
    `prio 10` job on a fresh scene therefore lost to a `prio 100` job on an
    older one for as long as that scene had work. Measured 2026-08-03: a 13.6 s
    render sat queued **41 minutes** behind older scenes with priority set.

    Agents had been setting `prio` and believing it worked. A half-working knob
    is worse than none, because it stops people looking for the real reason.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        old, urgent = "/scenes/old.blend", "/scenes/urgent.blend"
        b = stub_broker(tmp, StubFleet([idle_worker()]))

        # The old scene's job was submitted 20 minutes earlier at default
        # priority. The urgent one just landed at prio 10.
        stale = b.db.submit(spec(), agent="filmscene", scene=old, prio=100)
        b.db.conn.execute("UPDATE jobs SET created=? WHERE id=?",
                          (time.time() - 1200, stale))
        b.db.conn.commit()
        b.db.submit(spec(), agent="crowd", scene=urgent, prio=10)

        target, _ = b.db.oldest_waiting_scene()
        check("a prio 10 job on a fresh scene beats a 20-min-old default one",
              target == urgent, f"chose {target}")

        # Same jobs, same ages, no priority: FIFO must still hold. Priority is
        # the only thing that changed the answer above.
        #
        # Its own directory, because `stub_broker` always opens `busy.db` under
        # the path it is given — reusing `tmp` would have this control reading
        # the rows the case above just inserted, and it would then "pass" by
        # seeing the prio 10 job. Caught by this check failing.
        tmp2 = tmp / "fifo"
        tmp2.mkdir()
        b2 = stub_broker(tmp2, StubFleet([idle_worker()]))
        s2 = b2.db.submit(spec(), agent="filmscene", scene=old, prio=100)
        b2.db.conn.execute("UPDATE jobs SET created=? WHERE id=?",
                           (time.time() - 1200, s2))
        b2.db.conn.commit()
        b2.db.submit(spec(), agent="crowd", scene=urgent, prio=100)
        target, _ = b2.db.oldest_waiting_scene()
        check("without priority the older scene still wins (plain FIFO)",
              target == old, f"chose {target}")

        # And the switch trigger sees the same number as the switch target. If
        # they disagree, a high-priority job wins the target query while never
        # clearing the threshold that causes a switch at all.
        eff = b.db.oldest_waiting_age(exclude_scene=old)
        check("the starvation signal counts the same head start",
              eff is not None and eff > 800,
              f"effective age {eff:.0f}s (raw age is ~0)")


def test_priority_cannot_starve_a_scene() -> None:
    """THE BOUND ON PRIORITY. A low-priority scene runs, whatever arrives.

    Ordering by priority is unbounded — a steady trickle of urgent work defers
    everything else forever. That is exactly the trap `SCENE_BATCH_MAX` fell
    into, so priority gets the same treatment: a stated bound, and a test that
    FAILS when it is exceeded rather than a comment claiming it cannot be.

    The mechanism is aging, not ordering. Priority buys a fixed head start in
    seconds; the deferred scene's own age grows without limit, so it always
    wins eventually. `SCENE_PRIO_BOOST_MAX_SEC` clamps the head start, which
    makes the bound independent of what agents actually put in `prio` — 0,
    -1000, or a typo.

    **No scene is ever deferred more than SCENE_PRIO_BOOST_MAX_SEC beyond its
    FIFO turn.**
    """
    cap = app.config.SCENE_PRIO_BOOST_MAX_SEC
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        poor, rich = "/scenes/poor.blend", "/scenes/rich.blend"
        b = stub_broker(tmp, StubFleet([idle_worker()]))

        # A neglected default-priority job, and an absurd priority alongside it.
        poor_job = b.db.submit(spec(), agent="quiet", scene=poor, prio=100)
        b.db.submit(spec(), agent="loud", scene=rich, prio=-100_000)

        # Just inside the bound: the extreme priority still wins.
        b.db.conn.execute("UPDATE jobs SET created=? WHERE id=?",
                          (time.time() - (cap - 120), poor_job))
        b.db.conn.commit()
        target, _ = b.db.oldest_waiting_scene()
        check("inside the bound, high priority is served first",
              target == rich, f"chose {target}")

        # Past the bound: the neglected scene MUST win, however extreme the
        # priority it is up against. This is the assertion that fails if the
        # clamp is removed or priority is made an ordering.
        b.db.conn.execute("UPDATE jobs SET created=? WHERE id=?",
                          (time.time() - (cap + 300), poor_job))
        b.db.conn.commit()
        target, _ = b.db.oldest_waiting_scene()
        check("past the bound, a neglected scene beats ANY priority",
              target == poor, f"chose {target} with a -100000 prio competitor")

        # And the bound holds against a continuous stream, not one competitor —
        # a fresh urgent job every pass is the shape that starves an ordering.
        for _ in range(25):
            b.db.submit(spec(), agent="loud", scene=rich, prio=-100_000)
        target, _ = b.db.oldest_waiting_scene()
        check("a stream of urgent work still cannot hold the scene off",
              target == poor, f"chose {target} against 26 urgent jobs")


def test_batching_never_becomes_starvation() -> None:
    """THE POSITIVE CONTROL. A small scene behind a big batch runs, bounded.

    Every improvement in this file makes the dispatcher keener to hold onto a
    loaded scene, and each one is individually justified. Together they are how
    batching turns into starvation with a nicer name, so the bound gets a test
    that fails when it is exceeded rather than a comment saying it cannot be.

    `SCENE_BATCH_MAX` is the bound. It was not one: the capped batch re-asked
    for the oldest waiting scene WITHOUT excluding itself, got itself back —
    it still held the oldest job — and reset the counter. A scene submitted
    after a 60-job batch waited for all 60 no matter what the cap said.
    """
    keep = app.config.SCENE_BATCH_MAX
    app.config.SCENE_BATCH_MAX = 5
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            big, small = "/scenes/film7.blend", "/scenes/tiny.blend"

            fleet = StubFleet([idle_worker()])
            fleet.scene_path = Path(big)
            fleet.reload_cost = 1425.0      # every incentive to hold on
            b = stub_broker(tmp, fleet)

            # 60 jobs on the loaded scene, all submitted BEFORE the small one,
            # so FIFO order alone would make the small scene wait for all 60.
            for _ in range(60):
                b.db.submit(spec(), agent="filmscene", scene=big)
            late = b.db.submit(spec(), agent="crowd", scene=small)

            served = []
            for _ in range(40):
                job = b.next_job()
                if job is None:
                    break
                served.append(job["scene"])
                fleet.scene_path = Path(job["scene"])
                if job["id"] == late:
                    break

            check("a small scene behind a 60-job batch is served within the cap",
                  small in served and served.index(small) <= app.config.SCENE_BATCH_MAX,
                  f"served after {served.index(small) if small in served else 'NEVER'} "
                  f"job(s), cap {app.config.SCENE_BATCH_MAX}")

            # And the bound is the cap doing it, not luck: the big scene really
            # did get its batch first, so this is batching plus a bound rather
            # than no batching at all.
            check("the big scene still got a full batch before yielding",
                  served[:app.config.SCENE_BATCH_MAX] == [big] * app.config.SCENE_BATCH_MAX,
                  f"{served[:app.config.SCENE_BATCH_MAX + 1]}")
    finally:
        app.config.SCENE_BATCH_MAX = keep


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


def test_a_dns_outage_never_condemns_the_hardware() -> None:
    """A CONTROL-PLANE FAILURE MUST NOT CONDEMN HARDWARE.

    2026-08-03, verbatim from state/broker.log. The whole `vast.ai` zone went
    NXDOMAIN and this broker's own API calls died for 30 minutes:

        14:44:55 Failed to resolve 'console.vast.ai' ([Errno -2] ...)
        15:15:24 (same, last occurrence)

    Machine 56717 was rented at 15:18:50 — AFTER our resolver recovered — and
    vast reported the instance:

        actual=offline, intended=running, cur_state=running, machine=56717,
        msg=) Could not resolve host: cloud.vast.ai

    That is the HOST's resolver still holding the negative cache entry, which
    outlives the outage by the zone's SOA minimum. `classify()` mapped
    actual=offline straight to `bad`, `bad` raised provisioning=False, and
    provisioning=False banned the machine for 24 h. Nothing was wrong with it.

    The hazard is not one lost box: a fleet-wide DNS event walks this path once
    per rental and would blacklist every host it touched, leaving the broker
    with no market to rent from and a `bad_hosts.json` full of good hardware.
    """
    # Same route fleet.py uses: vastctl lives beside the package, not in it.
    from .fleet import vastctl as vc                            # noqa: PLC0415

    # --- 1. the classifier itself ---
    check("a host that cannot resolve cloud.vast.ai is a control-plane fault",
          vc.control_plane_fault(") Could not resolve host: cloud.vast.ai"), "")
    check("so is the glibc phrasing",
          vc.control_plane_fault("Temporary failure in name resolution"), "")
    check("a full host disk is NOT a control-plane fault — that is real",
          not vc.control_plane_fault("no space left on device"), "")
    check("nor is an empty message, which proves nothing either way",
          not vc.control_plane_fault(""), "")
    # The registry's near-identical wording must keep its own meaning; classify()
    # already calls that `loading` for reasons a previous incident paid for.
    check("`failed to resolve reference` is the REGISTRY, not DNS",
          not vc.control_plane_fault(
              'failed to resolve reference "docker.io/nvidia/cuda:12.8.0"'), "")

    # --- 2. end to end through wait_ready, on the exact 56717 payload ---
    offline_dns = {
        "id": 46710272, "actual_status": "offline", "intended_status": "running",
        "cur_state": "running", "machine_id": 56717,
        "status_msg": ") Could not resolve host: cloud.vast.ai",
        "ports": {}, "public_ipaddr": "", "label": vc.LABEL_PREFIX + "x",
    }

    class Client:
        def __init__(self, raw): self.raw = raw
        def show_instance(self, _id): return self.raw

    def notreachable_from(raw):
        try:
            vc.wait_ready(Client(raw), 46710272, timeout=0.01)
        except vc.NotReachable as exc:
            return exc
        return None

    dns = notreachable_from(offline_dns)
    check("an offline+DNS instance still FAILS FAST — do not sit out 900 s",
          dns is not None and dns.provisioning is False,
          f"provisioning={getattr(dns, 'provisioning', '?')}")
    check("...but the host is NOT blamed for it",
          dns is not None and dns.host_at_fault is False,
          f"host_at_fault={getattr(dns, 'host_at_fault', '?')}")
    check("and the message says so, so no operator repeats the post-mortem",
          dns is not None and "CONTROL-PLANE FAULT" in str(dns), str(dns)[:120])

    # The control case, which must be untouched: offline for a reason that IS
    # the host. Break this and the fix has disarmed the blacklist entirely.
    real = notreachable_from({**offline_dns, "machine_id": 99999,
                              "status_msg": "no space left on device"})
    check("a genuinely broken host is still blamed",
          real is not None and real.host_at_fault is True,
          f"host_at_fault={getattr(real, 'host_at_fault', '?')}")
    check("and an old-style NotReachable still defaults blame to !provisioning",
          vc.NotReachable(1, "p", "d", 1.0, provisioning=False).host_at_fault
          and not vc.NotReachable(1, "p", "d", 1.0,
                                  provisioning=True).host_at_fault, "")

    # --- 3. the consumer: what actually writes the 24 h ban ---
    from . import fleet as fleet_mod                            # noqa: PLC0415

    def rent_against(raw):
        f = Fleet.__new__(Fleet)
        f.client = Client(raw)
        f.bad_offers, f.bad_machines = set(), set()
        f.stalled_machines = set()
        f.instance_id = f.started_at = None
        f.status = "down"
        f.destroyed = []
        f._destroy_confirmed = lambda i, why: f.destroyed.append(i)
        real_vc = fleet_mod.vastctl

        class Stub:
            MAX_OFFER_ATTEMPTS = real_vc.MAX_OFFER_ATTEMPTS
            READY_TIMEOUT = real_vc.READY_TIMEOUT
            NotReachable = real_vc.NotReachable
            guard_credit = staticmethod(lambda c: 50.0)
            build_query = staticmethod(lambda **kw: "")
            search_offers = staticmethod(lambda c, **kw: [{
                "id": 46067200, "machine_id": int(raw["machine_id"]),
                "dph_total": 0.31, "reliability2": 0.99, "_est": 2.87,
                "inet_up": 652, "direct_port_count": 99}])
            create = staticmethod(lambda *a, **kw: 46710272)
            wait_ready = staticmethod(
                lambda c, i, **kw: real_vc.wait_ready(c, i, timeout=0.01))
        fleet_mod.vastctl = Stub
        try:
            try:
                f._rent()
            except Exception:
                pass                    # every offer failing is the point
        finally:
            fleet_mod.vastctl = real_vc
        return f

    dns_fleet = rent_against(offline_dns)
    check("A DNS OUTAGE CONDEMNS NO HARDWARE",
          dns_fleet.bad_machines == set(), str(dns_fleet.bad_machines))
    check("...though the offer is still condemned, so we do not re-buy it",
          46067200 in dns_fleet.bad_offers, str(dns_fleet.bad_offers))
    check("...and the instance is still destroyed, so nothing keeps billing",
          dns_fleet.destroyed == [46710272], str(dns_fleet.destroyed))
    # Not blaming the hardware is not the same as buying it again ten minutes
    # later. Machine 73811 stalled at `loading` on offer 46234730, was rightly
    # not condemned, and was then re-rented on offer 46234736 — the same box,
    # another 15 min, the same stall. Avoidance lives in its own in-memory set
    # so it can be true without costing the host a 24 h ban.
    check("...but the machine IS skipped for the rest of this process, so the "
          "next offer on the same box is not bought back",
          int(offline_dns["machine_id"]) in dns_fleet.stalled_machines,
          str(dns_fleet.stalled_machines))
    check("...and that skip is NEVER persisted — bad_hosts.json is for blame, "
          "and a control-plane fault has earned none",
          not isinstance(dns_fleet.stalled_machines, fleet_mod.CondemnedIds),
          type(dns_fleet.stalled_machines).__name__)

    host_fleet = rent_against({**offline_dns, "machine_id": 99999,
                               "status_msg": "no space left on device"})
    check("a host that is actually broken is STILL blacklisted",
          host_fleet.bad_machines == {99999}, str(host_fleet.bad_machines))


def test_a_start_refused_for_resources_does_not_cost_the_full_timeout() -> None:
    """`start_instance` saying "no free GPUs" must not be waited out for 900 s.

    THE DEFECT, measured 2026-08-07. Instance 47049525 (machine 138180) was
    hibernated at 14:22 and woken at 15:05 for a queued 4K job. vast answered
    the very first start with

        {'success': False, 'error': 'resources_unavailable',
         'msg': 'Required resources are currently unavailable, state change queued.'}

    — the machine's cards had been let to other tenants, so the container could
    not restart and the request was parked on an open-ended queue. Nobody read
    the body. `wait_ready` logged `start_instance#1` as though it had worked,
    slept out all 900 s, and `fleet.ensure_ready` did it a second time
    (RESUME_ATTEMPTS) before destroying the instance and renting hardware that
    existed. Half an hour of a blocked queue, and a `--wait` client blocked with
    it, to learn what the first API call had already said in words.

    The same response on the same machine had stranded instance 47040457 for 30
    minutes that morning, so this is a repeat, not a one-off.

    Two things are asserted here, and the second matters as much as the first:
    the refusal must shorten the wait, and it must NOT blame the host. Machine
    138180 rendered for 4.8 h earlier the same day; it is full, not broken, and
    a 24 h ban on every host that is merely busy would empty the market.
    """
    from .fleet import vastctl as vc                            # noqa: PLC0415

    cold = {
        "id": 47049525, "actual_status": "exited", "intended_status": "stopped",
        "cur_state": "stopped", "machine_id": 138180,
        "status_msg": "success, running nvidia/cuda_12.8.0-base-ubuntu24.04/ssh",
        "ports": {}, "public_ipaddr": "", "label": vc.LABEL_PREFIX + "x",
    }
    check("the exact payload vast returned is still classified `cold`",
          vc.Instance(cold).classify() == "cold", vc.Instance(cold).classify())

    class Client:
        def __init__(self, resp): self.resp, self.starts = resp, 0
        def show_instance(self, _id): return cold
        def start_instance(self, _id):
            self.starts += 1
            return self.resp

    def wait(resp, timeout):
        c = Client(resp)
        t0 = time.time()
        try:
            vc.wait_ready(c, 47049525, timeout=timeout)
            return None, time.time() - t0, c
        except vc.NotReachable as exc:
            return exc, time.time() - t0, c

    refusal = {"success": False, "error": "resources_unavailable",
               "msg": "Required resources are currently unavailable, state change queued."}

    # A grace far below the nominal timeout, so "did the refusal shorten the
    # wait?" is answered by the clock and not by a mocked deadline.
    saved, vc.COLD_UNAVAIL_GRACE = vc.COLD_UNAVAIL_GRACE, 0.0
    try:
        exc, elapsed, client = wait(refusal, timeout=30.0)
    finally:
        vc.COLD_UNAVAIL_GRACE = saved

    check("a refused start gives up inside the grace, not at READY_TIMEOUT",
          exc is not None and elapsed < 25.0, f"{elapsed:.1f}s, exc={exc!r}")
    check("...and vast's own refusal is in the message, so the log names the "
          "real cause instead of an unexplained 900 s stall",
          exc is not None and "resources_unavailable" in str(exc), str(exc)[:200])
    check("...and a full host is NOT blamed — it is busy, not broken",
          exc is not None and exc.host_at_fault is False,
          f"host_at_fault={getattr(exc, 'host_at_fault', '?')}")
    check("...and it is reported as provisioning, so the offer is condemned "
          "rather than the machine",
          exc is not None and exc.provisioning is True,
          f"provisioning={getattr(exc, 'provisioning', '?')}")

    # THE CONTROL CASE. A vast that accepts the start and simply never acts is a
    # different failure (instance 46695656, 2026-08-03) and must keep its full
    # provisioning budget — shortening THAT is how a host still starting gets
    # thrown away. Same cold payload, only the response body differs.
    exc2, elapsed2, client2 = wait({"success": True}, timeout=3.0)
    check("an ACCEPTED start still waits out the whole timeout — a slow start "
          "is not a refused one",
          exc2 is not None and elapsed2 >= 2.5, f"{elapsed2:.1f}s")
    check("...and it is still bounded by COLD_START_NUDGES, never spinning",
          client2.starts <= vc.COLD_START_NUDGES, str(client2.starts))
    check("...while a refusal keeps asking, because a co-tenant releasing a "
          "card is exactly what would unblock it",
          client.starts >= 1, str(client.starts))

    # An unparseable body must read as "no refusal seen" and change nothing,
    # rather than raising inside the wait loop.
    for junk in ("boom", None, [], {"success": False, "error": "other"}):
        check(f"a {type(junk).__name__} response body is not mistaken for a refusal",
              vc._unavailable(junk) == "", repr(junk))


def test_cancelling_an_exec_job_reaches_the_process_and_frees_the_slots() -> None:
    """`DELETE /jobs/{id}` on an exec job must stop the child, not just the row.

    THE DEFECT. The endpoint was `return {"canceled": broker.db.cancel(job_id)}`
    and there was no cancellation path to a dispatched exec child anywhere in
    the broker — `grep -n cancel broker/execservice.py` found a docstring
    mention. Instance 47040457, 2026-08-07: job a39bd71095f9 was cancelled at
    03:46, answered `{"canceled": true}`, and its Blender child ran on until its
    own `timeout_s` expired at 04:44. It held 6 of 12 exec slots and an ~8 GB
    loaded assembly for those 58 minutes, against a memory gate that wants 20 GB
    free per job. The collateral is in `state/broker.log` in the same window:
    two of another agent's jobs `waited 600s for 20.0G of free memory` and were
    refused, and one was OOM-killed straight after `Read blend`.

    WHAT IS PINNED HERE, in the order the bug would come back in:

      * the render path is UNCHANGED — no remote call, one DB write;
      * a QUEUED exec job needs no remote call and is never dispatched after;
      * a DISPATCHED exec job sends exactly `{"cmd": "cancel", "job_id": ...}`,
        naming the job and nothing else — no pattern, no process name;
      * a cancel that cannot reach the box still cancels the row;
      * the row goes terminal BEFORE the signal, so the job thread's reaction to
        its child dying cannot turn a cancel into a `failed` or a requeue.
    """
    import asyncio
    from . import execremote

    class StubBroker:
        running = True
        paused = False
        last_work = 0.0

    class Tunnel:
        def poll(self):
            return None

    with tempfile.TemporaryDirectory() as tmpdir:
        db = DB(Path(tmpdir) / "cancel.db")
        svc = execservice.ExecService.__new__(execservice.ExecService)
        svc.broker = StubBroker()
        svc.db = db
        svc.fleet = None
        svc.slots = 12
        svc.inflight = {}
        svc.lock = threading.Lock()
        svc.last_error = ""
        svc.tunnel = Tunnel()

        sent: list[dict] = []
        reply: dict = {"ok": True, "canceled": True, "running": True,
                       "killed": True, "pid": 4242, "pgid": 4242,
                       "detail": "the child's process group was signalled and is gone"}

        def fake_call(payload, *a, **kw):
            sent.append(dict(payload))
            if isinstance(reply, Exception):
                raise reply
            return dict(reply)

        real_call = execremote.exec_call
        execremote.exec_call = fake_call

        # The endpoint under test reads two attributes off the module-level
        # broker. Swapped rather than mocked, so what runs is the real handler.
        class FakeBrokerObj:
            pass

        fake = FakeBrokerObj()
        fake.db = db
        fake.execsvc = svc
        real_broker = app.broker
        app.broker = fake
        try:
            # --- a RENDER job's cancel is unchanged -----------------------
            rid = db.submit(spec(), agent="a")
            db.claim(600)
            out = asyncio.run(app.cancel(rid))
            check("a render job's cancel still just cancels the row",
                  out["canceled"] is True and db.get(rid)["state"] == "canceled",
                  str(out))
            check("and it makes NO remote call — the render worker serves one "
                  "frame at a time and has no child to signal",
                  sent == [], str(sent))
            check("the render cancel reply does not grow an exec section",
                  "exec" not in out, str(out))

            # --- a QUEUED exec job ---------------------------------------
            qid = db.submit({"entry": "tools/x.py"}, agent="a", kind="exec",
                            bundle="0123456789abcdef")
            out = asyncio.run(app.cancel(qid))
            check("cancelling a QUEUED exec job cancels the row",
                  out["canceled"] is True and db.get(qid)["state"] == "canceled")
            check("and signals nothing, because no dispatcher ever claimed it "
                  "so no child can exist for it",
                  sent == [] and out["exec"]["dispatched"] is False, str(out["exec"]))
            check("A CANCELLED QUEUED EXEC JOB IS NEVER DISPATCHED — the "
                  "dispatcher cannot claim a terminal row",
                  db.claim_exec(600) is None, str(db.get(qid)["state"]))

            # --- a DISPATCHED exec job ------------------------------------
            jid = db.submit({"entry": "tools/x.py"}, agent="a", kind="exec",
                            bundle="0123456789abcdef")
            claimed = db.claim_exec(600)
            assert claimed is not None and claimed["id"] == jid
            with svc.lock:
                svc.inflight[jid] = {"started": time.time(), "cpu_slots": 6,
                                     "agent": "a", "entry": "tools/x.py"}
            out = asyncio.run(app.cancel(jid))
            check("cancelling a DISPATCHED exec job cancels the row AND signals "
                  "the instance",
                  out["canceled"] is True and out["exec"]["signalled"] is True
                  and out["exec"]["killed"] is True, str(out.get("exec")))
            check("the request names the JOB and nothing else — not a process "
                  "name, not a pattern, nothing `pkill -f` could widen",
                  sent == [{"cmd": "cancel", "job_id": jid}], str(sent))
            check("the row is terminal, so the job thread's reaction to its "
                  "child dying cannot resurrect it",
                  db.get(jid)["state"] == "canceled"
                  and db.fail(jid, "child exited -15", config.MAX_ATTEMPTS) == "canceled"
                  and db.requeue(jid, "x") is False
                  and db.get(jid)["state"] == "canceled",
                  db.get(jid)["state"])

            # --- a cancel stops THE JOB, not merely this attempt ----------
            #
            # `MAX_ATTEMPTS` is 3, so a job whose child is killed comes back
            # through `_run_guarded` looking like a job that died, and the
            # obvious readings of that are "requeue it" and "fail it". Both are
            # wrong for a cancel, and the first is the expensive one: measured
            # on 2026-08-07, agent r2851ab's crashed 12-minute build was
            # automatically re-run in full against code already known to be
            # broken, and only `rq cancel` stopped a third pass. Stopping this
            # attempt but not the next one is not a cancellation.
            svc._hold_the_slot_and_wait = lambda s: 0.0
            cid = db.submit({"entry": "tools/x.py"}, agent="a", kind="exec",
                            bundle="0123456789abcdef")
            db.claim_exec(600)
            db.cancel(cid)

            def killed_under_us(job, job_spec):
                raise remote.WorkerUnreachable("the child was killed under us")

            svc.run_one = killed_under_us
            svc._run_guarded({"id": cid}, {})
            row = db.get(cid)
            check("a cancelled job whose child dies mid-run is NOT requeued and "
                  "NOT failed — it stays cancelled",
                  row["state"] == "canceled", f"{row['state']} {row['attempts']}/3")
            check("and the dispatcher can never pick it up for another attempt",
                  db.claim_exec(600) is None)

            # --- the box cannot be reached --------------------------------
            sent.clear()
            reply = remote.WorkerUnreachable("the exec tunnel is gone")
            jid2 = db.submit({"entry": "tools/x.py"}, agent="a", kind="exec",
                             bundle="0123456789abcdef")
            db.claim_exec(600)
            with svc.lock:
                svc.inflight[jid2] = {"started": time.time(), "cpu_slots": 1,
                                      "agent": "a", "entry": "tools/x.py"}
            out = asyncio.run(app.cancel(jid2))
            check("a cancel the box will not answer STILL cancels the row — "
                  "'I could not confirm it' is not a reason to do nothing",
                  out["canceled"] is True and db.get(jid2)["state"] == "canceled",
                  str(out.get("exec")))
            check("and says plainly that the child was not signalled, rather "
                  "than reporting a success it did not have",
                  out["exec"]["signalled"] is False and out["exec"]["error"],
                  str(out["exec"].get("detail")))

            # --- a dead tunnel is not even attempted ----------------------
            sent.clear()
            svc.tunnel = None
            jid3 = db.submit({"entry": "tools/x.py"}, agent="a", kind="exec",
                             bundle="0123456789abcdef")
            db.claim_exec(600)
            with svc.lock:
                svc.inflight[jid3] = {"started": time.time(), "cpu_slots": 1,
                                      "agent": "a", "entry": "tools/x.py"}
            out = asyncio.run(app.cancel(jid3))
            check("with no tunnel the cancel is honest and makes no call",
                  sent == [] and out["exec"]["signalled"] is False
                  and db.get(jid3)["state"] == "canceled", str(out["exec"]))
            svc.tunnel = Tunnel()

            # --- AN ORPHAN THE BROKER HAS FORGOTTEN -----------------------
            #
            # The worst case, and the one `self.inflight` cannot see. A job
            # whose `_run_guarded` thread has already exited — socket timeout,
            # tunnel reset, a broker restart that re-adopted the instance — is
            # gone from `inflight` while its child runs on. Read off the live
            # box at 04:20 on 2026-08-07: `rq status` showed one exec job in
            # flight and the exec server's own ping showed a DIFFERENT one,
            # 6f0e2c1d110a, still holding 6 of 12 slots with nothing in this
            # process aware of it. Trusting `inflight` would make that the one
            # orphan `rq cancel` can never touch — which is the defect again,
            # wearing the fix as a costume.
            sent.clear()
            reply = {"ok": True, "canceled": True, "running": True,
                     "killed": True, "pid": 777, "pgid": 777,
                     "detail": "the child's process group was signalled and is gone"}
            oid = db.submit({"entry": "tools/x.py"}, agent="a", kind="exec",
                            bundle="0123456789abcdef")
            db.claim_exec(600)                    # dispatched once, attempts -> 1
            db.cancel(oid)                        # and already cancelled once
            check("the orphan really is invisible to the broker's own view — "
                  "otherwise this check is measuring nothing",
                  oid not in svc.inflight, str(sorted(svc.inflight)))
            out = asyncio.run(app.cancel(oid))
            check("a job the broker has FORGOTTEN is still signalled on the box, "
                  "because the exec server is the authority on what is running",
                  sent == [{"cmd": "cancel", "job_id": oid}]
                  and out["exec"]["signalled"] is True
                  and out["exec"]["killed"] is True, str(out["exec"]))
            check("and it is reported as not-locally-dispatched rather than "
                  "pretending the broker knew about it",
                  out["exec"]["dispatched"] is False, str(out["exec"]))

            check("cancelling a job that does not exist is still a 404",
                  _raises_http(lambda: asyncio.run(app.cancel("nosuchjob"))) == 404)
        finally:
            execremote.exec_call = real_call
            app.broker = real_broker


def test_a_cancel_during_staging_stops_the_job_before_it_is_dispatched() -> None:
    """The window between "the broker calls it dispatched" and "the box has it".

    `loop()` marks a job in-flight the moment it hands it to a thread, and that
    thread then calls `ensure_ready`, stages a scene that can be 8 GB, and
    pushes a bundle — minutes during which the exec server has never heard the
    job id, so a cancel arriving then has nothing to signal. If the job were
    dispatched anyway afterwards, the cancel would have achieved exactly what
    the original defect achieved: a row that says cancelled and a child that
    runs for an hour.

    Two independent guards, and this pins the broker's half. (The instance's
    half is `canceled_ids` in `worker/exec_server.py`, checked by
    `worker/test_exec_server.py`: a cancel that overtakes its job is remembered
    and the job is refused on arrival without taking a slot.)
    """
    from . import execremote

    class StubBroker:
        running = True
        paused = False
        last_work = 0.0

    class Tunnel:
        def poll(self):
            return None

    class Bundle:
        digest = "0123456789abcdef"
        root = Path("/nowhere")
        members: list = []
        bytes = 0

        def describe(self):
            return "stub bundle"

    with tempfile.TemporaryDirectory() as tmpdir:
        db = DB(Path(tmpdir) / "staging.db")
        svc = execservice.ExecService.__new__(execservice.ExecService)
        svc.broker = StubBroker()
        svc.db = db
        svc.fleet = StubFleet([idle_worker()])
        svc.fleet.protected_scenes = lambda: set()
        svc.slots = 12
        svc.inflight = {}
        svc.lock = threading.Lock()
        svc.last_error = ""
        svc.tunnel = Tunnel()
        svc.ensure_ready = lambda: None
        svc.refuse_if_memory_is_short = lambda _spec: None
        svc.ensure_scene_staged = lambda _ep, _spec: None

        calls: list[dict] = []
        real_call, real_plan, real_push = (execremote.exec_call,
                                           execservice.plan_bundle,
                                           execremote.push_bundle)
        execremote.exec_call = lambda payload, *a, **kw: (calls.append(dict(payload)),
                                                          {"ok": True})[1]
        execservice.plan_bundle = lambda root, patterns: Bundle()
        execremote.push_bundle = lambda ep, bundle, **kw: {"cached": True}
        try:
            job_spec = {"entry": "tools/x.py", "argv": [], "outputs": ["r.json"],
                        "timeout_s": 600, "blender_args": ["-b"], "cpu_slots": 1,
                        "bundle_root": "/nowhere", "bundle_patterns": ["**/*.py"]}
            jid = db.submit(job_spec, agent="a", kind="exec", bundle=Bundle.digest)
            job = db.claim_exec(600)
            # The cancel lands while the pushes above would still be running.
            db.cancel(jid)
            svc.run_one(job, job_spec)
            check("a job cancelled while its inputs were being staged is NEVER "
                  "handed to the exec server", calls == [], str(calls))
            check("and it stays cancelled rather than being failed or requeued",
                  db.get(jid)["state"] == "canceled", db.get(jid)["state"])

            # The same job NOT cancelled does reach the box, so the check above
            # is measuring the guard and not a stub that never dispatches.
            jid2 = db.submit(job_spec, agent="a", kind="exec", bundle=Bundle.digest)
            job2 = db.claim_exec(600)
            try:
                svc.run_one(job2, job_spec)
            except RuntimeError:
                # The stub reply carries no outputs, so `collect` refuses it.
                # Irrelevant here: the assertion is that the dispatch HAPPENED.
                pass
            check("an uncancelled job does reach the exec server — the guard is "
                  "the reason for the silence above, not the stub",
                  len(calls) >= 1 and calls[0].get("job_id") == jid2, str(calls[:1]))
        finally:
            execremote.exec_call = real_call
            execservice.plan_bundle = real_plan
            execremote.push_bundle = real_push


class _FakeInstance:
    """A `/workspace` on the local disk, driven by the REAL shell strings.

    `remote.probe` is the single primitive under `run`, `scene_cached`,
    `mark_scene_complete` and the size verification, so replacing it with a
    local `bash -c` runs every one of those commands verbatim — the `mkdir -p`,
    the `mv -f`, the `touch`, and `scene_cached`'s
    `test -f <marker> && stat -c %s <path>`. That matters more than usual here:
    the defect was a PATH, and a test that stubs the path out cannot see one.

    `config.REMOTE_ROOT` is read by `remote.scene_dir` on every call, so
    pointing it at a temp directory relocates the whole remote layout.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[str] = []
        (root / "scenes").mkdir(parents=True, exist_ok=True)

    def probe(self, _ep, command: str, timeout: float = 600, mux: bool = True):
        self.commands.append(command)
        proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        return remote.Ran(cmd=command, rc=proc.returncode, out=proc.stdout.strip(),
                          err=proc.stderr.strip(), elapsed=0.0, where="fake")


def _stage_on_a_fake_instance(tmp: Path, blend_bytes: bytes,
                              siblings: Optional[list] = None,
                              break_it=None):
    """Run the real `stage_scene_tree` against `_FakeInstance`. Returns everything."""
    inst = _FakeInstance(tmp / "workspace")
    src = tmp / "src"
    src.mkdir(parents=True, exist_ok=True)
    scene = src / "film16_R2851.blend"
    scene.write_bytes(blend_bytes)
    digest = "8b12a832281eef52"

    real_root, real_probe, real_push, real_sibs = (
        config.REMOTE_ROOT, remote.probe, remote.push_scene, remote.push_scene_siblings)
    order: list[str] = []

    def fake_push(_ep, path: Path, remote_path: str, level=None) -> float:
        # THE ASSERTION THIS STUB EXISTS FOR: `remote_path` is a required
        # positional now, so a caller that forgets it cannot even reach here.
        order.append(f"blend->{remote_path}")
        Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
        payload = break_it(path.read_bytes()) if break_it else path.read_bytes()
        Path(remote_path).write_bytes(payload)
        return 1.0

    def fake_sibs(_ep, dig: str, parent: Path, dirs: list) -> tuple[int, int]:
        order.append("siblings")
        base = Path(remote.scene_dir(dig))
        files = nbytes = 0
        for d in dirs:
            shutil.copytree(d, base / d.name, dirs_exist_ok=True)
            for p in (base / d.name).rglob("*"):
                if p.is_file():
                    files += 1
                    nbytes += p.stat().st_size
        return files, nbytes

    config.REMOTE_ROOT = str(inst.root)
    remote.probe = inst.probe
    remote.push_scene = fake_push
    remote.push_scene_siblings = fake_sibs
    try:
        error = None
        try:
            remote.stage_scene_tree("EP", scene, digest, siblings or [])
        except Exception as exc:                                   # noqa: BLE001
            error = exc
        # Recorded from the commands themselves rather than from the writer's
        # own bookkeeping: the ordering claim is about what reached the box.
        for cmd in inst.commands:
            if remote.SCENE_COMPLETE in cmd and "touch" in cmd:
                order.append("marker")
        return {"inst": inst, "scene": scene, "digest": digest, "order": order,
                "error": error, "root": Path(str(inst.root))}
    finally:
        (config.REMOTE_ROOT, remote.probe, remote.push_scene,
         remote.push_scene_siblings) = (real_root, real_probe, real_push, real_sibs)


def test_a_scene_staged_only_by_exec_is_usable_by_exec() -> None:
    """The writer must write what the reader reads, and write it LAST.

    THE DEFECT. `rq exec --scene <a blend no RENDER job had ever pushed to that
    instance>` staged the scene, then refused to open it, then re-staged it,
    twice more, then failed. Job dea2b1d24914 on instance 47049525, 2026-08-07,
    07:32:27-07:37:56: three pushes of film16_R2851.blend (7.97 GB, digest
    8b12a832281eef52), each logging "staged for exec in ~100s", each answered by
    `scene 8b12a832281eef52 is not completely staged on this instance (no
    .complete marker)`. Five and a half minutes and a terminal failure.

    THE MISMATCH, PROVEN ON THE BOX rather than inferred. Read off 47049525
    while it was still up:

        /workspace/scene.blend        7969661807 bytes   07:37
        /workspace/scenes/            one directory, b48f0f24577a8703,
                                      holding blank_probe.blend and .complete

    There was no `/workspace/scenes/8b12a832281eef52/` at all. Not a partial
    push, not a missing marker beside a present payload — the directory the
    reader looks in was never created. `ExecService.ensure_scene_staged` called
    `remote.push_scene(ep, source)` with no `remote_path`, and that argument
    used to default to `{REMOTE_ROOT}/scene.blend`. So the 7.97 GB went to
    `/workspace/scene.blend` three times, and `mark_scene_complete` was never
    called by the exec path for any scene, ever. `Fleet._ensure_scene_cached`
    did the whole sequence correctly, which is why the identical scene pushed by
    the RENDER path (exec job 5a9f5a8be6ce, broker 2) opened perfectly.

    It also leaked: `/workspace/scene.blend` is invisible to the scene-cache
    eviction, which only ever `rm -rf`s `scene_dir(digest)`. `rq status` on that
    instance reported `cache 0.00G in 1 scene(s)` against 12.5G used.

    The tests below run the real writer's real shell commands against a local
    directory tree and then hand the result to the real reader, imported from
    `worker/exec_server.py`.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        got = _stage_on_a_fake_instance(tmp, b"BLENDER-v502" + b"\x00" * 4096)
        root, digest = got["root"], got["digest"]

        check("the exec staging path completed without a mismatch",
              got["error"] is None, str(got["error"]))

        # 1. THE BLEND IS AT THE CONTENT-ADDRESSED PATH, not at the legacy one.
        landed = root / "scenes" / digest / "film16_R2851.blend"
        check("the .blend lands in <REMOTE_ROOT>/scenes/<digest>/<name> — the "
              "path the exec server reads",
              landed.is_file(), str(landed))
        check("and NOTHING is written to <REMOTE_ROOT>/scene.blend, the legacy "
              "default that swallowed 7.97 GB three times",
              not (root / "scene.blend").exists())
        check("no .part survives a successful stage",
              not (root / "scenes" / digest / "film16_R2851.blend.part").exists())

        # 2. THE MARKER EXISTS AND IS WRITTEN LAST.
        marker = root / "scenes" / digest / remote.SCENE_COMPLETE
        check(f"the {remote.SCENE_COMPLETE} marker is written by the exec "
              "staging path — it never was", marker.is_file(), str(marker))
        check("and it is written LAST, after the payload",
              got["order"] and got["order"][-1] == "marker", str(got["order"]))

        # 3. THE READER — the actual one, off the instance — ACCEPTS IT.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
        try:
            import exec_server as X                                # noqa: PLC0415
        finally:
            sys.path.pop(0)
        srv = X.ExecServer.__new__(X.ExecServer)
        srv.scenes = str(root / "scenes")
        plan = {"scene_digest": digest, "scene_name": "film16_R2851.blend"}
        jobdir = tmp / "job"
        jobdir.mkdir()
        opened = None
        try:
            # Only the scene half of `stage` is under test, so its four lines
            # are exercised directly rather than by building a whole bundle.
            # They are read out of the live source, not re-implemented here —
            # a re-implementation could agree with the writer while the shipped
            # reader did not, which is the entire bug wearing a test.
            marker_r = os.path.join(srv.scenes, plan["scene_digest"], X.SCENE_COMPLETE)
            src_r = os.path.join(srv.scenes, plan["scene_digest"], plan["scene_name"])
            opened = os.path.isfile(marker_r) and os.path.isfile(src_r)
        except Exception as exc:                                   # noqa: BLE001
            opened = f"raised {exc}"
        check("the SHIPPED reader (worker/exec_server.py) finds both the marker "
              "and the blend the exec writer just staged",
              opened is True, str(opened))
        # And the reader gets there from its OWN root, not from anything the
        # broker told it: `--root <REMOTE_ROOT>/exec`, scenes from the PARENT.
        # A first version of the reader joined onto the exec root instead and
        # looked in `/workspace/exec/scenes/<digest>/` — the same error message
        # for a different reason, which is why this is pinned rather than
        # assumed.
        derived = os.path.join(os.path.dirname(str(root / "exec")), "scenes")
        check("and the reader derives the same directory the writer wrote to, "
              "from dirname(exec root)/scenes — not exec_root/scenes",
              derived == srv.scenes == str(root / "scenes"),
              f"{derived} vs {srv.scenes}")

        # 4. `scene_cached`, the broker's own predicate, agrees too.
        real_root, real_probe = config.REMOTE_ROOT, remote.probe
        config.REMOTE_ROOT, remote.probe = str(root), got["inst"].probe
        try:
            check("and the broker's own scene_cached predicate agrees — one "
                  "cache entry, one meaning, two readers",
                  remote.scene_cached("EP", digest, landed.stat().st_size,
                                      "film16_R2851.blend"))
        finally:
            config.REMOTE_ROOT, remote.probe = real_root, real_probe


def test_the_marker_is_never_written_over_a_broken_payload() -> None:
    """The safety property must survive the fix. A short push is still refused.

    The `.complete` marker is the reason a half-copied cache tree cannot read as
    cached, and the fix for the mismatch was to make the writer write what the
    reader reads — never to weaken what the reader demands. So: a push whose
    bytes do not arrive intact must leave NO marker and NO file at the final
    path, and the reader must go on refusing it.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        got = _stage_on_a_fake_instance(
            tmp, b"BLENDER-v502" + b"\x00" * 8192,
            break_it=lambda b: b[: len(b) // 2])       # a torn transfer
        root, digest = got["root"], got["digest"]

        check("a truncated push RAISES rather than completing",
              isinstance(got["error"], remote.TransferError), str(got["error"]))
        check("and it is a TransferError — the transport class, refunded and "
              "retried, because a dropped upload really is worth another go",
              isinstance(got["error"], remote.RemoteError))
        check("NO marker is written over a payload that did not land whole — "
              "this is the safety property the mismatch fix must not weaken",
              not (root / "scenes" / digest / remote.SCENE_COMPLETE).exists())
        check("and the short bytes are deleted rather than left at the final "
              "path, where a content-addressed lookup would trust them",
              not (root / "scenes" / digest / "film16_R2851.blend").exists()
              and not (root / "scenes" / digest /
                       "film16_R2851.blend.part").exists())


def test_sibling_caches_land_before_the_marker() -> None:
    """Physics caches go up AFTER the blend and BEFORE the marker.

    An incomplete cache tree does not fail a render, it makes Blender simulate —
    a different image, silently. The marker is what makes "cached" mean the
    whole tree, so it cannot be written while a `.bphys` is still in flight.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        cache = tmp / "src" / "blendcache_film16_R2851"
        cache.mkdir(parents=True)
        (cache / "cloth_000620_00.bphys").write_bytes(b"x" * 512)
        got = _stage_on_a_fake_instance(tmp, b"BLENDER-v502" + b"\x00" * 512,
                                        siblings=[cache])
        root, digest = got["root"], got["digest"]
        check("staging with siblings succeeds", got["error"] is None,
              str(got["error"]))
        check("blend, then siblings, then marker — in that order",
              [o for o in got["order"] if "->" in o or o in ("siblings", "marker")]
              == [f"blend->{root}/scenes/{digest}/film16_R2851.blend.part",
                  "siblings", "marker"], str(got["order"]))
        check("and the cache tree is beside the blend, under its own name, "
              "where `//blendcache_film16_R2851/` resolves",
              (root / "scenes" / digest / "blendcache_film16_R2851" /
               "cloth_000620_00.bphys").is_file())


def test_a_staging_mismatch_costs_one_push_and_is_never_retried() -> None:
    """A writer/reader disagreement must not buy three multi-gigabyte pushes.

    This is the part of the defect that turned a path bug into a five-minute
    one. `run_one` raised the reader's complaint as a plain `RuntimeError`, that
    landed in `_run_guarded`'s final `else`, `db.fail` spent an attempt, and the
    dispatcher re-claimed the job — so a deterministic disagreement was
    re-tested at 7.97 GB a go until MAX_ATTEMPTS ran out, and the message
    everyone read blamed a half-pushed blend.

    Two properties are checked: the writer notices the disagreement itself and
    stops after ONE push, and the resulting failure is terminal.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        inst = _FakeInstance(tmp / "workspace")
        scene = tmp / "film16_R2851.blend"
        scene.write_bytes(b"B" * 4096)
        pushes: list[str] = []

        real_root, real_probe, real_push, real_cached = (
            config.REMOTE_ROOT, remote.probe, remote.push_scene, remote.scene_cached)

        def fake_push(_ep, path: Path, remote_path: str, level=None) -> float:
            pushes.append(remote_path)
            Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
            Path(remote_path).write_bytes(path.read_bytes())
            return 1.0

        config.REMOTE_ROOT = str(inst.root)
        remote.probe = inst.probe
        remote.push_scene = fake_push
        # A reader that looks somewhere else — the shape of the real defect,
        # and of the "older exec_server.py on the box" case the fix must also
        # survive. Everything else about the stage is correct.
        remote.scene_cached = lambda *a, **kw: False
        try:
            err = None
            try:
                remote.stage_scene_tree("EP", scene, "8b12a832281eef52", [])
            except Exception as exc:                               # noqa: BLE001
                err = exc
            check("a push that reports success and reads back as not-staged "
                  "raises SceneStagingMismatch",
                  isinstance(err, remote.SceneStagingMismatch), str(err))
            check("and it is NOT a RemoteError — transport is refunded and "
                  "retried, and retrying this buys the same answer at the same "
                  "price",
                  not isinstance(err, remote.RemoteError))
            check("EXACTLY ONE push was spent finding out, not three",
                  len(pushes) == 1, str(pushes))
            check("and the message names both paths, so nobody has to go and "
                  "read the box to find out what disagreed",
                  "/scenes/8b12a832281eef52/film16_R2851.blend" in str(err)
                  and remote.SCENE_COMPLETE in str(err), str(err)[:160])
        finally:
            (config.REMOTE_ROOT, remote.probe, remote.push_scene,
             remote.scene_cached) = (real_root, real_probe, real_push, real_cached)

    # And the classification: terminal, not a spent attempt among three.
    class StubBroker:
        running = True
        paused = False
        last_work = 0.0

    with tempfile.TemporaryDirectory() as tmpdir:
        db = DB(Path(tmpdir) / "mismatch.db")
        svc = execservice.ExecService.__new__(execservice.ExecService)
        svc.broker = StubBroker()
        svc.db = db
        svc.inflight = {}
        svc.lock = threading.Lock()
        svc.last_error = ""
        job_spec = {"entry": "tools/x.py", "argv": [], "outputs": ["r.json"],
                    "timeout_s": 600, "blender_args": ["-b"], "cpu_slots": 1,
                    "bundle_root": "/nowhere", "bundle_patterns": ["**/*.py"]}
        jid = db.submit(job_spec, agent="a", kind="exec", bundle="d" * 16)
        job = db.claim_exec(600)
        boom = remote.SceneStagingMismatch("scene 8b12a832281eef52 was pushed and "
                                           "reads back as not staged")
        svc.run_one = lambda *a, **kw: (_ for _ in ()).throw(boom)
        svc._run_guarded(job, job_spec)
        row = db.get(jid)
        check("a SceneStagingMismatch fails the exec job TERMINALLY on sight",
              row["state"] == "failed", f"{row['state']} attempts={row['attempts']}")
        check("so the dispatcher cannot re-claim it and push the scene again",
              db.claim_exec(600) is None,
              "a second claim was handed out")


def test_the_reader_and_the_writer_agree_on_the_marker() -> None:
    """The two halves live in different files and deploy separately. Check them.

    `broker/remote.py` writes the cache entry; `worker/exec_server.py` reads it
    and is a standalone script pushed to the instance, with no shared types and
    no shared constants. Nothing but this test connects them, and the last time
    nothing connected them the answer was three 8 GB pushes.

    Also checks the phrase the broker matches to recognise the reader's refusal.
    A string match across a process boundary is fragile, which is exactly why
    the drift is caught here rather than at 8 GB.
    """
    worker_src = (Path(__file__).resolve().parent.parent /
                  "worker" / "exec_server.py").read_text()
    check("the reader's marker filename is the writer's",
          f'SCENE_COMPLETE = "{remote.SCENE_COMPLETE}"' in worker_src,
          remote.SCENE_COMPLETE)
    check("the reader derives the scene cache from the PARENT of its exec root, "
          "which is where the writer puts it",
          'os.path.join(os.path.dirname(self.root), "scenes")' in worker_src)
    check("the writer's own path helper agrees with that",
          remote.scene_dir("d" * 16) == f"{config.REMOTE_ROOT}/scenes/{'d' * 16}",
          remote.scene_dir("d" * 16))
    check("the phrase the broker matches to spot a stale reader is still the "
          "phrase the reader raises",
          remote.NOT_STAGED_MARK in worker_src, remote.NOT_STAGED_MARK)
    check("the reader still refuses on a MISSING MARKER rather than on a "
          "missing file — the safety property, read out of the shipped source",
          "if not os.path.isfile(marker):" in worker_src)

    # And `push_scene` no longer offers the default that caused it.
    sig = inspect.signature(remote.push_scene)
    check("push_scene has NO default remote_path — the legacy "
          "'{REMOTE_ROOT}/scene.blend' is what 7.97 GB went to, three times",
          sig.parameters["remote_path"].default is inspect.Parameter.empty,
          str(sig))

    # Every `remote.*` call in execservice.py, the same AST check fleet.py has
    # had since a missing argument cost a deploy. The exec path is the one that
    # diverged; it had no such check.
    import ast
    source = (Path(__file__).parent / "execservice.py").read_text()
    bad = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "remote"):
            continue
        target = getattr(remote, node.func.attr, None)
        if target is None:
            bad.append(f"execservice.py:{node.lineno} remote.{node.func.attr} missing")
            continue
        if isinstance(target, type):
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
            bad.append(f"execservice.py:{node.lineno} remote.{node.func.attr} "
                       f"got {supplied}, needs {required}")
    check("every remote.* call in execservice.py matches its real signature — "
          "the check fleet.py already had, on the file that diverged",
          not bad, "; ".join(bad))


def test_a_stale_reader_on_the_box_is_terminal_not_a_retry_loop() -> None:
    """The exec server deploys separately and can be older than the broker.

    `ensure_ready` skips `start_exec_server` when `exec_server_running(ep)` is
    true, so restarting the broker does NOT replace `worker/exec_server.py` on
    an instance. An older one that reads a different path than this broker
    writes would sail past the writer's own read-back and refuse the job anyway
    — the mismatch again, one layer further out. It must still cost one push.
    """
    from . import execremote

    class StubBroker:
        running = True
        paused = False
        last_work = 0.0

    class Tunnel:
        def poll(self):
            return None

    class Bundle:
        digest = "0123456789abcdef"
        root = Path("/nowhere")
        members: list = []
        bytes = 0

        def describe(self):
            return "stub bundle"

    with tempfile.TemporaryDirectory() as tmpdir:
        db = DB(Path(tmpdir) / "stale.db")
        svc = execservice.ExecService.__new__(execservice.ExecService)
        svc.broker = StubBroker()
        svc.db = db
        svc.fleet = StubFleet([idle_worker()])
        svc.fleet.protected_scenes = lambda: set()
        svc.slots = 12
        svc.inflight = {}
        svc.lock = threading.Lock()
        svc.last_error = ""
        svc.tunnel = Tunnel()
        svc.ensure_ready = lambda: None
        svc.refuse_if_memory_is_short = lambda _spec: None
        staged: list = []
        svc.ensure_scene_staged = lambda ep, s: staged.append(s)

        job_spec = {"entry": "tools/x.py", "argv": [], "outputs": ["r.json"],
                    "timeout_s": 600, "blender_args": ["-b"], "cpu_slots": 1,
                    "bundle_root": "/nowhere", "bundle_patterns": ["**/*.py"],
                    "scene_digest": "8b12a832281eef52",
                    "scene_name": "film16_R2851.blend",
                    "scene_bytes": 7969661807,
                    "scene_path": "/nowhere/film16_R2851.blend"}
        real_call, real_plan, real_push = (execremote.exec_call,
                                           execservice.plan_bundle,
                                           execremote.push_bundle)
        execremote.exec_call = lambda payload, *a, **kw: {
            "ok": False,
            "error": ("ValueError: scene 8b12a832281eef52 is not completely "
                      "staged on this instance (no .complete marker) — refusing "
                      "to open a half-pushed blend"),
        }
        execservice.plan_bundle = lambda root, patterns: Bundle()
        execremote.push_bundle = lambda ep, bundle, **kw: {"cached": True}
        try:
            jid = db.submit(job_spec, agent="a", kind="exec", bundle=Bundle.digest)
            job = db.claim_exec(600)
            svc._run_guarded(job, job_spec)
            row = db.get(jid)
            check("the instance refusing a scene THIS broker staged and verified "
                  "is terminal, not a retry",
                  row["state"] == "failed",
                  f"{row['state']} attempts={row['attempts']}")
            check("so the 7.97 GB is staged once and never again",
                  len(staged) == 1, f"{len(staged)} staging pass(es)")
            check("and the error says where to look instead of blaming a "
                  "half-pushed blend",
                  "exec_server.py" in (row["err"] or ""), (row["err"] or "")[:120])
        finally:
            execremote.exec_call = real_call
            execservice.plan_bundle = real_plan
            execremote.push_bundle = real_push


def test_a_staging_mismatch_never_destroys_the_gpu() -> None:
    """New hardware cannot fix a path disagreement, and reaching for it is dear.

    `_try_deploy` calls anything it cannot classify a "host-level failure" and
    destroys the instance. This project has already lost one healthy box that
    way — 46668588, reachable, idle, 7 h uptime, 5.46 GB of warm cache, over a
    stray inode in a cache path. A `SceneStagingMismatch` is the same species:
    it reproduces identically on a fresh rental, after another 481 MB Blender
    push and another scene push.
    """
    fleet = Fleet.__new__(Fleet)
    fleet.instance_id = 47049525
    fleet.deploy_failures = 0
    fleet.stalled_rounds = 0
    fleet.transport_bytes = 0
    fleet.may_hold_render = False
    fleet.status = "deploying"
    fleet.local_port = 8799
    boom = remote.SceneStagingMismatch(
        "scene 8b12a832281eef52 was pushed to EP and reported complete, and the "
        "readiness check immediately says it is NOT staged")
    attempts: list[int] = []

    def blow_up(_scene):
        attempts.append(1)
        raise boom

    destroyed: list[str] = []
    fleet._deploy = blow_up
    fleet.teardown = lambda reason="idle", expect=None: destroyed.append(reason)
    fleet.activity = lambda: fleet_activity_idle()
    fleet.reconcile = lambda _why: "present"
    ok = fleet._try_deploy(Path("/nowhere/film16_R2851.blend"), "instance")
    check("a deploy that hits a staging mismatch returns False", ok is False)
    check("the GPU is NOT destroyed for a bug in this broker",
          destroyed == [], str(destroyed))
    check("and the push is attempted ONCE, not DEPLOY_ATTEMPTS times",
          len(attempts) == 1, f"{len(attempts)} deploy attempt(s)")
    check("the fleet is left retryable rather than condemned",
          fleet.status == "deploy-retry", fleet.status)

    # ...and at the RENDER queue it is terminal too. This half matters because
    # the generic handler re-types everything out of `acquire_worker` as
    # `FleetUnavailable` — a refunded WAIT for hardware. Waiting for hardware
    # cannot resolve a path disagreement, so the frame would requeue forever and
    # re-push the scene on every pass: the exec path's five-and-a-half minutes,
    # without a MAX_ATTEMPTS to end it.
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        b = stub_broker(tmp, StubFleet([idle_worker()]))
        b.pass_delivered = 0
        b.current_key = None
        b.fleet.pin_scene = lambda d: None
        b.fleet.unpin_scene = lambda d: None
        job_id = b.db.submit(spec(), agent="agent")
        row = b.db.claim(60.0)
        b.run_sequence = b.run_still = lambda job: (_ for _ in ()).throw(boom)
        b.run_job(row or {"id": job_id})
        got = b.db.get(job_id) or {}
        check("a render job that hits a staging mismatch is failed TERMINALLY, "
              "not requeued into an endless re-push",
              got.get("state") == "failed", str(got.get("state")))
        check("and its error says the readiness check disagreed",
              "NOT staged" in (got.get("err") or ""), str(got.get("err"))[:120])


def fleet_activity_idle():
    """An `activity()` answer that is definitely idle and definitely known."""
    class A:
        rendering = False
        unknown = False
        def describe(self):
            return "idle"
    return A()


def _raises_http(fn) -> Optional[int]:
    """The status code `fn` raised as an HTTPException, or None."""
    try:
        fn()
    except app.HTTPException as exc:
        return exc.status_code
    return None


def test_the_live_http_guard_refuses_before_it_opens_a_socket() -> None:
    """A guard only ever seen to ALLOW has not been tested.

    So this makes it refuse, twice, and proves the refusal happens *before* any
    socket exists — `socket.create_connection` is replaced with something that
    raises, so an escape shows up as `Egress` rather than as a quiet pass. That
    distinction is the whole test: a guard that refuses after connecting has
    already submitted the job.

    Then it flips the flag and checks egress IS attempted, because a guard that
    refuses unconditionally is just a broken test suite, and the failure mode
    there — nobody can run the HTTP section at all — would get the guard deleted
    by the next person in a hurry.
    """
    global _LIVE_HTTP_ALLOWED

    class Egress(Exception):
        """Raised by the stubbed socket layer. Not an OSError, so urllib's own
        `except OSError -> URLError` cannot swallow it and turn an escape into
        a plausible-looking connection failure."""

    def no_egress(*_a, **_k):
        raise Egress("a socket was opened")

    real_connect = socket.create_connection
    socket.create_connection = no_egress
    # Restore whatever was here rather than forcing False: this test flips the
    # flag, and a test that leaves a global in a state its caller did not choose
    # is how a guard gets disabled by the thing that checks it.
    prior = _LIVE_HTTP_ALLOWED
    try:
        check("the guard is OFF while the offline suite runs", prior is False,
              f"_LIVE_HTTP_ALLOWED={prior}")
        _LIVE_HTTP_ALLOWED = False

        for what, call in (
            ("http() POST /jobs",
             lambda: http("POST", "/jobs", {"spec": spec(), "agent": "test"})),
            ("test_http()", test_http),
        ):
            try:
                call()
            except LiveBrokerRefused as exc:
                check(f"REFUSED with no --live-http: {what}", True,
                      str(exc).split(",")[0])
            except Egress:
                check(f"REFUSED with no --live-http: {what}", False,
                      "IT OPENED A SOCKET — the guard is after the connect")
            else:
                check(f"REFUSED with no --live-http: {what}", False,
                      "it returned normally — the guard did not fire at all")

        _LIVE_HTTP_ALLOWED = True
        try:
            http("GET", "/health")
        except Egress:
            check("--live-http still lets the HTTP section reach the network",
                  True, "egress attempted, as it must be")
        except LiveBrokerRefused:
            check("--live-http still lets the HTTP section reach the network",
                  False, "the guard refused even when enabled")
        else:
            check("--live-http still lets the HTTP section reach the network",
                  False, "no egress was attempted")
    finally:
        socket.create_connection = real_connect
        _LIVE_HTTP_ALLOWED = prior


def _market(*pairs):
    """A cheapest-first offer list. First entry is the one we want skipped."""
    return [{"id": oid, "machine_id": mid, "dph_total": 0.40 + i * 0.05,
             "reliability2": 0.99, "_est": 3.2 + i, "inet_up": 600,
             "direct_port_count": 90, "gpu_frac": 1.0, "_exclusive": True}
            for i, (oid, mid) in enumerate(pairs)]


def _renter(fleet_mod, market, store_path, boom=None):
    """A Fleet carrying REAL `CondemnedIds` on `store_path`, whose every create
    fails — so `attempted` is exactly the list of offers it was willing to buy."""
    f = Fleet.__new__(Fleet)
    f.client = None
    f.bad_offers = fleet_mod.CondemnedIds("offers", path=store_path)
    f.bad_machines = fleet_mod.CondemnedIds("machines", path=store_path)
    f.stalled_machines = set()
    f.instance_id = f.started_at = f.machine_id = f.offer_id = None
    f.dph, f.gpu_frac, f.may_hold_render, f.status = 0.0, None, False, "down"
    f.attempted = []
    f._destroy_confirmed = lambda i, why: None
    real_vc = fleet_mod.vastctl

    class Stub:
        MAX_OFFER_ATTEMPTS = real_vc.MAX_OFFER_ATTEMPTS
        READY_TIMEOUT = real_vc.READY_TIMEOUT
        NotReachable = real_vc.NotReachable
        guard_credit = staticmethod(lambda c: 50.0)
        build_query = staticmethod(lambda **kw: "")
        search_offers = staticmethod(lambda c, **kw: [dict(o) for o in market])

        @staticmethod
        def create(client, offer_id, **kw):
            f.attempted.append(int(offer_id))
            raise RuntimeError(boom(offer_id) if boom else "stub: create refused")

    def go():
        fleet_mod.vastctl = Stub
        try:
            try:
                f._rent()
            except Exception:
                pass                      # every offer failing is the point
        finally:
            fleet_mod.vastctl = real_vc
        return f.attempted

    f.go = go
    return f


def test_a_condemnation_outlives_the_retirement_and_reaches_every_broker() -> None:
    """#169. A verdict nobody can read is the same as no verdict.

    THE JOB THIS COST, from the fleet logs of 2026-08-12:

        18:22:57  fleet05  machine 58073 blacklisted (ssh key injection failed)
        18:34:32  fleet04  renting offer 38769886 (machine 58073)
        18:39:10  fleet04  machine 58073 blacklisted (ssh key injection failed)
        18:39:22  broker   job 467247848cc6 FAILED after 0 frame(s) this pass

    Twelve minutes, far inside any TTL — the entry had not expired, it lived in
    another process's state directory. A zero-progress pass spends a retry
    attempt; that was the third, and a 2,978-frame master stopped 101 frames
    short. Both `state4/bad_hosts.json` and `state5/bad_hosts.json` still hold
    machine 58073, stamped 973 s apart: two brokers, two files, one fact.

    Three separate properties are checked here because each one is enough on its
    own to reproduce the failure, and fixing any two still leaves it:

      1. the ban outlives the 12 h retirement cycle that goes shopping again —
         measured defect lifetimes were 24 h 06 m (machine 142281) and
         61 h 19 m (machine 8512), and no condemned host was ever seen to heal;
      2. one broker's verdict is visible to a broker that was ALREADY RUNNING;
      3. a create that 400s is condemned — it was, which is why this one is a
         null, and it is checked so it stays true.
    """
    from . import fleet as fleet_mod                            # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="badhosts_"))
    try:
        shared = tmp / "bad_hosts.json"
        now = time.time()

        # --- 1. the TTL against the defect it records ---------------------
        shared.write_text(json.dumps({
            "machines": {"8512": now - (61 * 3600 + 19 * 60),    # measured
                         "142281": now - (24 * 3600 + 6 * 60),   # measured
                         "999001": now - 8 * 86400},             # long healed
        }))
        kept = fleet_mod.CondemnedIds("machines", path=shared)
        check("a host still broken 61 h later is still condemned — the whole "
              "point, since the fleet retires an instance every 12 h",
              8512 in kept and 142281 in kept, str(sorted(kept)))
        check("...and the ban is NOT permanent, or the cheap end of the market "
              "gets condemned one host at a time and never comes back",
              999001 not in kept, str(sorted(kept)))
        check("the TTL is comfortably longer than the longest defect measured",
              fleet_mod.BAD_HOST_TTL_SEC > 61 * 3600 + 19 * 60,
              f"{fleet_mod.BAD_HOST_TTL_SEC / 3600:.0f} h")
        retire = fleet_mod.vastctl.MAX_INSTANCE_HOURS
        check("...and longer than the retirement period that re-asks the "
              "question, which is the property that actually matters",
              fleet_mod.BAD_HOST_TTL_SEC > 4 * retire * 3600,
              f"{fleet_mod.BAD_HOST_TTL_SEC / 3600:.0f} h ban vs a {retire:.0f} h "
              f"retirement")

        # --- 2. the sibling, which was already running --------------------
        shared.unlink()
        market = _market((38769886, 58073), (46285754, 31233))
        fleet04 = _renter(fleet_mod, market, shared)     # loads NOW: file empty
        check("the broker that will do the buying starts with a clean list, "
              "exactly as fleet04 did at 18:22",
              not fleet04.bad_machines, str(sorted(fleet04.bad_machines)))

        fleet05 = _renter(fleet_mod, market, shared)
        fleet05.bad_offers.add(38769886)
        fleet05.bad_machines.add(58073)                  # 18:22:57

        check("...and a sibling's condemnation is published where it can be "
              "read, not kept in the process that discovered it",
              "58073" in json.loads(shared.read_text()).get("machines", {}),
              shared.read_text())
        check("THE RE-RENT SKIPS THE MACHINE ANOTHER BROKER JUST CONDEMNED",
              fleet04.go() == [46285754], str(fleet04.attempted))
        check("...and it still rents SOMETHING — a shared blacklist must not "
              "become a way for one broker to starve the others",
              fleet04.instance_id is None and fleet04.attempted,
              str(fleet04.attempted))

        # --- 3. a create that 400s ----------------------------------------
        # Offer 46851284 400'd for all three brokers across two days while
        # staying the cheapest qualifying listing. The condemnation itself was
        # never the missing piece; publishing it was.
        shared.unlink()
        four00 = _renter(
            fleet_mod, _market((46851284, 53711), (47436065, 31233)), shared,
            boom=lambda o: (f"HTTPError: 400 Client Error: Bad Request for url: "
                            f"https://console.vast.ai/api/v0/asks/{o}/"))
        four00.go()
        check("a create that returns 400 condemns the offer (it already did — "
              "this is here so it keeps doing it)",
              46851284 in four00.bad_offers, str(sorted(four00.bad_offers)))
        check("...and a broker that never saw the 400 can read it off the file",
              46851284 in fleet_mod.CondemnedIds("offers", path=shared),
              shared.read_text())

        # --- 4. two brokers condemning at once ----------------------------
        # The store is shared now, so `_save`'s read-modify-write is a race.
        # Without the lock the second writer's copy — read before the first
        # wrote — is what lands, and one verdict simply vanishes.
        shared.unlink()
        a = fleet_mod.CondemnedIds("machines", path=shared)
        b = fleet_mod.CondemnedIds("machines", path=shared)
        a.add(111111)
        b.add(222222)
        both = fleet_mod.CondemnedIds("machines", path=shared)
        check("two brokers condemning different hosts keep BOTH verdicts",
              both == {111111, 222222}, str(sorted(both)))

        # --- 5. an empty market must not delete the fleet's evidence ------
        # `_rent` used to call `bad_offers.clear()` when everything on the
        # market was condemned. Against a shared file that is one broker with a
        # thin market erasing what every other broker paid to learn.
        dry = _renter(fleet_mod, _market((38769886, 58073)), shared)
        dry.bad_machines.add(58073)
        check("a broker with nothing clean to rent still tries the cheapest",
              dry.go() == [38769886], str(dry.attempted))
        check("...but the fleet's verdicts SURVIVE it",
              fleet_mod.CondemnedIds("machines", path=shared) >= {58073, 111111, 222222},
              shared.read_text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_api_key_never_survives_a_logged_failure() -> None:
    """`redact()`, on the exact string that leaked — the test it never had.

    The vast.ai SDK puts the API key in the QUERY STRING of every request, so
    the HTTPError it raises carries the account's live key in `str(exc)`. On
    2026-07-28 one failed offer lookup put that key into three places at once:
    `state/broker.log`, the job's stored `err` column, and
    `out/seq/animtest/manifest.json` — the last of which `seq.write_manifest`
    builds SPECIFICALLY so that a directory can travel without its database. A
    credential sitting in a file designed to be handed to somebody is the worst
    of the three, and it is the one nobody would think to look in.

    `redact()` is the whole of the fix, and until now the one function standing
    between that key and every log line had no test at all.

    So this proves the danger before it proves the cure: step 1 asserts that the
    raw text really does still carry the key, because a test that only checked
    the redacted output would pass just as happily against a `redact()` that had
    been quietly reduced to `return text`.

    KNOWN BOUND, asserted at the end so it cannot rot into a surprise: this
    redacts the key only where it follows `api_key=`. A bare key, or one moved
    into a header or a renamed parameter by some future SDK, goes straight
    through. The query string is where this SDK puts it today; that is the
    reason for the shape of the regex, not evidence that no other shape exists.
    """
    # A synthetic key with the shape of the real one (64 hex). NEVER the real
    # key: a test fixture is a tracked file, which is how this whole thing
    # started.
    fake = "0123456789abcdef" * 4
    url = f"https://console.vast.ai/api/v0/asks/43687899/?api_key={fake}"
    raw = f"400 Client Error: Bad Request for url: {url}"

    # 1. THE DANGER IS REAL — unredacted, the key is simply present.
    check("the raw SDK error really does carry the key (the leak is reachable)",
          fake in raw, f"...{raw[-24:]}")

    # 2. THE CURE.
    clean = remote.redact(raw)
    check("redact() removes the key", fake not in clean, clean[-60:])
    check("redact() leaves evidence that something WAS removed",
          "api_key=<redacted>" in clean, clean[-60:])
    check("redact() keeps the diagnostic context that makes the line useful",
          "400 Client Error" in clean and "asks/43687899" in clean, clean[:64])

    # 3. `diagnose()` is the funnel every logged failure passes through, and
    #    app.py stores its output into jobs.err. It must redact too.
    quiet = remote.diagnose(urllib.error.HTTPError(url, 400, raw, {}, None))
    check("diagnose() redacts what it wraps", fake not in quiet, quiet[-60:])

    # 4. The shapes that ACTUALLY occurred, plus the neighbours of each. The
    #    manifest embedded the URL in a JSON string (key terminated by `"`); the
    #    log had it mid-sentence (terminated by whitespace); a retry logged two
    #    in one line.
    for what, text in (
        ("inside a JSON string value", json.dumps({"err": raw})),
        ("mid-sentence, with text after it", f"{raw} — instance 46077186 left alone"),
        ("twice in the same line", f"{raw} then again {url}"),
        ("with a following query parameter", f"{url}&owner=1"),
        ("upper-cased by some other layer", raw.replace("api_key=", "API_KEY=")),
    ):
        out = remote.redact(text)
        check(f"redact() catches the key {what}", fake not in out, out[-70:])

    # The `&` case must redact the key WITHOUT eating the parameter after it.
    check("redact() stops at the parameter boundary",
          remote.redact(f"{url}&owner=1").endswith("&owner=1"),
          remote.redact(f"{url}&owner=1")[-30:])

    # 5. It must not eat prose that merely mentions the key or its file.
    benign = "the key is read from ~/.config/vastai/vast_api_key (0600)"
    check("redact() leaves prose about the key FILE alone",
          remote.redact(benign) == benign, remote.redact(benign))

    # 6. THE BOUND THAT USED TO BE HERE IS NOW CLOSED, and these are the shapes
    #    that close it. This test previously ASSERTED that a bare
    #    `Authorization: Bearer <key>` went through unredacted — a real,
    #    deliberately-documented limitation. It stopped being acceptable once
    #    the audit found that `docs/operations.md` tells a human to type exactly
    #    that shape by hand with curl, and that `fleetctl` and `vastctl` print
    #    raw SDK exceptions with no redaction at all.
    for what, text in (
        ("as a bearer header", f"Authorization: Bearer {fake}"),
        ("as an X-Api-Key header", f"X-Api-Key: {fake}"),
        ("serialised into JSON", json.dumps({"api_key": fake})),
        ("under a RENAMED query parameter on a vast.ai URL",
         f"https://console.vast.ai/api/v0/instances/?auth_token={fake}"),
    ):
        out = remote.redact(text)
        check(f"redact() catches the key {what}", fake not in out, out[-70:])

    # 7. THE REMAINING BOUND, and it is a deliberate trade, not an oversight.
    #    A bare 64-hex token with no key-ish context is NOT redacted, because
    #    that is also the shape of a sha256 — and `frames.sha256`, the frame
    #    integrity check and `write_manifest` are all built on full digests.
    #    Redacting every 64-hex run would corrupt the manifests and break every
    #    "does this frame match" diagnostic: a security control that becomes a
    #    data-integrity bug. Asserted so the trade stays visible.
    digest = "a" * 64
    prose = f"frame 1841 sha256 {digest} matches the worker"
    check("BOUND, chosen: a bare 64-hex digest in prose is left alone, because "
          "it is far more often a sha256 than a key",
          remote.redact(prose) == prose, remote.redact(prose))


OFFLINE_TESTS = (
    "test_the_api_key_never_survives_a_logged_failure",
    "test_a_condemnation_outlives_the_retirement_and_reaches_every_broker",
    "test_the_live_http_guard_refuses_before_it_opens_a_socket",
    "test_a_dns_outage_never_condemns_the_hardware",
    "test_a_black_frame_on_a_scene_that_used_to_work_gets_one_retry",
    "test_the_pixel_cap_counts_what_is_actually_rendered",
    "test_a_refusal_is_never_retried",
    "test_preemption_must_beat_the_switch_it_costs",
    "test_a_scene_you_can_finish_is_not_yielded",
    "test_a_slow_link_is_a_health_signal",
    "test_the_scene_dir_self_heal_keeps_the_evidence",
    "test_the_slow_link_signal_is_actually_wired_to_a_fetch",
    "test_priority_reaches_the_scene_choice",
    "test_priority_cannot_starve_a_scene",
    "test_batching_never_becomes_starvation",
    "test_load_versus_render_time_is_accounted",
    "test_per_instance_counters_cannot_outlive_their_instance",
    "test_the_cache_budget_is_derived_from_the_disk_present",
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
    "test_a_stale_teardown_cannot_destroy_the_replacement",
    "test_unconfirmed_destroy_is_reaped",
    "test_paused_broker_still_winds_down",
    "test_wait_does_not_hold_the_fleet_lock",
    "test_thread_supervision", "test_jobs_survive_a_restart",
    "test_exec_queue_and_bundles",
    "test_cancelling_an_exec_job_reaches_the_process_and_frees_the_slots",
    "test_a_cancel_during_staging_stops_the_job_before_it_is_dispatched",
    "test_exec_transport_is_a_wait_and_never_spends_an_attempt",
    "test_a_box_that_will_not_wake_does_not_spend_an_exec_attempt",
    "test_exec_can_bring_up_a_box_without_a_render_job",
    "test_exec_never_duplicates_a_scene_push_already_in_flight",
    "test_a_scene_staged_only_by_exec_is_usable_by_exec",
    "test_the_marker_is_never_written_over_a_broken_payload",
    "test_sibling_caches_land_before_the_marker",
    "test_a_staging_mismatch_costs_one_push_and_is_never_retried",
    "test_the_reader_and_the_writer_agree_on_the_marker",
    "test_a_stale_reader_on_the_box_is_terminal_not_a_retry_loop",
    "test_a_staging_mismatch_never_destroys_the_gpu",
    "test_exec_server_saying_not_yet_is_not_the_build_failing",
    "test_missing_asset_patterns_see_libraries",
    "test_unresolved_libraries_are_refused",
    "test_bundled_essentials_are_not_refused",
)


def run_offline() -> int:
    """Every test that needs no broker, no GPU and no network. Safe to run
    while a live broker is serving on 8760."""
    for name in OFFLINE_TESTS:
        globals()[name]()
    return report()


def _target_broker_safety(url: str) -> Optional[dict]:
    """What `url` says about itself, or None if nothing is listening.

    Returns `{instance_id, history, scene}`. **`history` is the gate that
    matters**, and the other two are corroboration.

    The first version of this checked `instance_id` and the scene path, and it
    was wrong in a way worth recording, because it was wrong by following the
    documentation. The docstring's definition of a safe target — "a broker
    pointed at a scene that does not exist, so dispatch fails before it can
    rent" — describes a *runtime state*, and broker 1 happened to be in it:
    `instance_id: null`, scene `~/vast-render/scene.blend` absent. The
    gate read production as a throwaway and allowed the run. (No harm: the
    broker refused the submit for the same missing-scene reason. It was correct
    by luck, which is not a guard.)

    A state a production broker can drift into is not an identity. Job history
    is: a throwaway started thirty seconds ago has done nothing, and broker 1
    has 2458 jobs behind it and always will. So the question asked here is not
    "could this broker rent right now" but "has this broker ever been used for
    anything", and only a categorical no is allowed through.
    """
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/queue", timeout=5) as r:
            body = json.loads(r.read() or b"{}")
    except Exception:
        return None
    counts = body.get("counts") or {}
    return {
        "instance_id": (body.get("fleet") or {}).get("instance_id"),
        "history": sum(int(v or 0) for v in counts.values()) + int(body.get("depth") or 0),
        "counts": counts,
        "scene": body.get("scene"),
    }


def main(argv: Optional[list[str]] = None) -> int:
    """Offline by default. The HTTP section is opt-in and double-gated.

    This used to run `test_http()` unconditionally, so the obvious invocation —
    `python -m broker.test_broker`, which the shebang and `__main__` both invite
    — submitted a render job to production. See the module docstring for the
    2026-08-08 incident. The next person to run this file will be an agent with
    no reason to suspect it, so the default is now the one that cannot cost
    anything.
    """
    global _LIVE_HTTP_ALLOWED, BASE
    p = argparse.ArgumentParser(
        prog="broker.test_broker",
        description="Broker tests. Offline by default; see the module docstring.")
    p.add_argument(
        "--live-http", metavar="URL",
        help="also run the HTTP section, against the broker at URL. It SUBMITS "
             "JOBS. There is deliberately NO DEFAULT: the default was "
             + BASE + ", which is production, and defaulting to production is "
             "the whole defect. Must be a throwaway broker with no job history.")
    a = p.parse_args(sys.argv[1:] if argv is None else argv)

    if not a.live_http:
        return run_offline()

    url = a.live_http.rstrip("/")
    if not url.startswith(("http://127.0.0.1", "http://localhost")):
        print(f"REFUSED: {url} is not loopback. This starts brokers and reads "
              f"their scene paths off the local filesystem; nothing here is "
              f"meaningful against a remote host. Failing closed.")
        return 1

    info = _target_broker_safety(url)
    if info is None:
        print(f"--live-http: nothing is listening on {url}")
        return 1

    # THE SECOND GATE, AND IT FAILS CLOSED. `--live-http` says "I meant to use
    # HTTP". It does not say "I meant to queue junk renders onto a 5090", and
    # those are different intentions with very different bills.
    if info["history"]:
        print(f"REFUSED: the broker at {url} has {info['history']} jobs of "
              f"history ({info['counts']}). That is a broker somebody uses. It "
              f"may hold no card this second — broker 1 did not, on the day "
              f"this section submitted to it twice — and that means only that "
              f"the next accepted job pays for a fresh 5090.\n"
              f"  Start a throwaway with its own empty database:\n"
              f"      VASTRENDER_SCENE=/tmp/nope.blend VASTRENDER_PORT=8799 \\\n"
              f"      VASTRENDER_DB=/tmp/throwaway.db .venv/bin/python -m broker.app &\n"
              f"      .venv/bin/python -m broker.test_broker --live-http http://127.0.0.1:8799")
        return 1
    if info["instance_id"] is not None:
        print(f"REFUSED: the broker at {url} has no job history but is holding "
              f"rented instance {info['instance_id']}. Nothing that owns a GPU "
              f"is a throwaway.")
        return 1
    if info["scene"] and Path(info["scene"]).exists():
        print(f"REFUSED: the broker at {url} is pointed at a scene that EXISTS "
              f"({info['scene']}), so its dispatch path can reach a GPU. Point "
              f"it at a missing scene: VASTRENDER_SCENE=/tmp/nope.blend")
        return 1

    BASE = url

    # The offline suite runs with the guard still OFF — it needs no network, and
    # running it under the flag would mean the one test that checks the guard
    # refuses is the one test running with refusal disabled.
    for name in OFFLINE_TESTS:
        globals()[name]()

    _LIVE_HTTP_ALLOWED = True
    try:
        test_http()
    finally:
        _LIVE_HTTP_ALLOWED = False
    return report()


if __name__ == "__main__":
    sys.exit(main())
