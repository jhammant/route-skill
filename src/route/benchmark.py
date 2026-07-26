"""route benchmark — manufacture evidence instead of waiting for it.

The bandit only learns from work that actually happens; benchmark generates
evidence on demand for rarely-used cells, for exploration, and for a newly
added pool that would otherwise sit on a guessed prior.

Every suite is MACHINE-CHECKABLE — an LLM judge would import its own bias
into the very numbers used to decide routing, so quality here is measured by
execution, not opinion:

    throughput  tok/s single + concurrent (mechanical — no ground truth)
    instruct    constrained-output adherence: answers ONLY from a permitted
                set (exact set membership — a real 4-bit run substituted its
                own taxonomy 2.3% of the time, and nobody measures this)
    classify    accuracy on a labelled set (held-out labels)
    code        make a failing test pass in a scratch repo (suite exits 0)

Distribution shift is the reason this needs care: benchmark tasks are not
drawn from the user's real work, so benchmark outcomes are recorded in a
SEPARATE namespace (``bench:{shape}:{tier}``), never mixed into the live
``route:{shape}:{tier}`` cell. ``--seed`` merges them into live priors at
0.3x — below the 0.4x community weight, because synthetic evidence is worth
less than a stranger's real evidence. ``route stats`` shows real vs
benchmark counts in separate columns.
"""

from __future__ import annotations

import json
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .pools import Pool
from .router import IMPRESSION_EVENT, REWARD_EVENT
from .stats import Stats, Throughput
from .storage import JsonStorage, state_dir

#: The separate namespace. Benchmark counts NEVER touch ``route:`` cells.
BENCH_PREFIX = "bench"

#: --seed merges benchmark counts into live priors at this weight — below
#: the 0.4x community discount. One real observation always outranks a
#: benchmark run of the same size.
BENCHMARK_DISCOUNT = 0.3

SUITES: tuple[str, ...] = ("throughput", "instruct", "classify", "code")

#: A runner answers one prompt on one arm and returns raw model output.
#: Tests inject fakes; the default shells out through the pool's dispatch.
Runner = Callable[[str, str], str]


@dataclass
class BenchResult:
    suite: str
    arm: str
    shape: str = ""
    tier: str = ""
    total: int = 0
    passed: int = 0
    detail: dict = field(default_factory=dict)
    error: str = ""


# -- suite task sets -----------------------------------------------------------

def _instruct_prompt(item: str, permitted: Iterable[str]) -> str:
    choices = ", ".join(sorted(permitted))
    return (
        f"Classify: {item!r}\n"
        f"Reply with exactly one word, chosen only from: {choices}. "
        "No other output."
    )


_SENTIMENT = ("positive", "negative", "neutral")
_TICKET = ("billing", "technical", "feature")

#: Constrained-output adherence: a fixed permitted answer set per task.
INSTRUCT_TASKS: tuple[dict, ...] = tuple(
    {"prompt": _instruct_prompt(item, permitted), "permitted": permitted}
    for item, permitted in (
        ("The update completely broke my workflow", _SENTIMENT),
        ("Fast, friendly support — resolved in minutes", _SENTIMENT),
        ("It does what it says, nothing more", _SENTIMENT),
        ("Login page returns a 500 error after deploy", _TICKET),
        ("Please add a dark mode to the settings page", _TICKET),
        ("My invoice shows the wrong total again", _TICKET),
    )
)

#: Held-out labels for the classify suite.
CLASSIFY_TASKS: tuple[dict, ...] = tuple(
    {"prompt": _instruct_prompt(item, labels), "label": label}
    for item, labels, label in (
        ("app crashes on startup since the last update", ("bug", "feature", "question"), "bug"),
        ("can you add CSV export to the reports page?", ("bug", "feature", "question"), "feature"),
        ("how do I reset my password?", ("bug", "feature", "question"), "question"),
        ("invoice shows the wrong total", ("bug", "billing", "feature"), "billing"),
        ("payment was taken twice this month", ("bug", "billing", "feature"), "billing"),
    )
)

#: Failing tests the arm must make pass in a scratch repo.
CODE_TASKS: tuple[dict, ...] = (
    {
        "filename": "solution.py",
        "prompt": (
            "In a file solution.py, implement add(a, b) returning a + b. "
            "Reply with the Python code only."
        ),
        "test_src": (
            "from solution import add\n\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n"
            "    assert add(-1, 1) == 0\n"
        ),
    },
    {
        "filename": "solution.py",
        "prompt": (
            "In a file solution.py, implement reverse_text(s) returning the "
            "string s reversed. Reply with the Python code only."
        ),
        "test_src": (
            "from solution import reverse_text\n\n\n"
            "def test_reverse():\n"
            "    assert reverse_text('abc') == 'cba'\n"
            "    assert reverse_text('') == ''\n"
        ),
    },
)

THROUGHPUT_PROMPT = "Write a 200-word summary of the water cycle."

