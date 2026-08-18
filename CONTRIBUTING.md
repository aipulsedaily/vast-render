# Contributing to vast-render

Thanks for looking. This is working middleware that grew around one project, so
the useful thing to know before you spend effort is what shape of change fits
and what bar it has to clear.

---

## Before your first commit: two things

### 1. Set a git identity that is not your personal address

```bash
git config user.email 'ID+username@users.noreply.github.com'
git config user.name  'Your Name'
```

The numeric ID is on `https://api.github.com/users/<username>`. The repository
ships configured with the generic `noreply@users.noreply.github.com`, which
keeps an address out of the log but means GitHub will not attribute the commits
to you. This is local configuration; it changes nothing already committed.

### 2. Never commit a credential

A vast.ai API key is a **live billing credential**. It rents hardware, destroys
hardware and spends money.

- Keys live in the environment (`VAST_API_KEY`), or in a `.env` you copied from
  `.env.example`. `.env` is gitignored; `.env.example` is tracked.
- `.gitignore` carries a deliberately broad credential block — `.env`, `.env.*`,
  `*.pem`, `*.key`, `id_*`, `*api_key*`, `*secret*`, `*token*`, `credentials*` —
  with an explicit `!.env.example` negation. It is a backstop, not the control.
- Before opening a pull request, run the gate:

  ```bash
  python3 tools/publication/check_publication.py --canary   # prove the tool works
  python3 tools/publication/check_publication.py            # then trust its answer
  ```

  `--canary` first, always. It plants real-shaped secrets in a throwaway
  repository, deletes them so they survive only in history, and requires the
  scanner to still find every one. A secret scanner that prints "clean" is
  indistinguishable from a broken one — and the first version of this scanner
  did report this repository clean while a real key fragment sat in its history.

**If you ever do commit one: rotate it.** Do not amend the commit and consider
it handled. Deleting bytes does not un-expose bytes that already existed
somewhere readable.

### Do not paste raw output into an issue without reading it

`redaction.py` is the single definition of what a secret looks like here, and
everything that prints or stores text routes through it — traceback hooks, the
`err` columns in the database, the sequence manifest, `fleetctl`, `vastctl`.
It is a backstop for the moment something goes wrong, not permission to stop
looking. Read what you are about to paste.

---

## The bar: a change to behaviour comes with a test

Tests live beside the code they cover, and they are the main thing standing
between a bad edit and a dead render pipeline. **A change to the broker without
a test in `broker/test_broker.py` will not be believed.**

Fully offline — rents nothing, contacts nothing, safe to run anywhere:

```bash
.venv/bin/python -m broker.test_broker            # 508/508 at time of writing
.venv/bin/python worker/test_exec_server.py       # 118/118
.venv/bin/python farm/test_claim_crossproc.py     # 8 processes against one queue
.venv/bin/python farm/test_gpu_guard.py           # multi-GPU refusal and pinning
```

Needs a live worker, so it is **not** part of the offline run:

```bash
.venv/bin/python worker/test_worker.py --port 8799
```

Two habits from `farm/test_claim_crossproc.py` that are worth copying, because
this project keeps a catalogue of checks that passed while the thing they
checked was broken:

- **Include a control that fails.** That test runs the same harness with the
  transaction removed and requires it to double-claim. A concurrency test that
  has never seen the race is not evidence that the race is fixed.
- **Report the number, not just the verdict.** "PASS" carries much less than
  "400 claims by 8 processes, zero double-handed, zero SQLITE_BUSY, 64 claims/s".

---

## House style, which is really one rule

**A default carries the measurement or the incident that produced it.**

Look at `broker/config.py` before writing anything. Almost every constant in it
is followed by a comment saying what was measured, what went wrong, or what was
tried and rejected — including the values that were wrong at first and why
raising them was not enough. `EXEC_SCENE_MEM_FACTOR` records being raised from
3.0 to 5.5 *the same day*, by the gate failing to fire.

That convention is the most valuable thing in this repository and it is what a
review will ask you for. A number with no provenance is a number nobody can
safely change later.

Beyond that: standard library preferred, `from __future__ import annotations`,
type hints where they help, no new runtime dependencies without a reason
(`requirements.txt` explains why each existing one is there and which two are
optional).

---

## What fits, and what does not

**Good contributions**

- Failure modes you hit that this project has not seen. Bad-host detection,
  transfer behaviour, vast.ai API surprises — an incident report with numbers is
  valuable even with no patch attached.
- Making it work for GPU work that is not this project's Blender pipeline. The
  brokering, blacklist, retirement and dispatch layers are general; the worker
  is not. That seam is where reuse lives.
- Packaging, portability, non-Linux support. All absent, all real gaps.
- Documentation that corrects something. Especially something the docs claim
  and the code does not do.

**Likely to be declined**

- Removing a safeguard because it fired when you did not want it to. Every one
  of them exists because its absence cost money. Argue with the *threshold* and
  bring the measurement.
- Blanket-redacting anything that looks like a 64-hex string. That is also the
  shape of a sha256, and this project's frame integrity checks are built on full
  digests — the bound is chosen, asserted by a test, and documented.
- Dedup of identical render requests. A params-only hash cannot see scene state,
  so it would silently serve stale frames across a reassembly.
- Caller-supplied job IDs. That was a path-traversal vector; IDs are
  broker-minted UUIDs.

**Please raise before building**

- Anything that changes the wire protocol (`docs/protocol.md`).
- Anything that changes when an instance is created or destroyed. That is the
  part that spends money, and there are four independent destroy paths on
  purpose.

---

## Testing costs money, so mostly do not

You do not need a vast.ai account to work on most of this. The offline suites
above cover the queue, the claim logic, the image checks, the path allowlists and
the exec server. If a change genuinely needs a live rental, say so in the pull
request and describe what you ran, at what price, for how long — and confirm you
destroyed it. `docs/quickstart.md` step 6 is the teardown ritual, and
`scripts/panic.sh` works even with the broker dead.

---

## Licence

By contributing you agree your contribution is licensed under **Apache-2.0**,
the licence of this project — that is section 5 of the licence itself, and there
is no separate CLA.

Source files carry **no per-file licence header**. `LICENSE` and `NOTICE` cover
the whole tree; please do not add headers to some files and not others, because
a partial header set reads as though the unheadered files are excluded. If a
per-file `SPDX-License-Identifier: Apache-2.0` is ever wanted, it should land
across the tree in one change.

Note that the companion repository `f1-round2` — the film this broker was built
to render — is GPL-3.0-or-later, because it is almost entirely `bpy` code.
Apache-2.0 is one-way compatible with GPL-3.0, so code can move from here to
there and not back.
