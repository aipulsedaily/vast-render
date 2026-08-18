# Publication audit — vast-render

### A full-history secret scan, and what is still exposed under each publishing option

Audit date: 2026-08-18. Scope: **every object in the git object database**, not
only the working tree — 219 blobs, 156 trees, 64 commit objects (2 of them
unreachable), plus the 4 uncommitted working-tree changes.

---

## 0. THE BLOCKER THAT NO SCAN CAN CLEAR — CLEARED 2026-08-18

> ## RESOLVED: the account owner reports the vast.ai API key was **revoked** on 2026-08-18, together with an SSH key.
>
> **Revocation is stronger than the rotation this section demanded**, and it
> closes the blocker. A rotated key leaves the old one alive until it is deleted
> server-side; a revoked key cannot authenticate at all. The eight hex
> characters that survive in this repository's history (§3) therefore describe a
> credential that no longer exists, and the plaintext that sat on disk buys
> nothing.
>
> **Two qualifications, and neither is a formality.**
>
> 1. **This is CLIENT-REPORTED, not measured.** Revocation was deliberately
>    *not* verified by calling the vast.ai API with the key. There is no
>    read-only way to ask "is this key dead" that does not involve presenting
>    it — so a test would mean transmitting a possibly-live credential over the
>    network in order to check whether it had stopped being a live credential.
>    The safe confirmation is visual, in the console: Account → API keys, and the
>    old key is **absent from the list**. Whoever publishes this should do that.
> 2. **This does not mean the key was never exposed.** It sat in plaintext at
>    `~/.config/vastai/vast_api_key` — 65 bytes, mode 0600, mtime 2026-07-26 —
>    and its first eight characters reached a tracked source file and a commit
>    message here. That happened, §3 records it, and §3 is not edited. What
>    changed is the *consequence*, not the history. A key that has existed in
>    plaintext outside a secret manager must still be treated as disclosed;
>    revocation is what makes disclosure harmless, not what makes it untrue.
>
> The local key file was still on disk when this was written. It is the owner's
> file, outside anything this repository controls, and deleting it is their call.
>
> **What follows is this section as it stood while the key was live.** It is
> kept verbatim, because the argument for why plaintext means disclosed is the
> argument that got the key revoked.

### The blocker as written (2026-08-15 — 2026-08-18)

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

**~~CONDITIONAL GO~~ → GO on the credential question, as of 2026-08-18.** The
condition was the key, the key is revoked (§0, client-reported), and the
fragment in history is now eight characters of a dead secret. The fragment is
still *there* — nothing below is retracted — it simply no longer protects
anything. The remaining publication decision is history and identity, not
credentials: see §5, §6, and `docs/publication.md`.

**The verdict as originally written:** *CONDITIONAL GO — one credential fragment
in history, and it is real.*

| Class | Result |
|---|---|
| vast.ai API key — full 64 hex | **0 occurrences** in history or tree |
| vast.ai API key — first 16 hex | **0 occurrences** |
| vast.ai API key — **first 8 hex** | ~~6 blobs + 1 commit message~~ **8 blobs + 1 commit message** — see §3 |
| Private-key headers (RSA/EC/OPENSSH/PGP) | 0 |
| AWS / GCP / Azure credentials | 0 |
| GitHub / Slack / Stripe / OpenAI / Anthropic / npm / PyPI / HF tokens | 0 |
| JWTs, `Authorization: Bearer` headers | 0 |
| `.env`, `.netrc`, `*.pem`, `*.key`, `id_rsa`, credentials files — **ever committed** | 0 |
| High-entropy strings not explained as a path, hash or identifier | **0** |
| 64-hex strings of any kind | ~~**0** (none at all in this repo)~~ — **WRONG WHEN WRITTEN, see below** |
| SSH private-key or public-key material (added to the scan set 2026-08-18) | **0** — six pattern-shaped hits across both repos, all six the scanner quoting its own regex; §4 |
| Real routable IP addresses | **13** — ~~5 in the current tree~~ **0 in the current tree since `3935e48`**, 13 in history; §4 |
| Personal email in commit author/committer fields | **40 of 69** commits — see §5 |

