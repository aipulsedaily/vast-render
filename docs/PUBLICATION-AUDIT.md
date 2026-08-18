# Publication audit — vast-render

### A full-history secret scan, and what is still exposed under each publishing option

Audit date: 2026-08-18. Scope: **every object in the git object database**, not
only the working tree — 219 blobs, 156 trees, 64 commit objects (2 of them
unreachable), plus the 4 uncommitted working-tree changes.

---

## 0. THE BLOCKER THAT NO SCAN CAN CLEAR

> ## The vast.ai API key must be rotated by the account owner before this repository is published.
>
> This is **not** conditional on what the scan found, and it is not satisfied by
> the scrub already performed. The key was written to disk in plaintext at
> `~/.config/vastai/vast_api_key`, and — as §3 documents — its first eight
> characters reached a tracked source file and a commit message in this
> repository. A key that has existed in plaintext outside a secret manager must
> be treated as disclosed. Removing the record does not remove the exposure.
>
> That key **is the account**: it can rent instances, destroy them, and spend
> money. `broker/remote.py` says so in the comment that motivated `redact()`.
>
> Rotate at <https://console.vast.ai/> → Account → API keys: issue a new key,
> update `VAST_API_KEY` in the environment and `~/.config/vastai/vast_api_key`,
> then **delete the old key server-side**. Only the account owner can do this,
> and until it is done the risk is live regardless of publication.

---

## 1. Verdict for this repository

**CONDITIONAL GO — one credential fragment in history, and it is real.**

| Class | Result |
|---|---|
| vast.ai API key — full 64 hex | **0 occurrences** in history or tree |
| vast.ai API key — first 16 hex | **0 occurrences** |
| vast.ai API key — **first 8 hex** | **6 blobs + 1 commit message** — see §3 |
| Private-key headers (RSA/EC/OPENSSH/PGP) | 0 |
| AWS / GCP / Azure credentials | 0 |
| GitHub / Slack / Stripe / OpenAI / Anthropic / npm / PyPI / HF tokens | 0 |
| JWTs, `Authorization: Bearer` headers | 0 |
| `.env`, `.netrc`, `*.pem`, `*.key`, `id_rsa`, credentials files — **ever committed** | 0 |
| High-entropy strings not explained as a path, hash or identifier | **0** |
| 64-hex strings of any kind | **0** (none at all in this repo) |
| Real routable IP addresses | **13** — 5 in the current tree, see §4 |
| Personal email in commit author/committer fields | **40 of 62** commits — see §5 |

The condition is §3. Whether it blocks publication is argued there, honestly, in
both directions.

---

## 2. Why a zero from this scanner is worth something

**A search proves nothing unless the needle is real.** An earlier pass ran
`git log -S` against an API-key prefix that had been *guessed* rather than read,
got zero hits, and briefly concluded the history was clean. That result was
worthless.

**The needle was read, not guessed.** The live key was read from
`~/.config/vastai/vast_api_key` — 65 bytes, i.e. 64 lowercase hex characters
plus a newline — which is what establishes the vast.ai key format. The key
appears nowhere in this document. Its SHA-256, safe to publish and sufficient
for the owner to confirm which key was audited, is:

```
8e41ee3c9ac96fd77d06379d6bd18ec66d7b90a07fe409f131a2d64a11224aed
```

**The scanner was proved to work before any zero from it was believed.** A
throwaway repository was built with secrets committed and then *deleted* in a
later commit, so they survived only in history:

```
$ git ls-files            # working tree empty — nothing left to see
$ python3 scan.py canary
blobs_scanned 4 commits 4
SUMMARY: {
 "LIVE_VAST_KEY_FULL": 1, "LIVE_VAST_KEY_PREFIX16": 1,
 "LIVE_VAST_KEY_PREFIX8_ANYCTX": 1, "PLANTED_CANARY": 1,
 "aws_akia": 1, "github_tok": 1, "hex64": 2, "private_key_hdr": 1
}
secret_filenames: [['214d9fad…', '.env'], ['491aaa85…', 'id_rsa']]
```

Every planted secret was found in a blob unreachable from the tree. Only then
was the same scanner, unchanged, run here — where it found the six real hits in
§3, and zero of everything else.

**Object enumeration used `git cat-file --batch-all-objects`**, which walks the
entire object database *including objects no ref points at*. This was not
academic: one of the six key-bearing blobs in this repository is unreachable,
and `git rev-list --all --reflog` does not list it.

