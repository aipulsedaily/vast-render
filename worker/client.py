#!/usr/bin/env python3
"""Client for the warm render server.

`spec()` exists to make the server's strictness usable: the server rejects any
job missing a field, so this builds the complete set in one place. It carries no
render policy of its own — every value is the caller's, and the defaults here
are only what Blender itself would do with an untouched scene.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import uuid
from typing import Any, Optional


def spec(
    camera: str,
    resolution: tuple[int, int],
    samples: int,
    *,
    job_id: Optional[str] = None,
    engine: str = "CYCLES",
    denoiser: str = "OPENIMAGEDENOISE",
    denoise_gpu: bool = True,
    use_dof: Optional[bool] = True,
    film_transparent: bool = False,
    border: Optional[tuple[float, float, float, float]] = None,
    zoom: float = 1.0,
    exposure: Optional[float] = None,
    max_bounces: Optional[int] = None,
    adaptive_threshold: float = 0.01,
    frame: Optional[int] = None,
    persistent_data: Optional[bool] = True,
    require_caches: bool = True,
) -> dict[str, Any]:
    """Build a complete job spec. Every server-controlled knob appears here.

    `use_dof=None` and `persistent_data=None` mean "whatever the .blend says",
    restored from the baseline the worker captured at load — not "leave it",
    which in a warm process means "inherit the previous job".
    """
    return {
        "job_id": job_id or uuid.uuid4().hex[:12],
        "camera": camera,
        "resolution": list(resolution),
        "samples": samples,
        "engine": engine,
        "denoiser": denoiser,
        "denoise_gpu": denoise_gpu,
        "use_dof": use_dof,
        "film_transparent": film_transparent,
        "border": list(border) if border else None,
        "zoom": zoom,
        "exposure": exposure,
        "max_bounces": max_bounces,
        "adaptive_threshold": adaptive_threshold,
        "frame": frame,
        "persistent_data": persistent_data,
        "require_caches": require_caches,
    }


def send(payload: dict, host: str = "127.0.0.1", port: int = 8799, timeout: float = 7200) -> dict:
    with socket.create_connection((host, port), timeout=30) as sock:
        sock.settimeout(timeout)
        sock.sendall(json.dumps(payload).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf)


def wait_ready(host: str, port: int, timeout: float = 900) -> bool:
    """Block until the server finishes loading the scene and pre-warming."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if send({"cmd": "ping"}, host, port, timeout=30).get("ok"):
                return True
        except (OSError, json.JSONDecodeError):
            time.sleep(2)
    return False


def main() -> int:
    p = argparse.ArgumentParser(prog="rq", description="submit a render to the warm worker")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8799)
    # Not marked required: --ping and --shutdown are complete commands on their
    # own, and argparse would otherwise demand render args to turn a worker off.
    p.add_argument("--cam")
    p.add_argument("--res", nargs=2, type=int, metavar=("W", "H"))
    p.add_argument("--samples", type=int)
    p.add_argument("--engine", default="CYCLES")
    p.add_argument("--denoiser", default="OPENIMAGEDENOISE")
    p.add_argument("--no-denoise-gpu", action="store_true")
    p.add_argument("--nodof", action="store_true")
    p.add_argument("--film-transparent", action="store_true")
    p.add_argument("--border", nargs=4, type=float, default=None)
    p.add_argument("--zoom", type=float, default=1.0)
    p.add_argument("--exposure", type=float, default=None)
    p.add_argument("--max-bounces", type=int, default=None)
    p.add_argument("--adaptive-threshold", type=float, default=0.01)
    p.add_argument("--frame", type=int, default=None,
                   help="frame to render; omit to use the scene's current frame")
    p.add_argument("--no-persistent-data", action="store_true")
    p.add_argument("--no-require-caches", action="store_true",
                   help="render even if a physics cache does not cover the frame")
    p.add_argument("--scene-dof", action="store_true",
                   help="use the .blend's own DOF (and its animation) instead of forcing it")
    p.add_argument("--ping", action="store_true")
    p.add_argument("--scan", action="store_true",
                   help="report cameras, frame range and physics caches")
    p.add_argument("--shutdown", action="store_true")
    args = p.parse_args()

    if args.ping:
        print(json.dumps(send({"cmd": "ping"}, args.host, args.port), indent=2))
        return 0
    if args.scan:
        print(json.dumps(send({"cmd": "scan"}, args.host, args.port), indent=2))
        return 0
    if args.shutdown:
        print(json.dumps(send({"cmd": "shutdown"}, args.host, args.port), indent=2))
        return 0

    missing = [n for n in ("cam", "res", "samples") if getattr(args, n) is None]
    if missing:
        p.error(f"a render needs {', '.join('--' + m for m in missing)}")

    payload = spec(
        camera=args.cam,
        resolution=tuple(args.res),
        samples=args.samples,
        engine=args.engine,
        denoiser=args.denoiser,
        denoise_gpu=not args.no_denoise_gpu,
        use_dof=None if args.scene_dof else not args.nodof,
        film_transparent=args.film_transparent,
        border=tuple(args.border) if args.border else None,
        zoom=args.zoom,
        exposure=args.exposure,
        max_bounces=args.max_bounces,
        adaptive_threshold=args.adaptive_threshold,
        frame=args.frame,
        persistent_data=not args.no_persistent_data,
        require_caches=not args.no_require_caches,
    )
    reply = send(payload, args.host, args.port)
    print(json.dumps(reply, indent=2))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
