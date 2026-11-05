"""Turn a goal into independent, checkable units for a route swarm.

The planner is a model, so its output is data, not code: the parser is strict
about shape, the lint applies the same command policy the checks will later run
under, and units whose files overlap are merged so two workers never edit one
file in parallel. Whatever survives becomes a ``run_swarm_manifest`` manifest
with every unit pinned to the arm ``ultra_select`` chose for it.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path

from .complexity import TIERS
from .shapes import SHAPES
from .swarm_dispatch import UNIT_ID
from .ultra_safety import ALLOWED, lint_command

MANIFEST_BEGIN = "ULTRA-MANIFEST-BEGIN"
MANIFEST_END = "ULTRA-MANIFEST-END"

_FENCE = re.compile(r"^```[\w-]*\s*$", re.M)
_MAX_ID = 32


class PlanError(ValueError):
    """The planner's output cannot be used; the message says what to fix."""


@dataclass
class Unit:
    id: str
    task: str
    check: str
    touches: list[str] = field(default_factory=list)
    shape: str | None = None
    tier: str | None = None

    def to_dict(self) -> dict:
        row = {"id": self.id, "task": self.task, "check": self.check, "touches": list(self.touches)}
        if self.shape is not None:
            row["shape"] = self.shape
        if self.tier is not None:
            row["tier"] = self.tier
        return row


@dataclass
class Plan:
    name: str
    shared: str
    goal_check: str
    units: list[Unit]

    def to_dict(self) -> dict:
        return {"name": self.name, "shared": self.shared, "goal_check": self.goal_check,
                "units": [unit.to_dict() for unit in self.units]}


def planner_prompt(goal: str, *, repo: str | Path, base: str, max_units: int, context_note: str = "") -> str:
    """The planner sees the repo but not this conversation, so the brief carries every rule.

    Checks are spelled out here because the lint refuses anything else: a plan
    whose checks cannot run is a plan that has to be made twice.
    """
    allowed = "\n".join(f"  - {' '.join(prefix)}" for prefix in ALLOWED)
    note = f"\n{context_note.strip()}\n" if context_note.strip() else ""
    return f"""You are the planner for an automated parallel build ("route ultra").
Repository: {repo} at revision {base}. You are in a throwaway checkout: explore it read-only
(read files, grep, list tests). Do not edit, commit, install or run anything that changes it.
{note}
# The goal

{goal.strip()}

# Your job

Split the goal into at most {max_units} INDEPENDENT units. Independent means: each unit can be built
by a separate worker, in its own copy of the repository, without seeing the others' work, and the
results merge cleanly because no two units edit the same file. Fewer, well-scoped units beat many
tiny ones; one unit is fine when the goal does not split. Shared decisions (interfaces, names,
formats that two units both rely on) go in "shared", which every worker reads.

Each unit needs:
- "id": lowercase letters, digits and hyphens, at most {_MAX_ID} characters, not "hub", unique.
- "task": a complete brief for a worker that sees ONLY this text plus "shared": which files to create
  or change, the interfaces to keep, the tests to write, the style to match, what is out of bounds.
- "check": ONE command, run from the repository root, that passes only when the unit is done. It must
  be a real test command: it may only start with one of these prefixes, parts may be joined with
  " && " and nothing else (no ; | > < ` $( or newlines), paths must be relative and inside the
  repository (no ".."), and a check that only echoes, exits 0 or tests that a file exists is refused:
{allowed}
- "touches": the relative paths (files or directories) the unit creates or edits, tests included.
  Touches must be DISJOINT across units: units that share a path are merged into one.
- optional "shape": one of {", ".join(SHAPES)}.
- optional "tier": one of {", ".join(TIERS)}.

Also give:
- "name": a short name for the whole build.
- "goal_check": ONE command (same rules as a check) that verifies the WHOLE goal once every unit is
  merged together, for example the full test suite or the goal's own acceptance test.

# Output

Think and explore as you need, then print ONLY this, as the last thing, with nothing after it:

{MANIFEST_BEGIN}
{{"name": "...", "shared": "...", "goal_check": "...",
 "units": [{{"id": "...", "task": "...", "check": "...", "touches": ["..."], "shape": "...", "tier": "..."}}]}}
{MANIFEST_END}
"""