#: Bench observations land on fixed cells of the closed vocabulary.
_SUITE_CELL: dict[str, tuple[str, str]] = {
    "instruct": ("batch:classify", "simple"),
    "classify": ("batch:classify", "simple"),
    "code": ("coding:implement", "moderate"),
}


# -- the default runner --------------------------------------------------------


def shell_runner(pools: dict[str, Pool], timeout: float = 120.0) -> Runner:
    """Shell out through the pool's dispatch template; stdout is the answer."""

    def run(arm: str, prompt: str) -> str:
        pool = pools[arm]
        if not pool.dispatch:
            raise RuntimeError(f"pool {arm!r} has no dispatch command")
        argv = [prompt if tok == "{task}" else tok for tok in shlex.split(pool.dispatch)]
        if "{task}" not in shlex.split(pool.dispatch):
            argv.append(prompt)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"dispatch exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return proc.stdout

    return run


# -- suite scorers -------------------------------------------------------------


def _first_word(output: str) -> str:
    match = re.search(r"[a-z0-9_-]+", output.lower())
    return match.group(0) if match else ""


def _run_instruct(arm: str, runner: Runner) -> BenchResult:
    result = BenchResult(suite="instruct", arm=arm, shape=_SUITE_CELL["instruct"][0],
                         tier=_SUITE_CELL["instruct"][1])
    for task in INSTRUCT_TASKS:
        answer = _first_word(runner(arm, task["prompt"]))
        ok = answer in task["permitted"]
        result.total += 1
        result.passed += int(ok)
        result.detail.setdefault("answers", []).append({"answer": answer, "adherent": ok})
    return result


def _run_classify(arm: str, runner: Runner) -> BenchResult:
    result = BenchResult(suite="classify", arm=arm, shape=_SUITE_CELL["classify"][0],
                         tier=_SUITE_CELL["classify"][1])
    for task in CLASSIFY_TASKS:
        answer = _first_word(runner(arm, task["prompt"]))
        result.total += 1
        result.passed += int(answer == task["label"])
    return result


def _extract_code(output: str) -> str:
    fenced = re.search(r"```(?:python)?\s*\n(.*?)```", output, re.DOTALL)
    return fenced.group(1) if fenced else output


def _run_code(arm: str, runner: Runner, timeout: float = 60.0) -> BenchResult:
    result = BenchResult(suite="code", arm=arm, shape=_SUITE_CELL["code"][0],
                         tier=_SUITE_CELL["code"][1])
    for task in CODE_TASKS:
        code = _extract_code(runner(arm, task["prompt"]))
        result.total += 1
        with tempfile.TemporaryDirectory(prefix="route-bench-") as scratch:
            root = Path(scratch)
            (root / task["filename"]).write_text(code, encoding="utf-8")
            (root / "test_solution.py").write_text(task["test_src"], encoding="utf-8")
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-x"],
                    cwd=root, capture_output=True, text=True, timeout=timeout,
                )
                result.passed += int(proc.returncode == 0)
            except (OSError, subprocess.TimeoutExpired):
                pass  # counts as a failed task
    return result


def _hardware() -> str:
    return f"{platform.system().lower()}-{platform.machine()}"


