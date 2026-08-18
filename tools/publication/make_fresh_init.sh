#!/usr/bin/env bash
# make_fresh_init.sh — build the "publish without history" option, so it can be
# compared against the real thing instead of imagined.
#
# WHAT THIS IS FOR
#   40 of this repository's 62 commits carry a personal email address in their
#   author and committer fields. Re-measure before quoting this, it drifts:
#     git log --all --format='%an <%ae>' | sort | uniq -c | sort -rn
#   Publishing the repository publishes it. There
#   are three ways out here:
#
#     (a) publish WITH history, accepting the address is public;
#     (b) publish a fresh repository with ONE commit and no history at all;
#     (c) REWRITE the history with `git filter-repo --mailmap`.
#
#   This script builds (b) so it can be compared rather than imagined.
#
#   BUT READ THIS FIRST: (c) is a genuinely good option in THIS repository, and
#   it is not in the companion one. `git filter-repo` breaks anything that cites
#   a commit SHA, and this repository's documentation cites exactly two — versus
#   several dozen next door, which is why f1-round2 rules a rewrite out. 61
#   commits, two citations to fix by hand, and you keep the entire provenance
#   with a clean address on every commit. Try (c) before settling for (b):
#
#       git filter-repo --email-callback \
#         'return b"noreply@users.noreply.github.com" if b"@gmail.com" in email else email'
#
#   (filter-repo rewrites every SHA, so re-check the two citations afterwards.
#   Work on a clone, never on the only copy.)
#
# WHAT IT WILL NOT DO
#   It never writes inside this repository. It never deletes anything. It
#   refuses to touch a destination that already has content, and it refuses a
#   destination inside any git repository. It creates no remote and pushes
#   nothing.
#
# USAGE
#   tools/publication/make_fresh_init.sh [DEST] [--from-head|--from-worktree]
#
#   DEST defaults to  ~/publish/<reponame>-fresh
#
#   --from-worktree  (default) copy the tracked files as they are ON DISK,
#                    including any uncommitted edits to tracked files.
#   --from-head      copy the tracked files as COMMITTED at HEAD, ignoring
#                    uncommitted edits. Reproducible; use this if you want the
#                    fresh repository to be provably identical to a known commit.
#
set -euo pipefail

# ---------------------------------------------------------------- locate source
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SRC=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
NAME=$(basename "$SRC")

MODE=worktree
DEST=""
for arg in "$@"; do
    case "$arg" in
        --from-head)     MODE=head ;;
        --from-worktree) MODE=worktree ;;
        -h|--help)       sed -n '2,32p' "${BASH_SOURCE[0]}"; exit 0 ;;
        -*)              echo "unknown option: $arg" >&2; exit 2 ;;
        *)               DEST=$arg ;;
    esac
done
# beside the source repo, not under $HOME: this is often run as a different user
# than the one who owns the tree, and $HOME then points somewhere unhelpful.
DEST=${DEST:-"$(dirname "$SRC")/publish/${NAME}-fresh"}

NOREPLY_EMAIL=${FRESH_INIT_EMAIL:-noreply@users.noreply.github.com}
NOREPLY_NAME=${FRESH_INIT_NAME:-$NAME}

say()  { printf '%s\n' "$*"; }
rule() { printf '%s\n' "--------------------------------------------------------------------------------"; }
die()  { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }

rule
say "make_fresh_init — $NAME"
rule
say "source       $SRC"
say "destination  $DEST"
say "content from $( [ "$MODE" = head ] && echo 'HEAD (committed state)' || echo 'the working tree (tracked files as they are on disk)' )"
say "identity     $NOREPLY_NAME <$NOREPLY_EMAIL>"
say ""

