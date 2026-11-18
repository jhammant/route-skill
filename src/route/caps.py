"""Usage caps a dispatched model reports: detect them, and remember them until they reset.

A model CLI that has run out of a usage window does not fail like a broken tool.
It prints one line ("You've hit your Fable limit · resets Nov 23 at 3pm
(Europe/London). ...") and exits. Read as ordinary output, that line is "the
planner printed no plan", and the same arm is asked again. ``detect_cap`` reads
it for what it is, from any role's output.

A detected cap is recorded in ``model-caps.json`` under the state dir until its
reset. ``apply_caps`` lays the live records over a quota snapshot as
``critical`` headroom, so ``route``, ``route auto`` and the next ``route ultra``
all skip that model window without asking the model again. claude-swarm keeps
the same kind of record for its own runs; it is read here too (never written).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .eligibility import ARM_KEY, VETO_TARGET, WORK_ACCOUNT, gate_keys, in_claude_work_session, quota_provider
from .pools import Pool
from .storage import atomic_write, state_dir

#: Only the end of the output is read: a CLI stopped by a cap says so last.
TAIL_LINES = 12
#: An output this short may be a cap even when the CLI exited 0; a longer one that
#: exited 0 is a model that finished its work and happened to mention a limit.
SHORT_OUTPUT = 1500
#: How long a cap with no readable reset is remembered (claude-swarm uses the same hour).
DEFAULT_SECONDS = 3600.0
#: A request-rate limit (HTTP 429) clears quickly; it only parks the arm briefly.
RATE_SECONDS = 600.0

_LOCK = threading.Lock()


def _clock() -> float:
    """Now, epoch seconds; one place so a test can hold the date still."""
    return time.time()


@dataclass(frozen=True)
class Cap:
    """One usage cap, as the CLI reported it."""

    #: The model the limit belongs to ("Fable"); "" when the message names none.
    model: str = ""
    #: The window ("weekly", "5-hour", "session", "usage", "rate"); "" when unnamed.
    window: str = ""
    #: When it resets, epoch seconds; None when the message does not say or cannot be read.
    resets_at: float | None = None
    #: The reset for people ("Nov 23 15:00"), or the CLI's own words when it could not be parsed.
    resets: str = ""
    #: The line that matched.
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict) -> Cap:
        """A cap from a record that carries its fields (a swarm result's ``capped``, a quota event)."""
        return cls(model=str(row.get("model") or ""), window=str(row.get("window") or ""),
                   resets_at=row.get("resets_at") if isinstance(row.get("resets_at"), (int, float)) else None,
                   resets=str(row.get("resets") or ""), message=str(row.get("message") or ""))


# -- detection -------------------------------------------------------------------

_APOS = "(?:'|’)"
#: What may follow "limit reached" in a real cap line. "Usage limit reached handling
#: is done" (a worker describing its own change) does not qualify.
_AFTER = r"(?=\s*(?:$|[.|·∙•!,;:(–—-]|resets?\b|for\b|please\b|try\b|upgrade\b|your\b))"
_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple((re.compile(pattern, re.I), window) for pattern, window in (
    # Claude Code and Codex: "You've hit your Fable limit", "You've hit your usage limit".
    (rf"you(?:{_APOS}| ha)ve (?:hit|reached|exceeded) (?:your|the) (?P<what>[\w.\- ]{{0,40}}?)\s*limit\b", ""),
    # "5-hour limit reached", "Opus weekly limit reached", "Claude AI usage limit reached|1700000000".
    (r"(?P<what>(?:[\w.\-]+ ){0,3}?(?:usage|5-hour|five-hour|hourly|weekly|session|daily|monthly|rate))"
     r"\s+limit (?:reached|exceeded|hit)" + _AFTER, ""),
    (rf"you(?:{_APOS}| a)re out of (?:extra )?usage\b", "usage"),
    (r"limit reached\s*[·∙•|–—-]\s*resets?\b", ""),
    (r".*\busage_limit_reached\b", "usage"),
    (r".*\b(?:insufficient_quota|you exceeded your current quota)\b", "usage"),
    (r".*(?:exceeded retry limit, last status: 429|\b429 too many requests\b)", "rate"),
))
#: Log furniture in front of the message: a timestamp, a bullet, "ERROR:", "API Error:".
_LEAD = re.compile(r"^\s*(?:\[[^\]]{1,40}\]\s*)?[^\w\[{]{0,8}\s*(?:(?:\w+ ){0,2}error\w*:\s*|warning:\s*)?", re.I)
_WINDOWS = {"5-hour": "5-hour", "five-hour": "5-hour", "hourly": "hourly", "weekly": "weekly",
            "session": "session", "daily": "daily", "monthly": "monthly", "rate": "rate", "usage": "usage"}
#: Words in "your ... limit" that are neither a model nor a window.
_NOISE = frozenset({"claude", "ai", "chatgpt", "codex", "openai", "plan", "current", "extra", "account", "model"})


def detect_cap(text: str | None, rc: int | None = None, *, now: float | None = None) -> Cap | None:
    """The usage cap a role's output ends on, or None.

    Works on any dispatched role's output. The message must open one of the
    last ``TAIL_LINES`` lines, and the CLI must have failed (``rc`` non-zero or
    unknown) or have said almost nothing else: a model that exited 0 after a
    long answer was not stopped by a cap, whatever its answer quotes.
    """
    if not text or not text.strip():
        return None
    if rc == 0 and len(text.strip()) > SHORT_OUTPUT:
        return None
    lines = [line for line in text.splitlines() if line.strip()][-TAIL_LINES:]
    for line in reversed(lines):
        for candidate in _messages(line):
            cap = _match(candidate, now)
            if cap is not None:
                return cap
    return None


def _messages(line: str) -> list[str]:
    """The line itself, or the result text of a ``--output-format json`` line."""
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            event = json.loads(stripped)
        except ValueError:
            return [stripped]
        if isinstance(event, dict):
            return [str(value) for key in ("result", "message", "error") if isinstance(value := event.get(key), str)]
    return [stripped]


def _match(line: str, now: float | None) -> Cap | None:
    body = _LEAD.sub("", line, count=1)
    for pattern, default_window in _PATTERNS:
        found = pattern.match(body)
        if found is None:
            continue
        model, window = _model_and_window(found.groupdict().get("what") or "")
        # The reset follows the message; a wrapped error ("... usage_limit_reached") may carry it earlier.
        resets_at, resets = parse_reset(body[found.end():], now=now)
        if resets_at is None and not resets:
            resets_at, resets = parse_reset(body, now=now)
        return Cap(model=model, window=window or default_window, resets_at=resets_at, resets=resets,
                   message=" ".join(line.split())[:400])
    return None


def _model_and_window(what: str) -> tuple[str, str]:
    window, model = "", []
    for word in what.split():
        lowered = word.lower()
        if lowered in _WINDOWS:
            window = window or _WINDOWS[lowered]
        elif lowered not in _NOISE:
            model.append(word)
    return " ".join(model), window


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RESET = re.compile(r"(?:\bresets\b|\breset(?=\s+(?:at|in|on)\b)|\btry again\b|\bavailable again\b)"
                    r"\s+(?P<how>in|at|on)?\s*(?P<when>[^\n]*)", re.I)
_EPOCH = re.compile(r"^\s*\|\s*(\d{9,13})\b")
_ZONE = re.compile(r"\(([A-Za-z][\w+\-]*(?:/[\w+\-]+)*)\)")
_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(w|d|h|m|s)(?:eeks?|ays?|(?:ou)?rs?|in(?:ute)?s?|ec(?:ond)?s?)?\b", re.I)
_SECONDS = {"w": 604800.0, "d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}
_MONTH_DAY = re.compile(rf"\b({'|'.join(_MONTHS)})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}})\b)?", re.I)
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({'|'.join(_MONTHS)})[a-z]*\.?(?:,?\s+(\d{{4}})\b)?", re.I)
_WEEKDAY = re.compile(rf"\b({'|'.join(_DAYS)})[a-z]*\b", re.I)
_CLOCK_12 = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b", re.I)
_CLOCK_24 = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def parse_reset(text: str, *, now: float | None = None) -> tuple[float | None, str]:
    """(epoch seconds, text for people) for the reset a cap message gives; (None, raw words) when unread.

    Reads "resets Nov 23 at 3pm (Europe/London)", "resets 3pm", "resets Mon 9am",
    "try again in 3 days 4 hours", "try again at Nov 23rd, 2026 3:00 PM" and the
    old "...limit reached|1700000000". A time with no zone is the machine's own.
    """
    now = _clock() if now is None else now
    epoch = _EPOCH.match(text)
    if epoch:
        stamp = float(epoch.group(1))
        stamp = stamp / 1000 if stamp > 1e11 else stamp
        return stamp, format_reset(stamp)
    found = _RESET.search(text)
    if found is None:
        return None, ""
    when = found.group("when")
    zone_match = _ZONE.search(when)
    zone = _zone(zone_match.group(1)) if zone_match else None
    # The reset ends where the sentence does; what follows ("Switch to another model...") is advice.
    when = when[:zone_match.start()] if zone_match else re.split(r"\.(?:\s|$)|;|,\s+or\b|\s+to continue\b", when, maxsplit=1)[0]
    raw = " ".join(when.split()).strip(" .,")
    if (found.group("how") or "").lower() == "in":
        seconds = sum(float(n) * _SECONDS[unit.lower()] for n, unit in _UNIT.findall(when))
        return (now + seconds, format_reset(now + seconds)) if seconds > 0 else (None, raw)
    stamp = _absolute(when, zone, now)
    return (stamp, format_reset(stamp, zone)) if stamp is not None else (None, raw)


def _zone(name: str):
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, KeyError):  # an unknown or malformed zone name
        return None


def _absolute(when: str, zone, now: float) -> float | None:
    try:
        stamp = datetime.fromisoformat(when.strip().rstrip(".").replace("Z", "+00:00"))
        return (stamp if stamp.tzinfo else stamp.replace(tzinfo=zone) if zone else stamp.astimezone()).timestamp()
    except ValueError:
        pass
    here = datetime.fromtimestamp(now, zone) if zone else datetime.fromtimestamp(now).astimezone()
    hour = minute = None
    clock = _CLOCK_12.search(when)
    if clock:
        hour = int(clock.group(1)) % 12 + (12 if clock.group(3).lower() == "p" else 0)
        minute = int(clock.group(2) or 0)
    elif clock := _CLOCK_24.search(when):
        hour, minute = int(clock.group(1)), int(clock.group(2))
    if hour is not None and not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    month = day = year = None
    if dated := _MONTH_DAY.search(when):
        month, day, year = _MONTHS.index(dated.group(1).lower()) + 1, int(dated.group(2)), dated.group(3)
    elif dated := _DAY_MONTH.search(when):
        month, day, year = _MONTHS.index(dated.group(2).lower()) + 1, int(dated.group(1)), dated.group(3)
    at = {"hour": hour or 0, "minute": minute or 0, "second": 0, "microsecond": 0}
    try:
        if month is not None:
            stamp = here.replace(year=int(year) if year else here.year, month=month, day=day, **at)
            if not year and stamp < here - timedelta(days=1):
                stamp = stamp.replace(year=stamp.year + 1)
            return stamp.timestamp()
        weekday = _WEEKDAY.search(when)
        if weekday:
            ahead = (_DAYS.index(weekday.group(1).lower()) - here.weekday()) % 7
            stamp = (here + timedelta(days=ahead)).replace(**at)
            return (stamp if stamp > here else stamp + timedelta(days=7)).timestamp()
        if hour is None:
            return None
        stamp = here.replace(**at)
        return (stamp if stamp > here else stamp + timedelta(days=1)).timestamp()
    except ValueError:  # 31 Feb and the like
        return None


def format_reset(stamp: float | None, zone=None) -> str:
    """"Nov 23 15:00", in the zone the message named or else the machine's."""
    if stamp is None:
        return ""
    moment = datetime.fromtimestamp(stamp, zone) if zone else datetime.fromtimestamp(stamp).astimezone()
    return f"{moment:%b} {moment.day} {moment:%H:%M}"


def describe(row: dict) -> str:
    """"claude-work-fable hit its Fable limit (resets Nov 23 15:00)" for a cap record or a run's quota event."""
    limit = " ".join(part for part in (row.get("model"), row.get("window")) if part)
    kind = f"its {limit} limit" if limit else "a usage limit"
    subject = row.get("arm") or ", ".join(row.get("keys") or []) or "a model"
    return f"{subject} hit {kind}" + (f" (resets {row['resets']})" if row.get("resets") else "")


# -- what a cap exhausts -----------------------------------------------------------

def cap_keys(arm: str, pools: dict[str, Pool], cap: Cap) -> list[str]:
    """The quota keys a cap on ``arm`` spends, the arm's own key last.

    A cap that names a model spends that model's window in the arm's pool
    ("claude-work:Fable"), which also removes every other arm on that window. A
    cap that names none is the account's ("claude-work"), and removes every arm
    that spends it. The arm's own key is always included, so the arm that
    reported the cap is never asked again whatever its pool declares.
    """
    provider = quota_provider(arm, pools)
    keys: list[str] = []
    if provider:
        pool = pools.get(arm)
        scope = pool.quota_scope if pool is not None else ""
        if not cap.model:
            keys.append(provider)
        elif scope and scope.lower() == cap.model.lower():
            keys.append(f"{provider}:{scope}")
        else:
            keys.append(f"{provider}:{cap.model}")
    keys.append(ARM_KEY + arm)
    return keys


def exhausted_arms(keys: list[str], pools: dict[str, Pool]) -> list[str]:
    """Every arm one of these keys removes, in pool order."""
    wanted = {key.lower() for key in keys}
    return [arm for arm in pools if wanted & {key.lower() for key in gate_keys(arm, pools)}]


def mark(quota: dict, keys: list[str]) -> None:
    """Set these keys ``critical`` in a quota mapping, in place; a key that only differs in case is reused.

    A run's units share one mapping across threads; every write goes through
    here, under one lock, so two caps met at once cannot trip over each other.
    """
    with _LOCK:
        by_lower = {str(key).lower(): key for key in quota}
        for key in keys:
            quota[by_lower.get(key.lower(), key)] = "critical"


# -- the record --------------------------------------------------------------------

def caps_path() -> Path:
    return state_dir() / "model-caps.json"


def _own_caps(now: float) -> list[dict]:
    try:
        rows = json.loads(caps_path().read_text(encoding="utf-8")).get("caps", [])
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get("until"), (int, float))
            and row["until"] > now and isinstance(row.get("keys"), list)
            and all(isinstance(key, str) for key in row["keys"])]


