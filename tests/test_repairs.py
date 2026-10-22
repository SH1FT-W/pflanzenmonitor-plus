"""Reparaturen: offener KI-Vorschlag erscheint als Eintrag, der Dialog übernimmt oder lehnt ab."""

from __future__ import annotations

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pflanzenmonitor_plus.const import DOMAIN

from .conftest import INSIDE, OUTSIDE, FakeAI, set_limits, setup

pytestmark = pytest.mark.usefixtures("house", "stats", "forecast")

ISSUE = f"suggestion_{INSIDE}"
SUGGESTION = "sensor.schlafzimmer_bogenhanf_ki_vorschlag"


async def open_flow(hass: HomeAssistant, issue_id: str = ISSUE) -> dict:  # type: ignore[type-arg]
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_init(DOMAIN, data={"issue_id": issue_id})  # type: ignore[return-value]


async def choose(hass: HomeAssistant, flow_id: str, step: str) -> dict:  # type: ignore[type-arg]
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_configure(flow_id, {"next_step_id": step})  # type: ignore[return-value]


async def test_issue_and_accept(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, numbers: list[tuple[str, str, float]]
) -> None:
    assert await async_setup_component(hass, "repairs", {})
    await setup(hass, ai_entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)
    assert issue is not None
    assert issue.is_fixable
    assert issue.translation_key == "suggestion"
    assert issue.translation_placeholders == {"plant": "Bogenhanf"}
    # Olivenbaum noch nicht gefragt → kein Eintrag
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"suggestion_{OUTSIDE}") is None

    result = await open_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["accept", "reject"]
    placeholders = result["description_placeholders"]
    assert placeholders["plant"] == "Bogenhanf"
    assert placeholders["summary"] == "2 Grenzwerte ändern sich."
    assert placeholders["reasons"] == (
        "- Bodenfeuchte min 25 % → 15 %: Gießt meist bei 16 %.\n"
        "- Bodenfeuchte max 60 % → 45 % (Ziel 40 %, schrittweise)"
    )
    assert placeholders["confidence"] == "mittel"
    assert "höchstens Bodenfeuchte max ±15 Punkte" in placeholders["note"]

    result = await choose(hass, result["flow_id"], "accept")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(numbers) == 2
    state = hass.states.get(SUGGESTION)
    assert state is not None
    assert state.state == "uebernommen"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None


async def test_reject_and_outdated(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    assert await async_setup_component(hass, "repairs", {})
    await setup(hass, ai_entry)

    # Inzwischen von Hand geändert → Dialog bricht mit Erklärung ab, Eintrag bleibt
    set_limits(hass, INSIDE, max_moisture=20)
    result = await open_flow(hass)
    result = await choose(hass, result["flow_id"], "accept")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "suggestion_outdated"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is not None

    result = await open_flow(hass)
    result = await choose(hass, result["flow_id"], "reject")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    state = hass.states.get(SUGGESTION)
    assert state is not None
    assert state.state == "abgelehnt"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None


async def test_stale_dialogs(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    """Eintrag ohne offenen Vorschlag, gelöschte Pflanze, entladene Integration: sauber abbrechen."""
    assert await async_setup_component(hass, "repairs", {})
    await setup(hass, ai_entry)
    advisor = ai_entry.runtime_data.advisor
    assert advisor is not None

    # Vorschlag inzwischen anders erledigt, Eintrag hängt noch (z. B. aus einem alten Lauf)
    flow = await open_flow(hass)
    advisor.records[INSIDE]["state"] = "abgelehnt"
    result = await choose(hass, flow["flow_id"], "accept")
    assert result["reason"] == "no_suggestion"
    result = await open_flow(hass)
    assert result["reason"] == "no_suggestion"

    # Unbekannte Pflanze
    ir.async_create_issue(
        hass, DOMAIN, "suggestion_GONE", is_fixable=True, severity=ir.IssueSeverity.WARNING,
        translation_key="suggestion", translation_placeholders={"plant": "x"}, data={"key": "GONE"},
    )  # fmt: skip
    result = await open_flow(hass, "suggestion_GONE")
    assert result["reason"] == "no_suggestion"

    # Integration entladen → Dialog findet nichts mehr
    advisor.records[INSIDE]["state"] = "offen"
    flow = await open_flow(hass)
    assert await hass.config_entries.async_unload(ai_entry.entry_id)
    result = await choose(hass, flow["flow_id"], "reject")
    assert result["reason"] == "no_suggestion"


async def test_issues_removed_without_ai(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    """KI in den Optionen abgewählt → Vorschlags-Einträge verschwinden; fremde Hinweise bleiben."""
    ir.async_create_issue(
        hass, "andere", "suggestion_x", is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="x"
    )
    await setup(hass, ai_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is not None
    hass.config_entries.async_update_entry(ai_entry, options={})
    assert await hass.config_entries.async_reload(ai_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None
    assert ir.async_get(hass).async_get_issue("andere", "suggestion_x") is not None