```
$ git cat-file --batch-all-objects --batch-check='%(objecttype)' | sort | uniq -c
    219 blob
     64 commit
    156 tree
$ git rev-list --all --objects --reflog | grep -c 430a14baa8b32a5907deb29b716f380fea135d5d
0
$ git fsck --unreachable | grep 430a14ba
unreachable blob 430a14baa8b32a5907deb29b716f380fea135d5d
```

---

## 3. The finding: eight characters of the live key, in six blobs and one commit message

**The prior claim was that an 8-character API-key prefix remained in six
historical blobs. That claim is correct.** It is settled here, with the blobs
named — and the audit additionally found a seventh location the earlier round
missed.

### What is actually there

`broker/remote.py` carries a comment explaining why `redact()` exists, quoting a
failure URL observed verbatim in `broker.log`. Until 2026-08-15, that comment
quoted the real key **truncated to its first eight hex characters followed by a
literal ellipsis**:

```
#     https://console.vast.ai/api/v0/asks/43687899/?api_key=<8 real hex chars>...
```

It now reads:

```
#     https://console.vast.ai/api/v0/asks/43687899/?api_key=<64 hex chars>
```

### The six blobs, named exactly

```
$ P8=$(cut -c1-8 ~/.config/vastai/vast_api_key)
$ for b in <the six>; do echo "$b $(git cat-file -s $b)"; done
430a14baa8b32a5907deb29b716f380fea135d5d  127129   <UNREACHABLE — no ref, no path>
68e595b03859c9fc1850a7bf51a1b0bd22b54f6e  123210   broker/remote.py
934ff29931c604e893d14c8628c23187cfc8ccb8  131807   broker/remote.py
a8fbbeb07a9f002df8ecf098574406754cc4853e  124423   broker/remote.py
cc9011ce39115c755b5b23b2de1768d90abc0eb2  123898   broker/remote.py
e33cded3a51263996efd3c79407b139f7a4b0ccb  141422   broker/remote.py
```

All six hit at line 390 of the file. Five are reachable from `master`; the sixth
(`430a14ba`) is unreachable — a leftover from an amended or reset commit.

**Whether that sixth blob travels depends on how the repository is copied, and
this was tested rather than assumed.** The first attempt at this paragraph
asserted that a clone drops unreachable objects. That is only half true, and the
test caught it:

```
$ git clone /home/zany/vast-render clone2          # local path
$ git -C clone2 cat-file -t 430a14ba…
blob                                                # STILL THERE

$ git clone file:///home/zany/vast-render clone3    # real pack protocol
$ git -C clone3 cat-file -t 430a14ba…
fatal: git cat-file: could not get object info      # gone
```

A clone from a **local path** hardlinks the whole object database, unreachable
objects included — 219 blobs, and the scanner finds **6**. A clone over the
**pack protocol**, which is what `git push` to GitHub does, transfers only
reachable objects — 211 blobs, and the scanner finds **5**:

```
$ python3 scan.py clone3
blobs 211
SUMMARY { "LIVE_VAST_KEY_PREFIX8": 5, … }   # 430a14ba absent
```

**So: publishing by pushing to GitHub leaves the sixth blob behind. Publishing
by copying the directory, or by tarring `.git`, carries it.** `git gc --prune=now`
removes it locally either way and is worth running before any hand-off.

### The seventh location, which the earlier round missed

The commit that *removed* the fragment quotes it in its own message:

```
$ git log --all --pretty='%H %s' | grep d056d4ba
d056d4bae081dc2b0fd08ce5ba61497951cd26d8 secrets: drop the key fragment, cover
redact(), untrack the table that de-aliases the docs

$ git log -1 --pretty=%B d056d4ba | grep -n 'api_key'
45: comment explaining why redact() exists — `api_key=<8 real hex chars>...`,
    verified against
```

A commit message is not a blob and is not scrubbed by a blob-content filter. Any
remediation that only rewrites file contents will leave this behind. It is
included here because it is precisely the kind of thing an audit that stops at
`git grep` does not see.

### How bad is it, honestly

**Eight hex characters is 32 bits of a 256-bit secret.** The remaining 56 hex
characters — 224 bits — are not derivable from it. This fragment does not let
anyone authenticate, and brute-forcing the rest is not a real attack. What it
*does* do is let anyone who holds a candidate key confirm whether it is this
one, and it confirms the key's format and its association with this account.

So: **not directly exploitable, and not nothing.** Two things follow.

1. It is not, on its own, a reason to refuse publication — *provided the key is
   rotated*, at which point the fragment describes a dead credential and is
   harmless.