def _portable(key: str) -> bool:
    """Whether a key means the same thing in every session.

    Inside a claude-work session the host arm ``claude`` spends the second
    account, so "the claude arm is capped" must neither leave such a session
    nor be believed inside one.
    """
    return not (key == ARM_KEY + VETO_TARGET and in_claude_work_session())


def record_cap(arm: str, pools: dict[str, Pool], cap: Cap, *, source: str = "", now: float | None = None) -> dict:
    """Remember a cap until it resets and return its record (``keys`` are what it spends).

    A newer record for the same arm replaces the older one. Writing is best
    effort: a state dir that cannot be written loses the memory, never the run.
    """
    now = _clock() if now is None else now
    keys = cap_keys(arm, pools, cap)
    fallback = RATE_SECONDS if cap.window == "rate" else DEFAULT_SECONDS
    until = cap.resets_at if cap.resets_at is not None and cap.resets_at > now else now + (
        fallback if cap.resets_at is None else 60.0)
    row = {"arm": arm, "keys": keys, "model": cap.model, "window": cap.window, "resets": cap.resets,
           "resets_at": cap.resets_at, "until": until, "message": cap.message, "source": source, "at": now}
    kept = {**row, "keys": [key for key in keys if _portable(key)]}
    with _LOCK:
        rows = [old for old in _own_caps(now) if old.get("arm") != arm] + [kept]
        try:
            atomic_write(caps_path(), json.dumps({"caps": rows}, indent=2) + "\n")
        except OSError:
            pass
    return row


