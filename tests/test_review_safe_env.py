"""Common credential shapes beyond *_TOKEN/*_SECRET/*_API_KEY must not reach checks."""
from route.ultra_safety import safe_env


def test_safe_env_strips_password_key_pat_and_agent_variables():
    base = {
        "DB_PASSWORD": "x", "PGPASSWORD": "x", "OPENAI_KEY": "x", "GITHUB_PAT": "x",
        "SSH_AUTH_SOCK": "/tmp/agent.sock", "GOOGLE_APPLICATION_CREDENTIALS": "/k.json",
        "PATH": "/usr/bin",
    }
    assert safe_env(base) == {"PATH": "/usr/bin", "ROUTE_WORKER": "1"}
