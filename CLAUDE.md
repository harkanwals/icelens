@AGENTS.md

Claude Code reads this file rather than `AGENTS.md`, so the line above imports it whole. Put new
instructions in `AGENTS.md`, where every other agent reads them too, not here.

In Claude Code on the web, `.claude/hooks/session-start.sh` runs `scripts/setup.py` before the
session starts, so the environment, DuckDB's extensions and the test tools are already there.
