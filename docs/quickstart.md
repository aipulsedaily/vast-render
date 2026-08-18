# Quickstart — from a fresh clone to a rendered frame

Twenty minutes, of which about ten are the machine installing Blender. It costs
real money: **well under a dollar** for the run below, but not zero, and the
dominant cost is the cold start rather than the render.

> **Before anything else.** This tool rents billable hardware on your vast.ai
> account and destroys it again. It has several independent safeguards and it
> has still, in development, kept an instance alive longer than intended.
> Prepaid credit with autobilling **off** is the only hard ceiling vast.ai
> itself offers — treat it as your real spend cap, not as a formality.

Every command below is meant to be run from the repository root.

---

## 0. What you need first

| | |
|---|---|
| **A vast.ai account with prepaid credit** | There is no free mode. `vastctl` refuses to create an instance below a $2.00 credit floor, which is a guard against surprises, not a budget |
| **An API key** | From the vast.ai console, account page. It is a **live billing credential** |
| **Python 3.13+** | The reference virtualenv is 3.13 |
| **A passphrase-less ed25519 SSH keypair** | The broker is unattended and cannot answer a passphrase prompt |
| **Linux** | `flock`, `fcntl`, `ssh`, `scp` and POSIX signals are used directly. Not tested anywhere else |
| **A `.blend` to render** | This repository does **not** build scenes. Step 4 makes a throwaway one so you are not blocked on that |

`ffmpeg` is only needed if you later want to turn a returned frame sequence into
a video. Nothing here requires it.

---

## 1. Install

```bash
git clone <this-repository> vast-render
cd vast-render
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

There is no packaging: no `pip install .`, no console scripts, no entry points.
You run the tools out of the checkout — `./rq`, `./fleetctl`, `.venv/bin/python
-m broker.app`. That is a genuine limitation and it is listed as one in the
README.

Prove the install before spending anything. This suite is fully offline — it
rents nothing, contacts nothing, and takes a couple of minutes:

```bash
.venv/bin/python -m broker.test_broker      # expect: 508/508 passed
```

**508 is now the number on a clean clone too, and until 2026-08-18 it was not.**
The last check in the imgstat section — the one that measures the real all-black
frame job `0908e534b1d3` returned — was guarded `if real.exists():` against
`out/0908e534b1d3.png`, and `out/` is the first rule in `.gitignore`. On any
clone the file was absent, the check silently did not run, nothing was reported
as skipped, and the suite printed **507/507 passed**, which reads as complete
success. This line said 508. If you cloned this repository before that date and
saw 507, nothing was broken and nothing was wrong with your machine: the fixture
could not reach you. It is tracked now, at `broker/fixtures/0908e534b1d3.png`,
and the check is unconditional — delete the fixture and you get a loud
`507/508` with the reason printed, not a quiet 507/507.

---

## 2. Credentials — environment, never a file in this tree

```bash
cp .env.example .env          # .env is gitignored; .env.example is tracked
$EDITOR .env                  # fill in VAST_API_KEY and the paths
set -a && . ./.env && set +a
```

**Never commit an API key and never write one into a file inside this
repository.** The `.gitignore` credential block (`.env`, `.env.*`, `*.pem`,
`*.key`, `id_*`, `*api_key*`, `*secret*`, `*token*`, `credentials*`) is there to
make that mistake hard, but it is a backstop, not the control.

`VAST_API_KEY` in the environment beats every config file the SDK would
otherwise read, and the broker never passes an explicit key, so the environment
variable is the supported path.

**If the key is ever pasted anywhere readable — a log, a chat, a screenshot, a
commit — rotate it in the console immediately.** Rotation is the only remedy.
Deleting the file does not un-expose bytes that already existed.

Check it works without renting anything:

```bash
.venv/bin/python vastctl/vastctl.py status    # your credit and every instance on the account
```

That command lists **every** instance on the account, not just this tool's, on
purpose: the other half of your bill is the half nothing here created.

---

## 3. SSH key

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_vast_render
cat ~/.ssh/id_vast_render.pub
```

Paste the **public** key into your vast.ai account's SSH keys page. Nothing in
this repository registers it for you, and nothing here ever uploads a private
key — the broker only ever passes `ssh -i <private key>` on your own machine.

The default path is `~/.ssh/id_vast_render`; `VASTRENDER_SSH_KEY` overrides it.
The keypair must have **no passphrase**: there is no human present to type one
when the broker reconnects at 03:00.

---

## 4. A scene to render

If you already have an assembled `.blend`, skip to step 5 and point
`VASTRENDER_SCENE` at it. **The directory holding `VASTRENDER_SCENE` becomes a
permitted scene root automatically**, so a single-scene setup needs no allowlist
configuration; `VASTRENDER_SCENE_ROOTS` is what lets a *client* name some other
scene (see "It refused my scene" below).

If you do not have one, make a throwaway with a local Blender. The broker's own
`scenes/` directory is a permitted root too whenever it exists:

