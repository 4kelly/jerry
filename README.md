# jerry

Self-improving nightly Claude Code usage pipeline.

When unused weekly claude code usage is about to expire, start a run loop that alternates between:

- **Research**: analyzes a target repo, creates 3 GitHub issues labeled `ai-backlog`
- **Fix**: picks the oldest open `ai-backlog` issue, implements a fix, opens a PR

Each Claude invocation is a fresh context (`claude -p`). The loop stops when you run out of usage limit.

## Self-improvement

This repo itself is a target. The research agent can file issues against `jerry` to improve its own prompts and scripts.

## Per-repo focus

Add a `jerry-research.md` at the root of any target repo to extend the base research prompt with repo-specific guidance.

## Setup

See `config.json` for target repos and thresholds. 
See Makefile for commands.
