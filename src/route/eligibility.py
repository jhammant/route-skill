"""Vetoes (step 2) and quota gating (steps 3-4).

Vetoes are hard gates, not scores: any veto short-circuits to ``claude``
regardless of quota or complexity. Quota gates *availability* — an arm whose
pool reports ``critical`` headroom is REMOVED from eligibility, never merely
penalised. The bandit decides quality among what survives.

Sensitive data (SPEC-ship section 1) is also an eligibility-stage veto, but
a different shape: remote third-party pools (``codex``, ``kimi``, ``free``)
are removed, while ``claude`` AND the local arms survive — local is the only
arm where sensitive data is safe by construction, and it is PREFERRED at
selection. If nothing survives, ``gate`` raises rather than falling back to
a remote arm.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace

from .pools import Pool
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
        if isinstance(value, dict):
            value = value.get("headroom", value.get("status", "ok"))
        out[str(pool)] = str(value).lower()
    return out


def fetch_quota(cmd: tuple[str, ...] = ("quotamax", "agent", "--json"), timeout: float = 5.0) -> dict[str, str]:
    """Live quota from quotamax. Fail-open: any error means no quota data."""
    if cmd == ("quotamax", "agent", "--json"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            parts = list(pool.map(lambda c: _fetch_quota(c, timeout),
                [cmd, ("quotamax", "providers", "--json")]))
        return {key: value for part in parts for key, value in part.items()}
    return _fetch_quota(cmd, timeout)


def _fetch_quota(cmd: tuple[str, ...], timeout: float) -> dict[str, str]:
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    # quotamax intentionally exits nonzero for constrained/critical headroom.
    try:
        return parse_quota(json.loads(proc.stdout))
    except ValueError:
        return {}


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
    mapping = quota_pool or {"claude": "claude", "codex": "codex", "kimi": "kimi"}
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

    Remote third-party pools (``codex``, ``kimi``, ``free``) are REMOVED — a
    veto, not a score. ``claude`` stays (the task is already there) and local
    arms stay: they are the only arms where sensitive data is safe by
    construction, because it physically never leaves the machine.
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
