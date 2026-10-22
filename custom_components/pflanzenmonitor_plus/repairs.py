"""Reparaturen: Umzug aus dem Plant Monitor; offener KI-Vorschlag mit Dialog zum Übernehmen oder Ablehnen."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import voluptuous as vol

from .advisor import Advisor
from .const import DOMAIN
from .migrate import ISSUE_MIGRATE, async_migrate, legacy_entries


def _advisor(hass: HomeAssistant) -> Advisor | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data.advisor if entries else None


class SuggestionRepair(RepairsFlow):
    """Zeigt Zusammenfassung, Gründe und Sicherheit; „Übernehmen“ bzw. „Ablehnen“ wirkt sofort."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        advisor = _advisor(self.hass)
        if advisor is None or self.key not in advisor.coordinator.plants:
            return self.async_abort(reason="no_suggestion")
        if advisor.record(self.key)["state"] != "offen":
            return self.async_abort(reason="no_suggestion")
        return self.async_show_menu(
            step_id="init",
            menu_options=["accept", "reject"],
            description_placeholders=advisor.repair_placeholders(self.key),
        )

    async def async_step_accept(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        return await self._run(accept=True)

    async def async_step_reject(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        return await self._run(accept=False)

    async def _run(self, *, accept: bool) -> RepairsFlowResult:
        advisor = _advisor(self.hass)
        if advisor is None:
            return self.async_abort(reason="no_suggestion")
        try:
            if accept:
                await advisor.async_apply(self.key)
            else:
                await advisor.async_reject(self.key)
        except ServiceValidationError as err:
            # no_suggestion, suggestion_outdated, busy – gleiche Texte wie bei den Knöpfen
            return self.async_abort(reason=err.translation_key or "no_suggestion")
        return self.async_create_entry(data={})


class MigrateRepair(RepairsFlow):
    """Pflanzen aus dem Plant Monitor übernehmen: zeigt, was passiert, und zieht nach Bestätigung um."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        # Beim Öffnen kommen die Daten des Hinweises als user_input – erst bestätigen lassen
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        legacy = legacy_entries(self.hass)
        if not entries or not legacy:
            return self.async_abort(reason="nothing_to_migrate")
        if user_input is not None:
            # Eigene Aufgabe: schließt jemand den Dialog, bricht die Anfrage ab, der Umzug aber nicht
            task = self.hass.async_create_task(async_migrate(self.hass, entries[0]), "pflanzenmonitor_plus Umzug")
            try:
                await asyncio.shield(task)
            except HomeAssistantError as err:
                return self.async_abort(reason=err.translation_key or "legacy_not_loaded")
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"plants": ", ".join(sorted(e.title for e in legacy))},
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Dialog für den Umzug bzw. den Hinweis „neuer KI-Vorschlag“."""
    if issue_id == ISSUE_MIGRATE:
        return MigrateRepair()
    return SuggestionRepair(str((data or {}).get("key", "")))
