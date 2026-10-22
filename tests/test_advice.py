"""Schutzregeln für KI-Antworten und Aufbau der Anfrage."""

from __future__ import annotations

from typing import Any

from custom_components.pflanzenmonitor_plus import advice

NOW: dict[advice.Field, float | None] = {"min_moisture": 10.0, "max_moisture": 40.0, "min_temperature": 10.0}


def answer(**values: Any) -> dict[str, Any]:
    return {
        "min_moisture": 15,
        "max_moisture": 40,
        "min_temperature": 10,
        "reason_min_moisture": " Gießt meist bei 16 %. ",
        "confidence": "high",
        "summary": "Untergrenze anheben.",
        "species": "Dracaena trifasciata",
    } | values


def test_structure_and_instructions() -> None:
    fields = advice.structure()
    assert {"min_moisture", "max_moisture", "min_temperature", "confidence", "summary"} <= set(fields)
    assert fields["species"]["required"] is False
    text = advice.instructions({"plant_name": "Bogenhanf", "month": "October"}, "de")
    assert "German" in text
    assert "at most 15 points" in text
    assert '"plant_name": "Bogenhanf"' in text
    # Gießgewohnheit ist kein Beweis: Art bestimmt die Richtung, Abweichung wird gemeldet
    assert "watering habit is NOT proof" in text
    assert "does not fit the species" in text
    assert "English" in advice.instructions({}, "xx")
    assert "German" in advice.instructions({}, "de-CH")


def test_unusable_answers() -> None:
    assert advice.sanitize(NOW, None) is None
    assert advice.sanitize(NOW, "kaputt") is None
    assert advice.sanitize(NOW, {"min_moisture": "viel"}) is None


def test_accepts_reasonable_change() -> None:
    result = advice.sanitize(NOW, answer())
    assert result is not None
    # Nur was sich ändert; Text getrimmt
    assert list(result.changes) == ["min_moisture"]
    change = result.changes["min_moisture"]
    assert (change.current, change.proposed, change.reason, change.requested) == (
        10.0,
        15.0,
        "Gießt meist bei 16 %.",
        None,
    )
    assert result.confidence == "high"
    assert result.species == "Dracaena trifasciata"


def test_step_range_and_rounding() -> None:
    result = advice.sanitize(NOW, answer(min_moisture=2, max_moisture=99.6, min_temperature=-40))
    assert result is not None
    # min 10 → nicht unter 5 (Bereich); max 40 → höchstens +15; Temperatur höchstens −5
    assert result.changes["min_moisture"].proposed == 5.0
    assert result.changes["max_moisture"].proposed == 55.0
    assert result.changes["min_temperature"].proposed == 5.0
    # Ziel nur, wenn die Schrittgrenze begrenzt (der Bereich begrenzt endgültig: Ziel = Bereichsgrenze)
    assert result.changes["min_moisture"].requested is None
    assert result.changes["max_moisture"].requested == 100.0
    assert result.changes["min_temperature"].requested == -15.0


def test_gap_between_limits() -> None:
    result = advice.sanitize(NOW, answer(min_moisture=25, max_moisture=30))
    assert result is not None
    # Abstand < 10 → beide Feuchtewerte verworfen
    assert "min_moisture" not in result.changes
    assert "max_moisture" not in result.changes


def test_without_current_values_and_defaults() -> None:
    none: dict[advice.Field, float | None] = {"min_moisture": None, "max_moisture": None, "min_temperature": None}
    result = advice.sanitize(none, {"min_moisture": 20, "max_moisture": 60, "confidence": "sehr", "species": " "})
    assert result is not None
    assert result.changes["min_moisture"].proposed == 20.0
    assert result.changes["min_moisture"].reason == ""
    assert result.confidence == "low"
    assert result.species is None
    assert result.ai_summary == ""


def test_nothing_to_change() -> None:
    result = advice.sanitize(NOW, answer(min_moisture=10.4))
    assert result is not None
    assert result.changes == {}


def test_only_settable_fields() -> None:
    result = advice.sanitize(NOW, answer(min_temperature=5), {"min_moisture", "max_moisture"})
    assert result is not None
    assert "min_temperature" not in result.changes
    assert advice.sanitize(NOW, {"min_temperature": 5}, {"min_moisture"}) is None


def test_describe_after_guardrails() -> None:
    result = advice.sanitize(NOW, answer(max_moisture=80, min_temperature=12))
    assert result is not None
    text = advice.describe(result.changes, "de")
    assert (
        text == "Bodenfeuchte min 10 % → 15 %, Bodenfeuchte max 40 % → 55 % (Ziel 80 %, schrittweise), "
        "Temperatur min 10 °C → 12 °C."
    )
    assert advice.describe({}, "de") == "Grenzwerte passen."
    assert advice.describe({}, "fr") == "Limits fit."
    assert advice.step_note(result.changes, "de", 7).startswith(
        "Große Änderungen macht Plus in Schritten (pro Vorschlag höchstens Bodenfeuchte max ±15 Punkte)"
    )
    assert advice.step_note({}, "de", 7) == ""
    new = advice.Change(None, 20.0, "")
    assert advice.value_text("min_moisture", new, "en-GB") == "new 20 %"


