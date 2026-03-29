# jerry

Self-improving nightly Claude Code usage pipeline.

Runs hourly (midnight–6am) via system cron. When weekly usage is below threshold and reset is within 24 hours, starts a run loop that alternates between:

- **Research**: analyzes a target repo, creates 3 small GitHub issues labeled `ai-backlog`
- **Fix**: picks the oldest open `ai-backlog` issue, implements a fix, opens a PR

Each Claude invocation is a fresh context (`claude -p`). The loop stops when usage nears the configured threshold.

## Self-improvement

This repo itself is a target. The research agent can file issues against `jerry` to improve its own prompts and scripts.

## Per-repo focus

Add a `jerry-research.md` at the root of any target repo to extend the base research prompt with repo-specific guidance.

## Setup

See `config.json` for target repos and thresholds.

Local state lives in `~/.claude/jerry/` (not tracked by git).

Cron: `7 0-6 * * * /Users/rk/github/jerry/bin/gate.sh`
