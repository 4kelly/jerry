"""Tests for bin/run-loop.py.

Gate is always assumed to pass here — we mock get_pct() to return a value
below threshold and drive the loop through research/fix cycles.
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import call, patch

import pytest

# run-loop.py has a hyphen so normal import doesn't work
_spec = importlib.util.spec_from_file_location(
    "run_loop", Path(__file__).parent.parent / "bin/run-loop.py"
)
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


FAKE_ISSUE = {"number": 7, "title": "Remove unused import", "body": "## Where\n`calendars/models.py:3`"}


@pytest.fixture()
def jerry(tmp_path):
    """Minimal jerry directory: config + prompts."""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/research.md").write_text("RESEARCH PROMPT BASE")
    (tmp_path / "prompts/fix.md").write_text("FIX PROMPT BASE")

    repo_path = tmp_path / "calbot"
    repo_path.mkdir()

    (tmp_path / "config.json").write_text(json.dumps({
        "repos": [{"owner": "4kelly", "repo": "calbot", "path": str(repo_path)}],
        "stop_threshold_pct": 92,
        "max_iterations_per_night": 6,
    }))
    return tmp_path


@pytest.fixture()
def patches(tmp_path, jerry):
    """Patch all I/O so tests are fast and hermetic."""
    state_file = tmp_path / "state.json"
    log_file = tmp_path / "run.log"

    with (
        patch.object(rl, "JERRY", jerry),
        patch.object(rl, "STATE", state_file),
        patch.object(rl, "LOG", log_file),
        patch.object(rl, "get_token", return_value="fake-token"),
        patch("time.sleep"),  # don't actually wait 30s between iterations
    ):
        yield {"state": state_file, "log": log_file, "repo": jerry / "calbot"}


def test_research_then_fix_cycle(patches):
    """Starting from scratch: research (opus) → fix (sonnet) → research → fix."""
    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "pick_issue", return_value=FAKE_ISSUE),
    ):
        rl.main()

    models_used = [c.kwargs["model"] for c in mock_claude.call_args_list]
    # Should alternate: research(opus), fix(sonnet), research(opus), fix(sonnet)...
    assert models_used[0] == "claude-opus-4-6",  "first call should be research (opus)"
    assert models_used[1] == "claude-sonnet-4-6", "second call should be fix (sonnet)"
    assert models_used[2] == "claude-opus-4-6",  "third call should be research (opus)"

    # Research prompt should contain REPO_INFO header
    research_prompt = mock_claude.call_args_list[0].args[0]
    assert "REPO_INFO:" in research_prompt
    assert "4kelly" in research_prompt

    # Fix prompt should contain the issue JSON
    fix_prompt = mock_claude.call_args_list[1].args[0]
    assert "ISSUE_JSON:" in fix_prompt
    assert str(FAKE_ISSUE["number"]) in fix_prompt


def test_stops_at_usage_threshold(patches):
    """Loop exits immediately if usage is already at threshold."""
    with (
        patch.object(rl, "get_pct", return_value=95.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "pick_issue", return_value=FAKE_ISSUE),
    ):
        rl.main()

    mock_claude.assert_not_called()


def test_fix_falls_back_to_research_when_no_issues(patches):
    """If no open issues exist during fix mode, switches to research without calling claude."""
    # Prime state to fix mode
    patches["state"].write_text(json.dumps({"mode": "fix", "last_repo_idx": 1}))

    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "pick_issue", return_value=None),  # no open issues
    ):
        rl.main()

    # Should eventually call research (opus) after fallback, never sonnet
    models_used = [c.kwargs["model"] for c in mock_claude.call_args_list]
    assert "claude-sonnet-4-6" not in models_used
    assert "claude-opus-4-6" in models_used


def test_repo_specific_prompt_appended(patches):
    """jerry-research.md in the target repo extends the base research prompt."""
    (patches["repo"] / "jerry-research.md").write_text("REPO SPECIFIC FOCUS")

    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0) as mock_claude,
        patch.object(rl, "pick_issue", return_value=FAKE_ISSUE),
    ):
        rl.main()

    first_prompt = mock_claude.call_args_list[0].args[0]
    assert "RESEARCH PROMPT BASE" in first_prompt
    assert "REPO SPECIFIC FOCUS" in first_prompt
    # Repo-specific content must come after base
    assert first_prompt.index("RESEARCH PROMPT BASE") < first_prompt.index("REPO SPECIFIC FOCUS")


def test_state_persists_between_iterations(patches):
    """State file is updated after each iteration so a restart picks up where it left off."""
    with (
        patch.object(rl, "get_pct", return_value=50.0),
        patch.object(rl, "run_claude", return_value=0),
        patch.object(rl, "pick_issue", return_value=FAKE_ISSUE),
    ):
        rl.main()

    final_state = json.loads(patches["state"].read_text())
    # last_repo_idx increments on each research pass
    assert final_state["last_repo_idx"] > 0