2. It **is** a reason the rotation in §0 is mandatory rather than advisory. The
   fragment is proof that this key leaked out of its intended storage at least
   once. A key that has done that has no remaining assurance.

---

## 4. Real host IP addresses — third-party infrastructure

Thirteen distinct real routable IPv4 addresses appear across full history (plus
`1.2.3.4`, an obvious test placeholder). These are the addresses of **rented
vast.ai GPU hosts** — third-party machines, not the owner's.

Five survive in the **current tracked tree**:

```
$ git grep -l -F <each>
192.0.2.11    broker/remote.py, broker/test_broker.py
192.0.2.12    broker/config.py,  broker/test_broker.py
192.0.2.13    broker/remote.py,  broker/test_broker.py
192.0.2.14   broker/test_broker.py
192.0.2.15   broker/remote.py
```

They are there for a good reason: they are inside docstrings and test fixtures
recording *observed* failures, and the observation is the point —

```python
err="root@192.0.2.11: Permission denied (publickey).",
# Measured 2026-08-03 on instance 46695656 (192.0.2.12), three independent
#     exit 1 after 0.6s on 192.0.2.13:23972 [stat -c %Y ...]: 1785254527
```

This is the documentation style the project is being published *for*: a real
recorded failure beats a sanitised paraphrase. **Preserve the meaning; replace
the identifier.** Substituting RFC 5737 documentation addresses
(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) keeps every one of these
records exactly as informative while naming no real machine. Note that
`broker/test_broker.py` asserts on some of these strings, so a replacement must
change fixture and assertion together — this audit did not make that change,
because it touches tested code and that is a change the owner should review.

A further eight addresses appear only in **history**, in earlier un-aliased
versions of `docs/operations.md`, `docs/incidents.md` and `docs/fleet.md`
(`192.0.2.16`, `192.0.2.17`, `192.0.2.18`, `192.0.2.19`,
`192.0.2.20`, `192.0.2.21`, `192.0.2.22`, `192.0.2.23`). The docs
themselves were correctly aliased to `host A-D` at some point; the rewrite did
not reach the history.

**`192.0.2.23` appears in both this repository and `f1-round2`**, where it
is mapped to `host-A` in that repo's `tools/publication/sanitise_docs.py`. That
file is tracked. If both repositories are published, that alias table is the key
that de-anonymises this repository's history as well. See §4 of the f1-round2
audit; the fix belongs over there.

Real vast.ai **machine and instance identifiers** also appear in tracked source
and docs (`machine_id = 42763`, `53217`, `55313`, `96679`, `138180`; instances
`46695656`, `46077186`, `43687899`). These are the same class of disclosure as
the IPs — they name third-party hardware. This repository's own `.gitignore`
already reasons about exactly this risk for `farm/hostrates.json`, and untracked
that file for it; the same reasoning applies, more weakly, to these.

Nothing else IP-shaped is a concern: 60 occurrences of `127.0.0.1`, and
`1.2.3.4` as a placeholder. **No LAN/private-range addresses. No references
anywhere to the private `f1-site-part2` website** (`git grep -l "f1-site-part2"`
→ 0 files).

---

## 5. Personal identity in commit metadata

```
$ git log master --pretty='%ae' | sort | uniq -c | sort -rn
     40 <owner-personal-address>@gmail.com       [redacted in this document]
     20 agent@local
      1 r2-3001@f1round2
      1 noreply@users.noreply.github.com
```

**40 of 62 commits on `master` carry a personal Gmail address** in the author or
committer field. One personal address, not two (unlike `f1-round2`).

> **Disclosure — this audit briefly made that worse.** The first draft of this
> document pasted the `git log` output above with the address unredacted, and
> committed it. It is redacted in the working tree now, but the earlier version
> is already a blob in this repository's history, in commit
> `78e349d4a05344ce2b1c02744d9d381a33dcd570`. History was **not** rewritten to
> remove it, because rewriting is the owner's decision and this round was scoped
> not to. Under **Option B or C, scrub blob content as well as author metadata**
> or this one blob will survive the rewrite that was meant to remove the address.

In tracked file *content* the address count is effectively zero: the single
`git grep` hit is `scripts/make_fresh_init.sh` line 24, which is a code comment
describing the rewrite callback itself —

```
#  'return b"noreply@users.noreply.github.com" if b"@gmail.com" in email else email'
```

— i.e. the sanitisation tooling, not a leak.

`.git/config` now sets

