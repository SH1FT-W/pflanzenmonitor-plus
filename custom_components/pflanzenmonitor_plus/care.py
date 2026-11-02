"""Care plan without HA: fertilizing and repotting per plant, next dates, AI request and guard rails.

A plan has a fertilizing season (first to last month, wrapping over new year on the southern hemisphere), the days
between two fertilizings within the season (outside it: winter pause) and the years between two repottings.
Repotting is due in the first month of the season (start of growth, spring).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
import json
import math
from typing import Any, Final, Literal

Kind = Literal["fertilized", "repotted"]
KINDS: Final[tuple[Kind, ...]] = ("fertilized", "repotted")
# Fields of the plan; stored in the plant's subentry (missing = default)
FIELDS: Final = ("fertilize_interval", "fertilize_from", "fertilize_to", "repot_years")
RANGE: Final[dict[str, tuple[int, int]]] = {
    "fertilize_interval": (7, 90),
    "fertilize_from": (1, 12),
    "fertilize_to": (1, 12),
    "repot_years": (1, 10),
}
DEFAULT_INTERVAL: Final = 21
DEFAULT_REPOT: Final = 2
# Never look further ahead than this (the calendar card may ask for years)
HORIZON: Final = timedelta(days=3 * 366)


@dataclass(frozen=True, slots=True)
class Plan:
    """When a plant is fertilized and repotted."""

    interval: int
    start: int
    end: int
    repot: int

    def as_data(self) -> dict[str, int]:
        """Fields as stored in the subentry."""
        return dict(zip(FIELDS, (self.interval, self.start, self.end, self.repot), strict=True))


def default(south: bool) -> Plan:
    """Without species knowledge: March to September (September to March in the south) every 3 weeks, repot every
    2 years."""
    return Plan(DEFAULT_INTERVAL, 9 if south else 3, 3 if south else 9, DEFAULT_REPOT)


def _int(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return round(number) if math.isfinite(number) else None


def _clamp(name: str, value: int) -> int:
    low, high = RANGE[name]
    return min(max(value, low), high)


def plan(values: Mapping[str, Any], south: bool) -> Plan:
    """Plan from stored values; missing or broken fields fall back to the default."""
    base = default(south).as_data()
    for name in FIELDS:
        if (number := _int(values.get(name))) is not None:
            base[name] = _clamp(name, number)
    return Plan(*(base[name] for name in FIELDS))


def in_season(day: date, p: Plan) -> bool:
    if p.start <= p.end:
        return p.start <= day.month <= p.end
    return day.month >= p.start or day.month <= p.end


def _month_on_or_after(day: date, month: int) -> date:
    """`day` itself in that month, else the first of the month next time it comes."""
    if day.month == month:
        return day
    return date(day.year if month > day.month else day.year + 1, month, 1)


def _season_from(day: date, p: Plan) -> date:
    """First day on or after `day` within the season."""
    return day if in_season(day, p) else _month_on_or_after(day, p.start)


def next_fertilize(last: date | None, today: date, p: Plan) -> date:
    """Next fertilizing; overdue stays on today, outside the season it waits for the season."""
    due = today if last is None else max(last + timedelta(days=p.interval), today)
    return _season_from(due, p)


def next_repot(last: date | None, today: date, p: Plan) -> date:
    """Next repotting, always in the first month of the season (overdue outside it: next spring)."""
    due = today if last is None else max(date(last.year + p.repot, p.start, 1), today)
    return _month_on_or_after(due, p.start)


def dates(kind: Kind, last: date | None, today: date, p: Plan, until: date) -> Iterator[date]:
    """The coming dates of one kind up to `until` (inclusive)."""
    until = min(until, today + HORIZON)
    if kind == "fertilized":
        day = next_fertilize(last, today, p)
        while day <= until:
            yield day
            day = _season_from(day + timedelta(days=p.interval), p)
    else:
        day = next_repot(last, today, p)
        while day <= until:
            yield day
            day = date(day.year + p.repot, p.start, 1)


def season_key(day: date) -> str:
    """Meteorological season (Dec-Feb, Mar-May, ...), so the AI is asked once per season."""
    year = day.year + 1 if day.month == 12 else day.year
    return f"{year}-{(day.month % 12) // 3}"


# Texts for events and the repair dialog
TEXTS: Final[dict[str, dict[str, str]]] = {
    "de": {
        "fertilized": "{plant} düngen",
        "repotted": "{plant} umtopfen",
        "fertilize_plan": "Düngen {start} bis {end} alle {interval} Tage, im Winter Pause",
        "repot_plan": "Umtopfen alle {years} Jahre im {start}",
        "plan": "{fertilize}; {repot}.",
        "months": "Januar Februar März April Mai Juni Juli August September Oktober November Dezember",
    },
    "en": {
        "fertilized": "Fertilize {plant}",
        "repotted": "Repot {plant}",
        "fertilize_plan": "Fertilize {start} to {end} every {interval} days, pause in winter",
        "repot_plan": "repot every {years} years in {start}",
        "plan": "{fertilize}; {repot}.",
        "months": "January February March April May June July August September October November December",
    },
}


def _texts(language: str) -> dict[str, str]:
    return TEXTS.get(language.split("-")[0], TEXTS["en"])


def summary(kind: Kind, plant: str, language: str) -> str:
    """Event title, e.g. "Bogenhanf düngen"."""
    return _texts(language)[kind].format(plant=plant)


def describe(p: Plan, language: str) -> str:
    """The whole plan in one sentence."""
    t = _texts(language)
    months = t["months"].split()
    fertilize = t["fertilize_plan"].format(start=months[p.start - 1], end=months[p.end - 1], interval=p.interval)
    repot = t["repot_plan"].format(years=p.repot, start=months[p.start - 1])
    return t["plan"].format(fertilize=fertilize, repot=repot)


def detail(kind: Kind, p: Plan, language: str) -> str:
    """Event description: the half of the plan that belongs to the event."""
    t = _texts(language)
    months = t["months"].split()
    if kind == "fertilized":
        text = t["fertilize_plan"].format(start=months[p.start - 1], end=months[p.end - 1], interval=p.interval)
    else:
        text = t["repot_plan"].format(years=p.repot, start=months[p.start - 1])
    return text[0].upper() + text[1:] + "."


def structure() -> dict[str, Any]:
    """Answer format for ai_task.generate_data."""

    def number(description: str, name: str) -> dict[str, Any]:
        low, high = RANGE[name]
        return {
            "description": description,
            "required": True,
            "selector": {"number": {"min": low, "max": high, "step": 1, "mode": "box"}},
        }

    return {
        "fertilize_interval": number("Days between two fertilizings during the growing season", "fertilize_interval"),
        "fertilize_from": number("First month (1-12) of the fertilizing season", "fertilize_from"),
        "fertilize_to": number("Last month (1-12) of the fertilizing season", "fertilize_to"),
        "repot_years": number("Years between two repottings", "repot_years"),
        "reason": {"description": "One or two short sentences why", "required": True, "selector": {"text": {}}},
    }


def instructions(context: dict[str, Any], language: str, languages: Mapping[str, str]) -> str:
    """Request to the AI: fertilizing season, interval and repotting for this plant."""
    lang = languages.get(language.split("-")[0], "English")
    rules = [
        "You are an experienced horticulturist. Suggest a care plan for a plant in a home: in which months it is "
        "fertilized (growing season; no fertilizer outside it), how many days lie between two fertilizings in that "
        "season, and every how many years it is repotted (in the first month of the season).",
        "Consider the species (from the name if not given), indoor/outdoor and the hemisphere (on the southern "
        "hemisphere the growing season is around September to March; a season may wrap over new year).",
        "Use ordinary liquid fertilizer at the dose on the label as reference. Slow growers and succulents get long "
        "intervals or a short season; fast growers shorter intervals. Keep the current plan when it fits.",
        f"Write the reason in {lang}. Be short and concrete.",
    ]
    return (
        "\n".join(f"- {r}" for r in rules)
        + f"\n\nPlant data (JSON):\n{json.dumps(context, ensure_ascii=False, indent=1)}"
    )


def sanitize(answer: Any) -> tuple[Plan, str] | None:
    """Clamp the answer into the allowed ranges; None if a value is missing or not a number."""
    if not isinstance(answer, dict):
        return None
    values: dict[str, int] = {}
    for name in FIELDS:
        if (number := _int(answer.get(name))) is None:
            return None
        values[name] = _clamp(name, number)
    return Plan(*(values[name] for name in FIELDS)), str(answer.get("reason") or "").strip()
