"""Field-aware search syntax parser for the Actions filter bar.

Examples:

    type:OPEN side:BUY                   -> structured filters
    score>70                             -> numeric comparison
    ticket:1001                          -> field by id
    rejected duplicate                   -> two substrings (AND)
    type:OPEN reason:tp1                 -> mixed

Supported fields (case-insensitive on field name; value preserved):

    type        action_type
    status      lifecycle status
    side        BUY / SELL
    ticket      positions.mt5_ticket (joined)
    reason      close_reason / ea_response
    verdict     evaluation.verdict
    score       evaluation.score    (supports > >= < <= = !=)
    mult        sizing.multiplier   (supports > >= < <= = !=)
    pnl         realized_pnl        (supports > >= < <= = !=)

Anything that doesn't match `field:value` or `field<op>number` is treated
as a bare substring matched against type, status, reason, verdict, and
the action's id. Multiple terms compose as AND.

The parser intentionally tolerates malformed input — partial matches
still produce useful filters. A search of "type:OPE" yields an
exact-string field filter that just won't match anything; that's
preferable to silently dropping the field on parse error.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# Fields with categorical (string equality) values.
_STR_FIELDS = frozenset({"type", "status", "side", "reason", "verdict", "ticket"})

# Fields with numeric values (also accept comparison operators).
_NUM_FIELDS = frozenset({"score", "mult", "pnl"})

# Comparison operators in precedence order (longest first so ">=" wins
# before ">" steals the leading char).
_OPS = (">=", "<=", "!=", ">", "<", "=", ":")


@dataclass(frozen=True)
class NumericPredicate:
    """One numeric comparison like `score>=70`."""
    field: str
    op: str          # one of >, >=, <, <=, =, !=
    value: float


@dataclass(frozen=True)
class ParsedQuery:
    """Output of `parse`. Each list is interpreted as AND across its
    members. The proxy applies them in arbitrary order; the result is
    independent of order because each predicate is independent.
    """
    str_filters: dict[str, list[str]] = field(default_factory=dict)
    numeric_filters: list[NumericPredicate] = field(default_factory=list)
    substrings: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return (
            not self.str_filters
            and not self.numeric_filters
            and not self.substrings
        )


# Token shape:
#   - field:value       -> capture group 1='field', 2=':', 3=value
#   - field<op>number   -> same as above with op in {>, >=, <, <=, !=, =}
#   - bare word         -> captured as the whole match
_TOKEN_RE = re.compile(
    r'(?:"(?P<dq>[^"]*)")'              # double-quoted substring
    r'|(?:\'(?P<sq>[^\']*)\')'          # single-quoted substring
    r'|(?P<word>\S+)'                   # plain whitespace-separated token
)


def _split_field_op(token: str) -> tuple[str, str, str] | None:
    """Find the longest matching operator in `token`. Returns
    (field_name, operator, value) or None when `token` is a bare word.

    Searches operators longest-first because '>=' must take priority
    over '>'. The operator must be preceded by a non-empty alphabetic
    field name and followed by at least one char (otherwise it's just
    "field:" with no value — treat as bare).
    """
    for op in _OPS:
        idx = token.find(op)
        if idx <= 0:
            continue
        before = token[:idx]
        after = token[idx + len(op):]
        if not after:
            continue
        if not before.replace("_", "").isalpha():
            continue
        return before.lower(), op, after
    return None


def parse(query: str) -> ParsedQuery:
    """Parse the search string into a `ParsedQuery`.

    Tolerant: malformed tokens become substrings rather than errors.
    Quoted substrings (single or double) preserve whitespace, so
    `reason:"sl too wide"` works.
    """
    if not query or not query.strip():
        return ParsedQuery()
    out = ParsedQuery(str_filters={}, numeric_filters=[], substrings=[])
    for m in _TOKEN_RE.finditer(query.strip()):
        # Quoted substrings bypass field-syntax parsing entirely.
        if m.group("dq") is not None:
            s = m.group("dq").strip()
            if s:
                out.substrings.append(s)
            continue
        if m.group("sq") is not None:
            s = m.group("sq").strip()
            if s:
                out.substrings.append(s)
            continue
        word = m.group("word")
        if not word:
            continue
        parsed = _split_field_op(word)
        if parsed is None:
            out.substrings.append(word)
            continue
        field_name, op, value = parsed
        if field_name in _NUM_FIELDS and op in (">", ">=", "<", "<=", "=", "!="):
            try:
                num = float(value)
            except ValueError:
                # Not a number — fall back to substring.
                out.substrings.append(word)
                continue
            out.numeric_filters.append(NumericPredicate(
                field=field_name,
                op="=" if op == ":" else op,
                value=num,
            ))
            continue
        if field_name in _STR_FIELDS and op == ":":
            out.str_filters.setdefault(field_name, []).append(value)
            continue
        if field_name in _NUM_FIELDS and op == ":":
            # `score:70` — treat as equality.
            try:
                num = float(value)
            except ValueError:
                out.substrings.append(word)
                continue
            out.numeric_filters.append(NumericPredicate(
                field=field_name, op="=", value=num,
            ))
            continue
        # Unknown field — fall back to substring so a typo doesn't
        # silently consume the operator's intent.
        out.substrings.append(word)
    return out


def _compare(actual: float, op: str, target: float) -> bool:
    """One numeric comparison. Used by the proxy when applying
    NumericPredicates to action rows."""
    if op == ">":
        return actual > target
    if op == ">=":
        return actual >= target
    if op == "<":
        return actual < target
    if op == "<=":
        return actual <= target
    if op == "!=":
        return actual != target
    return actual == target


def apply(query: ParsedQuery, fields: dict[str, Any]) -> bool:
    """Test a single row's `fields` dict against the parsed query.

    `fields` keys must match the parser's recognized field names: type,
    status, side, reason, verdict, ticket, score, mult, pnl. Missing
    keys mean the row doesn't satisfy any predicate on that field.

    Returns True when EVERY filter passes (AND semantics).
    """
    if query.is_empty:
        return True

    # String field equality — case-insensitive, value-as-substring per
    # token. `type:OPEN` matches "OPEN" exactly; `reason:tp` matches
    # both "tp1" and "tp2".
    for field_name, values in query.str_filters.items():
        actual = fields.get(field_name)
        if actual is None:
            return False
        actual_l = str(actual).lower()
        if not any(v.lower() in actual_l for v in values):
            return False

    for pred in query.numeric_filters:
        actual = fields.get(pred.field)
        if actual is None or not isinstance(actual, (int, float)):
            return False
        if not _compare(float(actual), pred.op, pred.value):
            return False

    if query.substrings:
        # Build a haystack of all stringy fields. Bare substrings
        # match anywhere across the row's text content.
        haystack = " ".join(
            str(v) for k, v in fields.items()
            if v is not None and not isinstance(v, (list, dict))
        ).lower()
        for s in query.substrings:
            if s.lower() not in haystack:
                return False

    return True
