"""Reparaturen: offener KI-Vorschlag (Grenzwerte, Pflegeplan) mit Dialog zum Übernehmen oder Ablehnen."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir

from .advisor import SUGGESTION_ISSUE, Advisor
from .careplan import CARE_ISSUE, CarePlans
from .const import DOMAIN


def _advisor(hass: HomeAssistant) -> Advisor | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data.advisor if entries else None


class SuggestionRepair(RepairsFlow):
    """Zeigt Zusammenfassung, Gründe und Sicherheit; „Übernehmen“ bzw. „Ablehnen“ wirkt sofort."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        # The suggestion shown in the dialog; a newer one may replace it meanwhile and must not be accepted unseen
        self.asked: Any = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        advisor = _advisor(self.hass)
        if advisor is None:
            return self.async_abort(reason="no_suggestion")
        if self.key not in advisor.coordinator.plants:
            # The plant is gone: this repair can never be fixed, remove it
            ir.async_delete_issue(self.hass, DOMAIN, f"{SUGGESTION_ISSUE}{self.key}")
            return self.async_abort(reason="no_suggestion")
        if (rec := advisor.record(self.key))["state"] != "offen":
            return self.async_abort(reason="no_suggestion")
        self.asked = rec.get("asked")
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
        if advisor.record(self.key).get("asked") != self.asked:
            return self.async_abort(reason="suggestion_outdated")
        try:
            if accept:
                await advisor.async_apply(self.key)
            else:
                await advisor.async_reject(self.key)
        except ServiceValidationError as err:
            # no_suggestion, suggestion_outdated, busy – gleiche Texte wie bei den Knöpfen
            return self.async_abort(reason=err.translation_key or "no_suggestion")
        return self.async_create_entry(data={})


def _plans(hass: HomeAssistant) -> CarePlans | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data.care if entries else None


class CareRepair(RepairsFlow):
    """Suggested care plan: shows the current and the suggested plan; accept writes it into the plant."""

    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key
        self.asked: Any = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        plans = _plans(self.hass)
        if plans is None or self.key not in plans.coordinator.plants or plans.proposal(self.key) is None:
            ir.async_delete_issue(self.hass, DOMAIN, f"{CARE_ISSUE}{self.key}")
            return self.async_abort(reason="no_suggestion")
        self.asked = plans.record(self.key).get("asked")
        return self.async_show_menu(
            step_id="init",
            menu_options=["accept", "reject"],
            description_placeholders=plans.repair_placeholders(self.key),
        )

    async def async_step_accept(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        return await self._run(accept=True)

    async def async_step_reject(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        return await self._run(accept=False)

    async def _run(self, *, accept: bool) -> RepairsFlowResult:
        plans = _plans(self.hass)
        # A newer suggestion (next season) must not be accepted unseen
        if plans is None or plans.record(self.key).get("asked") != self.asked:
            return self.async_abort(reason="suggestion_outdated")
        try:
            await (plans.async_apply(self.key) if accept else plans.async_reject(self.key))
        except ServiceValidationError as err:
            return self.async_abort(reason=err.translation_key or "no_suggestion")
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Dialog für den Hinweis „neuer KI-Vorschlag“ (Grenzwerte oder Pflegeplan)."""
    if issue_id.startswith(CARE_ISSUE):
        return CareRepair(str((data or {}).get("key", "")))
    return SuggestionRepair(str((data or {}).get("key", "")))
