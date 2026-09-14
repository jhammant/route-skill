"""Vetoes (step 2) and quota gating (steps 3-4).

Vetoes are hard gates, not scores: any veto short-circuits to ``claude``
regardless of quota or complexity. Quota gates *availability* — an arm whose
pool reports ``critical`` headroom is REMOVED from eligibility, never merely
penalised. The bandit decides quality among what survives.

Sensitive data (SPEC-ship section 1) is also an eligibility-stage veto, but
a different shape: remote third-party pools (``codex``, ``kimi``, ``free``,
``deepseek``) are removed, while ``claude`` AND the local arms survive —
local is the only arm where sensitive data is safe by construction, and it is
PREFERRED at selection. If nothing survives, ``gate`` raises rather than
falling back to a remote arm.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace

from .pools import DEEPSEEK_SKILL, Pool
from .shapes import FALLBACK_SHAPE

#: The pool every veto short-circuits to.
VETO_TARGET = "claude"

#: dataClass values: ``open`` (default) or ``sensitive`` (SPEC-ship section 1).
DATA_OPEN = "open"
DATA_SENSITIVE = "sensitive"


class SensitiveRoutingError(Exception):
    """A sensitive task has no eligible arm.

    Raised instead of falling back: silently routing sensitive work to a
    remote arm is the exact failure the flag exists to prevent.
    """


@dataclass(frozen=True)
class Veto:
    name: str
    reason: str
    target: str = VETO_TARGET


_NEEDS_CONTEXT_RE = re.compile(
    r"\b(this (conversation|chat|thread)|as (we )?discussed|the above|"
    r"what we were (doing|working on)|earlier in this)\b",
    re.IGNORECASE,
)
_CROSS_REPO_RE = re.compile(
    r"\b(cross[- ]repo|multi[- ]repo|across (repos|repositories|directories|projects)|"
    r"multiple (repos|repositories|directories)|several (repos|directories))\b",
    re.IGNORECASE,
)
_PRIVATE_RE = re.compile(
    r"\b(private data|secrets?|credentials?|passwords?|\.env\b|api keys?|"
    r"personal data|pii|customer data|medical|financial records)\b",
    re.IGNORECASE,
)
_NO_ACCEPTANCE_RE = re.compile(
    r"\b(no (clear )?(acceptance|way to verify)|can'?t (be )?verif|unverifiable)\b",
    re.IGNORECASE,
)


def probably_private(text: str) -> bool:
    """Content safety net. A hit means "treat as sensitive"; a miss proves nothing.

    Only ever used to increase caution — never to clear a task for a remote arm.
    """
    return bool(_PRIVATE_RE.search(text or ""))


def detect_veto(
    text: str,
    *,
    shape: str | None = None,
    pool_pin: str | None = None,
    private: bool = False,
    no_acceptance: bool = False,
    needs_context: bool = False,
    cross_repo: bool = False,
    skip_private: bool = False,
) -> Veto | None:
    """Evaluate hard gates before anything else. First veto wins.

    Explicit flags beat inferred cues; a user pool pin beats everything.
    ``skip_private`` suppresses the private-data safety net when the caller
    handles sensitivity itself (the sensitive dataClass), so later vetoes —
    e.g. orchestration — still surface.
    """
    if pool_pin:
        return Veto("user-pin", f"user pinned pool '{pool_pin}'", target=pool_pin)
    if needs_context or _NEEDS_CONTEXT_RE.search(text):
        return Veto("needs-context", "needs this conversation's context — no other pool has it")
    if cross_repo or _CROSS_REPO_RE.search(text):
        return Veto("cross-repo", "cross-repo / multi-directory orchestration — remote pools are dir-sandboxed")
    if not skip_private and (private or _PRIVATE_RE.search(text)):
        return Veto("private-data", "private data must not leave the machine")
    if no_acceptance or _NO_ACCEPTANCE_RE.search(text):
        return Veto("no-acceptance", "no clear acceptance check — can't verify a hand-off")
    if shape == FALLBACK_SHAPE:
        return Veto("orchestration", "orchestration never routes away")
    return None


def probe_health(url: str, timeout: float = 1.0) -> bool:
    """GET a health endpoint with a short timeout. True only on a 2xx.

    Fail-safe by contract: any error — connection refused, timeout, DNS,
    malformed URL — means unreachable, and unreachable is never an error.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except (OSError, ValueError):
        return False


def eligible_for(shape: str, pools: dict[str, Pool]) -> list[str]:
    """Step 3: arms that accept this shape at all, registry order.

    An arm with a ``health_url`` (the free-llm proxy) is eligible only while
    the endpoint answers — an unreachable proxy simply removes the arm.
    """
    out = []
    for name, pool in pools.items():
        if not pool.accepts(shape):
            continue
        if pool.health_url and not probe_health(pool.health_url):
            continue
        out.append(name)
    return out


