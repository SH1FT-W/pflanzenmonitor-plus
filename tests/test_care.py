"""Care plan without HA: plan from stored values, season, next dates, texts, AI answer."""

from __future__ import annotations

from datetime import date

from custom_components.pflanzenmonitor_plus import advice, care

NORTH = care.default(south=False)
SOUTH = care.default(south=True)


def test_plan_from_values() -> None:
    assert care.Plan(21, 3, 9, 2) == NORTH
    assert care.Plan(21, 9, 3, 2) == SOUTH
    # Missing and broken values fall back to the default, out-of-range values are clamped, strings from forms work
    assert care.plan({}, south=False) == NORTH
    assert care.plan({"fertilize_interval": "kaputt", "fertilize_from": "4", "repot_years": 99}, False) == care.Plan(
        21, 4, 9, 10
    )
    assert care.plan({"fertilize_interval": 2.4, "fertilize_to": float("nan")}, True) == care.Plan(7, 9, 3, 2)
    assert NORTH.as_data() == {"fertilize_interval": 21, "fertilize_from": 3, "fertilize_to": 9, "repot_years": 2}


def test_season() -> None:
    assert care.in_season(date(2026, 3, 1), NORTH)
    assert care.in_season(date(2026, 9, 30), NORTH)
    assert not care.in_season(date(2026, 10, 1), NORTH)
    # Southern hemisphere: wraps over new year
    assert care.in_season(date(2026, 12, 24), SOUTH)
    assert care.in_season(date(2026, 2, 1), SOUTH)
    assert not care.in_season(date(2026, 6, 1), SOUTH)


def test_next_fertilize() -> None:
    today = date(2026, 6, 10)
    # Never fertilized: today in season, next season start outside it
    assert care.next_fertilize(None, today, NORTH) == today
    assert care.next_fertilize(None, date(2026, 10, 16), NORTH) == date(2027, 3, 1)
    assert care.next_fertilize(None, date(2026, 1, 5), NORTH) == date(2026, 3, 1)
    # Interval within the season; overdue stays on today
    assert care.next_fertilize(date(2026, 6, 1), today, NORTH) == date(2026, 6, 22)
    assert care.next_fertilize(date(2026, 5, 1), today, NORTH) == today
    # Next date beyond the season: winter pause
    assert care.next_fertilize(date(2026, 9, 20), date(2026, 9, 25), NORTH) == date(2027, 3, 1)
    assert care.next_fertilize(date(2026, 3, 20), date(2026, 3, 25), SOUTH) == date(2026, 9, 1)


def test_next_repot() -> None:
    # Never repotted: next spring; in the spring month itself today
    assert care.next_repot(None, date(2026, 10, 16), NORTH) == date(2027, 3, 1)
    assert care.next_repot(None, date(2026, 3, 12), NORTH) == date(2026, 3, 12)
    assert care.next_repot(None, date(2026, 1, 2), NORTH) == date(2026, 3, 1)
    # N years after the last repotting, in spring; overdue outside spring: next spring
    assert care.next_repot(date(2025, 4, 2), date(2026, 10, 16), NORTH) == date(2027, 3, 1)
    assert care.next_repot(date(2023, 4, 2), date(2026, 10, 16), NORTH) == date(2027, 3, 1)
    assert care.next_repot(date(2023, 4, 2), date(2026, 3, 20), NORTH) == date(2026, 3, 20)
    assert care.next_repot(date(2026, 10, 1), date(2026, 10, 16), SOUTH) == date(2028, 9, 1)


def test_dates() -> None:
    today = date(2026, 8, 20)
    fertilize = list(care.dates("fertilized", date(2026, 8, 10), today, NORTH, date(2027, 4, 1)))
    assert fertilize == [date(2026, 8, 31), date(2026, 9, 21), date(2027, 3, 1), date(2027, 3, 22)]
    repot = list(care.dates("repotted", None, today, NORTH, date(2029, 12, 31)))
    assert repot == [date(2027, 3, 1), date(2029, 3, 1)]
    # Never further than the horizon, however far the calendar asks
    far = list(care.dates("fertilized", None, today, NORTH, date(2100, 1, 1)))
    assert far[-1] <= today + care.HORIZON
    assert list(care.dates("repotted", None, today, NORTH, date(2026, 9, 1))) == []


def test_season_key() -> None:
    assert care.season_key(date(2026, 1, 15)) == "2026-0"
    assert care.season_key(date(2025, 12, 15)) == "2026-0"
    assert care.season_key(date(2026, 3, 1)) == "2026-1"
    assert care.season_key(date(2026, 8, 31)) == "2026-2"
    assert care.season_key(date(2026, 10, 16)) == "2026-3"


def test_texts() -> None:
    assert care.summary("fertilized", "Bogenhanf", "de") == "Bogenhanf düngen"
    assert care.summary("repotted", "Ivy", "en-GB") == "Repot Ivy"
    assert care.summary("repotted", "Ivy", "fr") == "Repot Ivy"
    assert care.describe(NORTH, "de") == (
        "Düngen März bis September alle 21 Tage, im Winter Pause; Umtopfen alle 2 Jahre im März."
    )
    assert care.describe(SOUTH, "en") == (
        "Fertilize September to March every 21 days, pause in winter; repot every 2 years in September."
    )
    assert care.detail("fertilized", NORTH, "en") == "Fertilize March to September every 21 days, pause in winter."
    assert care.detail("repotted", NORTH, "en") == "Repot every 2 years in March."


def test_ai_request_and_answer() -> None:
    structure = care.structure()
    assert set(structure) == {*care.FIELDS, "reason"}
    assert structure["fertilize_from"]["selector"]["number"]["max"] == 12
    text = care.instructions({"plant_name": "Bogenhanf", "hemisphere": "north"}, "de", advice.LANGUAGES)
    assert "German" in text
    assert '"plant_name": "Bogenhanf"' in text
    assert "English" in care.instructions({}, "xx", advice.LANGUAGES)

    answer = {"fertilize_interval": 200, "fertilize_from": 0, "fertilize_to": 8.6, "repot_years": 3, "reason": " ok "}
    assert care.sanitize(answer) == (care.Plan(90, 1, 9, 3), "ok")
    assert care.sanitize({**answer, "reason": None}) == (care.Plan(90, 1, 9, 3), "")
    assert care.sanitize({**answer, "repot_years": None}) is None
    assert care.sanitize("kaputt") is None