# ------------------------------------------------------------- refuse to damage
[ "$DEST" = "/" ] && die "destination is /"
case "$DEST" in
    "$SRC"|"$SRC"/*) die "destination is inside the source repository. Pick a path outside it." ;;
esac
if [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null || true)" ]; then
    die "destination exists and is not empty: $DEST
        This script never overwrites. Inspect it, then remove it yourself if you
        want it rebuilt:   rm -rf '$DEST'"
fi
# a destination inside somebody else's repository would commit into that repo
if parent_repo=$(git -C "$(dirname "$DEST")" rev-parse --show-toplevel 2>/dev/null); then
    die "the parent of the destination is inside a git repository ($parent_repo).
        Initialising here would entangle the two. Pick a path outside any repo."
fi

# ------------------------------------------------------------------ copy tracked
mkdir -p "$DEST"

say "[1/5] copying tracked files"
SRC_LIST=$(mktemp); trap 'rm -f "$SRC_LIST" "$SRC_MANIFEST" "$DST_MANIFEST" 2>/dev/null || true' EXIT
git -C "$SRC" ls-files -z > "$SRC_LIST"
SRC_COUNT=$(tr -cd '\0' < "$SRC_LIST" | wc -c)

if [ "$MODE" = head ]; then
    git -C "$SRC" archive --format=tar HEAD | tar -xf - -C "$DEST"
else
    # tracked-but-deleted-on-disk: tar would abort on these, so say so first
    MISSING=$(git -C "$SRC" ls-files --deleted | wc -l)
    if [ "$MISSING" -gt 0 ]; then
        say "      NOTE: $MISSING tracked file(s) are deleted on disk but the deletion is"
        say "            not committed; they will be absent from the copy."
    fi
    # tar reads the NUL-separated TRACKED list, so untracked and ignored files
    # (renders, caches, the delivery master, state dirs) cannot come along even
    # by accident. -C comes before -T so the paths resolve against the source.
    tar -C "$SRC" --null -T "$SRC_LIST" --ignore-failed-read -cf - 2>/dev/null \
        | tar -C "$DEST" -xf -
fi

DST_COUNT=$(find "$DEST" -type f -not -path '*/.git/*' | wc -l)
say "      $DST_COUNT files copied (source tracks $SRC_COUNT)"

# how far the working tree has drifted from HEAD, stated rather than buried
DIRTY=$(git -C "$SRC" diff --name-only HEAD -- . | wc -l)
if [ "$MODE" = worktree ] && [ "$DIRTY" -gt 0 ]; then
    say ""
    say "      HEADS UP: $DIRTY tracked file(s) differ from HEAD, and this copy has"
    say "      the ON-DISK version of each. That is what --from-worktree means. Use"
    say "      --from-head if you want the fresh repo to match a known commit."
fi

# ------------------------------------------------------------------- initialise
say ""
say "[2/5] initialising a new repository"
git -C "$DEST" init -q -b main
git -C "$DEST" config user.email "$NOREPLY_EMAIL"
git -C "$DEST" config user.name  "$NOREPLY_NAME"
git -C "$DEST" config commit.gpgsign false
# core.hooksPath in the source points at tooling that assumes the source layout;
# a fresh repository must not inherit it.
git -C "$DEST" config --unset-all core.hooksPath 2>/dev/null || true

# --force is deliberate. A repository can track files that its own .gitignore
# excludes, and a plain `git add -A` in the fresh copy would then read the copied
# .gitignore and silently drop them — that happened on the first run of the
# companion repository's copy of this script, 998 files in and 928 committed.
# This repository currently has no such files, but that is a fact about today's
# .gitignore, not a property of the script. Everything in DEST came from
# `git ls-files`, so by construction nothing here should be excluded.
git -C "$DEST" add -A --force
GIT_AUTHOR_NAME="$NOREPLY_NAME"   GIT_AUTHOR_EMAIL="$NOREPLY_EMAIL" \
GIT_COMMITTER_NAME="$NOREPLY_NAME" GIT_COMMITTER_EMAIL="$NOREPLY_EMAIL" \
git -C "$DEST" commit -q --no-verify -m "$NAME

Initial commit of the published tree.

This repository has no history by design. It was developed over 61 commits whose
author and committer fields carry a personal email address; publishing that
history would publish the address, so a single fresh commit was made instead,
trading the provenance for the address.

What that costs: docs/incidents.md and docs/operations.md record what was known
at each point of a multi-day production render, and several passages reason
about the order in which findings arrived. Those passages are still true; they
simply can no longer be checked against a log from inside this repository."

say "      committed as $(git -C "$DEST" rev-parse --short HEAD)"

# ----------------------------------------------------------------------- verify
say ""
say "[3/5] verifying"
FAIL=0
check() { if [ "$1" = ok ]; then printf '      PASS  %s\n' "$2"; else printf '      FAIL  %s\n' "$2"; FAIL=$((FAIL+1)); fi; }

# 3a. exactly one commit, and no ancestry
NCOMMITS=$(git -C "$DEST" rev-list --count HEAD)
[ "$NCOMMITS" = 1 ] && check ok "history is exactly 1 commit (found $NCOMMITS)" \
                    || check no "history is exactly 1 commit (found $NCOMMITS)"

# 3b. no source .git carried in, anywhere below the top
STOWAWAYS=$(find "$DEST" -mindepth 2 -name .git -print 2>/dev/null | wc -l)
[ "$STOWAWAYS" = 0 ] && check ok "no nested .git directories carried over" \
                     || check no "no nested .git directories carried over (found $STOWAWAYS)"

# 3c. the object store cannot reach the old history: it has no old SHAs at all
OLD_HEAD=$(git -C "$SRC" rev-parse HEAD)
if git -C "$DEST" cat-file -e "$OLD_HEAD" 2>/dev/null; then
    check no "the source HEAD commit ($OLD_HEAD) is NOT present in the new object store"
else
    check ok "the source HEAD commit is absent from the new object store"
fi

# 3d. no reflog beyond the one commit we just made
NREF=$(git -C "$DEST" reflog show HEAD 2>/dev/null | wc -l)
[ "$NREF" -le 1 ] && check ok "reflog holds only the initial commit ($NREF entr(y|ies))" \
                  || check no "reflog holds only the initial commit ($NREF entries)"

# 3e. every commit identity is the noreply address
IDENTS=$(git -C "$DEST" log --format='%ae%n%ce' | sort -u | tr '\n' ' ')
if [ "$(git -C "$DEST" log --format='%ae%n%ce' | sort -u)" = "$NOREPLY_EMAIL" ]; then
    check ok "every author/committer identity is <$NOREPLY_EMAIL>"
else
    check no "every author/committer identity is <$NOREPLY_EMAIL> (found: $IDENTS)"
fi

# 3f. tracked file count matches
DST_TRACKED=$(git -C "$DEST" ls-files | wc -l)
if [ "$MODE" = head ]; then EXPECT=$(git -C "$SRC" ls-tree -r --name-only HEAD | wc -l)
else EXPECT=$SRC_COUNT; fi
[ "$DST_TRACKED" = "$EXPECT" ] && check ok "tracked file count matches source ($DST_TRACKED)" \
                               || check no "tracked file count matches source (new $DST_TRACKED vs expected $EXPECT)"

# 3g. content identity, per file, not just per count. A matching count with
#     mismatched bytes is exactly the kind of pass this project keeps catching.
SRC_MANIFEST=$(mktemp); DST_MANIFEST=$(mktemp)
if [ "$MODE" = head ]; then
    git -C "$SRC" ls-tree -r HEAD --format='%(objectname) %(path)' | sort -k2 > "$SRC_MANIFEST"
else
    # one git process for the whole tree, not one per file
    SRC_EXISTING=$(mktemp)
    git -C "$SRC" ls-files | while IFS= read -r f; do
        [ -f "$SRC/$f" ] && printf '%s\n' "$f"
    done > "$SRC_EXISTING"
    paste -d' ' \
        <(git -C "$SRC" hash-object --stdin-paths < "$SRC_EXISTING") \
        "$SRC_EXISTING" | sort -k2 > "$SRC_MANIFEST"
    rm -f "$SRC_EXISTING"
fi
git -C "$DEST" ls-tree -r HEAD --format='%(objectname) %(path)' | sort -k2 > "$DST_MANIFEST"
if diff -q "$SRC_MANIFEST" "$DST_MANIFEST" >/dev/null; then
    check ok "every file's content hash matches the source, path by path"
else
    NDIFF=$(diff "$SRC_MANIFEST" "$DST_MANIFEST" | grep -c '^[<>]' || true)
    check no "every file's content hash matches the source ($NDIFF mismatched line(s))"
    diff "$SRC_MANIFEST" "$DST_MANIFEST" | head -20 | sed 's/^/            /'
fi

# 3h. the verification itself must be capable of failing. Prove it by asking the
#     same comparison a question whose answer we know is "no".
if diff -q "$SRC_MANIFEST" /dev/null >/dev/null 2>&1; then
    check no "SELF-TEST: the manifest comparison can detect a difference"
else
    check ok "SELF-TEST: the manifest comparison fires on a known mismatch"
fi

# 3i. size
say ""
say "[4/5] size"
git -C "$DEST" gc -q --aggressive --prune=now 2>/dev/null || git -C "$DEST" gc -q --prune=now
PACK_H=$(git -C "$DEST" count-objects -vH | awk -F': ' '/^size-pack/{print $2}')
PACK_KB=$(git -C "$DEST" count-objects -v | awk -F': ' '/^size-pack/{print $2}')
WORK_H=$(du -sh --exclude=.git "$DEST" | cut -f1)
WORK_KB=$(du -sk --exclude=.git "$DEST" | cut -f1)
TOTAL_H=$(du -sh "$DEST" | cut -f1)
# The source's whole object store, loose AND packed. Comparing against
# `size-pack` alone is wrong and said so on the first run of this script: it
# reported the fresh pack as bigger than "the source", when the source simply
# had 32 MiB of unpacked loose objects that size-pack does not count.
SRC_GIT_KB=$(du -sk "$SRC/.git" | cut -f1)
SRC_GIT_H=$(du -sh "$SRC/.git" | cut -f1)
say "      working tree (no .git)   $WORK_H   in $DST_TRACKED files"
say "      packed git objects       $PACK_H"
say "      total on disk            $TOTAL_H"
say "      source .git, all objects $SRC_GIT_H  (loose + packed; the history this trades away)"

# Bounds that mean something rather than bounds that merely exist:
#   too small  -> the copy did not happen, or the pack is empty
#   bigger than the uncompressed tree it stores -> something is badly wrong
if [ "${PACK_KB:-0}" -lt 8 ]; then
    check no "packed size is plausible (${PACK_H} is too small to be a real tree)"
elif [ "${PACK_KB:-0}" -gt "${WORK_KB:-0}" ]; then
    check no "packed size is plausible (${PACK_H} exceeds the ${WORK_H} of content it stores)"
else
    check ok "packed size is plausible (${PACK_H} for ${WORK_H} of content; source .git is ${SRC_GIT_H})"
fi

# ------------------------------------------------------------- what is lost
say ""
say "[5/5] what publishing this INSTEAD of the history would cost"
rule

HIST=$(git -C "$SRC" rev-list --count HEAD)
say "  * $HIST commits of provenance. \`git log\`, \`git blame\`, \`git bisect\` and"
say "    every \"which change introduced this\" question stop working inside the"
say "    published copy. For a tool whose defaults each encode a specific"
say "    incident, the log is the record of which incident came first."

# commit SHAs cited in the prose, resolved against the real repository
CITED=$(git -C "$SRC" grep -hoIE '\b[0-9a-f]{7,40}\b' HEAD -- '*.md' 2>/dev/null \
        | sort -u \
        | git -C "$SRC" cat-file --batch-check='%(objecttype)' --buffer 2>/dev/null \
        | grep -c '^commit$' || true)
say ""
say "  * $CITED distinct commit SHAs cited in the tracked documentation resolve"
say "    to real commits in this repository today, and they de-reference here."
say "    That number is small — which is the whole argument for NOT choosing this"
say "    option in this repository. A \`git filter-repo --email-callback\` rewrite"
say "    keeps all $HIST commits, scrubs the address from every one, and leaves"
say "    only those $CITED citations to repoint by hand. See the header of this"
say "    script. Do that on a clone, and only after deciding you want it."

say ""
say "  * The passages that reason about WHEN something landed relative to"
say "    something else. These stay readable, but become unverifiable:"
git -C "$SRC" grep -nIE '(commit|landed|shipped|committed)[^.]{0,50}\b(before|after)\b' HEAD -- 'docs/*.md' 2>/dev/null \
  | sed 's/^HEAD://' | head -8 | cut -c1-118 | sed 's/^/        /' || true
NREL=$(git -C "$SRC" grep -cIE '(commit|landed|shipped|committed)[^.]{0,50}\b(before|after)\b' HEAD -- 'docs/*.md' 2>/dev/null | awk -F: '{s+=$NF} END{print s+0}')
say "        ... $NREL such lines across the tracked docs."
say ""
say "  * Anything a reader would want to check by date: the log is the only"
say "    record of the order in which findings arrived, and several entries are"
say "    interesting precisely because a retraction landed after the claim."

rule
say ""
if [ "$FAIL" -eq 0 ]; then
    say "RESULT: ready. $FAIL verification failures."
    say ""
    say "Nothing has been pushed and no remote exists. Inspect it:"
    say "    git -C '$DEST' log --stat"
    say "    ls '$DEST'"
    say ""
    say "If you prefer to publish WITH history, delete this directory and publish"
    say "the source repository as it stands. This script can be re-run at any time."
    exit 0
else
    say "RESULT: $FAIL verification failure(s) above. Do not publish this copy"
    say "        until they are understood."
    exit 1
fi