def parse_quota(payload: dict | list) -> dict[str, str]:
    """Normalise quotamax JSON to {pool: headroom}.

    Accepts {"codex": "ok"}, {"codex": {"headroom": "ok"}}, or either nested
    under a "pools" key. Headroom values: ok | low | critical.
    """
    if isinstance(payload, list):
        out = {}
        for provider in payload:
            if not isinstance(provider, dict) or not provider.get('ok'):
                continue
            percents = []
            for limit in provider.get('limits', []):
                reset = limit.get('resetsAt')
                try:
                    if reset and datetime.fromisoformat(reset.replace('Z', '+00:00')) <= datetime.now(timezone.utc):
                        continue
                except (ValueError, TypeError):
                    continue
                percent = limit.get('percent')
                if isinstance(percent, (int, float)):
                    percents.append(percent)
            if percents and provider.get('id'):
                used = max(percents)
                out[provider['id']] = 'critical' if used >= 95 else 'low' if used >= 80 else 'ok'
        return out
    if isinstance(payload, dict) and 'headroom' in payload:
        return {'claude': str(payload['headroom']).lower()} if payload.get('ok') else {}
    data = payload.get("pools", payload) if isinstance(payload, dict) else {}
    out: dict[str, str] = {}
    for pool, value in data.items():
        if pool == "backstop":
            continue  # a structured block, not a headroom string — see deepseek_headroom
        if isinstance(value, dict):
            value = value.get("headroom", value.get("status", "ok"))
        out[str(pool)] = str(value).lower()
    return out


def quotamax_available(cmd: tuple[str, ...] = ("quotamax", "agent", "--json")) -> bool:
    """Can the quota probe run at all? Fail-open is allowed, silent is not."""
    return shutil.which(cmd[0]) is not None


