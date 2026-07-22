#!/usr/bin/env bash
# Deploy (§15): git pull → uv sync → migrate → restart → health check →
# rollback on failure. Layout on the box:
#   ~/inkling/releases/<sha>/   checkouts
#   ~/inkling/current           symlink to the live release
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/sharduldhande/inkling-server.git}"
BASE="${INKLING_BASE:-$HOME/inkling}"
ENV_FILE="$HOME/.inkling/env"
# 8900 is inkling; 8899 belongs to v1 (habit-enforcer) — a health check
# against it fails every deploy into a spurious rollback.
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8900/health}"
SERVICE="inkling"

mkdir -p "$BASE/releases"
prev_target="$(readlink "$BASE/current" 2>/dev/null || true)"

echo "==> fetching"
sha="$(git ls-remote "$REPO_URL" main | cut -f1)"
release="$BASE/releases/$sha"
if [[ ! -d "$release" ]]; then
  git clone --depth 1 "$REPO_URL" "$release"
fi

echo "==> installing ($sha)"
(cd "$release" && uv sync --frozen 2>/dev/null || uv sync)

echo "==> migrating"
# DATABASE_URL_MIGRATE only; the runtime role cannot DDL (§3).
(cd "$release" && set -a && source "$ENV_FILE" && set +a \
  && .venv/bin/python -m inkling migrate)

echo "==> switching"
ln -sfn "$release" "$BASE/current"
systemctl --user restart "$SERVICE"

echo "==> health check"
ok=""
for _ in $(seq 1 20); do
  sleep 1
  if curl -fsS "$HEALTH_URL" | grep -q '"status":"ok"'; then
    ok=1
    break
  fi
done

if [[ -z "$ok" ]]; then
  echo "!! health check failed; rolling back" >&2
  if [[ -n "$prev_target" ]]; then
    ln -sfn "$prev_target" "$BASE/current"
    systemctl --user restart "$SERVICE"
  fi
  exit 1
fi

echo "==> deployed $sha"