def clear_caps(target: str | None = None) -> list[dict]:
    """Forget route's own records (all, or those naming this arm or key); the ones removed.

    For when the detector was wrong or credit was bought before the reset.
    claude-swarm's records are its own and are not touched.
    """
    with _LOCK:
        rows = _own_caps(_clock())
        gone = [row for row in rows if target is None or row.get("arm") == target or target in row["keys"]]
        if gone:
            atomic_write(caps_path(), json.dumps({"caps": [row for row in rows if row not in gone]}, indent=2) + "\n")
    return gone


#: claude-swarm (``~/dev/oss/claude-work/bin/claude-swarm``) records a limit one of its tasks
#: hit as ``<state>/quota/<account>.cool.json``: ``{"<model alias>" | "*": <epoch until>}``.
SWARM_WORK_KEYCHAIN = "claude-work-oauth"


def _swarm_accounts() -> dict[str, str]:
    """{claude-swarm account: the quota pool it spends}, for the accounts route has a pool for.

    Only the account on the claude-work login is mapped: it is the one whose
    pool name route knows. With no readable config that is claude-swarm's
    default account, ``work``.
    """
    path = Path(os.environ.get("CLAUDE_SWARM_CONFIG") or Path.home() / ".config" / "claude-swarm" / "accounts.json")
    try:
        accounts = json.loads(path.read_text(encoding="utf-8")).get("accounts")
    except (OSError, ValueError, AttributeError):
        return {"work": WORK_ACCOUNT}
    return {str(row["name"]): WORK_ACCOUNT for row in (accounts if isinstance(accounts, list) else [])
            if isinstance(row, dict) and row.get("name") and row.get("keychain") == SWARM_WORK_KEYCHAIN}