> **Correction, 2026-08-18 — "64-hex strings of any kind: 0 (none at all in
> this repo)".** That row was false at the moment it was written, and it was
> falsified *by the document it appears in*. Two lines below the table, §2
> publishes the **SHA-256 of the vast.ai API key** — a 64-lowercase-hex string —
> deliberately, so the owner can confirm which key was audited. The scanner
> agrees: `check_publication.py` lists that exact digest in `BENIGN_64HEX`,
> which is a maintained exception, and an exception only exists for something
> that is *present*.
>
> **The true count is two occurrences of one value**, both the same digest:
> `docs/PUBLICATION-AUDIT.md` (§2) and `tools/publication/check_publication.py`
> (the `BENIGN_64HEX` entry that suppresses it). The number that row was
> reaching for is the useful one and it is unchanged: **zero 64-hex strings that
> are not accounted for.** A count of zero and a count of "two, both explained"
> are very different claims, and a secrets audit that rounds the second down to
> the first has given up the only thing it was for.
>
> Note what this digest now is. §0 records that the key was **revoked** by the
> owner on 2026-08-18 (client-reported). So this is the SHA-256 of a *dead*
> credential — no longer merely irreversible, but a fingerprint of something
> that cannot authenticate. The reason for publishing it stands: it is how the
> owner tells which key this document is about.

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
$ git clone ~/vast-render clone2                    # local path
$ git -C clone2 cat-file -t 430a14ba…
blob                                                # STILL THERE

$ git clone file://$HOME/vast-render clone3         # real pack protocol
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
$ git log --all --pretty='%H %s' | grep 3ffea4c4
3ffea4c4411a6e4364297d272656c9cdbdb5d0d0 secrets: drop the key fragment, cover
redact(), untrack the table that de-aliases the docs

$ git log -1 --pretty=%B 3ffea4c4 | grep -n 'api_key'
45: comment explaining why redact() exists — `api_key=<8 real hex chars>...`,
    verified against
