# Publishing this repository

What has to be true before `vast-render` goes public, what has been verified,
and what only the account owner can do.

Every claim below is reproducible with the command beside it. Where something
was *not* verified, it says so.

---

## THE HARD BLOCKER: rotate the vast.ai API key

**The key must be rotated in the vast.ai console before this repository is
published, and rotation is the only remedy.** Nobody but the account owner can
do it.

This is not a formality and it is not satisfied by anything else in this
document:

- The key existed **in plaintext on disk** on the development machine. An
  earlier round scrubbed it out of three files. Scrubbing a file changes what is
  on disk now; it does not change the fact that the bytes existed somewhere
  readable, in backups, in shell history, in agent transcripts, in terminal
  scrollback.
- The leading 8 hex characters of the real key are **still in this repository's
  git history**, in six blobs *and in one commit message* — see
  [History](#the-history-decision) below.
- The key is a **live billing credential**. It can rent GPUs, destroy instances
  and spend money. The exposure is financial, not reputational.

A perfectly clean repository published against an un-rotated key is still an
exposed key. Rotate first.

After rotating, set the new key as `VAST_API_KEY` in the environment (see
`.env.example`) and confirm nothing broke:

```bash
.venv/bin/python vastctl/vastctl.py status     # should list your instances
```

---

## The publication gate

```bash
.venv/bin/python tools/publication/check_publication.py --canary    # prove the tool works
.venv/bin/python tools/publication/check_publication.py --history   # then trust its answer
```

`--canary` first, always. It builds a throwaway repository, commits four
real-shaped secrets, **deletes them in a second commit so they survive only in
history**, and requires the scanner to still find every one. A secret scanner
that prints "clean" is indistinguishable from a secret scanner that is broken,
and this project keeps a whole catalogue of checks that passed while the thing
they checked was broken. `--canary` is what makes a clean report mean something.

That is not hypothetical here. **The first version of this checker reported the
history clean while a real key fragment was sitting in it** — its `api_key`
pattern required 16+ characters and the fragment is 8. The fragment was found by
a hand-written grep, not by the tool. The `api_key-fragment` rule exists because
of that miss.

### What it checks

| check | what it means |
|---|---|
| personal paths | `/home/<someone>` in any tracked file. `/home/user`, `/home/you`, `/home/<user>` are the accepted placeholders |
| email | any address that is not a `users.noreply.github.com`, `sshN.vast.ai`, `example.com` or `example.invalid` placeholder |
| third-party IPs | any address that is not loopback, private, or an RFC 5737 documentation range. Rented hosts belong to **other people** |
| secrets | vast 64-hex keys, truncated `api_key=` fragments, AWS/GitHub/Slack/OpenAI/Anthropic/Google tokens, JWTs, private-key blocks |
| entropy | any base64/hex run of 40+ chars at ≥4.4 bits/char that is not a known-benign digest |
| commit messages | the same secret patterns, over `git log --all` — **not** just file contents |
| config safety | `.env` really is ignored, and `.env.example` really can be committed |

Three of those rows exist because the first draft of this tool did not have them
and was wrong:

- **commit messages.** A blob scan cannot see them, and in this repository the
  commit that *removed* the key fragment quotes it in its own message to explain
  what it was removing. The cleanup commit is a seventh copy of the secret.
- **third-party IPs.** The scanner as first written looked only for credentials
  and would have passed a tree containing thirteen strangers' IP addresses
  without a word. Found by a parallel audit, not by this tool.
- **placeholder suppression, scoped.** `.env.example` exists to contain fake
  values, so the assignment rule ignores obvious placeholders. Applying that
  filter to *every* rule was tried and the canary caught it in seconds: AWS's
  published sample key id ends in the literal word EXAMPLE, so a blanket
  placeholder filter switched off AWS key detection entirely.

---

## Current state, measured

Run on the working tree and the full object database, unreachable objects
included.

| | before this round | after |
|---|---|---|
| tracked files scanned | 48 | 53 |
| personal paths in tracked files | 9 files | **0 in source; 4 remain in `PUBLICATION-AUDIT.md`** |
| non-placeholder emails in tracked files | 0 | **0** (the 3 hits were `noreply@users.noreply.github.com` and `root@sshN.vast.ai` — documentation, not PII) |
| third-party IPs in tracked **source** | 5 addresses, 10 occurrences | **0** — replaced with RFC 5737 addresses |
| third-party IPs in tracked **docs** | 13 | **13, all in `PUBLICATION-AUDIT.md`** |
| secrets in the working tree | 0 | **0** |
| history blobs scanned | 219 | 243, including unreachable |
| secrets in history | unverified claim | **7 locations: 6 blobs + 1 commit message**, all the same 8-hex fragment |

### The remaining working-tree finding is one file

Every outstanding tree finding is inside `docs/PUBLICATION-AUDIT.md` — the
parallel audit that *reported* the IP exposure and, in doing so, listed all
thirteen addresses in the clear, along with four absolute home-directory paths
inside `git clone` transcripts. Its 64-hex string is **not** a leak: it is the SHA-256
*of* the API key, published deliberately so the owner can confirm which key was
audited, and a digest of a 256-bit random secret is not reversible.

**That file is left as its author wrote it.** It argues, correctly, for exactly
the RFC 5737 substitution that has now been applied to the source, and it
explicitly deferred making that change itself because it touches tested code.
The source change has been made and the tests pass; aliasing the addresses in
the audit document is the last step and is the owner's call, because the
document's argument depends on naming what it found.

### The one finding

```
[api_key-fragment] blob 430a14baa8 (<unreachable>):390  api_key=<8 hex of the live key>
[api_key-fragment] blob 68e595b038 (broker/remote.py):390  api_key=<8 hex of the live key>
[api_key-fragment] blob 934ff29931 (broker/remote.py):390  api_key=<8 hex of the live key>
[api_key-fragment] blob a8fbbeb07a (broker/remote.py):390  api_key=<8 hex of the live key>
[api_key-fragment] blob cc9011ce39 (broker/remote.py):390  api_key=<8 hex of the live key>
[api_key-fragment] blob e33cded3a5 (broker/remote.py):390  api_key=<8 hex of the live key>
```

…plus a seventh location that a blob scan cannot reach:

```
[api_key-fragment] commit message d056d4bae0:6  api_key=<8 hex of the live key>
```

A code comment in `broker/remote.py` quoted the leaked log line **verbatim**,
including the first 8 hex characters of the live key, to explain why `redact()`
exists. Commit `d056d4b` replaced it with `<64 hex chars>`; `git grep` on `HEAD`
finds nothing, and the working tree is clean. The blobs remain — **and so does
the message of the very commit that removed it**, which quotes the fragment to
explain what it was deleting. Any rewrite must therefore rewrite commit
*messages*, not just trees. `git filter-repo --email-callback` alone will not do
it; add `--message-callback`.

**How bad is 8 hex characters?** 32 bits of a 256-bit key. It does not
reconstruct the key and it is not brute-forceable into one. It *is* a
confirmation oracle — anyone holding a candidate key can check it against this
prefix — and it is a live credential's bytes in a public repository. It is a
reason to rotate. It is not, on its own, a reason to abandon the history.

This was previously recorded as an unverified note. It is now verified: the
count, the blobs and the removing commit are all reproducible with the command
above.

---

## The history decision

**This is the account owner's call, and all three options are still open.**
Nothing in this round has rewritten history.

The relevant measurements, re-run before you decide:

```bash
git log --all --format='%an <%ae>' | sort | uniq -c | sort -rn   # who authored what
git rev-list --all --count                                        # 62
```

- **62 commits**, of which **40 carry `the author's personal gmail address`** in the author
  and committer fields. The rest are agent identities (`agent@local`,
  `r2-3001@f1round2`) and one already-clean `noreply` address.
- **Exactly 2 commit SHAs are cited anywhere in the documentation, in 2 places.**
  Verified by extracting every 7-40 char hex string from the tracked markdown and
  asking `git cat-file -e <sha>^{commit}` which ones are real commits.

That second number is the one that decides this, and **it is why this repository
can afford an option that the companion repository cannot.** `f1-round2` cites
82 distinct SHAs in 214 places, so a rewrite there invalidates its
cross-references wholesale. Here it invalidates two.

### (a) Publish with history, as is

Keeps all 62 commits and every SHA citation. Publishes the personal email
address in 40 commit headers, permanently and irrevocably — GitHub will index
it and mirrors will copy it. The key fragment stays in the six blobs.

Nothing to run. This is what happens if no decision is made, which is the reason
to make one deliberately.

### (b) Rewrite the history — RECOMMENDED HERE

Keeps all 62 commits and the whole provenance, with a clean address on every
one. Changes every SHA, so the two citations must be re-pointed by hand
afterwards. **Also removes the key fragment**, because the blob containing it is
rewritten along with everything else.

```bash
cp -a ~/vast-render ~/vast-render-rewrite-test   # work on a clone, never the only copy
cd ~/vast-render-rewrite-test
git filter-repo \
  --email-callback \
    'return b"noreply@users.noreply.github.com" if b"@gmail.com" in email else email' \
  --message-callback \
    'import re; return re.sub(rb"api_key=[0-9a-f]+", b"api_key=<redacted>", message)'

# --message-callback is NOT optional. The commit that removed the key fragment
# quotes it in its own message; rewriting only the trees leaves it in `git log`.

# then, and this is not optional either:
.venv/bin/python tools/publication/check_publication.py --history   # fragment should be gone
git log --all --format='%ae' | sort -u                              # no gmail address
grep -rn "<the two old SHAs>" docs/                                 # re-point them
```

Two citations to fix by hand, and the engineering history survives intact.

### (c) Fresh single-commit repository

Loses all 62 commits and the append-only record of what was tried and what was
wrong — which is the most interesting thing about this project. Guarantees no
address and no fragment survives, because there is no history to survive in.

```bash
tools/publication/make_fresh_init.sh              # writes to ~/publish/vast-render-fresh
```

It never writes inside this repository, never deletes anything, refuses a
non-empty destination, and creates no remote.

**Whichever is chosen, (a), (b) and (c) all still require the key rotation
above.** A rewrite removes the fragment from the repository; it does not
un-expose a key that has already existed in plaintext.

---

## Configuring a fresh clone safely

A stranger must be able to run this without ever putting a credential in the
tree.

```bash
cp .env.example .env        # .env is gitignored; .env.example is tracked
$EDITOR .env                # fill in VAST_API_KEY
set -a && . ./.env && set +a
```

`VAST_API_KEY` in the environment takes precedence over every config file the
SDK looks at, and the broker never passes an explicit key, so the environment
variable is the supported path.

The `.gitignore` credential block is deliberately broad (`.env`, `.env.*`,
`*.pem`, `*.key`, `id_*`, `*api_key*`, `*secret*`, `*token*`, `credentials*`)
and carries an explicit `!.env.example` negation — **without which `.env.*`
silently swallowed the template**, so the one file a stranger needs in order to
configure the tool safely could not be added to the repository. `check_publication.py`
asserts both halves with `git add --dry-run`, which is the behaviour that
matters; `git check-ignore` prints the matching rule for a negation too and
reads like a failure when it is a pass.

### Path allowlists fail CLOSED on a fresh clone

`config.DEFAULT_SCENE_ROOTS` and `execservice.DEFAULT_BUNDLE_ROOTS` name the two
sibling project trees this broker was written for, `~`-relative, and each is
used only if that directory exists. On any other machine the default allowlist
is empty apart from the broker's own `scenes/`, and every other scene path is
**refused** until `VASTRENDER_SCENE_ROOTS` / `VASTRENDER_BUNDLE_ROOTS` say
otherwise. That refusal is intended: an allowlist that fails open is not an
allowlist.

---

## Runtime-written files: ignored by design, not by luck

Everything this tool writes while running was checked against `.gitignore`,
with the matching rule recorded rather than assumed:

| written at runtime | rule that covers it |
|---|---|
| `state/`, `state2/` … `state12/` — job SQLite, WAL, logs, locks | `.gitignore:22 state/`, `.gitignore:26 state[0-9]*/` |
| `out/`, `out2/` … — returned frames and sequence manifests | `.gitignore:11 out/`, `out[0-9]*/` |
| `farm/bad_hosts.json` — the fleet-wide bad-host blacklist | `.gitignore:38` |
| `farm/hostrates.json` — measured per-machine rates | `.gitignore:63` |
| any `.env`, `*.pem`, `*.key`, `id_*`, `*api_key*`, `*secret*`, `*token*` | the credentials block |

`farm/hostrates.json` is the one worth understanding: it is keyed on **real
vast.ai machine ids** beside the exact rate values that `docs/fleet.md` publishes
under the aliases "host A-D". Anyone holding both files joins them on those
values and recovers every identifier the aliasing existed to hide. It was
tracked until 2026-08-15. It is untracked rather than genericised because the
machine id *is* the lookup key, so opaque labels would not break the leak, they
would break the table.

Checked, and clean:

- **No tracked file matches any credential ignore rule** — so no source file was
  made invisible by the broad patterns.
- **No key-shaped string in any live `broker.log`.** The 64-hex strings in
  `broker.db` are `frames.sha256` digests, confirmed against the schema; the
  real key prefix appears in none of them.

---

## The credential paths, audited

`redaction.py` at the repository root is now the single definition of what a
secret looks like. Full findings and the trace of every path are in
[incidents.md](incidents.md#2026-08-18--the-redactor-guarded-one-of-six-credential-paths).

| path | before | now |
|---|---|---|
| `broker/remote.py` `diagnose()` | redacted `api_key=` only | shared redactor |
| `broker/diagnostics.py` traceback hooks | **unredacted** | redacted |
| `broker/db.py` `jobs.err` / `frames.err` | unredacted at the column | redacted on write |
| `broker/seq.py` `manifest.json` | unredacted | redacted on write |
| `fleetctl` five `{exc}` prints | **unredacted** | redacted |
| `vastctl/vastctl.py` top-level handler | **unredacted** | redacted |

---

## Before you publish — the list

- [ ] **Rotate the vast.ai API key.** Hard blocker. Only the account owner can.
- [ ] Decide (a), (b) or (c) above and record the decision.
- [ ] `check_publication.py --canary` passes.
- [ ] `check_publication.py --history` passes, or its only finding is the key
      fragment and you have chosen (a) knowingly.
- [ ] `.venv/bin/python -m broker.test_broker` passes (508/508 offline).
- [ ] `LICENSE` (Apache-2.0) and `NOTICE` are the licence you actually want —
      they landed as a *recommendation* and are still yours to change.
- [ ] `git remote -v` is empty. Nothing in this repository adds one.
