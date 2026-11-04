"""Plant Monitor next to Plus: only notices, nothing of it is read, changed or removed (hacs/default review).

Up to 26.11.2 a repair moved the plants over and removed them from Plant Monitor. That is gone: moving over is done
by hand (README). What stays: a notice while Plant Monitor is set up, and the notice about automations that still
use old plant.* IDs after a move with an earlier version.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration

from custom_components.pflanzenmonitor_plus.const import CONF_OUTDOOR, DOMAIN

pytestmark = pytest.mark.usefixtures("stats", "forecast")

KEY = "01JPLANT0000000000BOGENHANF"
COMPONENT = Path(__file__).parent.parent / "custom_components" / DOMAIN
GUIDE = "https://github.com/SH1FT-W/pflanzenmonitor-plus#moving-from-plant-monitor"


def plant_monitor_entry(hass: HomeAssistant) -> tuple[MockConfigEntry, str, str]:
    """A plant as Plant Monitor sets it up (entry, device, status entity); returns entry, device ID, entity ID."""
    mock_integration(hass, MockModule("plant"))
    entry = MockConfigEntry(
        domain="plant",
        entry_id=KEY,
        title="Bogenhanf",
        data={"plant_info": {"name": "Bogenhanf", "moisture_sensor": "sensor.bogen_feuchte"}},
        options={"moisture_trigger": True},
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=KEY, identifiers={("plant", KEY)}, name="Bogenhanf")
    entity = er.async_get(hass).async_get_or_create(
        "plant", "plant", KEY, config_entry=entry, device_id=device.id, suggested_object_id="bogenhanf"
    )
    return entry, device.id, entity.entity_id


async def setup_plus(hass: HomeAssistant, **kwargs: Any) -> MockConfigEntry:
    plus = MockConfigEntry(domain=DOMAIN, title="Pflanzenmonitor Plus", **({"data": {}, "options": {}} | kwargs))
    plus.add_to_hass(hass)
    assert await hass.config_entries.async_setup(plus.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return plus


async def test_notice_while_plant_monitor_is_set_up(hass: HomeAssistant) -> None:
    """Notice with the guide, nothing to fix in a dialog; Plant Monitor's entry, device and entity stay as they are."""
    entry, device_id, entity_id = plant_monitor_entry(hass)
    data, options = dict(entry.data), dict(entry.options)
    device_before = dr.async_get(hass).async_get(device_id)
    entity_before = er.async_get(hass).async_get(entity_id)
    # Fixable "move plants over" notice of an earlier version: removed
    ir.async_create_issue(
        hass, DOMAIN, "migrate", is_fixable=True, severity=ir.IssueSeverity.WARNING, translation_key="migrate"
    )
    plus = await setup_plus(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "migrate") is None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "plant_monitor_installed")
    assert issue is not None
    assert not issue.is_fixable
    assert issue.learn_more_url == GUIDE

    assert hass.config_entries.async_entries("plant") == [entry]
    assert (dict(entry.data), dict(entry.options)) == (data, options)
    # Exactly as before (device, registry entry with all its fields)
    assert dr.async_get(hass).async_get(device_id) == device_before
    assert er.async_get(hass).async_get(entity_id) == entity_before
    assert not plus.subentries

    # Plant Monitor gone (removed by hand): the notice goes with the next load
    assert await hass.config_entries.async_remove(entry.entry_id)
    assert await hass.config_entries.async_reload(plus.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "plant_monitor_installed") is None


async def test_no_notice_without_plant_monitor(hass: HomeAssistant) -> None:
    await setup_plus(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "plant_monitor_installed") is None


def test_plant_monitor_only_named_for_the_notice() -> None:
    """The domain "plant" appears in const.py only, and only plant_monitor.py uses it (whether entries exist).

    No code reads Plant Monitor's entries, entities or devices, or removes them.
    """
    for path in COMPONENT.glob("*.py"):
        text = path.read_text()
        # "plant" as a value, not as a key ({"plant": name} placeholders)
        found = re.findall(r'"plant"(?!\s*:)', text)
        assert len(found) == (2 if path.name == "const.py" else 0), path.name  # LEGACY_DOMAIN, SUBENTRY_PLANT
        # No other spelling of the domain, no plant.* entity IDs and no Plant Monitor config data
        assert "'plant'" not in text, path.name
        assert not re.search(r"""["']plant\.""", text), path.name
        assert "plant_info" not in text, path.name
        if path.name not in ("const.py", "plant_monitor.py"):
            assert "LEGACY_DOMAIN" not in text, path.name
    text = (COMPONENT / "plant_monitor.py").read_text()
    assert text.count("LEGACY_DOMAIN") == 2  # import and the one existence check
    assert "async_remove" not in text
    assert "plant_info" not in text


async def test_references_after_an_earlier_move(hass: HomeAssistant) -> None:
    """Moved with an earlier version: automations that still use plant.* or the old device are listed."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "Gießen",
                    "triggers": [{"trigger": "state", "entity_id": "plant.bogenhanf", "to": "problem"}],
                    "actions": [],
                },
                {
                    "alias": "Gerät",
                    "triggers": [{"trigger": "state", "entity_id": "sensor.bogen_feuchte"}],
                    "actions": [{"action": "homeassistant.update_entity", "target": {"device_id": "OLDDEVICE"}}],
                },
                {"alias": "Anderes", "triggers": [{"trigger": "state", "entity_id": "sensor.x"}], "actions": []},
            ]
        },
    )
    await setup_plus(
        hass,
        data={"migrated": {"plant.bogenhanf": "sensor.bogenhanf"}, "migrated_devices": {"OLDDEVICE": "NEWDEVICE"}},
    )
    refs = ir.async_get(hass).async_get_issue(DOMAIN, "references")
    assert refs is not None
    assert refs.translation_placeholders == {
        "references": "- Gerät: device_id OLDDEVICE → NEWDEVICE\n- Gießen: plant.bogenhanf → sensor.bogenhanf"
    }


async def test_no_references_without_automations(hass: HomeAssistant) -> None:
    await setup_plus(hass, data={"migrated": {"plant.bogenhanf": "sensor.bogenhanf"}})
    assert ir.async_get(hass).async_get_issue(DOMAIN, "references") is None


async def test_old_outdoor_list_is_not_followed(hass: HomeAssistant) -> None:
    """Up to 26.10 "outdoors" was a plant.* list in the options: Plus no longer follows renames of those entities,
    and saving the options drops the list."""
    registry = er.async_get(hass)
    registry.async_get_or_create("plant", "plant", KEY, suggested_object_id="bogenhanf")
    plus = await setup_plus(hass, options={CONF_OUTDOOR: ["plant.bogenhanf"]})
    registry.async_update_entity("plant.bogenhanf", new_entity_id="plant.bogen")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert plus.options[CONF_OUTDOOR] == ["plant.bogenhanf"]
    result = await hass.config_entries.options.async_init(plus.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert CONF_OUTDOOR not in plus.options


async def test_care_calendar_off_cleans_up(hass: HomeAssistant) -> None:
    """Care calendar off: its calendar entity is removed, also while Plant Monitor is set up."""
    plant_monitor_entry(hass)
    plus = MockConfigEntry(domain=DOMAIN, title="Pflanzenmonitor Plus", data={}, options={})
    plus.add_to_hass(hass)
    registry = er.async_get(hass)
    calendar = registry.async_get_or_create("calendar", DOMAIN, f"{plus.entry_id}-care", config_entry=plus)
    assert await hass.config_entries.async_setup(plus.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registry.async_get(calendar.entity_id) is None