def test_non_finite_numbers() -> None:
    assert advice.sanitize(NOW, {"min_moisture": float("nan")}) is None
    assert advice.sanitize(NOW, {"min_moisture": "1e999"}) is None
    assert advice.sanitize(NOW, {"min_moisture": float("inf")}) is None


FULL: dict[advice.Field, float | None] = NOW | {
    "max_temperature": 40.0,
    "min_soil_temperature": 10.0,
    "max_soil_temperature": 40.0,
    "min_humidity": 20.0,
    "max_humidity": 60.0,
    "min_illuminance": 0.0,
    "max_illuminance": 100000.0,
    "min_dli": 2.0,
    "max_dli": 30.0,
}
LIGHT: frozenset[advice.Field] = frozenset({"min_illuminance", "max_illuminance", "min_dli", "max_dli"})


def test_structure_and_instructions_only_asked_fields() -> None:
    fields: tuple[advice.Field, ...] = ("min_humidity", "max_humidity", "min_illuminance", "max_illuminance")
    structure = advice.structure(fields)
    assert {k for k in structure if k.startswith(("min_", "max_"))} == set(fields)
    assert structure["max_illuminance"]["selector"]["number"]["step"] == 100
    text = advice.instructions({}, "en", fields, frozenset(fields))
    # Kein Feuchte-Absatz, dafür Licht und Luftfeuchte; alle frei → keine Schrittgrenzen
    assert "Soil moisture percentages" not in text
    assert "illuminance sensor may not sit at the plant" in text
    assert "heating season" in text
    assert "by at most" not in text
    assert "replace them fully with fitting values: min_humidity, max_humidity" in text
    assert "min_illuminance at least 2000 below max_illuminance" in text
    assert "min_moisture at least" not in text
    # Gemischt: schrittweise und frei
    text = advice.instructions({}, "en", ("min_temperature", "max_temperature"), frozenset({"max_temperature"}))
    assert "min_temperature by at most 5 °C per suggestion" in text
    assert "fitting values: max_temperature." in text
    assert "Temperatures:" in text


def test_free_first_step_rounding_and_range() -> None:
    answer_ = {"max_illuminance": 12345, "min_illuminance": 1100, "max_dli": 7.34, "min_dli": 1.2, "max_humidity": 70}
    result = advice.sanitize(FULL, answer_, free=LIGHT)
    assert result is not None
    # Frei: in einem Schritt, auf 100 lx bzw. 0,1 DLI gerundet; Luftfeuchte ist nicht frei → höchstens +15
    assert result.changes["max_illuminance"] == advice.Change(100000.0, 12300.0, "", None, True)
    assert result.changes["min_illuminance"].proposed == 1100.0
    assert result.changes["max_dli"].proposed == 7.3
    assert result.changes["min_dli"].proposed == 1.2
    assert result.changes["max_humidity"].proposed == 70.0
    # Nicht frei: Schritte begrenzt, Ziel bleibt sichtbar
    result = advice.sanitize(FULL, answer_)
    assert result is not None
    assert result.changes["max_illuminance"].proposed == 80000.0
    assert result.changes["max_illuminance"].requested == 12300.0
    assert result.changes["max_dli"].proposed == 20.0
    # Bereich gilt auch frei
    result = advice.sanitize(FULL, {"max_illuminance": 500, "max_dli": 500}, free=LIGHT)
    assert result is not None
    assert result.changes["max_illuminance"].proposed == 2000.0
    assert result.changes["max_illuminance"].requested is None
    assert result.changes["max_dli"].proposed == 80.0
    # Kleiner als ein Rundungsschritt = keine Änderung
    result = advice.sanitize(FULL, {"max_illuminance": 100040}, free=LIGHT)
    assert result is not None
    assert result.changes == {}


def test_gap_for_every_pair() -> None:
    result = advice.sanitize(
        FULL,
        {"min_humidity": 60, "max_humidity": 65, "min_temperature": 12, "min_dli": 5, "max_dli": 6},
        free=LIGHT | {"min_humidity", "max_humidity"},
    )
    assert result is not None
    assert set(result.changes) == {"min_temperature"}
    # Abstand gegen den bestehenden Wert der anderen Grenze
    result = advice.sanitize(
        FULL | {"min_temperature": 12.0}, {"max_temperature": 13}, free=frozenset({"max_temperature"})
    )
    assert result is not None
    assert "max_temperature" not in result.changes


