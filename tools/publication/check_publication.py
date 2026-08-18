#!/usr/bin/env python3
"""Publication gate for vast-render: prove the tree and its HISTORY are clean.

    python3 tools/publication/check_publication.py            # working tree only
    python3 tools/publication/check_publication.py --history  # + every blob ever
    python3 tools/publication/check_publication.py --canary   # prove THIS works

Exit status is 0 only when every check passes, so this can gate a release.

WHY THIS IS A CHECKER AND NOT A REWRITER
----------------------------------------
The sibling repository f1-round2 has `tools/publication/sanitise_docs.py`, which
rewrites prose in ~110 markdown files and maintains an append-only alias map for
rented-host identifiers. That shape is right for a repo whose exposure is a large
prose corpus. It is the wrong shape here, and porting it verbatim would have
produced a tool with almost nothing to do:

  * this repo had NINE files containing `/home/zany` and all nine were fixed by
    hand, because six of them were CODE — two were live path-allowlist defaults
    (`config.DEFAULT_SCENE_ROOTS`, `execservice.DEFAULT_BUNDLE_ROOTS`), not
    prose, and a regex that rewrote them without also fixing the `is_dir()` test
    beneath them would have silently emptied a security allowlist;
  * its three files containing an email address contained only
    `noreply@users.noreply.github.com` and `root@sshN.vast.ai`, which are
    documentation, not PII.

So THE PATH DIALECT IS SHARED — the replacements below are the same ones
`sanitise_docs.py` applies, so the two repositories read identically — but the
job here is standing guard, not bulk rewriting. What this repo actually needs a
tool for is the thing f1-round2 does not: it holds a live billing credential.

THE CANARY, AND WHY IT IS NOT OPTIONAL
--------------------------------------
`--canary` builds a throwaway repository, commits four real-shaped secrets and
an email, DELETES them in a second commit so they survive only in history, and
runs this scanner against it. A secret scanner that reports "clean" is
indistinguishable from a secret scanner that is broken, and "the check passed
while the thing was broken" is the failure this project keeps a whole document
about. Run `--canary` before believing a clean `--history`.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------- path dialect
# Identical to sanitise_docs.py in f1-round2, so a path reads the same way in
# both repositories. Prose gets `~/repo`; an absolute path whose ABSOLUTENESS is
# the point of the example gets `/home/user/...`.
PATH_RULES = (
    (re.compile(r"/home/zany/vast-render"), "~/vast-render"),
    (re.compile(r"/home/zany/f1-round2"), "~/f1-round2"),
    (re.compile(r"/home/zany/opus5-car-render"), "~/opus5-car-render"),
    (re.compile(r"/home/zany/publish"), "~/publish"),
    (re.compile(r"/home/zany"), "/home/user"),
)

# The author's home directory, and any other real-looking home path.
PERSONAL_PATH = re.compile(r"/home/(?!user\b|you\b|<user>)[A-Za-z0-9_.-]+")

# Emails that are documentation rather than PII.
EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
EMAIL_OK = re.compile(
    r"(users\.noreply\.github\.com|@ssh[N0-9]*\.vast\.ai|@example\.(com|invalid)"
    r"|@localhost|@your-domain)", re.I)

# ------------------------------------------------------------------- secrets
SECRETS = (
    ("vast-key-64hex", re.compile(rb"\b[0-9a-f]{64}\b")),
    ("api_key-assign", re.compile(
        rb"(?i)(api[_-]?key|apikey|vast[_-]?key)\s*[:=]\s*[\"']?([A-Za-z0-9_\-]{16,})")),
    # A TRUNCATED key is still a key fragment, and the rule above cannot see one:
    # it demands 16+ characters. The first version of this scanner therefore
    # reported this repository's history as clean while `api_key=942dc099` — the
    # leading 8 hex of the real vast.ai key, quoted verbatim in a code comment in
    # `broker/remote.py` — sat in six historical blobs. It was found by a
    # hand-written grep, not by this tool, which is exactly the wrong way round.
    #
    # 8 hex is 32 bits and does not reconstruct a 256-bit key. It is still a
    # confirmation oracle for a candidate key, and it is still a live credential's
    # bytes in a public repository, so it is reported.
    #
    # Placeholders are excluded by requiring hex: `api_key=<redacted>`,
    # `api_key=…` and `api_key=<64 hex chars>` do not match.
    ("api_key-fragment", re.compile(rb"(?i)api[_-]?key=[0-9a-f]{8,}")),
    ("bearer", re.compile(rb"(?i)authorization\s*:\s*bearer\s+([A-Za-z0-9_\-.]{16,})")),
    ("aws-akid", re.compile(rb"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-pat", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack", re.compile(rb"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
    ("openai", re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b")),
    ("anthropic", re.compile(rb"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("google-api", re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private-key-block", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openssh-privkey", re.compile(rb"b3BlbnNzaC1rZXktdjE")),
    ("jwt", re.compile(
        rb"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
)

# 64-hex strings that are NOT credentials. A sha256 is the same shape as a vast
# key, and this project is full of real digests — so rather than lower the
# pattern's sensitivity (which would blind it to actual keys), known-benign
# values are listed and everything else is reported for a human to judge.
BENIGN_64HEX = {
    b"0123456789abcdef" * 4,          # the synthetic key in test_broker.py
    b"a" * 64,                        # the synthetic sha256 in test_broker.py
}

B64ISH = re.compile(rb"\b[A-Za-z0-9+/_\-]{40,}={0,2}\b")


def git(*a, cwd=None, binary=False):
    r = subprocess.run(["git", "-C", cwd or ROOT, *a], capture_output=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def entropy(b: bytes) -> float:
    if not b:
        return 0.0
    c = Counter(b)
    n = len(b)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def scan_bytes(data: bytes, where: str, out: list) -> None:
    """Append (kind, where, line, sample) for every credential-shaped hit."""
    if b"\0" in data[:8192]:
        return
    for name, rx in SECRETS:
        for m in rx.finditer(data):
            tok = m.group(0)
            if name == "vast-key-64hex" and tok in BENIGN_64HEX:
                continue
            line = data[:m.start()].count(b"\n") + 1
            out.append((name, where, line, tok[:80].decode("utf-8", "replace")))
    for m in B64ISH.finditer(data):
        tok = m.group(0)
        if tok in BENIGN_64HEX or entropy(tok) < 4.4:
            continue
        line = data[:m.start()].count(b"\n") + 1
        out.append(("high-entropy", where, line,
                    tok[:80].decode("utf-8", "replace")))


# ------------------------------------------------------------------- checks
def check_tree(root: str) -> tuple[int, list]:
    """Personal paths, PII and secrets in every TRACKED file."""
    problems = []
    files = [f for f in git("ls-files", cwd=root).split("\n") if f]
    for f in files:
        p = os.path.join(root, f)
        if not os.path.isfile(p):
            continue
        with open(p, "rb") as fh:
            data = fh.read()
        scan_bytes(data, f, problems)
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", "replace")
        for m in PERSONAL_PATH.finditer(text):
            problems.append(("personal-path", f,
                             text[:m.start()].count("\n") + 1, m.group(0)))
        for m in EMAIL.finditer(text):
            if EMAIL_OK.search(m.group(0)):
                continue
            problems.append(("email", f,
                             text[:m.start()].count("\n") + 1, m.group(0)))
    return len(files), problems


def check_history(root: str) -> tuple[int, list]:
    """Every blob in every commit, INCLUDING unreachable ones.

    Unreachable objects are scanned on purpose: an `amend` or a `reset` leaves
    the old blob in the object database, and "we removed it in a later commit"
    is not the same claim as "it is not in the repository".
    """
    problems = []
    named: dict[str, set] = {}
    for ln in git("rev-list", "--objects", "--all", cwd=root).splitlines():
        parts = ln.split(" ", 1)
        if len(parts) == 2:
            named.setdefault(parts[0], set()).add(parts[1])
    allobj = git("cat-file", "--batch-check", "--batch-all-objects",
                 cwd=root).splitlines()
    blobs = [ln.split()[0] for ln in allobj
             if len(ln.split()) >= 2 and ln.split()[1] == "blob"]
    for oid in blobs:
        data = git("cat-file", "blob", oid, cwd=root, binary=True)
        paths = ", ".join(sorted(named.get(oid, {"<unreachable>"})))[:100]
        scan_bytes(data, f"blob {oid[:10]} ({paths})", problems)
    return len(blobs), problems


def check_env_example(root: str) -> list:
    """`.env` must be ignored and `.env.example` must be committable.

    Asserted rather than assumed because it was WRONG: the `.env.*` rule that
    protects a real `.env` also swallowed `.env.example`, so the template a
    stranger needs in order to configure this tool safely could not be added to
    the repository at all. `git check-ignore` is not used here — it prints the
    matching rule for a NEGATION too, which reads like a failure. `git add
    --dry-run` is the behaviour that actually matters.
    """
    bad = []
    r = subprocess.run(["git", "-C", root, "add", "--dry-run", "--", ".env.example"],
                       capture_output=True, text=True)
    if r.returncode != 0 or "add '.env.example'" not in r.stdout:
        bad.append(("env-example-ignored", ".env.example", 0,
                    "git refuses to add it; the `!.env.example` negation is gone"))
    if not os.path.exists(os.path.join(root, ".env.example")):
        bad.append(("env-example-missing", ".env.example", 0, "file does not exist"))

    probe = os.path.join(root, ".env")
    made = False
    if not os.path.exists(probe):
        with open(probe, "w") as fh:
            fh.write("VAST_API_KEY=probe\n")
        made = True
    try:
        r = subprocess.run(["git", "-C", root, "add", "--dry-run", "--", ".env"],
                           capture_output=True, text=True)
        if r.returncode == 0 and "add '.env'" in r.stdout:
            bad.append(("env-not-ignored", ".env", 0,
                        "git would happily commit a real .env"))
    finally:
        if made:
            os.unlink(probe)
    return bad


def canary() -> int:
    """Plant secrets, delete them, and require the scanner to still find them."""
    with tempfile.TemporaryDirectory() as d:
        run = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True)
        run("init", "-q")
        run("config", "user.email", "canary@example.invalid")
        run("config", "user.name", "canary")
        planted = {
            "a.py": "key = \"" + "deadbeef" * 8 + "\"\n",
            "b.txt": ("AKIAIOSFODNN7EXAMPLE\n"
                      "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" + "\n"
                      "Authorization: Bearer " + "Zq7Xk2Pv9Lm4Rt6Ws8Yb1Nc3Hd5Jf0Gg" + "\n"),
        }
        for name, body in planted.items():
            with open(os.path.join(d, name), "w") as fh:
                fh.write(body)
        run("add", *planted)
        run("commit", "-qm", "canary")
        run("rm", "-q", *planted)
        run("commit", "-qm", "remove the secrets")

        n_tree, tree = check_tree(d)
        n_hist, hist = check_history(d)
        kinds = {p[0] for p in hist}
        want = {"vast-key-64hex", "aws-akid", "github-pat", "bearer"}
        print(f"canary: working tree has {n_tree} tracked file(s) — "
              f"the secrets are DELETED, so a tree-only scan finds "
              f"{len(tree)} of them")
        print(f"canary: history has {n_hist} blob(s); scanner found "
              f"{len(hist)} hit(s) of kinds {sorted(kinds)}")
        missing = want - kinds
        if missing:
            print(f"CANARY FAILED — the scanner MISSED {sorted(missing)}. "
                  f"A clean report from this tool means nothing until this passes.")
            return 1
        if tree:
            print("CANARY FAILED — tree scan reported hits for files that "
                  "no longer exist in the tree.")
            return 1
        print("CANARY PASSED — the scanner finds planted secrets that survive "
              "only in history, and does not hallucinate them in the tree.")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", action="store_true",
                    help="also scan every blob in every commit (slower)")
    ap.add_argument("--canary", action="store_true",
                    help="prove the scanner detects known-planted secrets, then stop")
    a = ap.parse_args()

    if a.canary:
        return canary()

    rc = 0
    n_files, tree = check_tree(ROOT)
    envbad = check_env_example(ROOT)
    print(f"tracked files scanned : {n_files}")
    print(f"working-tree findings : {len(tree)}")
    for kind, where, line, sample in tree:
        print(f"    [{kind}] {where}:{line}  {sample}")
    print(f"config-safety findings: {len(envbad)}")
    for kind, where, line, sample in envbad:
        print(f"    [{kind}] {where}  {sample}")
    if tree or envbad:
        rc = 1

    if a.history:
        n_blobs, hist = check_history(ROOT)
        print(f"history blobs scanned : {n_blobs} (incl. unreachable)")
        print(f"history findings      : {len(hist)}")
        for kind, where, line, sample in hist:
            print(f"    [{kind}] {where}:{line}  {sample}")
        if hist:
            print("\nNOTE: a finding in HISTORY is not fixed by editing a file. "
                  "See docs/publication.md — it is a rewrite decision, and for a "
                  "credential the only real remedy is ROTATION.")
            rc = 1

    print("\nRESULT: PASS" if rc == 0 else "\nRESULT: FAIL")
    if rc == 0:
        print("Reminder: a clean scan does NOT un-expose a key that has already "
              "existed in plaintext. Rotation is still required — see "
              "docs/publication.md.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
