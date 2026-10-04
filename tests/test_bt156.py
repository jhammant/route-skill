"""BT-156: sensitivity false positives, feature-build classification, broken arms."""

from __future__ import annotations

from route.complexity import estimate_complexity
from route.eligibility import eligible_for, probably_private
from route.shapes import classify_shape

BT31_SPEC = (
    "Build the a-mail digest feature on a branch: add DB migrations, an endpoint that "
    "returns the daily digest, summarising each thread. Links are HMAC-signed with a "
    "server secret; no secrets in code, and a password policy is out of scope. "
    "Add tests and run typecheck before committing."
)


def test_design_words_are_not_sensitive():
    assert not probably_private(BT31_SPEC)
    assert not probably_private("rotate the API key and token credentials config")


def test_actual_secrets_are_sensitive():
    assert probably_private("use sk-abcdef0123456789abcdef01 for the call")
    assert probably_private("AWS key AKIAABCDEFGHIJKLMNOP leaked")
    assert probably_private("ghp_" + "a1" * 20)
    assert probably_private("-----BEGIN RSA PRIVATE KEY-----")
    assert probably_private("API_KEY=abc123def456ghi789")
    assert probably_private("password: 'hunter2hunter2hunter2'")
    assert probably_private("summarise the customer data export")


def test_feature_build_beats_summarise_words():
    assert classify_shape(BT31_SPEC) == "coding:implement"
    assert estimate_complexity(BT31_SPEC, use_taskgauge=False) == "hard"


def test_real_batch_still_batch():
    assert classify_shape("summarise these 500 emails") == "batch:summarize"


def test_arm_missing_subcommand_is_excluded(pools, monkeypatch):
    monkeypatch.setattr("route.eligibility.subcommand_available", lambda spec: False)
    assert "local-agent" not in eligible_for("coding:implement", pools)
    monkeypatch.setattr("route.eligibility.subcommand_available", lambda spec: True)
    assert "local-agent" in eligible_for("coding:implement", pools)