def test_texts_for_new_fields() -> None:
    change = advice.Change(30.0, 20.0, "Wenig Licht im Winter.", 12.0)
    assert advice.value_text("max_dli", change, "de") == "30 mol/m²/d → 20 mol/m²/d (Ziel 12 mol/m²/d, schrittweise)"
    assert advice.value_text("max_illuminance", advice.Change(None, 25000.0, ""), "en") == "new 25,000 lx"
    assert advice.reasons({"max_dli": change}, "de") == (
        "- Tageslicht max 30 mol/m²/d → 20 mol/m²/d (Ziel 12 mol/m²/d, schrittweise): Wenig Licht im Winter."
    )
    note = advice.step_note({"max_dli": change}, "en", 7)
    assert note.startswith("Plus makes large changes in steps (at most Daily light max ±10 mol/m²/d per suggestion)")
    assert "About light" in note
    assert "first time" not in note
    # Freier Sprung und Licht erklärt, ohne Schritt-Hinweis; Kurzsatz für den Dialog
    free = {"max_illuminance": advice.Change(50000.0, 6000.0, "", None, True)}
    note = advice.step_note(free, "de", 7)
    assert note.startswith("Werte, die Plus zum ersten Mal einstellt, dürfen ganz springen")
    assert "Tageslicht min 0 heißt" in note
    assert advice.value_text("max_illuminance", free["max_illuminance"], "de") == (
        "50.000 lx → 6.000 lx (erstmals eingestellt)"
    )
    assert advice.overview(free, "de") == "1 Grenzwert ändert sich."
    assert advice.overview(free | {"min_dli": change}, "en") == "2 limits change."
    assert advice.overview({}, "en") == "Limits fit."
    assert advice.step_note({"min_humidity": advice.Change(20.0, 35.0, "", 50.0)}, "en", 7).startswith(
        "Plus makes large changes in steps (at most Humidity min ±15 points"
    )
    assert advice.describe({"min_humidity": advice.Change(20.0, 35.0, "")}, "de") == "Luftfeuchte min 20 % → 35 %."
    # Deutsch mit Komma, Englisch mit Punkt
    assert advice.value_text("min_dli", advice.Change(2.0, 0.4, ""), "de") == "2 mol/m²/d → 0,4 mol/m²/d"
    assert advice.value_text("min_dli", advice.Change(2.0, 0.4, ""), "en") == "2 mol/m²/d → 0.4 mol/m²/d"


def test_outdoor_frost_floor_and_summer_light() -> None:
    now: dict[advice.Field, float | None] = {
        "min_temperature": -3.0,
        "max_temperature": 35.0,
        "min_soil_temperature": 2.0,
        "max_soil_temperature": 35.0,
        "max_illuminance": 100000.0,
        "max_dli": 30.0,
    }
    reply = {"min_temperature": -8, "min_soil_temperature": -5, "max_illuminance": 20000, "max_dli": 20}
    every = set(advice.FIELDS)
    # Drinnen gilt nur der normale Bereich und die Schrittgrenze
    inside = advice.sanitize(now, reply, every, frozenset(advice.FIELDS))
    assert inside is not None
    assert inside.changes["min_temperature"].proposed == -8
    assert inside.changes["max_illuminance"].proposed == 20000
    # Draußen im Herbst: Frostgrenze im Kübel, Lichtobergrenzen werden nicht gesenkt (kein Schrittziel)
    autumn = advice.sanitize(now, reply, every, frozenset(advice.FIELDS), outdoor=True)
    assert autumn is not None
    assert autumn.changes["min_temperature"].proposed == -5
    assert autumn.changes["min_temperature"].requested is None
    assert autumn.changes["min_soil_temperature"].proposed == 0
    assert "max_illuminance" not in autumn.changes
    assert "max_dli" not in autumn.changes
    # Anheben geht draußen immer
    brighter = advice.sanitize(now, {"max_illuminance": 120000}, every, frozenset(advice.FIELDS), outdoor=True)
    assert brighter is not None
    assert brighter.changes["max_illuminance"].proposed == 120000
    # Im Sommer kennt der Verlauf die Sonne: Senken erlaubt, Frostgrenze bleibt
    summer = advice.sanitize(now, reply, every, frozenset(advice.FIELDS), outdoor=True, summer=True)
    assert summer is not None
    assert summer.changes["max_illuminance"].proposed == 20000
    assert summer.changes["min_temperature"].proposed == -5


def test_outdoor_instructions() -> None:
    assert "outdoors, most likely in a pot" in advice.instructions({"outdoor": True}, "de")
    assert "most likely in a pot" not in advice.instructions({"outdoor": False}, "de")
