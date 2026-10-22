"""KI-Vorschläge im Test-Haus: fragen, übernehmen, ablehnen, zurücknehmen, Fehler, Sperre, Speicher."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.pflanzenmonitor_plus.advisor import EVENT_SUGGESTION, Advisor
from custom_components.pflanzenmonitor_plus.const import DOMAIN

from .conftest import INSIDE, OUTSIDE, FakeAI, add_plant, now, pending, set_limits, setup

pytestmark = pytest.mark.usefixtures("house", "stats", "forecast")

SUGGESTION = "sensor.schlafzimmer_bogenhanf_ki_vorschlag"
OLIVE = "sensor.terrasse_olivenbaum_ki_vorschlag"
ACCEPT = "button.schlafzimmer_bogenhanf_vorschlag_ubernehmen"
REJECT = "button.schlafzimmer_bogenhanf_vorschlag_ablehnen"
UNDO = "button.schlafzimmer_bogenhanf_vorschlag_zurucknehmen"
ASK = "button.schlafzimmer_bogenhanf_ki_jetzt_fragen"


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def next_round(hass: HomeAssistant, minutes: int) -> None:
    async_fire_time_changed(hass, now() + timedelta(minutes=minutes))
    await hass.async_block_till_done(wait_background_tasks=True)


def advisor_of(entry: MockConfigEntry) -> Advisor:
    advisor = entry.runtime_data.advisor
    assert advisor is not None
    return advisor


def state_of(hass: HomeAssistant, entity_id: str) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_ask_one_per_round_accept_undo(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, numbers: list[tuple[str, str, float]]
) -> None:
    events: list[Event[dict[str, Any]]] = async_capture_events(hass, EVENT_SUGGESTION)
    await setup(hass, ai_entry)

    # Erster Durchlauf: genau eine Pflanze gefragt, mit allen Zahlen und den Schrittgrenzen im Auftrag
    assert len(ai.calls) == 1
    call = ai.calls[0]
    assert call["entity_id"] == "ai_task.claude"
    assert "Bogenhanf" in call["task_name"]
    assert "German" in call["instructions"]
    assert '"usually_watered_at_moisture"' in call["instructions"]
    assert '"air_temperature": {' in call["instructions"]
    assert "min_moisture" in call["structure"]

    # Lesbar statt verschachtelt; Zusammenfassung aus den Werten NACH den Schutzregeln (max: 40 gewollt, 45 erlaubt)
    state = state_of(hass, SUGGESTION)
    assert state.state == "offen"
    assert state.attributes["summary"] == (
        "Bodenfeuchte min 25 % → 15 %, Bodenfeuchte max 60 % → 45 % (Ziel 40 %, schrittweise)."
    )
    assert state.attributes["min_moisture"] == "25 % → 15 %"
    assert state.attributes["min_moisture_reason"] == "Gießt meist bei 16 %."
    assert state.attributes["max_moisture"] == "60 % → 45 % (Ziel 40 %, schrittweise)"
    assert "max_moisture_reason" not in state.attributes  # leerer Grund wird weggelassen
    assert "min_temperature" not in state.attributes  # 2 → 2: keine Änderung
    assert "error" not in state.attributes
    # Keine Art eingetragen → höchstens „mittel“
    assert state.attributes["confidence"] == "medium"
    assert len(events) == 1
    assert events[0].data["entity_id"] == "sensor.schlafzimmer_bogenhanf"
    assert events[0].data["summary"] == state.attributes["summary"]

    # Knöpfe nur, wenn sie etwas bewirken
    assert state_of(hass, ACCEPT).state != "unavailable"
    assert state_of(hass, REJECT).state != "unavailable"
    assert state_of(hass, UNDO).state == "unavailable"

    # Zweiter Durchlauf: die nächste Pflanze; danach nichts mehr (offen bzw. gerade gefragt)
    await next_round(hass, 11)
    assert len(ai.calls) == 2
    assert state_of(hass, OLIVE).state == "offen"
    await next_round(hass, 22)
    assert len(ai.calls) == 2

    await press(hass, ACCEPT)
    assert sorted(numbers) == [(INSIDE, "max_moisture", 45.0), (INSIDE, "min_moisture", 15.0)]
    # Steht auch in den Grenzwert-Entitäten
    assert state_of(hass, "number.schlafzimmer_bogenhanf_min_bodenfeuchtigkeit").state == "15.0"
    state = state_of(hass, SUGGESTION)
    assert state.state == "uebernommen"
    assert "applied" in state.attributes
    # Begrenzter Schritt übernommen → nächster Schritt schon in 7 statt 30 Tagen
    assert state.attributes["next_check"].startswith("2026-10-23")
    assert state_of(hass, ACCEPT).state == "unavailable"
    assert state_of(hass, UNDO).state != "unavailable"
    with pytest.raises(ServiceValidationError) as err:
        await advisor_of(ai_entry).async_apply(INSIDE)
    assert err.value.translation_key == "no_suggestion"

    # Zurücknehmen stellt die alten Werte her; danach ist nichts mehr zurückzunehmen
    numbers.clear()
    await press(hass, UNDO)
    assert sorted(numbers) == [(INSIDE, "max_moisture", 60.0), (INSIDE, "min_moisture", 25.0)]
    assert state_of(hass, SUGGESTION).state == "zurueckgenommen"
    with pytest.raises(ServiceValidationError) as err:
        await advisor_of(ai_entry).async_undo(INSIDE)
    assert err.value.translation_key == "nothing_to_undo"


async def test_reject_and_ask_again_after_a_month(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, freezer: Any
) -> None:
    await setup(hass, ai_entry)
    await press(hass, REJECT)
    assert state_of(hass, SUGGESTION).state == "abgelehnt"
    assert state_of(hass, REJECT).state == "unavailable"
    with pytest.raises(ServiceValidationError):
        await advisor_of(ai_entry).async_reject(INSIDE)

    await next_round(hass, 11)  # Olive
    calls = len(ai.calls)
    await next_round(hass, 22)
    assert len(ai.calls) == calls

    # Nach 30 Tagen wird automatisch neu gefragt
    freezer.move_to(now() + timedelta(days=31))
    async_fire_time_changed(hass, now() + timedelta(days=31))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(ai.calls) == calls + 1


async def test_nothing_to_change(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    events = async_capture_events(hass, EVENT_SUGGESTION)
    pending(hass, INSIDE)["species"] = "Dracaena trifasciata"
    ai.answer = {
        "min_moisture": 25,
        "max_moisture": 60,
        "min_temperature": 2,
        "confidence": "high",
        "summary": "Untergrenze auf 99 % anpassen.",  # wird nicht übernommen
    }
    await setup(hass, ai_entry)
    state = state_of(hass, SUGGESTION)
    assert state.state == "keine_aenderung"
    assert state.attributes["summary"] == "Grenzwerte passen."
    assert state.attributes["confidence"] == "high"  # Art eingetragen → bleibt „hoch“
    assert not events


async def test_species_goes_to_ai(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    pending(hass, INSIDE)["species"] = " Dracaena trifasciata "
    await setup(hass, ai_entry)
    assert '"species": "Dracaena trifasciata"' in ai.calls[0]["instructions"]
    assert state_of(hass, SUGGESTION).attributes["confidence"] == "high"


async def test_ai_errors(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    ai.error = HomeAssistantError("Kontingent erschöpft")
    await setup(hass, ai_entry)
    state = state_of(hass, SUGGESTION)
    assert state.state == "keiner"
    assert state.attributes["error"] == "Kontingent erschöpft"
    # Erneut erst nach 6 Std.; inzwischen die andere Pflanze (die ebenfalls scheitert), dann Ruhe
    await next_round(hass, 11)
    assert ai.calls[-1]["task_name"].endswith("Olivenbaum")
    await next_round(hass, 22)
    assert len(ai.calls) == 2

    # Knopf zeigt den Fehler an
    with pytest.raises(HomeAssistantError) as err:
        await press(hass, ASK)
    assert err.value.translation_key == "ask_failed"

    # Unbrauchbare Antwort zählt als Fehler
    ai.error = None
    ai.answer = "kaputt"
    with pytest.raises(HomeAssistantError):
        await press(hass, ASK)

    # Danach klappt es per Knopf sofort
    ai.answer = {"min_moisture": 15, "max_moisture": 60, "confidence": "medium", "summary": "Etwas höher."}
    await press(hass, ASK)
    state = state_of(hass, SUGGESTION)
    assert state.state == "offen"
    assert "error" not in state.attributes


async def test_unexpected_error_does_not_loop(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    """Auch Fehler von außerhalb HA (Zeitüberschreitung beim Anbieter) sperren 6 Std. – keine Kostenschleife."""
    ai.error = TimeoutError()
    await setup(hass, ai_entry)
    assert state_of(hass, SUGGESTION).attributes["error"] == "TimeoutError"
    await next_round(hass, 11)
    await next_round(hass, 22)
    await next_round(hass, 33)
    assert len(ai.calls) == 2  # Bogenhanf + Olivenbaum, dann nichts mehr


async def test_hanging_ai_hits_timeout(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, freezer: Any
) -> None:
    """Antwortet der Anbieter nie, endet die Anfrage nach AI_TIMEOUT mit Fehler statt die Pflanze ewig zu sperren."""
    from custom_components.pflanzenmonitor_plus.advisor import AI_TIMEOUT

    await setup(hass, ai_entry)
    ai.hang = True
    asking = hass.async_create_task(
        hass.services.async_call("button", "press", {"entity_id": ASK}, blocking=True), eager_start=False
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not asking.done() and advisor_of(ai_entry)._lock.locked()
    # Die eingefrorene Uhr steht auch für den Event-Loop: erst der Zeitsprung lässt die Zeitgrenze ablaufen
    freezer.tick(timedelta(seconds=AI_TIMEOUT + 1))
    with pytest.raises(HomeAssistantError) as err:
        await asking
    assert err.value.translation_key == "ask_failed"
    assert state_of(hass, SUGGESTION).attributes["error"] == "TimeoutError"
    assert not advisor_of(ai_entry)._lock.locked()
    ai.hang = False


async def test_ask_without_data(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    add_plant(hass, "PLANTEMPTY001", "Monstera", "monstera", "Flur")
    await setup(hass, ai_entry)
    with pytest.raises(ServiceValidationError) as err:
        await press(hass, "button.flur_monstera_ki_jetzt_fragen")
    assert err.value.translation_key == "no_data"


async def test_busy(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    """Nie zwei Anfragen oder Anfrage + Übernehmen gleichzeitig."""
    await setup(hass, ai_entry)
    advisor = advisor_of(ai_entry)
    async with advisor._lock:
        for action in (advisor.async_ask, advisor.async_apply, advisor.async_reject, advisor.async_undo):
            with pytest.raises(ServiceValidationError) as err:
                await action(OUTSIDE)
            assert err.value.translation_key == "busy"
        await next_round(hass, 11)
    assert len(ai.calls) == 1


async def test_outdated_and_changed_since(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, numbers: list[tuple[str, str, float]]
) -> None:
    """Schutzregeln gelten gegen die Werte von JETZT – auch beim Übernehmen und Zurücknehmen."""
    await setup(hass, ai_entry)
    advisor = advisor_of(ai_entry)
    coordinator = ai_entry.runtime_data
    learning = replace(coordinator.data[INSIDE], learned=replace(coordinator.data[INSIDE].learned, stage="lernt"))
    assert not advisor._due(INSIDE, learning, now())

    # Obergrenze von Hand auf 20: Vorschlag 45 wäre jetzt ein zu großer Schritt
    set_limits(hass, INSIDE, max_moisture=20)
    numbers.clear()
    with pytest.raises(ServiceValidationError) as err:
        await press(hass, ACCEPT)
    assert err.value.translation_key == "suggestion_outdated"
    assert not numbers

    # Wieder wie vorher → Übernehmen geht; danach von Hand geändert → Zurücknehmen würde das überschreiben
    set_limits(hass, INSIDE, max_moisture=60)
    await press(hass, ACCEPT)
    set_limits(hass, INSIDE, min_moisture=18)
    with pytest.raises(ServiceValidationError) as err:
        await press(hass, UNDO)
    assert err.value.translation_key == "changed_since"


async def test_store_survives_reload_and_remove(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    await setup(hass, ai_entry)
    assert await hass.config_entries.async_reload(ai_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state_of(hass, SUGGESTION).state == "offen"
    advisor = advisor_of(ai_entry)
    assert set(advisor.records) <= {INSIDE, OUTSIDE}
    # Kaputte oder fremde Einträge zählen als „keiner“
    advisor.records[INSIDE] = {"state": "komisch", "changes": "x"}
    assert advisor.record(INSIDE) == {"state": "keiner", "changes": {}, "tuned": []}
    # Unbekannte Grenzwerte in alten Einträgen werden nicht angezeigt
    advisor.records[INSIDE] = {"state": "offen", "changes": {"ph": {"current": 6, "proposed": 7, "reason": ""}}}
    ai_entry.runtime_data.async_update_listeners()
    assert "ph" not in state_of(hass, SUGGESTION).attributes
    # Nach dem Entladen speichert die alte Instanz nichts mehr
    assert await hass.config_entries.async_unload(ai_entry.entry_id)
    await advisor._save()
    assert await hass.config_entries.async_remove(ai_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_without_ai(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    assert hass.states.get(SUGGESTION) is None
    assert hass.states.get(ACCEPT) is None
    advisor = Advisor(hass, entry.runtime_data, None)
    await advisor.async_run_due()
    with pytest.raises(ServiceValidationError) as err:
        await advisor.async_ask(INSIDE)
    assert err.value.translation_key == "no_ai"
    assert advisor.next_check(INSIDE) is None
    assert DOMAIN == "pflanzenmonitor_plus"


async def test_no_suggestion_without_sensor(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    """Ohne Temperatursensor schlägt die KI keine Mindesttemperatur vor."""
    pending(hass, INSIDE).pop("temperature_sensor")
    ai.answer = ai.answer | {"min_temperature": 8}
    await setup(hass, ai_entry)
    state = state_of(hass, SUGGESTION)
    assert "min_temperature" not in state.attributes
    assert "min_moisture" in state.attributes