def _marked_block(text: str, begin: str, end: str, error: type[ValueError]) -> str | None:
    """The LAST marked block wins: a planner may quote the format before printing the plan."""
    start = text.rfind(begin)
    if start < 0:
        return None
    start += len(begin)
    stop = text.find(end, start)
    if stop < 0:
        raise error(f"{begin} has no matching {end}")
    return _FENCE.sub("", text[start:stop]).strip()


def _string(raw: dict, key: str, where: str, *, default: str | None = None) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise PlanError(f"{where}: {key!r} must be a string")
    return value


def _unit_from(raw: object, index: int) -> Unit:
    if not isinstance(raw, dict):
        raise PlanError(f"unit {index}: must be an object")
    where = f"unit {raw.get('id', index)!r}" if isinstance(raw.get("id"), str) else f"unit {index}"
    touches = raw.get("touches", [])
    if not isinstance(touches, list) or not all(isinstance(path, str) for path in touches):
        raise PlanError(f"{where}: 'touches' must be a list of relative paths")
    optional = {}
    for key in ("shape", "tier"):
        if raw.get(key) is not None:
            optional[key] = _string(raw, key, where)
    return Unit(id=_string(raw, "id", where), task=_string(raw, "task", where),
                check=_string(raw, "check", where), touches=list(touches), **optional)


def parse_plan(text: str) -> Plan:
    """The last marked JSON object in the planner's output, checked for shape only.

    Content rules (ids, checks, limits) live in ``lint_plan`` so their problems
    can be reported together and handed back to the planner in one re-plan.
    """
    block = _marked_block(text or "", MANIFEST_BEGIN, MANIFEST_END, PlanError)
    if block is None:
        raise PlanError(f"no {MANIFEST_BEGIN} block in the planner output")
    try:
        raw = json.loads(block)
    except ValueError as exc:
        raise PlanError(f"plan is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise PlanError("plan must be a JSON object")
    units = raw.get("units")
    if not isinstance(units, list):
        raise PlanError("plan 'units' must be a list")
    return Plan(name=_string(raw, "name", "plan"), shared=_string(raw, "shared", "plan", default=""),
                goal_check=_string(raw, "goal_check", "plan"),
                units=[_unit_from(unit, index) for index, unit in enumerate(units)])


def _touch_problem(path: str) -> str | None:
    if not path.strip():
        return "an empty path"
    if path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path):
        return f"absolute path {path!r}"
    if ".." in Path(path.replace("\\", "/")).parts:
        return f"parent segment in {path!r}"
    return None


def lint_plan(plan: Plan, *, max_units: int) -> list[str]:
    """Every reason the plan cannot run, so the planner can fix them all at once."""
    problems: list[str] = []
    if not plan.name.strip():
        problems.append("name must be non-empty")
    if not plan.units:
        problems.append("plan has no units")
    elif len(plan.units) > max_units:
        problems.append(f"{len(plan.units)} units; at most {max_units} are allowed")
    if not plan.goal_check.strip():
        problems.append("goal_check must be non-empty")
    else:
        reason = lint_command(plan.goal_check)
        if reason:
            problems.append(f"goal_check {plan.goal_check!r}: {reason}")
    seen: set[str] = set()
    for unit in plan.units:
        label = f"unit {unit.id!r}"
        if not UNIT_ID.fullmatch(unit.id) or unit.id == "hub":
            problems.append(f"{label}: id must match [a-z0-9][a-z0-9-]{{0,31}} and not be 'hub'")
        if unit.id in seen:
            problems.append(f"{label}: duplicate id")
        seen.add(unit.id)
        if not unit.task.strip():
            problems.append(f"{label}: task must be non-empty")
        if not unit.check.strip():
            problems.append(f"{label}: check must be non-empty")
        else:
            reason = lint_command(unit.check)
            if reason:
                problems.append(f"{label}: check {unit.check!r}: {reason}")
        for path in unit.touches:
            reason = _touch_problem(path)
            if reason:
                problems.append(f"{label}: touches has {reason}")
        if unit.shape is not None and unit.shape not in SHAPES:
            problems.append(f"{label}: unknown shape {unit.shape!r}")
        if unit.tier is not None and unit.tier not in TIERS:
            problems.append(f"{label}: unknown tier {unit.tier!r}")
    return problems


