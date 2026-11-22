"""'last 12 months', '2024', 'Q3 2025', 'since January 2025', '2025-01-01 to 2025-06-30' -> (start, end) dates."""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Optional

_UNITS = {"day": 1, "week": 7, "month": 30, "year": 365}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _months_back(today: date, n: int) -> date:
    y, m = today.year, today.month - n
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, min(today.day, calendar.monthrange(y, m)[1]))


def parse_period(raw: str, today: Optional[date] = None) -> tuple[date, date]:
    """The date range a phrase means. Anything not understood is the last 12 months."""
    today = today or date.today()
    s = (raw or "").strip().lower()
    default = (_months_back(today, 12), today)
    if not s:
        return default
    isos = [date(int(a), int(b), int(c)) for a, b, c in _ISO.findall(s) if 1 <= int(b) <= 12 and 1 <= int(c) <= 31]
    if len(isos) >= 2:
        return min(isos[:2]), max(isos[:2])
    m = re.search(r"\b(?:last|past|previous)\s+(\d+)\s*(day|week|month|year)s?\b", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return ((_months_back(today, n) if unit == "month" else today - timedelta(days=n * _UNITS[unit])), today)
    if re.search(r"\b(last|past|previous)\s+(year|12 months)\b", s):
        return default
    if re.search(r"\blast\s+quarter\b", s):
        return (today - timedelta(days=91), today)
    if re.search(r"\b(ytd|year to date|this year)\b", s):
        return (date(today.year, 1, 1), today)
    q = re.search(r"\bq([1-4])\s*(?:of\s*)?(\d{4})\b", s) or re.search(r"\b(\d{4})\s*q([1-4])\b", s)
    if q:
        a, b = q.groups()
        quarter, year = (int(a), int(b)) if len(a) == 1 else (int(b), int(a))
        start = date(year, 3 * quarter - 2, 1)
        end_m = 3 * quarter
        return start, date(year, end_m, calendar.monthrange(year, end_m)[1])
    mon = re.search(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{4})\b", s)
    if mon and re.search(r"\b(since|from|after)\b", s):
        return date(int(mon.group(2)), _MONTHS[mon.group(1)], 1), today
    if mon:
        y, mm = int(mon.group(2)), _MONTHS[mon.group(1)]
        return date(y, mm, 1), date(y, mm, calendar.monthrange(y, mm)[1])
    yr = re.search(r"\b((?:19|20)\d{2})\b", s)
    if yr:
        y = int(yr.group(1))
        if re.search(r"\b(since|from|after)\b", s):
            return date(y, 1, 1), today
        return date(y, 1, 1), min(date(y, 12, 31), today) if y == today.year else date(y, 12, 31)
    return default
