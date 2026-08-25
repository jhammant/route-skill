"""SPEC 'route benchmark': separate namespace, 0.3x seed, instruct adherence.

Deterministic: scripted runners, no network, no real dispatch, no money.
"""

from __future__ import annotations

import pytest
from conftest import MeanRNG

from route.benchmark import (
    BENCHMARK_DISCOUNT,
    bench_counts,
    enable_seed,
    run_benchmark,
    selectable_arms,
    seed_enabled,
    seed_priors,
)
from route.federate import COMMUNITY_DISCOUNT
from route.pools import Pool
from route.router import Router
from route.stats import report_cells


class ScriptedRunner:
    """Answers prompts from a script; falls back to a default answer."""

    def __init__(self, outputs=(), default=""):
        self.outputs = list(outputs)
        self.default = default

    def __call__(self, arm, prompt):
        return self.outputs.pop(0) if self.outputs else self.default


def test_benchmark_outcomes_stay_out_of_live_cells(router, storage, pools):
    """Benchmark evidence goes to bench:{shape}:{tier}, never the live cell."""
    results = run_benchmark(["local-batch"], ["instruct", "classify"], pools,
                            storage=storage, runner=ScriptedRunner(default="bug"))
    assert results and all(r.total > 0 for r in results)

    assert storage.keys("bench:")  # the separate namespace has the counts
    assert not storage.keys("route:")  # and NOTHING leaked into live cells
    assert router.observed_counts("batch:classify", "simple") == {}
    assert router.posterior("batch:classify", "simple") == {}
    assert "batch:classify:simple" in bench_counts(storage)


def test_instruct_suite_scores_permitted_set_adherence(storage, pools):
    """Constrained-output adherence: answers only from the permitted set."""
    # Three sentiment tasks (positive/negative/neutral), then three ticket
    # tasks (billing/technical/feature) — every answer from the permitted set.
    runner = ScriptedRunner(["negative", "neutral", "positive",
                             "billing", "technical", "feature"])
    (result,) = run_benchmark(["local-batch"], ["instruct"], pools,
                              storage=storage, runner=runner)
    assert result.passed == result.total == 6
    assert all(a["adherent"] for a in result.detail["answers"])


def test_instruct_counts_substituted_categories_as_failures(storage, pools):
    """The 2.3% failure mode: a model substituting its OWN taxonomy scores 0."""
    (result,) = run_benchmark(["local-batch"], ["instruct"], pools,
                              storage=storage, runner=ScriptedRunner(default="content"))
    assert result.passed == 0  # 'content' is the model's category, not ours
    assert result.total == 6


def test_code_suite_passes_only_when_the_scratch_tests_pass(storage, pools):
    good = ScriptedRunner([
        "def add(a, b):\n    return a + b\n",
        "```python\ndef reverse_text(s):\n    return s[::-1]\n```",
    ])
    (result,) = run_benchmark(["local-agent"], ["code"], pools,
                              storage=storage, runner=good)
    assert result.passed == result.total == 2

    bad = ScriptedRunner(default="pass\n")
    (result,) = run_benchmark(["local-agent"], ["code"], pools,
                              storage=storage, runner=bad)
    assert result.passed == 0
    assert result.total == 2


def test_throughput_records_mechanical_stats(storage, pools, tmp_path):
    from route.stats import Stats

    stats = Stats(directory=tmp_path)
    text = "word " * 200  # ~250 tokens at the 4-chars-per-token estimate
    (result,) = run_benchmark(["local-batch"], ["throughput"], pools,
                              storage=storage, stats=stats,
                              runner=ScriptedRunner(default=text))
    assert result.detail["tok_s"] > 0
    assert result.detail["concurrent_tok_s"] > 0
    rows = stats.throughput()
    assert len(rows) == 1
    assert rows[0]["model"] == "qwen3.6-27b"
    assert rows[0]["tok_s"] > 0
    # Throughput is mechanical, not Bernoulli — no bench cell for it.
    assert bench_counts(storage) == {}


