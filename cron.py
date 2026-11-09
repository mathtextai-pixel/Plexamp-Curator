"""
Minimal 5-field cron matcher for the in-container scheduler.

    minute hour day-of-month month day-of-week

Each field supports `*`, numbers, lists (`1,15`), ranges (`1-5`) and steps
(`*/15`, `0-30/10`). Day-of-week accepts 0-7 (0 and 7 = Sunday) or names
(`mon`, `tue`, ...). As in standard cron, when both day-of-month and day-of-week
are restricted, a time matches if EITHER of them matches.
"""

import datetime
from typing import Dict, List, Set, Tuple

_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# (name, min, max, aliases)
_FIELDS: List[Tuple[str, int, int, Dict[str, int]]] = [
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day-of-month", 1, 31, {}),
    ("month", 1, 12, _MONTH_NAMES),
    ("day-of-week", 0, 7, _DOW_NAMES),
]


class CronError(ValueError):
    pass


def _value(token: str, lo: int, hi: int, aliases: Dict[str, int], field: str) -> int:
    t = token.strip().lower()
    if t in aliases:
        return aliases[t]
    if not t.isdigit():
        raise CronError(f"bad {field} value {token!r}")
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(f"{field} value {v} out of range {lo}-{hi}")
    return v


def _parse_field(expr: str, lo: int, hi: int, aliases: Dict[str, int], field: str) -> Set[int]:
    values: Set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty {field} entry")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"bad {field} step {step_s!r}")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, lo, hi, aliases, field), _value(b, lo, hi, aliases, field)
            if start > end:
                raise CronError(f"bad {field} range {part!r}")
        else:
            start = _value(part, lo, hi, aliases, field)
            end = hi if step > 1 else start
        values.update(range(start, end + 1, step))
    return values


class CronSchedule:
    def __init__(self, expr: str):
        parts = (expr or "").split()
        if len(parts) != 5:
            raise CronError(f"expected 5 fields, got {len(parts)} in {expr!r}")
        self.expr = expr
        parsed = [_parse_field(p, lo, hi, al, name) for p, (name, lo, hi, al) in zip(parts, _FIELDS)]
        self.minutes, self.hours, self.days, self.months, dows = parsed
        self.dows = {d % 7 for d in dows}  # 7 -> Sunday (0)
        self._dom_any = parts[2] == "*"
        self._dow_any = parts[4] == "*"

    def matches(self, when: datetime.datetime) -> bool:
        if when.minute not in self.minutes or when.hour not in self.hours or when.month not in self.months:
            return False
        dom_ok = when.day in self.days
        dow_ok = (when.isoweekday() % 7) in self.dows  # Python: Mon=1..Sun=7 -> cron Sun=0
        if self._dom_any and self._dow_any:
            return True
        if self._dom_any:
            return dow_ok
        if self._dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def __repr__(self) -> str:
        return f"CronSchedule({self.expr!r})"
