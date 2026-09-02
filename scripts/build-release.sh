#!/usr/bin/env bash
set -euo pipefail
# Trinity boxed artifact 0.8.0 — reproducible tarball without .venv/node_modules/.git
VERSION="${1:-0.8.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
echo "==> vite build"
npm run build >/dev/null
echo "==> pytest"
SESSION_SECRET=test-secret-do-not-use-in-prod-0123456789abcdef0123456789abcdef WORKSPACE_DIR=/tmp/trinity_ci_workspace /tmp/trinity_venv/bin/python -m pytest -q -k "not real_api" --tb=short
echo "==> tarball trinity-${VERSION}.tar.gz"
tar --exclude='.git' --exclude='.venv' --exclude='node_modules' --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.mypy_cache' --exclude='logs' --exclude='.trinity' -czf "trinity-${VERSION}.tar.gz" \
  main.py requirements.txt package.json package-lock.json vite.config.js tailwind.config.js postcss.config.js \
  ruff.toml .env.example README.md CHANGELOG.md LICENSE AGENT.md \
  core/ agents/ tools/ trinity/ routers/ ui/ docs/ scripts/ systemd/ templates/ tests/ install.sh start.sh start.ps1 dist/
echo "==> sha256"
sha256sum "trinity-${VERSION}.tar.gz" | tee "trinity-${VERSION}.tar.gz.sha256"
ls -lh "trinity-${VERSION}.tar.gz"
