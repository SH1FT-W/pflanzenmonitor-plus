"""Reine Auswertung: Spielraum an den Grenzen, Zustand, VPD, Lichtsumme."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.pflanzenmonitor_plus import health

LIMITS = health.DEFAULTS | {"min_moisture": 20.0, "max_moisture": 60.0}
ALL = frozenset(health.MEASUREMENTS)


def test_check_hysteresis() -> None:
    assert health.check(19, 20, 60, None) == "Low"
    assert health.check(61, 20, 60, None) == "High"
    # Wer zu niedrig ist, bleibt es bis 5 % über der Grenze (hier 21)
    assert health.check(20.5, 20, 60, "Low") == "Low"
    assert health.check(21.5, 20, 60, "Low") == "ok"
    assert health.check(20.5, 20, 60, "ok") == "ok"
    # Zu hoch: bis 5 % unter der Grenze, höchstens 5 % der Spanne
    assert health.check(58, 20, 60, "High") == "High"
    assert health.check(56, 20, 60, "High") == "ok"
    assert health.check(1010, 0, 1000, "High") == "High"
    assert health.check(940, 0, 1000, "High") == "ok"
    # Ohne Untergrenze bzw. Obergrenze
    assert health.check(5, None, 60, "Low") == "ok"
    assert health.check(70, 20, None, "High") == "ok"


def test_evaluate_rules() -> None:
    values = {"moisture": 10.0, "illuminance": 0.0, "dli": 0.0, "temperature": 50.0}
    result = health.evaluate(values, LIMITS | {"min_illuminance": 100.0}, {}, ALL)
    assert result.state == "problem"
    # Dunkel ist kein Problem (Zustand trotzdem „Low“), DLI 0 = kein Licht gemessen = ok
    assert result.statuses["illuminance"] == "Low"
    assert result.statuses["dli"] == "ok"
    assert result.statuses["vpd"] is None
    assert [p["sensor_type"] for p in result.problems] == ["moisture", "temperature"]
    assert result.problems[0] == {
        "sensor_type": "moisture",
        "status": "Low",
        "current": "10.0",
        "min": "20.0",
        "max": "60.0",
    }
    # Nicht gezählt: Zustand ja, Problem nein
    result = health.evaluate(values, LIMITS, {}, frozenset({"moisture"}))
    assert [p["sensor_type"] for p in result.problems] == ["moisture"]
    assert result.statuses["temperature"] == "High"


def test_evaluate_grace_ppfd_unknown() -> None:
    wet = {"moisture": 80.0}
    assert health.evaluate(wet, LIMITS, {}, ALL).state == "problem"
    # Wartezeit nach dem Gießen: „zu nass“ zählt nicht, der Zustand der Messgröße bleibt „High“
    result = health.evaluate(wet, LIMITS, {}, ALL, grace=True)
    assert result.state == "ok"
    assert result.statuses["moisture"] == "High"
    # Zu trocken zählt auch in der Wartezeit
    assert health.evaluate({"moisture": 5.0}, LIMITS, {}, ALL, grace=True).state == "problem"
    # PPFD-Sensor: keine lx-Prüfung
    result = health.evaluate({"illuminance": 999999.0}, LIMITS, {}, ALL, ppfd_source=True)
    assert result.state is None
    assert result.statuses["illuminance"] is None
    # Nichts bekannt
    assert health.evaluate({}, LIMITS, {}, ALL) == health.Result(None, dict.fromkeys(health.MEASUREMENTS), [])


def test_evaluate_without_limit_text() -> None:
    result = health.evaluate({"co2": 5000.0}, {"max_co2": 2000.0}, {}, ALL)
    assert result.problems[0]["min"] == "unknown"


def test_measured_and_limit_fields() -> None:
    sensors = {"moisture": "s.m", "temperature": "s.t", "humidity": "s.h", "illuminance": "s.l"}
    assert health.measured("vpd", sensors)
    assert health.measured("dli", sensors)
    assert not health.measured("co2", sensors)
    fields = health.limit_fields(sensors)
    assert fields[:2] == ("max_moisture", "min_moisture")
    assert {"max_dli", "min_vpd", "lux_to_ppfd"} <= set(fields)
    assert "max_co2" not in fields
    assert health.limit_fields({"moisture": "s.m"}) == ("max_moisture", "min_moisture")


def test_vpd() -> None:
    assert health.vpd(25.0, 50.0) == pytest.approx(1.58, abs=0.01)
    assert health.vpd(20.0, 100.0) == 0.0


T0 = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)


def test_light_trapezoid_and_hours() -> None:
    light = health.Light()
    health.roll(light, date(2026, 10, 16))
    health.advance(light, T0, 0.0001)
    # 1 Std. von 100 auf 300 µmol: Mittel 200 µmol × 3600 s = 0,72 mol
    health.advance(light, T0 + timedelta(hours=1), 0.0003)
    assert light.today == pytest.approx(0.72)
    # Ohne neue Messung zählt der letzte Wert weiter
    health.advance(light, T0 + timedelta(hours=2), None)
    assert light.total == pytest.approx(0.72 + 1.08)
    assert light.last_ppfd is None
    # Nach einem Wert „None“ zählt nichts, bis wieder einer kommt
    health.advance(light, T0 + timedelta(hours=3), 0.0001)
    assert light.total == pytest.approx(1.8)
    assert light.day_24h == pytest.approx(1.8)
    # Ältere Zeit verändert nichts
    health.advance(light, T0, 0.5)
    assert light.last_t == T0 + timedelta(hours=3)
    # Töpfe älter als 24 Std. fallen weg (ein Abschnitt zählt zur Stunde, in der er beginnt)
    health.advance(light, T0 + timedelta(hours=30), 0.0)
    assert light.hours == {}
    assert light.day_24h == 0.0


def test_light_roll() -> None:
    light = health.Light(today=5.0, day=date(2026, 10, 15))
    health.roll(light, date(2026, 10, 15))
    assert light.today == 5.0
    health.roll(light, date(2026, 10, 16))
    assert (light.yesterday, light.today, light.day) == (5.0, 0.0, date(2026, 10, 16))
    # Tage übersprungen (HA aus): gestern unbekannt = 0
    light.today = 3.0
    health.roll(light, date(2026, 10, 19))
    assert light.yesterday == 0.0


def test_light_dump_load() -> None:
    light = health.Light(total=2.0, today=1.0, day=date(2026, 10, 16), yesterday=4.0, last_t=T0, last_ppfd=0.0002)
    light.hours = {T0.isoformat(): 1.0}
    assert health.load(health.dump(light)) == light
    assert health.load(None) == health.Light()
    broken = health.load({"total": "x", "day": "kaputt", "last_t": "nie", "hours": [1], "yesterday": float("nan")})
    assert broken == health.Light()
    assert health.load({"hours": {"a": 1, "b": "x"}}).hours == {"a": 1.0}
