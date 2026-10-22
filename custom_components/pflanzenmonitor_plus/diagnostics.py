"""Diagnose: je Pflanze Einstellungen, Grenzwerte, Live-Stand und Gelerntes (freie KI-Texte geschwärzt)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import PlusConfigEntry

REDACT = {"summary", "ai_summary", "reason", "error"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PlusConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    plants: dict[str, Any] = {}
    for key, plant in coordinator.plants.items():
        item = data.get(key)
        learned = asdict(item.learned) if item else None
        if learned is not None:
            learned["rises"] = [
                {"start": r["start"].isoformat(), "before": r["before"], "peak": r["peak"], "kind": r["kind"]}
                for r in learned["rises"]
            ]
        live = coordinator.live.plants[key]
        plants[key] = {
            "plant": asdict(plant) | {"problems": sorted(plant.problems)},
            "limits": coordinator.limits.plant(key),
            "live": live.dump() | {"state": live.result.state, "problems": live.result.problems},
            "source": coordinator.sources.get(plant.moisture or ""),
            "hours": coordinator.hours(key),
            "moisture": item.moisture if item else None,
            "learned": learned,
            "rain": asdict(item.rain) if item and item.rain else None,
            "frost": asdict(item.frost) if item and item.frost else None,
            # Freie Texte (KI-Begründungen, Fehler) können Namen enthalten – geschwärzt
            "advice": async_redact_data(coordinator.advisor.record(key), REDACT) if coordinator.advisor else None,
        }
    return {"options": dict(entry.options), "migrated": entry.data.get("migrated"), "plants": plants}
