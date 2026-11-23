"""Jira + GitLab instances on example hosts for tests."""


def default_test_integrations() -> dict:
    return {
        "jira": {"driver": "jira", "base_url": "https://jira.example.net", "auth": {"type": "token_file"},
                 "options": {"email": "u@test.example", "token_file": "/tmp/jira-test-token"}},
        "gitlab": {"driver": "gitlab", "base_url": "https://gitlab.example.com",
                   "auth": {"type": "private_token", "token": "t"}},
    }
