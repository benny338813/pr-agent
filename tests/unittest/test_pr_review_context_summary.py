import tomllib
from pathlib import Path
from unittest import mock

from pr_agent.tools.pr_reviewer import PRReviewer


def test_review_context_summary_reports_jira_and_gitnexus_usage():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.vars = {
        "title": "[FW-123]: Existing title",
        "related_tickets": [
            {"ticket_id": "FW-123", "ticket_url": "https://jira.example.com/browse/FW-123"},
            {"ticket_id": "FW-124", "ticket_url": "https://jira.example.com/browse/FW-124"},
        ]
    }
    reviewer.gitnexus_context_status = "used"
    reviewer.gitnexus_context_mode = "pr_head_context"

    summary = reviewer._get_review_context_summary_markdown()

    assert "本次 Review 使用的上下文" in summary
    assert "建議 MR Title：`[FW-123]: Existing title`" in summary
    assert "Jira：已使用 2 筆 ticket" in summary
    assert "GitNexus：已使用 pr_head_context" in summary


def test_review_context_summary_reports_missing_optional_context():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.vars = {"related_tickets": []}
    reviewer.gitnexus_context_status = "not_used"
    reviewer.gitnexus_context_mode = ""

    summary = reviewer._get_review_context_summary_markdown()

    assert "Jira：未使用" in summary
    assert "GitNexus：未使用" in summary
    assert "建議 MR Title：未產生" in summary


def test_pr_review_prompt_requires_traditional_chinese_and_context_disclosure():
    prompt = Path("pr_agent/settings/pr_reviewer_prompts.toml").read_text(encoding="utf-8")

    assert "繁體中文" in prompt
    assert "context_usage_summary" in prompt
    assert "context_usage_summary: str = Field" not in prompt
    assert "GitNexus" in prompt
    assert "Jira" in prompt


def test_repo_config_forces_traditional_chinese_responses():
    repo_config = tomllib.loads(Path(".pr_agent.toml").read_text(encoding="utf-8"))
    default_config = tomllib.loads(Path("pr_agent/settings/configuration.toml").read_text(encoding="utf-8"))

    assert repo_config["config"]["response_language"] == "zh-TW"
    assert default_config["config"]["response_language"] == "zh-TW"


def test_prepare_pr_review_prefixes_context_summary(monkeypatch):
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.vars = {"related_tickets": [{"ticket_id": "FW-123"}]}
    reviewer.gitnexus_context_status = "used"
    reviewer.gitnexus_context_mode = "base_context"
    reviewer.prediction = """review:
  relevant_tests: |
    No
  key_issues_to_review: []
  security_concerns: |
    No
"""
    reviewer.git_provider = type("Provider", (), {
        "is_supported": lambda self, feature: False,
        "get_diff_files": lambda self: [],
    })()
    reviewer.incremental = type("Incremental", (), {"is_incremental": False})()
    reviewer.set_review_labels = mock.Mock()
    monkeypatch.setattr("pr_agent.tools.pr_reviewer.github_action_output", lambda data, command: None)

    markdown = reviewer._prepare_pr_review()

    assert markdown.startswith("### 本次 Review 使用的上下文")
    assert "Jira：已使用 1 筆 ticket" in markdown
    assert "GitNexus：已使用 base_context" in markdown
