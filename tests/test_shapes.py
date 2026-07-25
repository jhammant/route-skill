"""SPEC test 1: every fixture maps into the enum; unknown -> orchestration."""

from __future__ import annotations

from route.shapes import SHAPES, classify_shape

FIXTURES = [
    ("Implement a login endpoint with JWT auth", "coding:implement"),
    ("Add a --verbose flag to the CLI", "coding:implement"),
    ("Refactor the router module into smaller functions", "coding:refactor"),
    ("Rename UserRecord to Account everywhere", "coding:refactor"),
    ("Add tests for the pagination parser", "coding:test"),
    ("Write unit tests covering the retry logic", "coding:test"),
    ("Fix the off-by-one error in the pager", "coding:debug"),
    ("Debug why the worker crashes on empty input", "coding:debug"),
    ("Review this diff for security issues", "coding:review"),
    ("Audit the auth code before we ship", "coding:review"),
    ("Classify these 2,000 support tickets by intent", "batch:classify"),
    ("Summarize every README in the org", "batch:summarize"),
    ("Extract dates from all 400 invoices", "batch:extract"),
    ("Embed the documentation corpus for search", "batch:embed"),
    ("Compare Postgres and SQLite for an embedded queue", "research"),
    ("What is the best way to structure a monorepo", "research"),
    ("Write a blog post about the migration", "writing"),
    ("Draft the release notes for 2.0", "writing"),
    ("Deploy the service and then update the DNS", "orchestration"),
    ("xyzzy plugh foobar", "orchestration"),
    ("", "orchestration"),
]

GARBAGE = [
    "asdkfj alskdjf",
    "12345 !!!",
    "the the the the",
    "do the thing with the stuff",
    "hmm",
]


def test_fixtures_map_into_the_enum():
    for text, expected in FIXTURES:
        assert classify_shape(text) == expected, f"{text!r}"


def test_every_output_is_in_the_closed_vocabulary():
    for text, _ in FIXTURES + [(g, None) for g in GARBAGE]:
        assert classify_shape(text) in SHAPES, f"{text!r} leaked off-enum"


def test_unknown_maps_to_orchestration():
    for text in GARBAGE:
        assert classify_shape(text) == "orchestration", f"{text!r}"


def test_llm_fallback_never_emits_off_enum(monkeypatch):
    """Even a hallucinating local LLM can't invent a shape."""
    import route.shapes as shapes

    class FakeProc:
        returncode = 0
        stdout = "definitely-not-a-shape\n"

    monkeypatch.setattr(shapes.subprocess, "run", lambda *a, **k: FakeProc())
    assert shapes._classify_llm("anything") is None
    assert classify_shape("anything", use_llm=True) == "orchestration"