def _normal(path: str) -> str:
    return posixpath.normpath(path.strip().replace("\\", "/")) if path.strip() else "."


def _overlap(a: str, b: str) -> bool:
    """Same path, or one inside the other; the repo root ('.') overlaps everything."""
    a, b = _normal(a), _normal(b)
    return a == b or a == "." or b == "." or a.startswith(b + "/") or b.startswith(a + "/")


def _join_ids(ids: list[str], taken: set[str]) -> str:
    joined = "-".join(ids)
    if len(joined) > _MAX_ID:
        joined = joined[:_MAX_ID].rstrip("-")
    candidate, suffix = joined, 2
    while candidate in taken:
        stem = joined[:_MAX_ID - len(str(suffix)) - 1].rstrip("-")
        candidate = f"{stem}-{suffix}"
        suffix += 1
    return candidate


def _merge_group(units: list[Unit], taken: set[str]) -> Unit:
    tiers = [unit.tier for unit in units if unit.tier in TIERS]
    shapes = {unit.shape for unit in units}
    checks: list[str] = []
    for unit in units:
        for part in unit.check.split("&&"):
            part = part.strip()
            if part and part not in checks:
                checks.append(part)
    touches: list[str] = []
    for unit in units:
        for path in unit.touches:
            if path not in touches:
                touches.append(path)
    return Unit(
        id=_join_ids([unit.id for unit in units], taken),
        task="\n\n".join(f"## {unit.id}\n{unit.task.strip()}" for unit in units),
        check=" && ".join(checks),
        touches=touches,
        shape=shapes.pop() if len(shapes) == 1 else None,
        # The merged unit is at least as hard as its hardest part.
        tier=max(tiers, key=TIERS.index) if tiers else None,
    )


def merge_overlaps(plan: Plan) -> Plan:
    """Units that edit the same files cannot run in parallel; make them one unit.

    Groups are found by transitive overlap and keep the order of their first
    member, so the plan reads the way the planner wrote it.
    """
    parent = list(range(len(plan.units)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, first in enumerate(plan.units):
        for j in range(i + 1, len(plan.units)):
            second = plan.units[j]
            if any(_overlap(a, b) for a in first.touches for b in second.touches):
                parent[root(j)] = root(i)
    groups: dict[int, list[Unit]] = {}
    for i, unit in enumerate(plan.units):
        groups.setdefault(root(i), []).append(unit)
    merged: list[Unit] = []
    taken = {unit.id for unit in plan.units}
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
        else:
            unit = _merge_group(group, taken - {u.id for u in group})
            taken.add(unit.id)
            merged.append(unit)
    return Plan(name=plan.name, shared=plan.shared, goal_check=plan.goal_check, units=merged)


def to_manifest(plan: Plan, *, repo: str | Path, base: str, swarm_id: str, arms: dict[str, str]) -> dict:
    """A ``run_swarm_manifest`` manifest with each unit pinned to its chosen arm.

    Units without an arm (held: no usable model) are left out, so the swarm only
    ever dispatches what ultra decided it could.
    """
    units = []
    for unit in plan.units:
        arm = arms.get(unit.id)
        if arm is None:
            continue
        row = {"id": unit.id, "task": unit.task, "check": unit.check, "pool": arm}
        if unit.shape is not None:
            row["shape"] = unit.shape
        if unit.tier is not None:
            row["tier"] = unit.tier
        units.append(row)
    # Every check here was written by a model: the swarm must run them through
    # ultra_safety (no shell, no credentials), as ultra does for every other command.
    return {"name": plan.name, "repo": str(repo), "base": base, "shared": plan.shared,
            "swarm_id": swarm_id, "safe_checks": True, "units": units}
