#!/usr/bin/env python3
"""Jerry run loop: dispatches research and fix agents until usage threshold."""

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from subprocess import run as _run
from urllib.request import Request, urlopen

JERRY = Path(__file__).parent.parent
LOG = Path.home() / ".claude/jerry/run.log"
STATE = Path.home() / ".claude/jerry/state.json"

DISALLOWED = "Agent,WebFetch,WebSearch,NotebookEdit,Skill,RemoteTrigger"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def get_pct(token: str) -> float:
    # Use subprocess curl with --config - so token never appears in process args
    result = subprocess.run(
        ["curl", "-sf", "--config", "-", "https://api.anthropic.com/api/oauth/usage"],
        input=f'header = "Authorization: Bearer {token}"\nheader = "anthropic-beta: oauth-2025-04-20"\n',
        text=True,
        capture_output=True,
    )
    data = json.loads(result.stdout)
    return float(data["seven_day"]["utilization"])


def run_claude(prompt: str, model: str) -> int:
    cmd = [
        "claude", "-p", prompt,
        "--model", model,
        "--no-color",
        "--disallowedTools", DISALLOWED,
    ]
    result = subprocess.run(cmd, text=True, capture_output=True)
    with LOG.open("a") as f:
        f.write(result.stdout)
        if result.stderr:
            f.write(result.stderr)
    return result.returncode


def pick_issue(owner: str, repo: str) -> dict | None:
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--repo", f"{owner}/{repo}",
            "--label", "ai-backlog",
            "--state", "open",
            "--json", "number,title,body",
            "--limit", "100",
        ],
        text=True,
        capture_output=True,
    )
    issues = json.loads(result.stdout or "[]")
    issues.sort(key=lambda x: x["number"])
    return issues[0] if issues else None


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"mode": "research", "last_repo_idx": 0}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2))


def build_research_prompt(repo_info: dict) -> str:
    owner, repo, path = repo_info["owner"], repo_info["repo"], repo_info["path"]
    base = (JERRY / "prompts/research.md").read_text()
    prompt = f"REPO_INFO: owner={owner} repo={repo} path={path}\n\n{base}"

    extra = Path(path) / "jerry-research.md"
    if extra.exists():
        log(f"Extending with {owner}/{repo}/jerry-research.md")
        prompt += f"\n\n## Repo-Specific Focus (from {owner}/{repo})\n{extra.read_text()}"

    return prompt


def get_token() -> str:
    """Read Bearer token from macOS Keychain or credentials file — never from argv."""
    result = _run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        text=True, capture_output=True,
    )
    raw = result.stdout.strip()
    token = ""
    if raw:
        try:
            data = json.loads(raw)
            # Keychain stores JSON: {"claudeAiOauth": {"accessToken": "..."}}
            token = (
                data.get("claudeAiOauth", {}).get("accessToken")
                or data.get("accessToken")
                or data.get("access_token", "")
            )
        except json.JSONDecodeError:
            token = raw  # raw token string (future format)
    if not token:
        creds = Path.home() / ".claude/credentials.json"
        if creds.exists():
            data = json.loads(creds.read_text())
            token = (
                data.get("claudeAiOauth", {}).get("accessToken")
                or data.get("accessToken")
                or data.get("access_token", "")
            )
    if not token:
        raise RuntimeError("No Claude credentials found")
    return token


def main() -> None:
    token = get_token()
    config = json.loads((JERRY / "config.json").read_text())
    repos = config["repos"]
    stop_threshold = config.get("stop_threshold_pct", 92)
    max_iter = config.get("max_iterations_per_night", 8)

    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = load_state()

    for i in range(max_iter):
        pct = get_pct(token)
        log(f"--- Iteration {i + 1} | Usage: {pct:.1f}% ---")

        if pct >= stop_threshold:
            log(f"Usage at threshold ({stop_threshold}%), stopping")
            break

        repo_info = repos[state["last_repo_idx"] % len(repos)]
        owner, repo = repo_info["owner"], repo_info["repo"]

        if state["mode"] == "research":
            log(f"Research: {owner}/{repo}")
            run_claude(build_research_prompt(repo_info), model="claude-opus-4-6")
            state["mode"] = "fix"
            state["last_repo_idx"] = state["last_repo_idx"] + 1

        else:
            issue = pick_issue(owner, repo)
            if not issue:
                log(f"No open issues for {owner}/{repo}, switching to research")
                state["mode"] = "research"
                save_state(state)
                continue

            log(f"Fix: {owner}/{repo}#{issue['number']}")
            fix_prompt = (JERRY / "prompts/fix.md").read_text()
            run_claude(f"ISSUE_JSON: {json.dumps(issue)}\n\n{fix_prompt}", model="claude-sonnet-4-6")
            state["mode"] = "research"

        save_state(state)
        time.sleep(30)

    log("Run loop complete.")


if __name__ == "__main__":
    main()
