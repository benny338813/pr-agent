from pr_agent.tools.ticket_pr_compliance_check import (
    fetch_jira_tickets,
    find_jira_tickets,
)


def _settings(values):
    class FakeSettings:
        @staticmethod
        def get(key, default=None):
            return values.get(key, default)

    return FakeSettings()


class FakeProvider:
    def get_user_description(self):
        return "Implements PROJ-123 and https://jira.example.com/browse/OPS-9"

    def get_pr_branch(self):
        return "feature/PROJ-123-local-llm"


def test_find_jira_tickets_deduplicates_keys():
    assert find_jira_tickets("PROJ-123 https://jira.example.com/browse/PROJ-123 OPS-9") == ["PROJ-123", "OPS-9"]


def test_fetch_jira_tickets_uses_bearer_pat_from_env(monkeypatch):
    import pr_agent.tools.ticket_pr_compliance_check as module

    monkeypatch.setenv("JIRA_BENNY_BOT_PAT", "pat-from-env")
    monkeypatch.setattr(module, "get_settings", lambda: _settings({
        "jira.jira_base_url": "https://jira.example.com",
        "jira.jira_api_token_env": "JIRA_BENNY_BOT_PAT",
    }))

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "key": "PROJ-123",
                "fields": {
                    "summary": "Local LLM integration",
                    "description": "Use Ainexus for PR-Agent.",
                    "labels": ["ai", "review"],
                    "subtasks": [{
                        "key": "PROJ-124",
                        "fields": {"summary": "Wire configuration"},
                    }],
                },
            }

    def fake_get(url, headers, auth, timeout):
        calls.append({"url": url, "headers": headers, "auth": auth, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(module.requests, "get", fake_get)

    tickets = fetch_jira_tickets(FakeProvider(), 10000)

    assert calls[0]["url"] == "https://jira.example.com/rest/api/2/issue/PROJ-123"
    assert calls[0]["headers"]["Authorization"] == "Bearer pat-from-env"
    assert calls[0]["auth"] is None
    assert tickets[0]["ticket_id"] == "PROJ-123"
    assert tickets[0]["ticket_url"] == "https://jira.example.com/browse/PROJ-123"
    assert tickets[0]["title"] == "Local LLM integration"
    assert tickets[0]["labels"] == "ai, review"
    assert tickets[0]["sub_issues"][0]["ticket_url"] == "https://jira.example.com/browse/PROJ-124"


def test_fetch_jira_tickets_uses_basic_auth_when_email_is_configured(monkeypatch):
    import pr_agent.tools.ticket_pr_compliance_check as module

    monkeypatch.setattr(module, "get_settings", lambda: _settings({
        "jira.jira_base_url": "https://jira.example.com",
        "jira.jira_api_token": "token",
        "jira.jira_api_email": "bot@example.com",
    }))

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"key": "PROJ-123", "fields": {"summary": "Summary", "description": None}}

    def fake_get(url, headers, auth, timeout):
        calls.append({"headers": headers, "auth": auth})
        return FakeResponse()

    monkeypatch.setattr(module.requests, "get", fake_get)

    tickets = fetch_jira_tickets(FakeProvider(), 10000)

    assert "Authorization" not in calls[0]["headers"]
    assert calls[0]["auth"] == ("bot@example.com", "token")
    assert tickets[0]["body"] == ""
