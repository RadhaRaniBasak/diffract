#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-click-demo}"
BASE_COMMIT=25edc1e31360efba4f64c8c3c540bbbcfb7876da
HEAD_COMMIT=0039359443e73ab1034c63a1f6d58aba22c0ebc8

if [ -e "$TARGET" ]; then
  echo "error: $TARGET already exists" >&2
  exit 1
fi

echo "==> cloning pallets/click into $TARGET"
git clone --quiet https://github.com/pallets/click.git "$TARGET"
cd "$TARGET"
REPO_DIR="$(pwd)"
VENV_DIR="$REPO_DIR-venv"

echo "==> creating branches base-monster and monster-pr (one squashed commit)"
git branch base-monster "$BASE_COMMIT"
git checkout --quiet -b monster-pr "$HEAD_COMMIT"
git reset --quiet --soft base-monster
git -c user.name="${GIT_AUTHOR_NAME:-Diffract Demo}" -c user.email="${GIT_AUTHOR_EMAIL:-demo@example.com}" \
  commit --quiet -m "Q2 CLI improvements: typing, i18n, command suggestions, pager API, prompt and completion fixes"
git diff --shortstat base-monster monster-pr

echo "==> creating test environment at $VENV_DIR (pytest 9.0.2, the version click pins here)"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet "pytest==9.0.2"

VERIFY="PYTHONPATH=src $VENV_DIR/bin/python -m pytest -q -p no:cacheprovider -k \"not pager\""
echo "==> baseline check at the branch head"
if eval "$VERIFY" | tail -1; then
  echo
  echo "Ready. Open $REPO_DIR in Bob and use this verify command:"
  echo "  $VERIFY"
else
  echo "Baseline tests failed at the head: fix the environment before recording." >&2
  exit 1
fi