def swarm_caps(now: float | None = None) -> list[dict]:
    """The limits claude-swarm has seen its accounts hit and that have not reset, as cap records."""
    now = _clock() if now is None else now
    state = Path(os.environ.get("CLAUDE_SWARM_STATE") or Path.home() / ".local" / "state" / "claude-swarm")
    rows = []
    for account, provider in _swarm_accounts().items():
        try:
            cooling = json.loads((state / "quota" / f"{account}.cool.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(cooling, dict):
            continue
        for alias, until in cooling.items():
            if isinstance(until, bool) or not isinstance(until, (int, float)) or until <= now:
                continue
            model = "" if alias == "*" else str(alias).capitalize()
            rows.append({"arm": None, "keys": [f"{provider}:{model}" if model else provider], "model": model,
                         "window": "", "resets": format_reset(until), "resets_at": float(until),
                         "until": float(until), "message": "", "source": f"claude-swarm:{account}", "at": None})
    return rows


def active_caps(now: float | None = None) -> list[dict]:
    """Every cap that has not reset: route's own records, then claude-swarm's."""
    now = _clock() if now is None else now
    return _own_caps(now) + swarm_caps(now)


def standing_cap(arm: str, pools: dict[str, Pool], *, source: str = "", now: float | None = None) -> dict | None:
    """The live record that takes ``arm`` out, if there is one; only records whose source starts with ``source``.

    A swarm asks this, for its own records, before starting a unit: the units
    queued behind the one that met the cap are not sent to ask again.
    """
    rows = [row for row in active_caps(now) if str(row.get("source") or "").startswith(source)]
    if not rows:
        return None
    keys = {key.lower() for key in gate_keys(arm, pools) if _portable(key)}
    return next((row for row in rows if keys & {key.lower() for key in row["keys"]}), None)


def apply_caps(quota: dict, now: float | None = None) -> list[dict]:
    """Lay the live caps over a quota mapping as ``critical``; the records applied."""
    rows = active_caps(now)
    for row in rows:
        mark(quota, [key for key in row["keys"] if _portable(key)])
    return rows
