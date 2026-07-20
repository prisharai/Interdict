"""Intent-mismatch detection (advisory only).

NEVER on the hot path of a query the agent is waiting on; NEVER load-bearing.
We do not claim to *know* the agent's intent -- we
**detect contradiction** between the agent's stated task (free text it gave us)
and what the statement actually does (its operation and measured blast radius).
On a strong contradiction we raise an advisory flag that can escalate a write to
*human confirmation* -- it never hard-blocks on its own.

The deterministic checks below are pure in-memory string/number heuristics
(microseconds, no I/O), so they are safe to compute inline as advisory metadata.
The optional LLM "second opinion" is strictly out-of-band: scheduled as a
fire-and-forget background task that only appends its opinion to the audit log,
never gating or delaying the agent's query. It is off by default.

Honest limit: we check *query-vs-task*, not *task-vs-reality*. If the
stated task is itself wrong, nothing here catches it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.classifier import DDL, WRITE, Classification

# Severity levels, increasing.
NONE = "none"
LOW = "low"
HIGH = "high"

# Read-only verbs: if the task speaks purely in these terms but the statement
# writes, that's a contradiction.
_READ_VERBS = {
    "show",
    "list",
    "get",
    "find",
    "display",
    "view",
    "see",
    "read",
    "fetch",
    "retrieve",
    "count",
    "check",
    "inspect",
    "print",
    "report",
    "lookup",
    "look",
    "select",
    "preview",
    "audit",
}

# Words signalling the task *intends* a broad change -- a large blast radius is
# then consistent, not a contradiction.
_BULK_CUES = {
    "all",
    "every",
    "everything",
    "entire",
    "whole",
    "bulk",
    "mass",
    "across",
    "globally",
    "wholesale",
}

# Words signalling the task modifies data (so it isn't a pure read).
_WRITE_WORDS = {
    "delete",
    "remove",
    "drop",
    "truncate",
    "purge",
    "wipe",
    "update",
    "insert",
    "add",
    "set",
    "modify",
    "change",
    "create",
    "clear",
    "reset",
    "rename",
    "alter",
    "archive",
    "deactivate",
    "disable",
}

# Explicit singular cues -- the task is about one / a specific thing.
_SINGLE_WORDS = {
    "single",
    "one",
    "only",
    "just",
    "specific",
    "particular",
    "this",
    "that",
    "individual",
    "sole",
}

# Determiners that introduce a singular object ("a user", "this account"). We
# deliberately exclude bare "the", which is used for plural sets just as often
# ("the users", "the old rows"). A following plural noun (trailing 's') or "few"
# is not a single-scope cue.
_SINGULAR_DETERMINERS = {"a", "an", "this", "that"}
_DETERMINER_SKIP = {"few"}

# Stated tasks are free text from the agent; cap before any analysis so an
# oversized task cannot blow the latency budget. 2k chars is far more
# than any real intent description; the rest is irrelevant to the heuristics.
_MAX_TASK_CHARS = 2000

# Numbers in a task mean different things by context. A number before a duration
# unit is a time window, not a row count. A number after a singular noun / id
# word is a specific entity (single scope). A number before a plural noun is a
# requested count.
_DURATION_UNITS = {
    "second",
    "seconds",
    "minute",
    "minutes",
    "hour",
    "hours",
    "day",
    "days",
    "week",
    "weeks",
    "month",
    "months",
    "year",
    "years",
    "time",
    "times",
}
_ID_PRECEDERS = {"id", "ids", "no", "number", "row", "record", "entry", "item"}
# Words that precede a number but are NOT a singular noun (so don't imply an id).
_NON_NOUN_PREV = {
    "than",
    "last",
    "first",
    "next",
    "past",
    "over",
    "under",
    "about",
    "around",
    "only",
    "just",
    "top",
    "up",
    "to",
    "of",
    "for",
    "by",
    "within",
    "after",
    "before",
    "since",
    "most",
    "least",
    "at",
    "more",
    "less",
}

# Destructive idioms that read as writes even though they contain a "read" verb
# ("get rid of"). Checked as substrings before the read-vs-write heuristic.
_WRITE_PHRASES = (
    "get rid of",
    "do away with",
    "clear out",
    "wipe out",
    "take out",
    "blow away",
)

# Statements that destroy a whole table/schema (not just rows). A row-level task
# paired with one of these is a HIGH contradiction.
_SCHEMA_DESTRUCTIVE_STMTS = {"DropStmt", "TruncateStmt", "AlterTableStmt"}
# Row-level DML verbs in a task -- "delete old customer rows" reads as removing
# *rows*, not the table itself.
_ROW_DML_VERBS = {
    "delete",
    "remove",
    "update",
    "insert",
    "add",
    "set",
    "modify",
    "change",
    "archive",
    "deactivate",
    "disable",
    "clear",
    "reset",
}
# Words that acknowledge a schema/table-level operation; their presence means the
# task is NOT purely row-level (so no contradiction with DDL).
_SCHEMA_WORDS = {
    "drop",
    "truncate",
    "alter",
    "rename",
    "create",
    "schema",
    "table",
    "column",
    "index",
    "database",
    "constraint",
}


@dataclass(frozen=True)
class IntentConfig:
    enabled: bool = False
    single_scope_max: int = 10  # a "single/specific" task shouldn't exceed this
    bulk_threshold: int = 1000  # a scope-less task affecting more than this is noted
    confirm_on_high: bool = True  # escalate HIGH mismatch to human confirmation
    llm_enabled: bool = False  # optional async second opinion (off by default)
    llm_timeout_s: float = 10.0  # hard cap on a background assessor call
    llm_max_concurrent: int = 4  # drop new assessor tasks beyond this many in-flight

    @classmethod
    def from_dict(cls, data: dict | None) -> IntentConfig:
        data = data or {}
        return cls(
            enabled=bool(data.get("enabled", False)),
            single_scope_max=int(data.get("single_scope_max", 10)),
            bulk_threshold=int(data.get("bulk_threshold", 1000)),
            confirm_on_high=bool(data.get("confirm_on_high", True)),
            llm_enabled=bool(data.get("llm_enabled", False)),
            llm_timeout_s=float(data.get("llm_timeout_s", 10.0)),
            llm_max_concurrent=int(data.get("llm_max_concurrent", 4)),
        )


@dataclass(frozen=True)
class IntentFlag:
    """Advisory result. ``mismatch=False`` means no contradiction was detected."""

    mismatch: bool
    severity: str  # NONE | LOW | HIGH
    reasons: tuple[str, ...]
    stated_task: str | None = None
    affected_rows: int | None = None

    def to_dict(self) -> dict:
        return {
            "mismatch": self.mismatch,
            "severity": self.severity,
            "reasons": list(self.reasons),
            "stated_task": self.stated_task,
            "affected_rows": self.affected_rows,
        }


_NO_FLAG = IntentFlag(False, NONE, ())
_WORD_RE = re.compile(r"[a-z_]+")
_TOKEN_RE = re.compile(r"[a-z_]+|\d+")  # words AND numbers, in order


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _has_bulk_cue(words: set[str]) -> bool:
    return bool(words & _BULK_CUES)


def _has_write_phrase(text: str) -> bool:
    return any(p in text for p in _WRITE_PHRASES)


def _determiner_single(toks: list[str]) -> bool:
    for det, nxt in zip(toks, toks[1:], strict=False):
        if (
            det in _SINGULAR_DETERMINERS
            and nxt not in _DETERMINER_SKIP
            and nxt.isalpha()
            and not nxt.endswith("s")
        ):
            return True
    return False


def _implied_scope(text: str, words: set[str]) -> tuple[str, int | None]:
    """Infer the task's implied scope: ('bulk'|'single'|'count'|'unknown', n).

    Context-aware about numbers (this is the fix for the "any small number is a
    single-row cue" false positive): durations are ignored, ``noun N`` is a
    specific id (single), and ``N <plural noun>`` is a requested count.
    """
    if _has_bulk_cue(words):
        return ("bulk", None)

    # Parse explicit numeric scope BEFORE single-word cues: an explicit count
    # ("only 50 rows") is more concrete than an ambiguous word like "only"/"just".
    toks = _TOKEN_RE.findall(text.lower())
    counts: list[int] = []
    verbs = _READ_VERBS | _WRITE_WORDS
    for i, tk in enumerate(toks):
        if not tk.isdigit():
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        prev = toks[i - 1] if i > 0 else ""
        if nxt in _DURATION_UNITS:
            continue  # "30 days" -- a time window, not a row scope
        # "<singular noun> N" / "id N" -> a specific entity.
        is_id = prev in _ID_PRECEDERS or (
            prev.isalpha()
            and prev not in _NON_NOUN_PREV
            and prev not in verbs
            and not prev.endswith("s")
        )
        if is_id:
            return ("single", None)
        counts.append(int(tk))  # a count (acknowledged scope), plural noun or not
    if counts:
        return ("count", max(counts))

    if words & _SINGLE_WORDS:
        return ("single", None)
    if _determiner_single(toks):
        return ("single", None)
    return ("unknown", None)


def _mentions_table(words: set[str], table: str) -> bool:
    """Loose match of a table name in the task (handles a trailing plural 's')."""
    name = table.split(".")[-1].lower()
    return name in words or f"{name}s" in words or name.rstrip("s") in words


def check_intent(
    stated_task: str | None,
    classification: Classification,
    affected_rows: int | None,
    config: IntentConfig,
    *,
    table_vocab: frozenset[str] | None = None,
) -> IntentFlag:
    """Detect contradiction between ``stated_task`` and the statement. Pure.

    Only meaningful for writes (the destructive surface). Returns ``_NO_FLAG``
    when disabled, when there's no stated task, or when nothing contradicts.
    Severity HIGH is reserved for clear contradictions worth a human's attention.
    """
    if not config.enabled or not stated_task or not classification.statements:
        return _NO_FLAG
    info = classification.statements[0]
    if info.kind not in (WRITE, DDL):
        return _NO_FLAG

    # Cap the free-text task before analysis to bound the work so an
    # oversized task can't blow the latency budget.
    task = stated_task[:_MAX_TASK_CHARS]
    task_lower = task.lower()
    words = _words(task)
    reasons: list[str] = []
    severity = NONE

    def raise_to(level: str) -> None:
        nonlocal severity
        if level == HIGH or (level == LOW and severity == NONE):
            severity = level

    # 1) Read-only language, but the statement writes. Destructive idioms that
    #    contain a read verb ("get rid of") are not read-only.
    if (
        words & _READ_VERBS
        and not (words & _WRITE_WORDS)
        and not _has_write_phrase(task_lower)
    ):
        reasons.append(
            "stated task uses read-only language but the statement modifies data"
        )
        raise_to(HIGH)

    # 2) Scope/magnitude: a narrow-sounding task with a wide blast radius.
    if affected_rows is not None and affected_rows > config.single_scope_max:
        scope, requested = _implied_scope(task, words)
        if scope == "single":
            reasons.append(
                f"task describes a single/specific change but the statement "
                f"affects {affected_rows} rows"
            )
            raise_to(HIGH)
        elif scope == "count" and affected_rows > requested:
            # Exceeds the count the task actually asked for.
            far_over = affected_rows > max(requested * 10, config.bulk_threshold)
            reasons.append(
                f"task asks for about {requested} rows but the statement affects "
                f"{affected_rows}"
            )
            raise_to(HIGH if far_over else LOW)
        elif scope == "unknown" and affected_rows > config.bulk_threshold:
            reasons.append(
                f"task does not state a broad scope but the statement affects "
                f"{affected_rows} rows"
            )
            raise_to(LOW)

    # 3) Table mismatch: the task names a known table the statement doesn't touch.
    if table_vocab:
        touched = {t.split(".")[-1].lower() for t in info.tables}
        if touched and not any(_mentions_table(words, t) for t in info.tables):
            mentioned = sorted(
                t
                for t in table_vocab
                if t.split(".")[-1].lower() not in touched and _mentions_table(words, t)
            )
            if mentioned:
                # The task names a known table and the statement touches a
                # different one: a clear wrong-target write/DDL -> HIGH.
                reasons.append(
                    f"task references {mentioned[0]!r} but the statement targets "
                    f"{', '.join(sorted(info.tables))}"
                )
                raise_to(HIGH)

    # 4) Schema-destructive DDL vs a row-level task: e.g. the task says "delete
    #    old customer rows" but the statement DROPs/TRUNCATEs the whole table.
    #    Only when the task reads as row-level (a DML verb, no schema word, not a
    #    bulk acknowledgement).
    if (
        info.stmt_type in _SCHEMA_DESTRUCTIVE_STMTS
        and (words & _ROW_DML_VERBS)
        and not (words & _SCHEMA_WORDS)
        and not _has_bulk_cue(words)
    ):
        reasons.append(
            f"statement performs a schema/table-destructive operation "
            f"({info.stmt_type}) but the task describes a row-level change"
        )
        raise_to(HIGH)

    if not reasons:
        return _NO_FLAG
    return IntentFlag(
        mismatch=True,
        severity=severity,
        reasons=tuple(reasons),
        stated_task=task,
        affected_rows=affected_rows,
    )


# --- Optional LLM second opinion (out-of-band, advisory) ---------------------
# A pluggable async assessor. When wired (off by default), the adapter schedules
# this as a fire-and-forget background task that appends to the audit log. It
# never gates or delays the agent's query. A real implementation should use a
# supported language model and feed it the stated task + classification summary.


async def llm_second_opinion(
    stated_task: str | None,
    classification: Classification,
    affected_rows: int | None,
    assessor,
) -> str | None:
    """Ask an out-of-band assessor whether task and statement contradict.

    ``assessor`` is any async callable ``(prompt) -> str``. Returns the opinion
    text (advisory) or ``None``. Never raises onto the caller -- this is a
    best-effort background check.
    """
    if assessor is None or not stated_task:
        return None
    summary = {
        "stated_task": stated_task,
        "operation": classification.kind,
        "tables": (
            list(classification.statements[0].tables)
            if classification.statements
            else []
        ),
        "affected_rows": affected_rows,
    }
    prompt = (
        "You are an advisory reviewer. Does the SQL statement's effect contradict "
        "the agent's stated task? Answer briefly.\n" + str(summary)
    )
    try:
        return await assessor(prompt)
    except Exception as exc:  # advisory only -- never propagate
        return f"llm second opinion unavailable: {type(exc).__name__}"