def test_seed_merges_benchmark_evidence_at_03_never_full_weight(storage, priors, pools):
    """CRITICAL: --seed inflates a live cell at 0.3x, below community's 0.4x."""
    assert BENCHMARK_DISCOUNT == 0.3 < COMMUNITY_DISCOUNT == 0.4

    (result,) = run_benchmark(["local-batch"], ["instruct"], pools,
                              storage=storage, runner=ScriptedRunner(default="negative"))
    assert not seed_enabled()  # off until explicitly asked
    enable_seed()
    assert seed_enabled()

    seeded = seed_priors(storage)
    context = "batch:classify:simple"
    alpha, beta = seeded[context]["local-batch"]
    assert alpha == pytest.approx(result.passed * 0.3)
    assert alpha + beta == pytest.approx(result.total * 0.3)

    live = Router(storage=storage, priors=priors, community=seeded, rng=MeanRNG())
    live.select("batch:classify", "simple", ["claude", "local-batch"],
                record_impression=False)
    pa, pb = live.posterior("batch:classify", "simple")["local-batch"]
    favoured = "local-batch" in priors.favoured_arms("batch:classify")
    rule_a, rule_b = (8, 2) if favoured else (2, 8)
    # Exactly the 0.3x weight on top of the rule prior — never full weight.
    assert pa - rule_a == pytest.approx(result.passed * 0.3)
    assert pb - rule_b == pytest.approx((result.total - result.passed) * 0.3)
    assert pa - rule_a < result.passed
    # And it is prior strength, not real evidence: export-worthy counts stay 0.
    assert live.observed_counts("batch:classify", "simple") == {}


def test_stats_reports_real_and_benchmark_counts_in_separate_columns(
    router, storage, pools,
):
    shape, tier = "batch:classify", "simple"
    router.select(shape, tier, ["claude", "local-batch"], record_impression=False)
    router.reward(shape, tier, "local-batch", "accepted")
    (result,) = run_benchmark(["local-batch"], ["instruct"], pools,
                              storage=storage, runner=ScriptedRunner(default="negative"))

    rows = report_cells(router)
    row = next(r for r in rows if r["arm"] == "local-batch")
    assert row["real_obs"] == 1              # one REAL observation...
    assert row["bench_obs"] == result.total  # ...benchmark in its OWN column
    assert row["obs"] == 1                   # benchmark never inflates real


def test_cli_benchmark_failed_dispatch_records_error_not_evidence(tmp_path, capsys):
    """A dispatch that never ran manufactures no counts, negative or otherwise."""
    import json

    from route.cli import main
    from route.storage import JsonStorage

    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "pools.toml").write_text(
        '[pools.fake]\nshapes = ["batch:classify"]\n'
        'dispatch = "definitely-not-a-real-binary-xyz {task}"\n'
    )
    assert main(["benchmark", "--arm", "fake", "--suite", "instruct"]) == 0
    out = json.loads(capsys.readouterr().out)
    (result,) = out["results"]
    assert result["error"]
    assert result["total"] == 0
    assert bench_counts(JsonStorage()) == {}  # ROUTE_STATE_DIR is isolated


def test_implicit_benchmark_skips_arms_without_shapes_or_commands(monkeypatch, capsys):
    """The everything sweep only asks arms able to produce benchmark evidence."""
    import json

    from route import cli
    from route.storage import JsonStorage

    pools = {
        "usable": Pool(
            name="usable",
            shapes=("batch:classify",),
            probe="printf usable",
        ),
        "dispatch-only": Pool(
            name="dispatch-only",
            shapes=("batch:classify",),
            dispatch="printf dispatch-only",
        ),
        "no-shapes": Pool(
            name="no-shapes",
            shapes=(),
            probe="printf unavailable",
        ),
        "no-command": Pool(
            name="no-command",
            shapes=("batch:classify",),
        ),
        "neither": Pool(name="neither", shapes=()),
    }
    monkeypatch.setattr(cli, "load_pools", lambda: pools)

    assert selectable_arms(pools) == ["usable", "dispatch-only"]
    assert cli.main(["benchmark", "--suite", "instruct"]) == 0
    output = json.loads(capsys.readouterr().out)
    results = output["results"]

    assert [result["arm"] for result in results] == ["usable", "dispatch-only"]
    assert all(not result["error"] for result in results)
    counts = bench_counts(JsonStorage())
    assert set(counts["batch:classify:simple"]) == {"usable", "dispatch-only"}

    assert cli.main(["benchmark", "--arm", "no-shapes", "--suite", "instruct"]) == 2
    assert "cannot be benchmarked" in capsys.readouterr().err
