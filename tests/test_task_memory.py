"""Local neighbours help the current decision, never the exported posterior."""

import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from route.cli import main
from route.federate import FederationState
from route.memory import TaskMemory, jaccard, normalise_tokens
from route.router import Router
from route.storage import state_dir
from tests.conftest import MeanRNG


def remember(memory, identity, task, *, shape="coding:implement", tier="hard", arm="codex", score=1.0):
    memory.remember(identity, task, shape, tier, arm)
    if score is not None:
        memory.settle(identity, score)


def test_normalisation_and_jaccard():
    # Arrange
    left = "Build THE Parser, parser! ＡＰＩ"
    right = "build parser endpoint"

    # Act
    tokens = normalise_tokens(left)
    similarity = jaccard(tokens, normalise_tokens(right))

    # Assert
    assert tokens == {"build", "parser", "api"}
    assert similarity == 0.5
    assert jaccard(set(), set()) == 0


def test_neighbours_require_same_shape_threshold_and_settled_score():
    # Arrange
    memory = TaskMemory()
    remember(memory, "exact", "parser endpoint", score=0.7, tier="simple")
    remember(memory, "threshold", "parser lexer token type", score=0.4)
    remember(memory, "below", "parser lexer token type extra", score=0.0)
    remember(memory, "different", "parser", shape="coding:debug")
    remember(memory, "pending", "parser", score=None)
    remember(memory, "ineligible", "parser", arm="kimi")

    # Act
    neighbours = memory.neighbours("parser", "coding:implement", ["codex"])

    # Assert
    assert neighbours == [{"arm": "codex", "score": 0.7, "similarity": 0.5},
                          {"arm": "codex", "score": 0.4, "similarity": 0.25}]


def test_five_nearest_only_and_scores_are_updatable():
    # Arrange
    memory = TaskMemory()
    for i in range(7):
        remember(memory, str(i), "build parser endpoint " + " ".join(f"extra{n}" for n in range(i)))
    memory.settle("0", 0.4)

    # Act
    neighbours = memory.neighbours("build parser endpoint", "coding:implement", ["codex"])

    # Assert
    assert len(neighbours) == 5 and len(memory.rows()) == 7
    assert neighbours[0] == {"arm": "codex", "score": 0.4, "similarity": 1.0}
    assert [r["similarity"] for r in neighbours] == sorted([r["similarity"] for r in neighbours], reverse=True)


def test_memory_discards_oldest_rows_and_does_not_resurrect_them():
    # Arrange
    memory = TaskMemory(max_rows=3)
    for i in range(5):
        remember(memory, str(i), f"build parser {i}")

    # Act
    memory.settle("0", 0.0)

    # Assert
    assert [r["decision_id"] for r in memory.rows()] == ["2", "3", "4"]


def test_repeated_remember_preserves_settled_row():
    # Arrange
    memory = TaskMemory()
    remember(memory, "same", "build parser", score=0.7)

    # Act
    memory.remember("same", "build parser", "coding:implement", "hard", "codex")

    # Assert
    assert len(memory.rows()) == 1 and memory.rows()[0]["score"] == 0.7
    assert memory.path.stat().st_mode & 0o777 == 0o600


def test_simultaneous_first_readers_share_a_complete_salt(tmp_path):
    # Arrange
    directory = tmp_path / "new-install"

    # Act
    with ThreadPoolExecutor(max_workers=8) as executor:
        token_sets = list(executor.map(lambda _: TaskMemory(directory).tokens("parser"), range(16)))

    # Assert
    assert all(tokens == token_sets[0] for tokens in token_sets)
    assert len(TaskMemory(directory).salt_path.read_bytes()) == 32


def test_hashes_are_stable_per_install_but_differ_between_installs(tmp_path):
    # Arrange
    first, second = TaskMemory(tmp_path / "first"), TaskMemory(tmp_path / "second")

    # Act
    tokens = first.tokens("CustomerPrivateMarker")
    reloaded = TaskMemory(tmp_path / "first").tokens("customerprivatemarker")

    # Assert
    assert tokens == reloaded and tokens != second.tokens("CustomerPrivateMarker")
    assert all(re.fullmatch(r"[0-9a-f]{64}", token) for token in tokens)
    assert first.salt_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("flag", ["--private", "--sensitive", ""])