```

A commit message is not a blob and is not scrubbed by a blob-content filter. Any
remediation that only rewrites file contents will leave this behind. It is
included here because it is precisely the kind of thing an audit that stops at
`git grep` does not see.

### The eighth and ninth locations — and the pattern is now unmistakable

> **Correction, 2026-08-18.** "Six blobs and one commit message" was true when
> written and is **no longer the count**. It is **eight blobs and one commit
> message.** Two more blobs carry the fragment, and — this is the part worth
> reading twice — **both were created by the cleanups that were removing it.**
>
> ```
> $ python3 tools/publication/check_publication.py --history | grep api_key-fragment
> ...
> [api_key-fragment] blob 1211342fee (tools/publication/check_publication.py):82
> [api_key-fragment] blob 7fce026a88 (docs/publication.md):91   ← ×6, lines 91-96
> ```
>
> - **`7fce026a88` — an earlier `docs/publication.md`**, which pasted the
>   scanner's own output as worked-example evidence. The output contained the
>   real fragment six times, once per hit. Fixed in `7ce714a`, whose subject is
>   literally *"stop the publication guide from tripping its own gate"*.
> - **`1211342fee` — an earlier `check_publication.py`**, which quoted the real
>   fragment in the comment explaining why the `api_key-fragment` rule exists.
>   Fixed in `3935e48`; that file now says `<8 hex>` and carries a standing rule
>   that nothing in it may quote a real secret, precisely so its self-exclusion
>   from the tree scan cannot be covering a genuine leak.
>
> **So the fragment has now been re-leaked three times by documents whose
> subject is the leak**: the commit message that removed it, the guide that
> demonstrated finding it, and the scanner that defines the rule for catching
> it. This is not carelessness three times over — it is structural. *Explaining
> a secret requires quoting it,* and every explanation is a new copy in a new
> object that the previous cleanup did not touch. §4 of this document did the
> same thing with thirteen IP addresses.
>
> The lesson is a rule, and it is now written into `CONTRIBUTING.md` and the
> scanner: **the artefact that documents a redaction must itself be redacted,
> and it must be run through the gate before it is committed.** A `--history`
> run is not optional after a "cleanup" commit; it is how you find out whether
> the cleanup added a copy.
>
> None of this changes the risk, because §0 records the key as revoked. It
> changes the *count*, and a secrets audit that quietly keeps a stale count is
> not one.

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

> ### CORRECTION, 2026-08-18 — this section used to leak what it audits.
>
> Until today §4 enumerated all thirteen addresses **in the clear**, in prose,
> as the evidence for its own argument. That is the single most obvious failure
> a document like this can make: the broker *source* was correctly aliased by
> [`3935e48`](#the-correction-log), and the document explaining that cleanup
> then republished every value the cleanup had removed. `check_publication.py`
> found it — 13 `third-party-ip` findings, every one of them in this file — and
> the gate was red on this document alone.
>
> **Every address below is now written as `ip-NN`.** The aliases are defined by
> the numbered list in this section, they are allocation-ordered and
> append-only, and **the real values are not recorded anywhere in this
> repository.** They are not in a table here, not in a side file, not in
> `.gitignore`. That is deliberate: an alias map that ships is not an alias map,
> it is a decoder ring, which is the exact criticism this section makes of
> `f1-round2`'s tracked `sanitise_docs.py` at the end.
>
> **Why `ip-NN` and not `host-A`.** The obvious scheme collides. `docs/
> operations.md` and `docs/fleet.md` already use `host A`–`host D` as
> **document-local** labels — `docs/incidents.md` opens by warning that "`host
> A` in operations.md is a different host" — so a repo-wide `host-A` would mean
> two things at once, in files that sit next to each other. `ip-NN` matches the
> numbering convention the sibling repository's tooling already uses for exactly
> this job (`mach-%02d` for machine ids, `id-%03d` in `alias_canon.txt` for
> rented-host identifiers) and cannot be confused with either.

Thirteen distinct real routable IPv4 addresses appear across full history (plus
`1.2.3.4`, an obvious test placeholder). These are the addresses of **rented
vast.ai GPU hosts** — third-party machines, not the owner's. **They are the
whole reason this section exists: nobody renting out a GPU agreed to have their
address published in somebody else's repository.**

The aliases, in allocation order:

| alias | where it was seen |
|---|---|
| `ip-01` … `ip-05` | the five that were in the tracked tree until `3935e48` |
| `ip-06` … `ip-13` | eight that appear in **history only** |

### The five that were in the tracked tree

They were in `broker/remote.py`, `broker/config.py` and `broker/test_broker.py`:

```
ip-01   broker/remote.py, broker/test_broker.py
ip-02   broker/config.py,  broker/test_broker.py
ip-03   broker/remote.py,  broker/test_broker.py
ip-04   broker/test_broker.py
ip-05   broker/remote.py
```

They were there for a good reason: they were inside docstrings and test fixtures
recording *observed* failures, and the observation is the point —

```python
err="root@<ip-01>: Permission denied (publickey).",
# Measured 2026-08-03 on instance 46695656 (<ip-02>), three independent
#     exit 1 after 0.6s on <ip-03>:23972 [stat -c %Y ...]: 1785254527
```

This is the documentation style the project is being published *for*: a real
recorded failure beats a sanitised paraphrase. **Preserve the meaning; replace
the identifier.** Substituting RFC 5737 documentation addresses
(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) keeps every one of these
records exactly as informative while naming no real machine — the addresses are
shape-valid, so a reader still sees "an SSH failure against a host", and they
are reserved, so they can never be anybody's.

**This has now been done.** See the correction log below: `3935e48` mapped
`ip-01`…`ip-05` onto `192.0.2.11`…`192.0.2.15` on a stable map, so the same host
still reads as the same host in every file, and changed the `test_broker.py`
fixtures and the assertions that read them **together** — which was the reason
this audit originally declined to make the change itself.

### The eight that are in history only

`ip-06` … `ip-13` appear only in **history**, in earlier un-aliased versions of
`docs/operations.md`, `docs/incidents.md` and `docs/fleet.md`. The docs
themselves were correctly aliased to `host A-D` at some point; the rewrite did
not reach the history, and editing a file has never removed anything from a
commit.

**`ip-13` appears in both this repository and `f1-round2`**, where its real
value is mapped to `host-A` by that repo's `tools/publication/sanitise_docs.py`.
That file is **tracked**. If both repositories are published, that alias table
is the key that de-anonymises this repository's history as well — the aliasing
here is only as good as the weakest table anywhere in the pair. See §4 of the
f1-round2 audit; the fix belongs over there, and it is the reason this section
does not keep a table of its own.

### The correction log

Corrections are recorded rather than silently applied, because a document whose
subject is "what a scan missed" cannot credibly edit its own history.

| date | claim as it stood | correction |
|---|---|---|
| 2026-08-18 | "Five survive in the **current tracked tree**", listing them | **Stale.** `3935e48` ("secrets: alias third-party host IPs in source, and close two scanner blind spots") replaced all five with `192.0.2.11`–`.15` across `broker/remote.py`, `broker/config.py` and `broker/test_broker.py`, fixture and assertion together, with 508/508 broker tests, 118/118 exec-server tests and the GPU guard all still passing. **Zero real IPs remain in tracked source.** They remain in history, which is what §6 is about. |
| 2026-08-18 | all thirteen addresses written out in prose | **Fixed.** Aliased to `ip-01`…`ip-13`; the real values are recorded nowhere in this repository. |
| 2026-08-18 | "this audit did not make that change, because it touches tested code" | Superseded — the change was made in `3935e48` and the tests were updated with it. |

Real vast.ai **machine and instance identifiers** also appear in tracked source
and docs (`machine_id = 42763`, `53217`, `55313`, `96679`, `138180`; instances
`46695656`, `46077186`, `43687899`). These are the same class of disclosure as
the IPs — they name third-party hardware. This repository's own `.gitignore`
already reasons about exactly this risk for `farm/hostrates.json`, and untracked
that file for it; the same reasoning applies, more weakly, to these.

**These were deliberately NOT aliased, and the reason is worth stating rather
than leaving as an inconsistency.** An IP address is *reachable*: it names a
machine you can send packets to, today, and it is often enough to locate the
person hosting it. A vast.ai machine id is an opaque integer in one vendor's
database — it identifies hardware to vast.ai and to nobody else, it routes
nothing, and it is not resolvable by anyone outside that account. Aliasing them
here would also be theatre: the same integers are load-bearing in tracked
source — `broker/fleet.py`, `broker/test_broker.py`, `farm/procure.py` — where
they are the keys real code looks things up by, and a document that hid what the
code beside it prints in the clear would be documenting a cleanup that had not
happened. If the owner wants them gone, the change is a source change and
`f1-round2`'s `MACHINES` / `mach-NN` map is the pattern for it.

Nothing else IP-shaped is a concern: 60 occurrences of `127.0.0.1`, and
`1.2.3.4` as a placeholder.

> **Read that as a statement about *routable* addresses, not private ones.** An
> earlier version of this sentence ended "**No LAN/private-range addresses**",
> and that phrasing is precisely the trap this audit exists to document: it is
> the sentence a scan writes when it has checked `10./172.16./192.168.` and
> nothing else. A check like that returns a clean, confident zero while walking
> straight past thirteen globally routable addresses, because none of them is in
> a private range — the absence of private addresses is *not evidence of
> anything*. `check_publication.py` matches **every** dotted quad and then
> excludes loopback, the private ranges and the RFC 5737 documentation ranges by
> name, so what it reports is what is left: real addresses belonging to real
> people. That is the only shape of this check worth running.

**No references anywhere to the private `f1-site-part2` website**
(`git grep -l "f1-site-part2"` → 0 files).

**No SSH key material, anywhere, in either tree or history** — added to the scan
set on 2026-08-18 when the owner reported deleting an SSH key alongside the API
key. Every object in the database (258 blobs here, 2,312 in `f1-round2`) plus
every commit message was searched for **eight forms**: the PEM armour that opens
an RSA, EC, generic or OpenSSH private key, the PGP private-key block header,
the fixed base64 prefix that begins an OpenSSH-v1 private key body, and the
`ssh-rsa` / `ssh-ed25519` / `ecdsa-sha2-nistp*` **public**-key encodings.

The needle set was proved before any zero from it was believed: a scratch file
carrying all eight forms was written outside both repositories and the same
expression was required to find all eight — it did — and only then was it run
for real. Six blobs matched across the two repositories, and **all six are a
scanner describing itself**: `check_publication.py`'s own `openssh-privkey`
pattern (2 blobs, one per revision of that file), and a one-line `printf`
in `f1-round2`'s audit demonstrating the fake key its `.gitignore` refuses to
add (4 blobs). Zero commit messages matched in either repository.

**This paragraph deliberately does not spell those eight strings out**, which is
why it describes them instead of listing them. An earlier draft quoted them
verbatim as evidence, and `check_publication.py` immediately reported
`private-key-block` and `openssh-privkey` findings against **this file** — the
document leaking what it audits, for the second time in one section. The gate
was right both times, and it is more useful than a document that reads slightly
better.

No key-shaped *filename* (`id_*`, `*.pem`, `*.key`, `*.pub`, `known_hosts`,
`authorized_keys`) was ever committed to either repository — checked against
every path in every tree, not just the current one. The only SSH-key references
in tracked source are the **path** `~/.ssh/id_vast_render` and log excerpts
naming `authorized_keys`. A filename is not a key.

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
> `11f8a429205e232c2c2520b395548309cdd31d9b`. History was **not** rewritten to
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

~~**9 tracked files contain the owner's home-directory literal** (`/home/` + the login).~~

> **Correction, 2026-08-18.** Stale, and then briefly self-inflicted. The nine
> were fixed by hand — six of them were *code*, including two live
> path-allowlist defaults (`config.DEFAULT_SCENE_ROOTS`,
> `execservice.DEFAULT_BUNDLE_ROOTS`) where a blind regex would have emptied a
> security allowlist without touching the `is_dir()` test beneath it. But **this
> document then re-introduced four**, in two `git clone` transcripts and two
> exposure summaries, and `check_publication.py` reported all four. They are
> written `~/vast-render` and `file://$HOME/vast-render` now, which is the same
> path dialect `PATH_RULES` applies everywhere else, and the transcripts still
> run.
>
> **Current count: one tracked file contains the literal, and it is
> `tools/publication/check_publication.py`** — the gate itself, which must
> contain the literal because that string is the *pattern it matches on*. It
> excludes itself from its own tree scan for exactly this reason, and that
> exclusion is paired with a standing rule: nothing in that file may quote a
> real secret. Every other tracked file is clean.