```
user.name  = SuperComboGamer
user.email = 36320904+SuperComboGamer@users.noreply.github.com
```

GitHub's standard `ID+username@users.noreply.github.com` privacy address, which
is designed to be public and attributes commits to the account without exposing
a personal mailbox. Future commits are clean.

*(Audit note: this changed during the audit — at 03:39 on 2026-08-18 both repos
still carried the generic `noreply@users.noreply.github.com`. Verify with
`git config --show-origin --get user.email` before publishing rather than
trusting this document.)*

**9 tracked files contain the literal `/home/zany`.**

---

## 6. What remains exposed under each of the three publishing options

The choice is the owner's; this audit does not make it, and **no history was
rewritten in producing this document.**

### Option A — publish as-is, with full history

Ships 62 commits, all SHAs stable.

Still exposed:
- **The 8-character key fragment: 5 blobs on a pushed repository, 6 if the
  directory is copied rather than pushed, plus 1 commit message.**
  Harmless *after* rotation; embarrassing and unnecessary before it.
- One personal Gmail address in 40 of 62 commits, shown on every commit page.
- 13 real third-party host IPs (5 in the current tree, 8 history-only), plus
  machine and instance ids.
- `/home/zany` in 9 tracked files.

**Option A is acceptable only if the key is rotated first.** With rotation, the
fragment is a dead string and this option is defensible.

### Option B — `git filter-repo` rewrite

Fixes: the key fragment in all six blobs **and** — if `--message-callback` is
used, which it must be — the commit message in `d056d4ba`; the Gmail address via
`--mailmap`; the historical IPs via `--replace-text`.

It also drops the unreachable blob `430a14ba` — which a push would have dropped
anyway, but a directory copy would not (see §3).

Cost here is genuinely low, and this is the material difference from `f1-round2`:
that repository has 81 distinct SHAs cited in 220 places across its docs, all of
which a rewrite invalidates. **This repository's docs cite no commit SHAs of its
own**, so a rewrite costs almost nothing beyond the rewrite itself. Note that
`f1-round2` cites *this* repo's commits in a handful of places; check
cross-references before rewriting.

**Option B is the recommended option for this repository.**

### Option C — fresh single-commit init

Fixes everything historical at once, including the commit message and the
unreachable blob.

Cost: 62 commits of history lost. Smaller loss than in `f1-round2`, but this
repository's `docs/incidents.md` derives its authority from being a contemporaneous
record, and a single squashed commit dated today undercuts that.

Still exposed after C: the **five IPs in the current tree**, because they are in
the tree, not the history. Option C does not fix a present-tense leak. `git log
-S` cannot save you from a file you are about to commit.

### Recommendation

Rotate the key (§0). Replace the five current-tree IPs with RFC 5737 addresses
(§4), adjusting the tests that assert on them. Then **Option B**, with a
message-callback so `d056d4ba`'s message is rewritten alongside the blobs. If
the owner prefers to skip the rewrite, Option A is still safe *after rotation* —
the fragment protects nothing once the key is dead.

---

## 7. Reproducing this audit

1. Enumerate every object with `git cat-file --batch-all-objects --batch-check`,
   **not** `rev-list HEAD` — one key-bearing blob here is unreachable.
2. Read the real key from `~/.config/vastai/vast_api_key`; derive needles for the
   full value, the first 16 characters and the first 8. Run the 8-character
   needle **both** with and without a hex word-boundary assertion. Here both
   variants return 6, because the fragment is followed by a literal `...`; but
   in the planted-canary repository the boundary-anchored variant returned **0**
   while the unanchored one returned 1, because there the prefix was followed by
   the rest of the key. A boundary assertion that looks like a false-positive
   filter is, against a *truncated* leak, correct — and against a *whole* key,
   silently blind.
3. Scan commit **messages** as well as blobs. One hit here is in a message only.
4. Add format patterns for private-key headers, `AKIA`/`ASIA`, `AIza`, `ghp_`,
   `xox[abposr]-`, `sk-`/`sk-ant-`, `ya29.`, `npm_`, `hf_`, JWT triplets,
   `Authorization: Bearer`, and 64-lowercase-hex.
5. Add a Shannon-entropy sweep over 24+ character tokens at ≥ 4.4 bits/byte and
   classify the hits. Here the raw count is 10 and all 10 are filesystem paths.
6. **Plant a secret in a scratch clone, delete it in a later commit, and confirm
   the scanner still finds it, before believing any zero.**

Step 6 is the one that was skipped last time, and step 2 is the one that would
have turned a true positive into a false negative.
