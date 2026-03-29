"""Tests for bin/run_loop.py."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import run_loop as rl


FAKE_ISSUE = {"number": 7, "title": "Remove unused import", "body": "## Where\n`calendars/models.py:3`"}
FAKE_ISSUE_2 = {"number": 8, "title": "Fix off-by-one", "body": "## Where\n`calendars/views.py:42`"}


@pytest.fixture()
def jerry(tmp_path):
    """Minimal jerry directory: config + prompts."""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/research.md").write_text("owner={owner} repo={repo} path={path} focus={focus}")
    (tmp_path / "prompts/fix.md").write_text("issue={issue_json} owner={owner} repo={repo} number={number}")

    repo_path = tmp_path / "calbot"
    repo_path.mkdir()

    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "repos": [{"owner": "4kelly", "repo": "calbot", "path": str(repo_path)}],
                "max_iterations_per_night": 6,
            }
        )
    )
    return tmp_path


@pytest.fixture()
def ctx(tmp_path, jerry):
    """Paths for hermetic test runs."""
    return {
        "jerry": jerry,
        "state": tmp_path / "state.json",
        "log": tmp_path / "run.log",
        "repo": jerry / "calbot",
    }


def run_main(ctx, **kwargs):
    kwargs.setdefault("count_open_issues_fn", lambda owner, repo: 0)
    kwargs.setdefault("has_open_pr_fn", lambda owner, repo, number: False)
    return rl.main(
        jerry=ctx["jerry"],
        state_path=ctx["state"],
        log_path=ctx["log"],
        get_token_fn=lambda: "fake-token",
        sleep_fn=lambda _: None,
        **kwargs,
    )


def test_research_then_fix_cycle(ctx):
    """Starting from scratch: research (opus) → fix (sonnet) → research → fix."""
    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "list_issues", return_value=[FAKE_ISSUE]),
    ):
        run_main(ctx)

    models_used = [c.kwargs["model"] for c in mock_claude.call_args_list]
    assert models_used[0] == "claude-opus-4-6", "first call should be research (opus)"
    assert models_used[1] == "claude-sonnet-4-6", "second call should be fix (sonnet)"
    assert models_used[2] == "claude-opus-4-6", "third call should be research (opus)"

    research_prompt = mock_claude.call_args_list[0].args[0]
    assert "4kelly" in research_prompt
    assert "calbot" in research_prompt

    fix_prompt = mock_claude.call_args_list[1].args[0]
    assert str(FAKE_ISSUE["number"]) in fix_prompt
    assert "4kelly" in fix_prompt


def test_runs_until_quota_exhausted(ctx):
    """Loop keeps running even at high usage — no early stop on threshold."""
    with (
        patch.object(rl, "get_pct", return_value=95.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "list_issues", return_value=[FAKE_ISSUE]),
    ):
        run_main(ctx)

    mock_claude.assert_called()


def test_fix_falls_back_to_research_when_no_issues(ctx):
    """If no open issues exist during fix mode, switches to research without calling claude."""
    ctx["state"].write_text(json.dumps({"mode": "fix", "last_repo_idx": 1}))

    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "list_issues", return_value=[]),
    ):
        run_main(ctx)

    models_used = [c.kwargs["model"] for c in mock_claude.call_args_list]
    assert "claude-sonnet-4-6" not in models_used
    assert "claude-opus-4-6" in models_used


def test_repo_specific_prompt_appended(ctx):
    """jerry-research.md in the target repo extends the base research prompt."""
    (ctx["repo"] / "jerry-research.md").write_text("REPO SPECIFIC FOCUS")

    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "list_issues", return_value=[FAKE_ISSUE]),
    ):
        run_main(ctx)

    first_prompt = mock_claude.call_args_list[0].args[0]
    assert "REPO SPECIFIC FOCUS" in first_prompt


def test_list_issues_filters_by_author():
    """list_issues must pass --author @me so external issues are never picked."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps([FAKE_ISSUE])
        result = rl.list_issues("4kelly", "calbot")

    cmd = mock_run.call_args.args[0]
    assert "--author" in cmd
    assert "@me" in cmd
    assert result == [FAKE_ISSUE]


def test_skips_research_when_issue_cap_reached(ctx):
    """When total open issues >= max_open_issues, skip research and go straight to fix."""
    config = json.loads((ctx["jerry"] / "config.json").read_text())
    config["max_open_issues"] = 2
    (ctx["jerry"] / "config.json").write_text(json.dumps(config))

    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "list_issues", return_value=[FAKE_ISSUE]),
    ):
        run_main(ctx, count_open_issues_fn=lambda owner, repo: 2)

    models_used = [c.kwargs["model"] for c in mock_claude.call_args_list]
    assert "claude-opus-4-6" not in models_used, "research should be skipped when at cap"
    assert "claude-sonnet-4-6" in models_used


def test_state_persists_between_iterations(ctx):
    """State file is updated after each iteration so a restart picks up where it left off."""
    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0),
        patch.object(rl, "list_issues", return_value=[FAKE_ISSUE]),
    ):
        run_main(ctx)

    final_state = json.loads(ctx["state"].read_text())
    assert final_state["last_repo_idx"] > 0


def test_fix_skips_issues_with_open_prs(ctx):
    """When the first issue has an open PR, skip it and fix the next one — one iteration."""
    ctx["state"].write_text(json.dumps({"mode": "fix", "last_repo_idx": 0}))

    # issue 7 has an open PR, issue 8 does not
    def fake_has_open_pr(owner, repo, number):
        return number == FAKE_ISSUE["number"]

    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "list_issues", return_value=[FAKE_ISSUE, FAKE_ISSUE_2]),
    ):
        run_main(ctx, has_open_pr_fn=fake_has_open_pr)

    fix_calls = [c for c in mock_claude.call_args_list if c.kwargs.get("model") == "claude-sonnet-4-6"]
    assert len(fix_calls) >= 1
    fix_prompt = fix_calls[0].args[0]
    assert str(FAKE_ISSUE_2["number"]) in fix_prompt
    assert str(FAKE_ISSUE["number"]) not in fix_prompt.split("issue=")[1].split(" ")[0]


def test_fix_falls_back_to_research_when_all_issues_have_prs(ctx):
    """When every issue has an open PR, fall back to research without dispatching a fix."""
    ctx["state"].write_text(json.dumps({"mode": "fix", "last_repo_idx": 0}))

    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "list_issues", return_value=[FAKE_ISSUE, FAKE_ISSUE_2]),
    ):
        run_main(ctx, has_open_pr_fn=lambda owner, repo, number: True)

    # First iteration: fix mode but all issues blocked → switches to research, no sonnet call
    # Subsequent iterations: research runs (opus)
    first_iter_calls = mock_claude.call_args_list
    # No sonnet call in any iteration (research always follows the fallback)
    models = [c.kwargs["model"] for c in first_iter_calls]
    assert "claude-sonnet-4-6" not in models[:2], "should not fix when all issues have open PRs"