def fetch_quota(cmd: tuple[str, ...] = ("quotamax", "agent", "--json"), timeout: float = 5.0) -> dict[str, str]:
    """Live quota from quotamax. Fail-open: any error means no quota data.

    The deepseek backstop is the exception to fail-open: it costs real money
    on every call, so unless its balance is positively confirmed OK it is
    gated out — see ``deepseek_headroom``.
    """
    if cmd == ("quotamax", "agent", "--json"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            payloads = list(pool.map(lambda c: _run_json(c, timeout),
                [cmd, ("quotamax", "providers", "--json")]))
        quota = {key: value for payload in payloads if payload is not None
                 for key, value in parse_quota(payload).items()}
        if "deepseek" not in quota:
            quota["deepseek"] = deepseek_headroom(payloads[0], timeout=min(timeout, 2.0))
        return quota
    return _fetch_quota(cmd, timeout)


def deepseek_headroom(payload, *, timeout: float = 2.0, runner=subprocess.run) -> str:
    """DeepSeek backstop headroom: 'ok' or 'critical' — never merely 'low'.

    quotamax exposes ``backstop: {provider, available, balanceUsd, low}``;
    unavailable credit or a balance below the floor means INELIGIBLE. When
    quotamax predates the backstop field, fall back to the skill's own
    ``balance --json``: exit 0 means eligible; below floor (3), error (1),
    timeout, or a missing skill all mean not — the arm spends real money,
    so an unconfirmable balance is a 'no'.
    """
    backstop = payload.get("backstop") if isinstance(payload, dict) else None
    if isinstance(backstop, dict):
        available = bool(backstop.get("available", True))
        low = bool(backstop.get("low", False))
        return "ok" if available and not low else "critical"
    try:
        proc = runner([DEEPSEEK_SKILL, "balance", "--json"],
                      capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return "critical"
    return "ok" if proc.returncode == 0 else "critical"


def _run_json(cmd: tuple[str, ...], timeout: float):
    """Run a command and parse its stdout as JSON; None on any failure."""
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # quotamax intentionally exits nonzero for constrained/critical headroom.
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _fetch_quota(cmd: tuple[str, ...], timeout: float) -> dict[str, str]:
    payload = _run_json(cmd, timeout)
    return parse_quota(payload) if payload is not None else {}


#: arm -> quotamax provider id. Local arms have no quota and no entry.
#: ``deepseek`` is the pay-as-you-go backstop: its "quota" is the credit
#: balance reported via quotamax's ``backstop`` block (or the skill's own
#: ``balance --json`` when quotamax predates it), folded into a headroom
#: value by ``deepseek_headroom``.
QUOTA_PROVIDER: dict[str, str] = {"claude": "claude", "codex": "codex", "kimi": "kimi", "deepseek": "deepseek"}


def parse_usage(agent, providers) -> dict[str, dict[str, float]]:
    """Raw percentages, {provider: {limit_label: percent}}, from both quotamax views.

    ``parse_quota`` buckets usage into ok/low/critical for gating; this keeps
    the numbers so a dispatch can be costed as before/after deltas.
    """
    out: dict[str, dict[str, float]] = {}
    if isinstance(agent, dict) and agent.get("ok"):
        claude = {}
        for label in ("session", "weekly"):
            window = agent.get(label)
            if isinstance(window, dict) and isinstance(window.get("percentUsed"), (int, float)):
                claude[label] = float(window["percentUsed"])
        if claude:
            out["claude"] = claude
    if isinstance(providers, list):
        for provider in providers:
            if not isinstance(provider, dict) or not provider.get("ok") or not provider.get("id"):
                continue
            limits = {
                str(limit.get("label", "limit")): float(limit["percent"])
                for limit in provider.get("limits", [])
                if isinstance(limit, dict) and isinstance(limit.get("percent"), (int, float))
            }
            if limits:
                out[str(provider["id"])] = limits
    return out


def fetch_usage(timeout: float = 5.0) -> dict[str, dict[str, float]]:
    """Live usage percentages from quotamax. Fail-open: {} when unavailable."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        agent, providers = pool.map(
            lambda c: _run_json(c, timeout),
            [("quotamax", "agent", "--json"), ("quotamax", "providers", "--json")],
        )
    return parse_usage(agent, providers)


def apply_quota(
    eligible: list[str],
    quota: dict[str, str],
    *,
    quota_pool: dict[str, str | None] | None = None,
) -> list[str]:
    """Step 4: remove arms whose pool reports critical headroom.

    ``quota_pool`` maps arm -> quotamax pool name; arms mapped to None (local
    models) have no quota and always survive.
    """
    mapping = quota_pool or QUOTA_PROVIDER
    out = []
    for arm in eligible:
        pool_name = mapping.get(arm)
        if pool_name is not None and quota.get(pool_name) == "critical":
            continue
        out.append(arm)
    return out


@dataclass
class GateResult:
    """The survivors of steps 2-4, plus why."""

    veto: Veto | None
    eligible: list[str]
    removed_by_quota: list[str] = field(default_factory=list)
    data_class: str = DATA_OPEN
    removed_by_sensitivity: list[str] = field(default_factory=list)


def sensitive_veto(
    eligible: list[str], pools: dict[str, Pool], *, host: str = VETO_TARGET
) -> tuple[list[str], list[str]]:
    """The sensitive-data gate, applied at the ELIGIBILITY stage.

    Remote third-party pools (``codex``, ``kimi``, ``free``, ``deepseek``)
    are REMOVED — a veto, not a score. ``claude`` stays (the task is already
    there) and local arms stay: they are the only arms where sensitive data
    is safe by construction, because it physically never leaves the machine.
    """
    survivors = [a for a in eligible if a == host or not pools[a].remote]
    removed = [a for a in eligible if a not in survivors]
    return survivors, removed


def gate(
    text: str,
    shape: str,
    pools: dict[str, Pool],
    *,
    quota: dict[str, str] | None = None,
    pool_pin: str | None = None,
    private: bool = False,
    sensitive: bool = False,
    no_acceptance: bool = False,
    needs_context: bool = False,
    cross_repo: bool = False,
    host: str = VETO_TARGET,
) -> GateResult:
    """Steps 2-4 in order: veto, eligibility, quota.

    When ``sensitive`` (an explicit signal only — see ``sensitive.py``), the
    private-data content regex is subsumed: instead of pinning the task to
    ``claude``, remote third-party arms are vetoed at the eligibility stage
    and local arms are kept, so sensitive work can prefer the machine it is
    already on. If that leaves nothing, raise — never fall back to remote.
    """
    if host not in pools:
        raise ValueError(f"unknown host pool: {host}")
    if pool_pin and pool_pin not in pools:
        raise ValueError(f"unknown pinned pool: {pool_pin}")
    veto = detect_veto(
        text,
        shape=shape,
        pool_pin=pool_pin,
        private=private,
        no_acceptance=no_acceptance,
        needs_context=needs_context,
        cross_repo=cross_repo,
        # The sensitive dataClass handles private data better than the regex
        # safety net (keep claude AND local-*, prefer local), so it subsumes
        # the private-data veto — later vetoes still apply.
        skip_private=sensitive,
    )
    if veto is not None:
        if veto.name != "user-pin":
            veto = replace(veto, target=host)
        return GateResult(
            veto=veto,
            eligible=[veto.target],
            data_class=DATA_SENSITIVE if sensitive else DATA_OPEN,
        )
    eligible = eligible_for(shape, pools)
    removed_sensitive: list[str] = []
    if sensitive:
        eligible, removed_sensitive = sensitive_veto(eligible, pools, host=host)
    if quota:
        survivors = apply_quota(eligible, quota)
        removed = [a for a in eligible if a not in survivors]
        eligible = survivors
    else:
        removed = []
    if not eligible:
        if sensitive:
            raise SensitiveRoutingError(
                "sensitive task has no eligible arm: remote arms "
                f"{removed_sensitive or '[]'} are vetoed for sensitive data, "
                f"quota removed {removed or '[]'}, and no local arm accepts "
                f"'{shape}'. Refusing to fall back to a remote arm — that is "
                "the failure the sensitive flag exists to prevent."
            )
        # Nobody can take it — stay here rather than route into the void.
        return GateResult(veto=Veto("no-survivors", "no eligible arm survived quota gating", target=host), eligible=[host])
    return GateResult(
        veto=None,
        eligible=eligible,
        removed_by_quota=removed,
        data_class=DATA_SENSITIVE if sensitive else DATA_OPEN,
        removed_by_sensitivity=removed_sensitive,
    )
