"""Arm registry — config-driven so a new model is a config entry, not code.

Shipped arms: ``claude``, ``codex``, ``kimi``, ``local-batch``, ``local-agent``,
``free``. Users override or extend via ``~/.config/route/pools.toml``; each arm
declares the shapes it accepts, whether it needs a repo, whether it is remote,
its cost class, and its pinned model identity (model + quant + version — a
``qwen3.6-27b`` at 4-bit and at bf16 must never merge into one arm).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .shapes import SHAPES
from .storage import config_dir

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

#: Fallback order for escalation — later is stronger.
STRENGTH_ORDER: tuple[str, ...] = ("free", "local-batch", "local-agent", "kimi", "codex", "claude")

_CODING = tuple(s for s in SHAPES if s.startswith("coding:"))
_BATCH = tuple(s for s in SHAPES if s.startswith("batch:"))

#: free-llm-skill: a local OpenAI-compatible proxy pooling hosted free tiers.
FREE_LLM_ENDPOINT = "http://127.0.0.1:8080/v1"
FREE_LLM_HEALTH = "http://127.0.0.1:8080/healthz"


@dataclass(frozen=True)
class Model:
    """Pinned model identity: name + quant + version, never just a name."""

    name: str
    quant: str = ""
    version: str = ""

    def ident(self) -> str:
        parts = [self.name]
        if self.quant:
            parts.append(self.quant)
        if self.version:
            parts.append(self.version)
        ident = "@".join(parts)
        # Federation-safe: no whitespace, no path separators, ever.
        return re.sub(r"[\s/\\]+", "-", ident)


@dataclass(frozen=True)
class Pool:
    name: str
    shapes: tuple[str, ...]
    needs_repo: bool = False
    remote: bool = False
    cost: str = "included"  # included | paid | local | free
    strength: int = 0
    dispatch: str = ""  # command template for REAL work; {task} is substituted
    #: Single-prompt command used by `route benchmark`. Distinct from
    #: dispatch because real-work commands often take a file and a template
    #: (local-llm batch) and cannot answer a bare prompt. Empty means the
    #: pool cannot be benchmarked by shelling out.
    probe: str = ""
    #: OpenAI-compatible base URL for a local server (LM Studio, Ollama,
    #: free-llm). When set, ``--endpoint <value>`` is appended to dispatch
    #: and probe, so one tool can back several arms (local-batch-lmstudio
    #: vs local-batch-ollama) and the bandit learns which backend wins.
    endpoint: str = ""
    #: Health URL probed before the arm is eligible (the free-llm proxy's
    #: /healthz). Empty means always considered; unreachable means simply
    #: not eligible, never an error.
    health_url: str = ""
    model: Model = field(default_factory=lambda: Model(name="unknown"))
    #: Server-authoritative fan-out limit (Kimi's /usages parallel.limit is
    #: 30 on ADVANCED). A swarm is a property of DISPATCH, never a separate
    #: arm — the bandit picks the pool, the concurrency planner picks n.
    parallel_limit: int = 1

    def __post_init__(self) -> None:
        if self.endpoint:
            suffix = f" --endpoint {self.endpoint}"
            if self.dispatch:
                object.__setattr__(self, "dispatch", self.dispatch + suffix)
            if self.probe:
                object.__setattr__(self, "probe", self.probe + suffix)

    def accepts(self, shape: str) -> bool:
        return shape in self.shapes


def _default_pools() -> dict[str, Pool]:
    def strength(name: str) -> int:
        return STRENGTH_ORDER.index(name)

    return {
        "claude": Pool(
            name="claude",
            shapes=SHAPES,
            remote=True,
            cost="included",
            strength=strength("claude"),
            dispatch="",  # "stay" — the task is already here
            model=Model(name="claude"),
        ),
        "codex": Pool(
            name="codex",
            shapes=_CODING,
            needs_repo=True,
            remote=True,
            cost="paid",
            strength=strength("codex"),
            dispatch="codex exec {task}",
            probe="codex exec {task}",
            model=Model(name="gpt-5.6-sol"),
            parallel_limit=4,
        ),
        "kimi": Pool(
            name="kimi",
            shapes=_CODING + ("research", "writing"),
            remote=True,
            cost="paid",
            strength=strength("kimi"),
            dispatch="kimi -p {task}",
            probe="kimi -p {task}",
            model=Model(name="kimi"),
            parallel_limit=30,  # /usages parallel.limit, ADVANCED
        ),
        "local-batch": Pool(
            name="local-batch",
            shapes=_BATCH,
            remote=False,
            cost="local",
            strength=strength("local-batch"),
            dispatch="local-llm batch {task}",
            probe="local-llm ask {task}",
            model=Model(name="qwen3.6-27b", quant="4bit"),
            parallel_limit=2,
        ),
        "local-agent": Pool(
            name="local-agent",
            shapes=_CODING + ("research", "writing"),
            remote=False,
            cost="local",
            strength=strength("local-agent"),
            dispatch="local-llm agent {task}",
            probe="local-llm ask {task}",
            model=Model(name="qwen3.6-27b", quant="4bit"),
            parallel_limit=2,
        ),
        "free": Pool(
            name="free",
            # Batch work primarily, plus the simplest coding shapes. Free-tier
            # models are materially weaker: never refactor, debug, or
            # orchestration.
            shapes=_BATCH + ("coding:test", "coding:review"),
            # REMOTE: the proxy is local but the providers are third parties,
            # and several free tiers train on submitted prompts.
            remote=True,
            cost="free",
            strength=strength("free"),
            dispatch="local-llm batch {task}",
            probe="local-llm ask {task}",
            endpoint=FREE_LLM_ENDPOINT,
            health_url=FREE_LLM_HEALTH,
            model=Model(name="free-llm-pool"),
            # The proxy self-limits to provider rate limits — do not stack
            # fan-out on top of it.
            parallel_limit=4,
        ),
    }


def load_pools(path: str | Path | None = None) -> dict[str, Pool]:
    """Shipped arms, overlaid with the user's pools.toml if present."""
    pools = _default_pools()
    path = Path(path) if path is not None else config_dir() / "pools.toml"
    if not path.is_file():
        return pools
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    for name, spec in (raw.get("pools") or {}).items():
        shapes = tuple(spec.get("shapes", ()))
        unknown = [s for s in shapes if s not in SHAPES]
        if unknown:
            raise ValueError(f"pools.toml: {name} declares unknown shapes {unknown}")
        model_spec = spec.get("model") or {}
        pools[name] = Pool(
            name=name,
            shapes=shapes,
            needs_repo=bool(spec.get("needs_repo", False)),
            remote=bool(spec.get("remote", False)),
            cost=str(spec.get("cost", "included")),
            strength=int(spec.get("strength", 0)),
            dispatch=str(spec.get("dispatch", "")),
            probe=str(spec.get("probe", "")),
            endpoint=str(spec.get("endpoint", "")),
            health_url=str(spec.get("health_url", "")),
            model=Model(
                name=str(model_spec.get("name", "unknown")),
                quant=str(model_spec.get("quant", "")),
                version=str(model_spec.get("version", "")),
            ),
            parallel_limit=int(spec.get("parallel_limit", 1)),
        )
    return pools