---

## 6. What remains exposed under each of the three publishing options

The choice is the owner's; this audit did not make it, and **no history was
rewritten in producing this document** — but history *has* been rewritten since,
and the update below records it.

> ### UPDATE, 2026-08-18 — OPTION B WAS CHOSEN AND EXECUTED, for identity only.
>
> Every commit's **author and committer** were rewritten to
> `SuperComboGamer <36320904+SuperComboGamer@users.noreply.github.com>` with
> `git filter-repo` 2.47.0. `%ae` and `%ce` were identical throughout, so
> rewriting only the author would have left the address in all 40 commits.
>
> **`git log --all --format='%an <%ae>%n%cn <%ce>' | sort -u` now returns exactly
> one line.** Commit count unchanged at 74, no `refs/original`, no remote,
> nothing pushed. A verified `git bundle` was taken first. This repository has no
> notes ref, so the `refs/notes` trap that caught the sibling repository — a
> default filter-repo run passes notes commits through *before* the name/email
> callbacks, by design — did not apply here. It is documented in f1-round2's
> audit §6, and it is worth reading before anyone runs a similar job.
>
> **Citations were repointed**: 20 occurrences across five files, matched on
> unique old-commit prefix with abbreviation length preserved — `d056d4b…` alone
> is cited in four different lengths across three files, one of them a hardcoded
> string in `check_publication.py`. The six key-bearing **blob** ids in §3 were
> deliberately untouched: a commit-map contains no blob ids, so they cannot
> match, and rewriting one would have destroyed the evidence this section is.
> Verified against the pre-rewrite object database restored from the bundle:
> **zero stale citations.**
>
> **The rewrite was identity-only. It did not remove the eight-hex key fragment
> from the eight blobs and one commit message in §3, and it did not remove the
> thirteen third-party IP addresses from history.** Those need
> `--replace-text` / `--message-callback` and remain an open decision — a much
> less urgent one now that §0 records the key as revoked.