```bash
mkdir -p scenes
blender -b --python-expr '
import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 0))
bpy.ops.object.light_add(type="SUN", location=(4, -4, 6))
bpy.ops.object.camera_add(location=(0, -6, 2), rotation=(1.2, 0, 0))
bpy.context.scene.camera = bpy.context.object
bpy.context.object.name = "CAM_Test"
bpy.context.scene.render.engine = "CYCLES"
bpy.context.scene.cycles.device = "GPU"
bpy.ops.wm.save_as_mainfile(filepath="scenes/quickstart.blend")'
```

**Save it with the same Blender version you will render it with.** A scene
should be rendered by the Blender that wrote it; `VASTRENDER_BLENDER_VERSION`
(default `5.2.0`) decides what gets installed on the instance.

**Pack your textures.** A `.blend` that was not packed stores absolute paths to
its images. Blender warns and renders anyway, so frames come back lit
differently from what you see locally, and nothing fails. The monkey above has
no textures, which is one reason it is the smoke test.

---

## 5. Render one frame

```bash
# start the broker (foreground first — you want to see it work)
VASTRENDER_SCENE="$PWD/scenes/quickstart.blend" .venv/bin/python -m broker.app
```

In a second terminal:

```bash
./rq status                                        # queue, GPU, spend
./rq render --cam CAM_Test --res 1280 720 --samples 64 -o first.png
```

The first job is the expensive one. In order, the broker will:

1. reap any orphan instance carrying its label — **before** creating anything;
2. search offers and print the **projected cost before renting**;
3. create an instance with `--cancel-unavail`, so a failed schedule cannot leave
   a stopped instance quietly billing for storage;
4. install Blender on it and start the warm worker;
5. push the scene, render, fetch the PNG, and **check there is a picture in it**.

Measured cold start on a healthy host is **502 s** — about eight minutes before
the first pixel, against a render measured in seconds at this size. This is
normal and it is why the tool keeps a warm worker rather than renting per frame.

`-o` implies `--wait`. If you would rather not block, drop it and use
`./rq get <job_id> -o first.png` later.

---

## 6. Stop paying

**Do this. It is the step people skip.**

```bash
./rq teardown        # destroy the GPU now
```

Left alone, an idle instance is *stopped* after 5 minutes and *destroyed* after
an hour — and **storage bills for the whole time an instance exists, stopped or
not**. Stopping only ends the GPU meter. If you are done, destroy it.

If the broker is dead, wedged, or you simply do not trust the state:

```bash
scripts/panic.sh                                  # destroys everything carrying the label
.venv/bin/python vastctl/vastctl.py status        # confirm with your own eyes
```

`panic.sh` is idempotent, needs no broker, and is the thing to reach for when
you are unsure. Then confirm against `status`, because a teardown you did not
verify is not a teardown.

---

## What to check when it goes wrong

**"It refused my scene."** `VASTRENDER_SCENE_ROOTS` is a containment check, not
a convenience: a client-supplied path becomes a real filesystem path on **two**
machines, so it is resolved through symlinks and `..` and then required to sit
inside a permitted root. The roots are the directory of `VASTRENDER_SCENE`, the
broker's own `scenes/` if it exists, and anything you list explicitly. On a
fresh clone the built-in defaults name two sibling project trees that do not
exist on your machine, so they are dropped — **any scene outside those
directories is refused until you set `VASTRENDER_SCENE_ROOTS`.** That refusal is
the intended behaviour; an allowlist that fails open is not an allowlist. Verify
what yours resolved to without renting anything:

```bash
.venv/bin/python -c 'from broker import config; print(*config.SCENE_ROOTS, sep="\n")'
```

**"It returned a black frame."** It should have failed the job instead —
`BLACK`, `UNIFORM` and `TRANSPARENT` are terminal unless you pass
`--allow-blank`. Check the camera name and that the scene has a light. The
pixel check exists because a job once returned a valid, correctly-sized,
correctly-hashed PNG that was entirely black, and every file-level check passed
it.

**"The broker will not start."** One broker per state directory, enforced with
`flock` before anything else happens. A second broker used to adopt the running
instance and then destroy it when its own port bind failed.

**"It rented a machine that is somehow terrible."** That happens, and it is the
single most expensive failure mode here — one instance passed every probe while
delivering 14 KB/s downstream, so a 7.5 MB frame took six minutes to fetch
against a 16-second render. Bad hosts are remembered fleet-wide in
`farm/bad_hosts.json` with a 7-day TTL. Read `docs/incidents.md` before a long
run; it is a catalogue of exactly this kind of thing.

**Anything else.** `docs/operations.md` is the runbook, and `docs/agents.md` is
the guide for a client that just wants renders.

---

## Next

- Frame **ranges** rather than single frames — `rq anim`, resume by name,
  per-frame verification: **`docs/agents.md`**.
- Spend caps, host selection, tuning, and the measured A/B results:
  **`docs/operations.md`**.
- What has gone wrong and what each fix actually was: **`docs/incidents.md`**.
