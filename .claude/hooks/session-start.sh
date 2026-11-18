#!/bin/bash
# Claude Code on the web: make the checkout ready before the session starts. What "ready" means
# lives in scripts/setup.py, which every agent's setup runs (see AGENTS.md); this file only says when.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"
# A cloud sandbox's network policy may not reach extensions.duckdb.org; PyPI it can.
python3 scripts/setup.py --extensions-from-pypi