### Option A — publish as-is, with full history

Ships 69 commits, all SHAs stable.

Still exposed:
- **The 8-character key fragment: 5 blobs on a pushed repository, 6 if the
  directory is copied rather than pushed, plus 1 commit message.**
  Harmless *after* revocation, which has happened (§0); embarrassing and
  unnecessary before it.
- One personal Gmail address in 40 of 69 commits, shown on every commit page.
- 13 real third-party host IPs — **none in the current tree since `3935e48`**,
  all 13 history-only — plus machine and instance ids, which are in the tree by
  design (§4).
- ~~The home-directory literal in 9 tracked files.~~ Fixed; one tracked file still contains
  the literal and it is the gate that matches on it. See §5.

**~~Option A is acceptable only if the key is rotated first.~~ The key is
revoked, so Option A is now defensible on the credential question** — the
fragment is a dead string. What Option A still ships is the personal Gmail
address on every commit page and thirteen strangers' IP addresses in history,
and neither of those is fixed by revoking anything.

### Option B — `git filter-repo` rewrite

Fixes: the key fragment in all six blobs **and** — if `--message-callback` is
used, which it must be — the commit message in `3ffea4c4`; the Gmail address via
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
message-callback so `3ffea4c4`'s message is rewritten alongside the blobs. If
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
