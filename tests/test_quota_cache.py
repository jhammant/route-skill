"""The quotamax probe is cached so a slow rescan can't strip quota awareness."""

from __future__ import annotations

from route import eligibility


def test_successful_quota_probe_is_cached(monkeypatch):
    # Arrange
    calls = []

    def fake_run_json(cmd, timeout):
        calls.append(cmd)
        return {"headroom": "constrained"} if cmd[1] == "agent" else {}

    monkeypatch.setattr(eligibility, "_run_json", fake_run_json)

    # Act
    eligibility.fetch_quota()
    eligibility.fetch_quota()

    # Assert: one probe pair, then served from the cache.
    assert len(calls) == 2


def test_failed_probe_is_not_cached(monkeypatch):
    # Arrange
    calls = []

    def fake_run_json(cmd, timeout):
        calls.append(cmd)
        return None

    monkeypatch.setattr(eligibility, "_run_json", fake_run_json)

    # Act
    eligibility.fetch_quota()
    eligibility.fetch_quota()

    # Assert: a timeout is retried next time rather than cached as "no data".
    assert len(calls) == 4
