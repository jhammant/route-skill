"""Arm registry — config-driven so a new model is a config entry, not code.

Shipped arms: ``claude``, ``codex``, ``kimi``, ``deepseek``, ``local-batch``,
``local-agent``, ``free``. Users override or extend via ``~/.config/route/pools.toml``; each arm
declares the shapes it accepts, whether it needs a repo, whether it is remote,
its cost class, and its pinned model identity (model + quant + version — a
``qwen3.6-27b`` at 4-bit and at bf16 must never merge into one arm).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .shapes import SHAPES
from .storage import config_dir

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

#: Fallback order for escalation — later is stronger.
STRENGTH_ORDER: tuple[str, ...] = ("free", "local-batch", "local-agent", "kimi", "deepseek", "codex", "claude")

_CODING = tuple(s for s in SHAPES if s.startswith("coding:"))
_BATCH = tuple(s for s in SHAPES if s.startswith("batch:"))

#: free-llm-skill: a local OpenAI-compatible proxy pooling hosted free tiers.
FREE_LLM_ENDPOINT = "http://127.0.0.1:8080/v1"
FREE_LLM_HEALTH = "http://127.0.0.1:8080/healthz"

#: What ``codex exec`` runs when ``~/.codex/config.toml`` does not say.
#: GPT-6 Astra (released 2026-09-03) is the ChatGPT-subscription default
#: and leads on long-horizon terminal work (Terminal-Bench 4.0: 57.7 vs
#: Fable 5.1 at 55.8) while tying or trailing on single-shot coding.
CODEX_DEFAULT_MODEL = "gpt-6-astra"

#: The Claude Code skill wrapping DeepSeek's pay-as-you-go API — the
#: backstop arm: when Claude/Codex/Kimi quota is gone, work still has
#: somewhere to go, but it costs real money, so it never leads.
DEEPSEEK_SKILL = str(Path.home() / ".claude" / "skills" / "deepseek" / "scripts" / "deepseek")


def codex_model(config_path: str | Path | None = None) -> Model:
    """The model ``codex exec`` will actually use — read, not assumed.

    ``codex exec`` inherits ``model`` from ``$CODEX_HOME/config.toml``
    (``~/.codex`` by default). The arm's pinned identity must match what
    ran, or counts learned under one model silently merge into the next
    (a ``gpt-5.6-sol`` posterior is not evidence about ``gpt-6-astra``).
    Any read or parse failure falls back to ``CODEX_DEFAULT_MODEL``.
    """
    if config_path is None:
        home = os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
        config_path = Path(home) / "config.toml"
    try:
        raw = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Model(name=CODEX_DEFAULT_MODEL)
    name = raw.get("model")
    if not isinstance(name, str) or not name.strip():
        return Model(name=CODEX_DEFAULT_MODEL)
    return Model(name=name.strip())


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
    #: Separate command for ``batch:*`` shapes when one tool splits by shape
    #: family (deepseek: `code` for coding, `ask` for batch). The task goes
    #: on STDIN, never argv — a batch prompt can be far too long for argv.
    batch_dispatch: str = ""
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
    #: ``"<tool> <subcommand>"`` that must appear in ``<tool> --help`` for the
    #: arm to be eligible; a missing subcommand removes the arm.
    requires_subcommand: str = ""
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

    def dispatch_for(self, shape: str) -> tuple[str, bool]:
        """(command template, task-on-stdin) for a shape.

        Most arms have one command for everything; an arm with a
        ``batch_dispatch`` uses it for ``batch:*`` shapes instead, with the
        task piped on stdin rather than substituted into argv.
        """
        if shape.startswith("batch:") and self.batch_dispatch:
            return self.batch_dispatch, True
        return self.dispatch, False


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
            cost="included",  # subscription quota, not pay-as-you-go
            strength=strength("codex"),
            dispatch="codex exec {task}",
            probe="codex exec {task}",
            model=codex_model(),
            parallel_limit=4,
        ),
        "kimi": Pool(
            name="kimi",
            shapes=_CODING + ("research", "writing"),
            remote=True,
            cost="included",  # subscription quota, not pay-as-you-go
            strength=strength("kimi"),
            dispatch="kimi -p {task}",
            probe="kimi -p {task}",
            model=Model(name="kimi"),
            parallel_limit=30,  # /usages parallel.limit, ADVANCED
        ),
        "deepseek": Pool(
            name="deepseek",
            # The backstop: pay-as-you-go, so it must never beat a healthy
            # included pool — but when Claude/Codex/Kimi quota is gone, work
            # still lands here instead of failing. Coding on v4-pro (strong
            # reasoning/coding), batch on flash (fast and cheap).
            shapes=_CODING + _BATCH,
            remote=True,
            cost="paid",
            strength=strength("deepseek"),
            dispatch=f"{DEEPSEEK_SKILL} code --dir . --model deepseek-v4-pro {{task}}",
            batch_dispatch=f"{DEEPSEEK_SKILL} ask - --model deepseek-flash",
            model=Model(name="deepseek", version="v4-pro+flash"),
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
            requires_subcommand="local-llm agent",
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
            batch_dispatch=str(spec.get("batch_dispatch", "")),
            probe=str(spec.get("probe", "")),
            endpoint=str(spec.get("endpoint", "")),
            health_url=str(spec.get("health_url", "")),
            requires_subcommand=str(spec.get("requires_subcommand", "")),
            model=Model(
                name=str(model_spec.get("name", "unknown")),
                quant=str(model_spec.get("quant", "")),
                version=str(model_spec.get("version", "")),
            ),
            parallel_limit=int(spec.get("parallel_limit", 1)),
        )
    return pools


def pools_for_host(pools: dict[str, Pool], host: str) -> dict[str, Pool]:
    """Keep provider identities and learned counts; change only the local host role."""
    if host not in ("claude", "codex") or host not in pools:
        raise ValueError(f"unsupported host: {host}")
    result = dict(pools)
    result[host] = replace(result[host], shapes=SHAPES, dispatch="", needs_repo=False)
    if host == "codex" and "claude" in result and not result["claude"].dispatch:
        result["claude"] = replace(result["claude"],
            dispatch="claude --print --permission-mode auto {task}")
    return result
