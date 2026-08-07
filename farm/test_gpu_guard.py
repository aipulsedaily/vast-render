#!/usr/bin/env python3
"""Prove the multi-GPU guard fires, without renting an 8-GPU box.

    python3 farm/test_gpu_guard.py

WHAT IS BEING GUARDED
---------------------
`worker/server.py:enable_gpu()` used to end with

    for d in prefs.devices:
        d.use = d.type == chosen

which enables EVERY OptiX device on the instance. Measured 2026-08-07
(docs/multi-gpu.md): eight cards on one frame is **1.27x**, not 8x. The master
rendered that way is **186.6 h and $512.05**; on eight separate single-GPU
boxes it is 20.4 h and $74.21. It is the worst option on the board, and it was
what happened when nobody chose — the frames come back correct, only slowly and
at eight times the rent, so nothing in the log, in `rq status` or in the output
distinguishes it from the good case.

WHY THIS TEST CAN EXIST AT ALL
------------------------------
Because the guard was written as `select_devices(devices, kind, slot, take_all)`
— a pure function over anything with `.type` and `.name` — rather than as a
loop inside `enable_gpu` over `bpy.context.preferences`. A guard for a failure
that only appears on a rented 8-GPU box, testable only on a rented 8-GPU box,
is not a guard. It is a hope with a comment above it.

`bpy` is not importable here, so `worker/server.py` is loaded as source and only
the two device functions are compiled. That keeps this test honest — it runs the
SHIPPING text of `select_devices`, not a copy — while needing no Blender.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "worker" / "server.py"


class Dev:
    def __init__(self, name, type_):
        self.name, self.type, self.use = name, type_, False

    def __repr__(self):
        return f"{self.name}{'*' if self.use else ''}"


def load_guard():
    """Compile `select_devices` and `TooManyGPUs` out of the shipping file.

    Not an import: `worker/server.py` does `import bpy` at module scope and bpy
    only exists inside Blender. Not a copy either — the source text of the real
    definitions is extracted and executed, so if somebody edits the guard this
    test tracks the edit.
    """
    tree = ast.parse(SERVER.read_text())
    want = {"TooManyGPUs", "select_devices"}
    picked = [n for n in tree.body
              if getattr(n, "name", None) in want]
    missing = want - {n.name for n in picked}
    if missing:
        raise AssertionError(f"{SERVER} no longer defines {missing} — the "
                             f"multi-GPU guard has been removed or renamed")
    ns = {"log": lambda *a, **k: None}
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(SERVER), "exec"), ns)
    return ns


def main() -> int:
    ns = load_guard()
    select, TooManyGPUs = ns["select_devices"], ns["TooManyGPUs"]
    fails = []

    def check(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
        if not cond:
            fails.append(name)

    print(f"multi-GPU guard, compiled from {SERVER}\n")

    one = [Dev("RTX 5090", "OPTIX"), Dev("EPYC", "CPU")]
    eight = [Dev(f"RTX 5090 #{i}", "OPTIX") for i in range(8)] + [Dev("EPYC", "CPU")]

    # 1. The good case must be completely unaffected. Brokers 1 and 2 are live
    #    on single-GPU cards right now; a guard that changed their behaviour
    #    would be a regression shipped to a running client render.
    got = select(one, "OPTIX")
    check("single-GPU instance: unchanged, uses its one card",
          got == [0], f"indices {got}")

    # 2. THE TRAP. Eight cards, nobody said anything.
    try:
        select(eight, "OPTIX")
        check("8-GPU instance with no instruction: REFUSED", False,
              "it did not raise — the $512 path is still reachable by default")
    except TooManyGPUs as exc:
        msg = str(exc)
        check("8-GPU instance with no instruction: REFUSED", True)
        check("  ...and the refusal names the count", "8 OPTIX devices" in msg)
        check("  ...and states the measured cost", "1.27x" in msg and "$512" in msg)
        check("  ...and says how to proceed deliberately",
              "VASTRENDER_GPU_SLOT" in msg and "VASTRENDER_GPU_ALL" in msg)

    # 3. Pinning: allowed, and it must say WHICH.
    got = select(eight, "OPTIX", slot=3)
    check("8-GPU with VASTRENDER_GPU_SLOT=3: pins exactly one",
          got == [3], f"indices {got}")
    got = select(eight, "OPTIX", slot=0)
    check("8-GPU with VASTRENDER_GPU_SLOT=0: pins exactly one",
          got == [0], f"indices {got}")

    # 4. An out-of-range slot is a refusal, not a silent clamp to 0. A clamp
    #    would put slots 7 and 0 on the same card and halve a fleet silently.
    for bad in (8, -1, 99):
        try:
            select(eight, "OPTIX", slot=bad)
            check(f"slot={bad} refused", False, "it was accepted")
        except TooManyGPUs:
            check(f"slot={bad} refused", True)

    # 5. The deliberate wide case still works — the guard blocks accidents, not
    #    choices. Somebody who has read the numbers may still want all eight.
    got = select(eight, "OPTIX", take_all=True)
    check("VASTRENDER_GPU_ALL=1: takes all eight, deliberately",
          got == list(range(8)), f"{len(got)} device(s)")

    # 6. CPU devices are never counted as GPUs. `prefs.devices` contains the
    #    host CPU, and counting it would make every single-GPU box look like a
    #    two-device box and refuse — i.e. the guard would take the live farm
    #    down on its first restart.
    check("CPU entries are not counted as cards",
          select([Dev("EPYC", "CPU"), Dev("RTX 5090", "OPTIX")], "OPTIX") == [1])

    # 7. Two cards is already the trap. 2x is where the market's cheap-looking
    #    `$/GPU-hr` offers start, and 28 of 42 two-GPU listings were quarter
    #    shares reading "2x RTX 5090" either way (R2-382).
    try:
        select([Dev("a", "OPTIX"), Dev("b", "OPTIX")], "OPTIX")
        check("2-GPU instance also refuses", False)
    except TooManyGPUs:
        check("2-GPU instance also refuses", True)

    print()
    if fails:
        print(f">> STAGE RESULT: FAIL — {len(fails)}: {', '.join(fails)}")
        return 1
    print(">> STAGE RESULT: PASS — the guard refuses an unannounced multi-GPU "
          "instance, pins on request, refuses an out-of-range pin, allows a "
          "deliberate wide run, and leaves single-GPU instances untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
