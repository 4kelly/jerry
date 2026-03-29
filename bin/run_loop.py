#!/usr/bin/env python3
"""Jerry run loop: dispatches research and fix agents until usage threshold."""

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from subprocess import run as _run
from typing import Optional

JERRY = Path(__file__).parent.parent
LOG = JERRY / "run.log"
STATE = JERRY / "state.json"

DISALLOWED = ""


def log(msg: str, log_path: Path) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with log_path.open("a") as f:
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


def run_claude(prompt: str, model: str, log_path: Path, repo_path: str = "") -> int:
    cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "--print",
        "--model",
        model,
        "--disallowedTools",
        DISALLOWED
    ]
    if repo_path:
        cmd.extend(["--add-dir", repo_path])
    result = subprocess.run(cmd, input=prompt, text=True, capture_output=True)
    with log_path.open("a") as f:
        f.write(result.stdout)
        if result.stderr:
            f.write(result.stderr)
    return result.returncode


def count_open_issues(owner: str, repo: str) -> int:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--label",
            "ai-backlog",
            "--author",
            "@me",
            "--state",
            "open",
            "--json",
            "number",
            "--limit",
            "100",
        ],
        text=True,
        capture_output=True,
    )
    return len(json.loads(result.stdout or "[]"))


def list_issues(owner: str, repo: str) -> list[dict]:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            f"{owner}/{repo}",
            "--label",
            "ai-backlog",
            "--author",
            "@me",
            "--state",
            "open",
            "--json",
            "number,title,body",
            "--limit",
            "100",
        ],
        text=True,
        capture_output=True,
    )
    issues = json.loads(result.stdout or "[]")
    issues.sort(key=lambda x: x["number"])
    return issues


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"mode": "research", "last_repo_idx": 0}


def save_state(state: dict, state_path: Path) -> None:
    state_path.write_text(json.dumps(state, indent=2))


def has_open_pr(owner: str, repo: str, issue_number: int) -> bool:
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--repo", f"{owner}/{repo}", "--json", "linkedPullRequests"],
        text=True,
        capture_output=True,
    )
    data = json.loads(result.stdout or "{}")
    return any(
        pr.get("state", "").upper() == "OPEN"
        for pr in data.get("linkedPullRequests", [])
    )


def build_fix_prompt(issue: dict, owner: str, repo: str, jerry: Path) -> str:
    base = (jerry / "prompts/fix.md").read_text()
    return base.format(
        issue_json=json.dumps(issue),
        owner=owner,
        repo=repo,
        number=issue["number"],
    )


def build_research_prompt(repo_info: dict, jerry: Path, log_fn) -> str:
    owner, repo, path = repo_info["owner"], repo_info["repo"], repo_info["path"]
    base = (jerry / "prompts/research.md").read_text()

    focus = ""
    extra = Path(path) / "jerry-research.md"
    if extra.exists():
        log_fn(f"Extending with {owner}/{repo}/jerry-research.md")
        focus = extra.read_text()

    return base.format(owner=owner, repo=repo, path=path, focus=focus)


def get_token() -> str:
    """Read Bearer token from macOS Keychain or credentials file — never from argv."""
    result = _run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        text=True,
        capture_output=True,
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
        raise RuntimeError("No Claude credentials found")
    return token


def main(
    *,
    jerry: Path = JERRY,
    state_path: Path = STATE,
    log_path: Path = LOG,
    get_token_fn=None,
    sleep_fn=time.sleep,
    count_open_issues_fn=None,
    has_open_pr_fn=None,
) -> None:
    if get_token_fn is None:
        get_token_fn = get_token
    if count_open_issues_fn is None:
        count_open_issues_fn = count_open_issues
    if has_open_pr_fn is None:
        has_open_pr_fn = has_open_pr

    def _log(msg: str) -> None:
        log(msg, log_path)

    token = get_token_fn()
    config = json.loads((jerry / "config.json").read_text())
    repos = config["repos"]
    max_iter = config.get("max_iterations_per_night", 8)
    max_open_issues = config.get("max_open_issues", 10)

    state = load_state(state_path)

    for i in range(max_iter):
        iter_start = time.time()
        pct = get_pct(token)
        _log(f"--- Iteration {i + 1}/{max_iter} | Usage: {pct:.1f}% | Start: {datetime.now().strftime('%H:%M:%S')} ---")

        repo_info = repos[state["last_repo_idx"] % len(repos)]
        owner, repo = repo_info["owner"], repo_info["repo"]

        if state["mode"] == "research":
            total_open = sum(count_open_issues_fn(r["owner"], r["repo"]) for r in repos)
            if total_open >= max_open_issues:
                _log(f"Issue cap reached ({total_open}/{max_open_issues}), skipping research → fix")
                state["mode"] = "fix"
                save_state(state, state_path)
                continue

            _log(f"Research: {owner}/{repo}")
            rc = run_claude(build_research_prompt(repo_info, jerry, _log), model="claude-opus-4-6", log_path=log_path, repo_path=repo_info["path"])
            _log(f"Research completed (exit code: {rc})")
            state["mode"] = "fix"
            state["last_repo_idx"] = state["last_repo_idx"] + 1

        else:
            issue = None
            for candidate in list_issues(owner, repo):
                if has_open_pr_fn(owner, repo, candidate["number"]):
                    _log(f"Skipping #{candidate['number']}: open PR already exists")
                else:
                    issue = candidate
                    break

            if not issue:
                _log(f"No fixable issues for {owner}/{repo}, switching to research")
                state["mode"] = "research"
                save_state(state, state_path)
                continue

            _log(f"Fix: {owner}/{repo}#{issue['number']} - {issue['title']}")
            rc = run_claude(build_fix_prompt(issue, owner, repo, jerry), model="claude-sonnet-4-6", log_path=log_path, repo_path=repo_info["path"])
            _log(f"Fix completed (exit code: {rc})")
            state["mode"] = "research"

        save_state(state, state_path)
        iter_duration = time.time() - iter_start
        _log(f"Iteration {i + 1} completed in {iter_duration:.1f}s | {owner}/{repo} {state['mode']} done")
        sleep_fn(30)

    _log("Run loop complete.")


if __name__ == "__main__":
    main()
