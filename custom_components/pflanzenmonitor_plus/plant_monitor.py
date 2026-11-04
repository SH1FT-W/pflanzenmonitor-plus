"""Plant Monitor (HACS, Olen/homeassistant-plant, domain `plant`) next to Plus: notices only, nothing is taken over.

Plus never reads, changes or removes anything of Plant Monitor (hacs/default review). Moving over is done by hand
(README, "Moving from Plant Monitor"). Two notices under Repairs:
- Plant Monitor is still set up: link to the guide.
- After a move with an earlier version (up to 26.11.2, which moved plants itself): automations and scripts that still
  use an old `plant.*` entity or an old device. Plus only reads its own entry data for that.
"""

from __future__ import annotations

from typing import Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, LEGACY_DOMAIN

ISSUE_INSTALLED: Final = "plant_monitor_installed"
ISSUE_REFERENCES: Final = "references"
# Fixable "move plants over" notice of earlier versions, removed at setup
OLD_ISSUE_MIGRATE: Final = "migrate"
GUIDE_URL: Final = "https://github.com/SH1FT-W/pflanzenmonitor-plus#moving-from-plant-monitor"
# In the entry (written by the move of earlier versions): old entity ID of the plant → new, old device ID → new
DATA_MIGRATED: Final = "migrated"
DATA_MIGRATED_DEVICES: Final = "migrated_devices"


def plant_monitor_set_up(hass: HomeAssistant) -> bool:
    """Is Plant Monitor set up? Only whether config entries of its domain exist, nothing of them is read.

    Home Assistant's own `plant` integration is YAML only and has no config entries.
    """
    return bool(hass.config_entries.async_entries(LEGACY_DOMAIN))


@callback
def sync_issues(hass: HomeAssistant, entry_data: dict[str, object]) -> None:
    """Notices: Plant Monitor still set up; automations that still use old plant.* IDs."""
    ir.async_delete_issue(hass, DOMAIN, OLD_ISSUE_MIGRATE)
    if plant_monitor_set_up(hass):
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_INSTALLED,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_INSTALLED,
            learn_more_url=GUIDE_URL,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_INSTALLED)
    migrated = entry_data.get(DATA_MIGRATED)
    moved = entry_data.get(DATA_MIGRATED_DEVICES)
    lines = _references(
        hass, dict(migrated) if isinstance(migrated, dict) else {}, dict(moved) if isinstance(moved, dict) else {}
    )
    if lines:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_REFERENCES,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_REFERENCES,
            translation_placeholders={"references": "\n".join(lines)},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_REFERENCES)


def _references(hass: HomeAssistant, migrated: dict[str, str], moved: dict[str, str]) -> list[str]:
    """Automations and scripts that still use an old plant.* entity or an old device.

    Home Assistant cannot search dashboards.
    """
    found: list[str] = []
    for component in ("automation", "script"):
        if component not in hass.config.components:
            continue
        if component == "automation":
            from homeassistant.components.automation import (
                automations_with_device as with_device,
                automations_with_entity as with_entity,
            )
        else:
            from homeassistant.components.script import (
                scripts_with_device as with_device,
                scripts_with_entity as with_entity,
            )
        pairs = [(with_entity(hass, old), f"{old} → {new}") for old, new in migrated.items()]
        pairs += [(with_device(hass, old), f"device_id {old} → {new}") for old, new in moved.items()]
        for users, what in pairs:
            for user in users:
                state = hass.states.get(user)
                found.append(f"- {state.name if state else user}: {what}")
    return sorted(set(found))
