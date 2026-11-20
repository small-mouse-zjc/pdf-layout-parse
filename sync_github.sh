#!/usr/bin/env bash
# sync_github.sh -- push this submission tree to the public repo.
#
# The repo https://github.com/small-mouse-zjc/pdf-layout-parse holds an earlier,
# partial snapshot (SKILL.md, README.md, docs/, scripts/pdf_struct.py). The rubric
# gives the 5 bonus points for a link where a reviewer can find *this* submission,
# so the repo must carry the same files that were uploaded.
#
# Needs a GitHub credential that can push to that repo:
#   GH_TOKEN=ghp_xxx ./sync_github.sh        # https
#   ./sync_github.sh                          # falls back to SSH
#
# Only git is required (no rsync). The tree is mirrored: files deleted from the
# submission directory are deleted in the repo as well.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="small-mouse-zjc/pdf-layout-parse"
WORK="$(mktemp -d)"
TOKEN="${GH_TOKEN:-}"
trap 'rm -rf "$WORK"; unset TOKEN' EXIT

redact() { sed -e "s/${TOKEN:-__none__}/***/g"; }

if [ -n "$TOKEN" ]; then
  URL="https://x-access-token:${TOKEN}@github.com/${REPO}.git"
else
  URL="git@github.com:${REPO}.git"
fi

echo "cloning ${REPO} ..."
if ! git clone --depth 1 "$URL" "$WORK/repo" 2>"$WORK/err"; then
  redact < "$WORK/err" >&2
  echo "clone failed: set GH_TOKEN or configure an SSH key for ${REPO}" >&2
  exit 1
fi

# mirror the submission tree (tar keeps permissions; .git is preserved)
find "$WORK/repo" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
tar -C "$SRC_DIR" \
    --exclude=./.git --exclude=__pycache__ --exclude=./results/work \
    -cf - . | tar -C "$WORK/repo" -xf -

cd "$WORK/repo"
git add -A
if git diff --cached --quiet; then
  echo "repo already up to date"
  exit 0
fi
git -c user.name="${GIT_AUTHOR_NAME:-pdf-layout-parse}" \
    -c user.email="${GIT_AUTHOR_EMAIL:-noreply@example.com}" \
    commit -q -m "sync submission: verification scripts, ARM bundle, self-test record"
echo "pushing ..."
if ! git push -q origin HEAD:main 2>"$WORK/err2"; then
  redact < "$WORK/err2" >&2
  echo "push failed" >&2
  exit 1
fi
echo "pushed to https://github.com/${REPO}"
git --no-pager log --oneline -1