def test_cli_never_persists_task_text_even_for_open_tasks(capsys, flag):
    # Arrange
    marker = "CustomerPrivateMarker"
    args = ["--host", "claude", "--no-llm", "--no-quota", "--plan-only",
            "--shape", "coding:implement", "--tier", "hard"]

    # Act
    assert main([*args, *([flag] if flag else []), f"build the {marker} endpoint"]) == 0
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    assert main(["settle", "--decision", plan["decision_id"], "--fix-passes", "1"]) == 0

    # Assert
    for path in state_dir().iterdir():
        assert marker.casefold().encode() not in path.read_bytes().lower()
    rows = TaskMemory().rows()
    assert rows[0]["score"] == 0.7
    assert all(re.fullmatch(r"[0-9a-f]{64}", t) for t in rows[0]["tokens"])


def test_pseudo_observations_use_half_similarity_and_do_not_persist(router):
    # Arrange
    shape, tier = "coding:implement", "moderate"
    eligible = ["claude", "codex"]
    router.select(shape, tier, eligible, record_impression=False)
    prior = router.posterior(shape, tier)
    draws = []

    class RecordingRNG(MeanRNG):
        def betavariate(self, alpha, beta):
            draws.append((alpha, beta))
            return super().betavariate(alpha, beta)

    router._rng = RecordingRNG()
    neighbours = [{"arm": "codex", "score": 0.7, "similarity": 0.6},
                  {"arm": "kimi", "score": 1.0, "similarity": 1.0}]

    # Act
    router.select(shape, tier, eligible, record_impression=False, neighbours=neighbours)

    # Assert
    assert draws[0] == prior["claude"]
    assert draws[1] == pytest.approx((8.21, 2.09))
    assert router.posterior(shape, tier) == prior
    assert router.observed_counts(shape, tier) == {}


def test_neighbour_can_change_choice_for_one_decision_only(router):
    # Arrange
    shape, tier, eligible = "coding:implement", "moderate", ["claude", "codex"]
    neighbours = [{"arm": "codex", "score": 1.0, "similarity": 1.0}]

    # Act / Assert
    assert router.select(shape, tier, eligible, record_impression=False) == "claude"
    assert router.select(shape, tier, eligible, record_impression=False, neighbours=neighbours) == "codex"
    assert router.select(shape, tier, eligible, record_impression=False) == "claude"


def test_cli_plan_shows_neighbours_without_federating_them(monkeypatch, capsys):
    # Arrange
    monkeypatch.setattr("route.cli._build_router", lambda: Router(rng=MeanRNG()))
    task = "implement privateprojectmarker parser endpoint"
    args = ["--host", "claude", "--no-quota", "--no-llm", "--plan-only",
            "--shape", "coding:implement", "--tier", "moderate"]
    main([*args, "--pool", "codex", task])
    first = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    main(["settle", "--decision", first["decision_id"], "--fix-passes", "1"])
    fed = FederationState()
    fed.data["cells"]["coding:implement:moderate"] = {"contributors": 2, "arms": {}}
    fed.save()
    capsys.readouterr()
    main(["federate", "export"])
    before = json.loads(capsys.readouterr().out)

    # Act
    assert main([*args, task]) == 0
    plan = json.JSONDecoder().raw_decode(capsys.readouterr().out)[0]
    assert main(["federate", "export"]) == 0
    payload_text = capsys.readouterr().out
    after = json.loads(payload_text)

    # Assert: an actual, unsuppressed export still contains only real counts.
    assert plan["neighbours"] == [{"arm": "codex", "score": 0.7, "similarity": 1.0}]
    assert after == before
    assert len(after["records"]) == 1
    record = after["records"][0]
    assert set(record) == {"schema", "context", "arm", "model", "alpha", "beta"}
    assert (record["alpha"], record["beta"]) == pytest.approx((0.7, 0.3))
    assert "privateprojectmarker" not in payload_text
    assert "neighbours" not in payload_text and "tokens" not in payload_text
    assert first["decision_id"] not in payload_text
    assert TaskMemory().rows()[0]["tokens"][0] not in payload_text


def test_top_level_shape_cells_can_learn_and_export(router):
    # Arrange
    from route.router import parse_cell

    # Act
    router.observe("writing", "simple", "claude", ["claude"])
    router.reward("writing", "simple", "claude", by=0.7)

    # Assert
    assert parse_cell("route:writing:simple:accepted") == ("writing", "simple")
    assert router.observed_counts("writing", "simple")["claude"] == pytest.approx((0.7, 0.3))
