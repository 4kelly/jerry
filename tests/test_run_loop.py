"""Tests for bin/run_loop.py."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import run_loop as rl


FAKE_ISSUE = {"number": 7, "title": "Remove unused import", "body": "## Where\n`calendars/models.py:3`"}


@pytest.fixture()
def jerry(tmp_path):
    """Minimal jerry directory: config + prompts."""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/research.md").write_text("RESEARCH PROMPT BASE")
    (tmp_path / "prompts/fix.md").write_text("FIX PROMPT BASE")

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
        patch.object(rl, "pick_issue", return_value=FAKE_ISSUE),
    ):
        run_main(ctx)

    models_used = [c.kwargs["model"] for c in mock_claude.call_args_list]
    # Should alternate: research(opus), fix(sonnet), research(opus), fix(sonnet)...
    assert models_used[0] == "claude-opus-4-6", "first call should be research (opus)"
    assert models_used[1] == "claude-sonnet-4-6", "second call should be fix (sonnet)"
    assert models_used[2] == "claude-opus-4-6", "third call should be research (opus)"

    # Research prompt should contain REPO_INFO header
    research_prompt = mock_claude.call_args_list[0].args[0]
    assert "REPO_INFO:" in research_prompt
    assert "4kelly" in research_prompt

    # Fix prompt should contain the issue JSON
    fix_prompt = mock_claude.call_args_list[1].args[0]
    assert "ISSUE_JSON:" in fix_prompt
    assert str(FAKE_ISSUE["number"]) in fix_prompt


def test_runs_until_quota_exhausted(ctx):
    """Loop keeps running even at high usage — no early stop on threshold."""
    with (
        patch.object(rl, "get_pct", return_value=95.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "pick_issue", return_value=FAKE_ISSUE),
    ):
        run_main(ctx)

    mock_claude.assert_called()


def test_fix_falls_back_to_research_when_no_issues(ctx):
    """If no open issues exist during fix mode, switches to research without calling claude."""
    # Prime state to fix mode
    ctx["state"].write_text(json.dumps({"mode": "fix", "last_repo_idx": 1}))

    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "pick_issue", return_value=None),  # no open issues
    ):
        run_main(ctx)

    # Should eventually call research (opus) after fallback, never sonnet
    models_used = [c.kwargs["model"] for c in mock_claude.call_args_list]
    assert "claude-sonnet-4-6" not in models_used
    assert "claude-opus-4-6" in models_used


def test_repo_specific_prompt_appended(ctx):
    """jerry-research.md in the target repo extends the base research prompt."""
    (ctx["repo"] / "jerry-research.md").write_text("REPO SPECIFIC FOCUS")

    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "pick_issue", return_value=FAKE_ISSUE),
    ):
        run_main(ctx)

    first_prompt = mock_claude.call_args_list[0].args[0]
    assert "RESEARCH PROMPT BASE" in first_prompt
    assert "REPO SPECIFIC FOCUS" in first_prompt
    # Repo-specific content must come after base
    assert first_prompt.index("RESEARCH PROMPT BASE") < first_prompt.index("REPO SPECIFIC FOCUS")


def test_pick_issue_filters_by_author():
    """pick_issue must pass --author @me so external issues are never picked."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = json.dumps([FAKE_ISSUE])
        result = rl.pick_issue("4kelly", "calbot")

    cmd = mock_run.call_args.args[0]
    assert "--author" in cmd
    assert "@me" in cmd
    assert result == FAKE_ISSUE


def test_state_persists_between_iterations(ctx):
    """State file is updated after each iteration so a restart picks up where it left off."""
    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0),
        patch.object(rl, "pick_issue", return_value=FAKE_ISSUE),
    ):
        run_main(ctx)

    final_state = json.loads(ctx["state"].read_text())
    # last_repo_idx increments on each research pass
    assert final_state["last_repo_idx"] > 0
