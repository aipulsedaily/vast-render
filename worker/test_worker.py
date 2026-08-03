#!/usr/bin/env python3
"""Correctness checks for the warm render server. Run against a live worker:

    python3 worker/test_worker.py --port 8799

These target the failure modes a *warm* process introduces and a cold one
cannot have: leaked state between jobs, and silently-defaulted parameters.
Speed is measured elsewhere; this is about the server never lying.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import uuid

from client import send, spec

PASS, FAIL = "ok", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((PASS if condition else FAIL, name, detail))


def raw_send(payload: dict, host: str, port: int) -> dict:
    with socket.create_connection((host, port), timeout=30) as sock:
        sock.settimeout(3600)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--cam", default="CAM_FrontQuarter")
    a = ap.parse_args()
    host, port = a.host, a.port

    # An incomplete spec must be refused, not filled in. Defaulting a missing
    # field would make the output depend on server state the agent cannot see.
    partial = spec(a.cam, (320, 180), 8)
    del partial["use_dof"]
    r = raw_send(partial, host, port)
    check("incomplete spec rejected", r.get("ok") is False and "use_dof" in r.get("error", ""),
          r.get("error", "")[:70])

    # An unknown camera is an error, not a fallback to the scene's current one.
    bad = spec("CAM_DoesNotExist", (320, 180), 8)
    r = raw_send(bad, host, port)
    check("unknown camera rejected", r.get("ok") is False and "camera" in r.get("error", "").lower(),
          r.get("error", "")[:70])

    # zoom multiplies pixel density before cropping, so the render is true
    # extra detail rather than an upscale.
    r = raw_send(spec(a.cam, (320, 180), 8, zoom=3.0), host, port)
    check("zoom scales resolution", r.get("resolution") == [960, 540], str(r.get("resolution")))

    # crop-to-border must yield a smaller image than the full frame...
    full = raw_send(spec(a.cam, (400, 400), 8), host, port)
    crop = raw_send(spec(a.cam, (400, 400), 8, border=(0.25, 0.75, 0.25, 0.75)), host, port)
    check("border crops output", crop.get("bytes", 0) < full.get("bytes", 1),
          f"full={full.get('bytes')} crop={crop.get('bytes')}")

    # ...and the NEXT job must come back full-size. This is the leak that a
    # warm server introduces: use_border persists unless explicitly cleared.
    after = raw_send(spec(a.cam, (400, 400), 8), host, port)
    check("border does not leak into next job",
          after.get("resolution") == [400, 400] and
          abs(after.get("bytes", 0) - full.get("bytes", 0)) < full.get("bytes", 1) * 0.05,
          f"full={full.get('bytes')} after={after.get('bytes')}")

    # Same for depth of field, which lives on the camera datablock.
    nodof = raw_send(spec(a.cam, (400, 400), 8, use_dof=False), host, port)
    redof = raw_send(spec(a.cam, (400, 400), 8, use_dof=True), host, port)
    check("dof toggles both ways", nodof.get("ok") and redof.get("ok") and
          nodof.get("bytes") != redof.get("bytes"),
          f"nodof={nodof.get('bytes')} dof={redof.get('bytes')}")

    # The FRAME must not leak either — the state-bleed that survives every
    # transport check. `frame: null` means the scene's own frame (the baseline
    # captured at load); in a warm process the only other candidate,
    # `frame_current`, is whatever the previous job left behind, so a still
    # submitted after an animation would silently render at the wrong frame,
    # reply ok, and verify perfectly. The reply's `frame` field reports what
    # was actually rendered, so no pixel comparison is needed.
    scan = raw_send({"cmd": "scan"}, host, port)
    baseline = scan.get("frame_baseline") or scan.get("frame_current")
    other = int(baseline) + 3
    r_exp = raw_send(spec(a.cam, (64, 64), 1, frame=other, denoiser="NONE",
                          denoise_gpu=False, require_caches=False), host, port)
    check("explicit frame honoured", bool(r_exp.get("ok")) and r_exp.get("frame") == other,
          f"asked {other}, got {r_exp.get('frame')}: {r_exp.get('error', '')[:50]}")
    r_null = raw_send(spec(a.cam, (64, 64), 1, frame=None, denoiser="NONE",
                           denoise_gpu=False, require_caches=False), host, port)
    check("frame:null restores the baseline, not the previous job's frame",
          bool(r_null.get("ok")) and r_null.get("frame") == baseline,
          f"baseline {baseline}, got {r_null.get('frame')} "
          f"(previous job rendered {other})")

    # Two identical specs must produce near-identical output. Not *byte*
    # identical: Cycles accumulates samples in parallel on the GPU, so float
    # ordering varies run to run. Measured spread is 2 bytes undenoised and
    # ~49 with OpenImageDenoise, which magnifies sub-pixel differences — both
    # well under 0.1%. A genuine state leak (border, dof, resolution) moves the
    # size by whole percent, which this tolerance still catches.
    jid = uuid.uuid4().hex[:12]
    one = raw_send(spec(a.cam, (400, 400), 16, job_id=jid + "a"), host, port)
    two = raw_send(spec(a.cam, (400, 400), 16, job_id=jid + "b"), host, port)
    drift = abs(one.get("bytes", 0) - two.get("bytes", 0)) / max(one.get("bytes", 1), 1)
    check("identical specs converge (<0.1%)", drift < 0.001,
          f"{one.get('bytes')} vs {two.get('bytes')} — {drift * 100:.4f}%")

    width = max(len(n) for _, n, _ in results) + 2
    print()
    for status, name, detail in results:
        print(f"  [{status:>4}] {name:<{width}} {detail}")
    failed = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    sys.exit(main())