def _estimate_tokens(output: str) -> int:
    return max(len(output) // 4, 1)


def _run_throughput(arm: str, pool: Pool, runner: Runner) -> tuple[BenchResult, Throughput]:
    started = time.monotonic()
    out = runner(arm, THROUGHPUT_PROMPT)
    single_s = max(time.monotonic() - started, 1e-6)
    tok_s = _estimate_tokens(out) / single_s

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool_exec:
        outputs = list(pool_exec.map(lambda _: runner(arm, THROUGHPUT_PROMPT), range(2)))
    concurrent_s = max(time.monotonic() - started, 1e-6)
    concurrent_tok_s = sum(_estimate_tokens(o) for o in outputs) / concurrent_s

    sample = Throughput(
        model=pool.model.name,
        quant=pool.model.quant,
        context_length=0,
        hardware=_hardware(),
        tok_s=round(tok_s, 2),
        load_s=0.0,
        peak_gb=0.0,
    )
    result = BenchResult(
        suite="throughput", arm=arm,
        detail={"tok_s": sample.tok_s, "concurrent_tok_s": round(concurrent_tok_s, 2)},
    )
    return result, sample


# -- top-level run + recording --------------------------------------------------


def bench_cell(shape: str, tier: str) -> str:
    """The SEPARATE namespace — never a live ``route:`` cell."""
    return f"{BENCH_PREFIX}:{shape}:{tier}"


def parse_bench_key(key: str) -> tuple[str, str, str] | None:
    """'bench:{shape}:{tier}:{event}' -> (shape, tier, event)."""
    parts = key.split(":")
    if len(parts) < 4 or parts[0] != BENCH_PREFIX:
        return None
    return ":".join(parts[1:-2]), parts[-2], parts[-1]


def local_llm_throughput_path() -> Path:
    """Where local-llm plan looks for measured throughput on this machine."""
    return Path.home() / ".local" / "state" / "local-llm" / "throughput.json"


def publish_throughput(sample: Throughput, path: Path) -> None:
    """Feed the rest of the stack: make local-llm plan accurate on this model.

    (model, quant, hardware) -> tok/s contains no task content at all — the
    zero-privacy-risk federated dataset.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            rows = []
    except (OSError, ValueError):
        rows = []
    rows.append({
        "model": sample.model, "quant": sample.quant,
        "context_length": sample.context_length, "hardware": sample.hardware,
        "tok_s": sample.tok_s, "items_s": sample.items_s,
        "load_s": sample.load_s, "peak_gb": sample.peak_gb,
    })
    path.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")


def run_benchmark(
    arms: Iterable[str],
    suites: Iterable[str],
    pools: dict[str, Pool],
    *,
    storage: JsonStorage | None = None,
    stats: Stats | None = None,
    runner: Runner | None = None,
    local_llm_path: Path | None = None,
) -> list[BenchResult]:
    """Run machine-checkable suites and record into the bench namespace.

    A dispatch failure (missing binary, non-zero exit) is recorded as an
    error on the result and NOT as failed evidence — we never manufacture
    negative counts from a task that never ran.
    """
    storage = storage if storage is not None else JsonStorage()
    runner = runner if runner is not None else shell_runner(pools)
    results: list[BenchResult] = []
    for arm in arms:
        if arm not in pools:
            results.append(BenchResult(suite="", arm=arm, error=f"unknown arm {arm!r}"))
            continue
        for suite in suites:
            try:
                if suite == "throughput":
                    result, sample = _run_throughput(arm, pools[arm], runner)
                    (stats or Stats()).record_throughput(sample)
                    if local_llm_path is not None:
                        publish_throughput(sample, local_llm_path)
                elif suite == "instruct":
                    result = _run_instruct(arm, runner)
                elif suite == "classify":
                    result = _run_classify(arm, runner)
                elif suite == "code":
                    result = _run_code(arm, runner)
                else:
                    result = BenchResult(suite=suite, arm=arm, error=f"unknown suite {suite!r}")
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                result = BenchResult(suite=suite, arm=arm, error=str(exc))
            if result.total:
                record_result(storage, result)
            results.append(result)
    return results


def record_result(storage: JsonStorage, result: BenchResult) -> None:
    """One Bernoulli observation per suite task, in the bench namespace."""
    cell = bench_cell(result.shape, result.tier)
    storage.incr(f"{cell}:{IMPRESSION_EVENT}", result.arm, result.total)
    if result.passed:
        storage.incr(f"{cell}:{REWARD_EVENT}", result.arm, result.passed)


def bench_counts(storage: JsonStorage) -> dict[str, dict[str, tuple[float, float]]]:
    """{context: {arm: (alpha, beta)}} of benchmark evidence, priors NOT included."""
    cells: dict[tuple[str, str], set[str]] = {}
    for key in storage.keys(f"{BENCH_PREFIX}:"):
        parsed = parse_bench_key(key)
        if parsed is not None:
            shape, tier, _event = parsed
            cells.setdefault((shape, tier), set())
    out: dict[str, dict[str, tuple[float, float]]] = {}
    for shape, tier in sorted(cells):
        cell = bench_cell(shape, tier)
        impressions = storage.counts(f"{cell}:{IMPRESSION_EVENT}")
        rewards = storage.counts(f"{cell}:{REWARD_EVENT}")
        arms: dict[str, tuple[float, float]] = {}
        for arm in impressions.keys() | rewards.keys():
            won = rewards.get(arm, 0)
            lost = max(impressions.get(arm, 0) - won, 0)
            if won or lost:
                arms[arm] = (won, lost)
        if arms:
            out[f"{shape}:{tier}"] = arms
    return out


# -- seeding live priors ---------------------------------------------------------


def _seed_flag_path() -> Path:
    return state_dir() / "benchmark-seed.json"


def enable_seed() -> None:
    """Opt in to merging benchmark evidence into live priors at 0.3x."""
    path = _seed_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": True, "discount": BENCHMARK_DISCOUNT}),
                    encoding="utf-8")


def seed_enabled() -> bool:
    try:
        return bool(json.loads(_seed_flag_path().read_text(encoding="utf-8")).get("enabled"))
    except (OSError, ValueError):
        return False


def seed_priors(storage: JsonStorage, *, discount: float = BENCHMARK_DISCOUNT) -> dict[str, dict[str, tuple[float, float]]]:
    """Benchmark counts discounted to prior strength; {} unless --seed was used.

    Fed into the Router exactly like pulled community priors: they become
    seeded pseudo-counts (subtracted again by ``observed_counts``), so
    federation never re-exports manufactured evidence as real.
    """
    if not seed_enabled():
        return {}
    return {
        context: {arm: (alpha * discount, beta * discount) for arm, (alpha, beta) in arms.items()}
        for context, arms in bench_counts(storage).items()
    }
