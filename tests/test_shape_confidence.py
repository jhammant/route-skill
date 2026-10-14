"""Classification exposes ambiguity and keeps feature delivery out of batches."""

import json
from types import SimpleNamespace

import pytest

from route.cli import main
from route.shapes import classify_shape, classify_with_confidence


@pytest.mark.parametrize("text,shape,confidence", [
    ("xyzzy plugh", "orchestration", "low"),
    ("parser coverage", "coding:test", "low"),
    ("daily digest for all teams", "batch:summarize", "low"),
    ("Write tests for the parser", "coding:test", "high"),
    ("summarize these 500 emails", "batch:summarize", "high"),
    ("build the trial SMS notices feature with a signed redirect route", "coding:implement", "high"),
    ("build the trial SMS digest feature for all accounts with a signed redirect route", "coding:implement", "high"),
])
def test_confidence_and_feature_build_regression(text, shape, confidence):
    # Arrange / Act
    result = classify_with_confidence(text)

    # Assert
    assert (result.shape, result.confidence) == (shape, confidence)
    assert classify_shape(text) == shape  # keep existing callers working


@pytest.mark.parametrize("text", ["xyzzy plugh", "parser coverage"])
def test_low_confidence_plan_suggests_explicit_shape_and_tier(capsys, text):
    # Arrange / Act
    assert main(["--host", "claude", "--plan-only", "--no-quota", "--no-llm", "--tier", "simple", text]) == 0
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]

    # Assert
    assert plan["shape_confidence"] == "low"
    assert plan["suggest"] == "pass --shape <shape> --tier <tier>"


def test_explicit_shape_is_confident_and_does_not_call_llm(monkeypatch, capsys):
    # Arrange
    monkeypatch.setattr("route.shapes._classify_llm", lambda text: pytest.fail("explicit shape wins"))

    # Act
    main(["--host", "claude", "--plan-only", "--no-quota", "--llm-shape",
          "--shape", "coding:implement", "--tier", "simple", "xyzzy plugh"])
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]

    # Assert
    assert plan["shape_confidence"] == "high" and plan["shape_source"] == "explicit"
    assert "suggest" not in plan


def test_llm_shape_flag_uses_existing_local_classifier_for_weak_cue(monkeypatch, capsys):
    # Arrange
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="coding:implement\n")

    monkeypatch.setattr("route.shapes.subprocess.run", run)

    # Act
    main(["--host", "claude", "--plan-only", "--no-quota", "--llm-shape",
          "--tier", "simple", "daily digest for all teams"])
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]

    # Assert
    assert len(calls) == 1 and calls[0][:4] == ["local-llm", "ask", "--class", "reflex"]
    assert plan["shape"] == "coding:implement" and plan["shape_source"] == "llm"
    assert plan["shape_confidence"] == "high"


@pytest.mark.parametrize("reply", ["invented:shape", "", "coding:implement plus an explanation"])
def test_failed_llm_keeps_low_confidence_and_closed_enum(monkeypatch, reply):
    # Arrange
    monkeypatch.setattr("route.shapes.subprocess.run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=reply))

    # Act
    result = classify_with_confidence("parser coverage", use_llm=True)

    # Assert
    assert (result.shape, result.confidence) == ("coding:test", "low")
