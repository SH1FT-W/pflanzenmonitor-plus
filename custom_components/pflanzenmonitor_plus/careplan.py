"""Care calendar: when each plant was last fertilized and repotted, and AI suggestions for its care plan.

The plan itself lives in the plant's subentry (editable in the plant dialog, missing fields = default plan). The store
`pflanzenmonitor_plus.<entry>.care` keeps the dates and the AI suggestion. The AI is asked once per season and plant;
nothing changes until the suggestion is accepted (repair dialog), which writes the plan into the subentry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import advice, care
from .const import DOMAIN
from .plants import Plant

if TYPE_CHECKING:
    from .coordinator import PlusCoordinator

_LOGGER = logging.getLogger(__name__)

CARE_ISSUE: Final = "care_"
# After a failed request wait this long before asking again
RETRY: Final = timedelta(hours=6)
AI_TIMEOUT: Final = 120
# State of the AI suggestion; same words as for the limits
STATES: Final = ("keiner", "offen", "keine_aenderung", "uebernommen", "abgelehnt")
STORE_VERSION: Final = 1


class CarePlans:
    """Dates and AI suggestions of all plants; the calendar and the buttons read from here."""

    def __init__(
        self, hass: HomeAssistant, coordinator: PlusCoordinator, ai: str | None, lock: asyncio.Lock | None = None
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.ai = ai
        entry = coordinator.config_entry
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.care")
        self.records: dict[str, dict[str, Any]] = {}
        self._listeners: list[Callable[[], None]] = []
        # Shared with the limit suggestions (Advisor): never two AI requests at once, also not via "Ask AI now"
        self._lock = lock or asyncio.Lock()
        self._seen: object = None
        self._unloaded = False

    def shutdown(self) -> None:
        """After unloading, save nothing anymore (a new instance is in charge)."""
        self._unloaded = True

    async def async_load(self, keys: set[str]) -> None:
        """Load the stored records; those of removed plants are dropped."""
        stored = await self._store.async_load() or {}
        plants = stored.get("plants") if isinstance(stored, dict) else None
        self.records = {k: v for k, v in (plants or {}).items() if k in keys and isinstance(v, dict)}

    def record(self, key: str) -> dict[str, Any]:
        """Stored record of a plant; unknown or broken states count as "keiner"."""
        rec = dict(self.records.get(key) or {})
        if rec.get("state") not in STATES:
            rec["state"] = "keiner"
        return rec

    @property
    def south(self) -> bool:
        return self.hass.config.latitude < 0

    def plan(self, plant: Plant) -> care.Plan:
        return care.plan(plant.care, self.south)

    def last(self, key: str, kind: care.Kind) -> date | None:
        value = self.record(key).get(kind)
        return dt_util.parse_date(value) if isinstance(value, str) else None

    def upcoming(self, start: date, end: date) -> list[tuple[Plant, care.Kind, date]]:
        """All care dates from `start` up to `end` (exclusive), sorted; nothing lies before today."""
        today = dt_util.now().date()
        found: list[tuple[Plant, care.Kind, date]] = []
        for key, plant in self.coordinator.plants.items():
            p = self.plan(plant)
            for kind in care.KINDS:
                last = self.last(key, kind)
                found.extend(
                    (plant, kind, d) for d in care.dates(kind, last, today, p, end - timedelta(days=1)) if d >= start
                )
        return sorted(found, key=lambda item: (item[2], item[1], item[0].name))

    def next_date(self, key: str, kind: care.Kind) -> date:
        plant = self.coordinator.plants[key]
        today = dt_util.now().date()
        p = self.plan(plant)
        last = self.last(key, kind)
        return care.next_fertilize(last, today, p) if kind == "fertilized" else care.next_repot(last, today, p)

    @callback
    def async_listen(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    async def async_done(self, key: str, kind: care.Kind, day: date | None = None) -> None:
        """Fertilized or repotted (today unless `day`): the next date moves on."""
        if key not in self.coordinator.plants:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="plant_removed")
        day = day or dt_util.now().date()
        if day > dt_util.now().date():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="date_in_future")
        self.records[key] = self.record(key) | {kind: day.isoformat()}
        await self._save()

    async def async_run_due(self) -> None:
        """After new data: ask the AI for at most one plant not asked this season yet."""
        data = self.coordinator.data
        if not self.ai or self._lock.locked() or not data or data is self._seen:
            return
        self._seen = data
        now = dt_util.utcnow()
        season = care.season_key(dt_util.now().date())
        for key in self.coordinator.plants:
            rec = self.record(key)
            retry = dt_util.parse_datetime(rec["retry_after"]) if isinstance(rec.get("retry_after"), str) else None
            if rec.get("season") != season and (retry is None or now >= retry):
                async with self._lock:
                    await self._ask(key, season)
                return

    async def _ask(self, key: str, season: str) -> None:
        plant = self.coordinator.plants[key]
        current = self.plan(plant)
        context = {
            "plant_name": plant.name,
            "species": plant.species,
            "outdoor": plant.outdoor,
            "hemisphere": "south" if self.south else "north",
            "country": self.hass.config.country,
            "month": dt_util.now().strftime("%B"),
            "current_plan": current.as_data(),
        }
        try:
            async with asyncio.timeout(AI_TIMEOUT):
                response = await self.hass.services.async_call(
                    "ai_task",
                    "generate_data",
                    {
                        "task_name": f"Pflanzenmonitor Plus care: {plant.name}",
                        ATTR_ENTITY_ID: self.ai,
                        "instructions": care.instructions(context, self.hass.config.language, advice.LANGUAGES),
                        "structure": care.structure(),
                    },
                    blocking=True,
                    return_response=True,
                )
            result = care.sanitize((response or {}).get("data"))
            if result is None:
                raise HomeAssistantError("unusable answer")
        except Exception as err:  # noqa: BLE001 - any failure blocks the plant for RETRY, never ask on every round
            _LOGGER.warning("Care plan suggestion for %s failed: %s", plant.name, err)
            self.records[key] = self.record(key) | {
                "retry_after": (dt_util.utcnow() + RETRY).isoformat(),
                "error": str(err) or type(err).__name__,
            }
            await self._save()
            return
        proposed, reason = result
        self.records[key] = self.record(key) | {
            "state": "offen" if proposed != current else "keine_aenderung",
            "season": season,
            "asked": dt_util.utcnow().isoformat(),
            "proposal": proposed.as_data(),
            "reason": reason,
            "retry_after": None,
            "error": None,
        }
        await self._save()

    def proposal(self, key: str) -> care.Plan | None:
        rec = self.record(key)
        values = rec.get("proposal")
        if rec["state"] != "offen" or not isinstance(values, dict):
            return None
        return care.plan(values, self.south)

    def _not_busy(self) -> None:
        """No decision while a request runs: its answer would replace the suggestion right after."""
        if self._lock.locked():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="busy")

    async def async_apply(self, key: str) -> None:
        """Write the suggested plan into the plant (the integration reloads afterwards)."""
        self._not_busy()
        # No request may start while the plan is written (the reload that follows would cancel it)
        async with self._lock:
            if (proposed := self.proposal(key)) is None:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_suggestion")
            plant = self.coordinator.plants.get(key)
            if plant is None:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_suggestion")
            entry = self.coordinator.config_entry
            if (subentry := entry.subentries.get(plant.subentry_id)) is None:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_suggestion")
            # State first: the update listener reloads eagerly and the new instance reads the stored state
            self.records[key] = self.record(key) | {"state": "uebernommen"}
            await self._save()
            self.hass.config_entries.async_update_subentry(
                entry, subentry, data={**subentry.data, **proposed.as_data()}
            )

    async def async_reject(self, key: str) -> None:
        self._not_busy()
        if self.proposal(key) is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_suggestion")
        self.records[key] = self.record(key) | {"state": "abgelehnt"}
        await self._save()

    async def _save(self) -> None:
        if self._unloaded:
            return
        self.records = {k: v for k, v in self.records.items() if k in self.coordinator.plants}
        await self._store.async_save({"plants": self.records})
        self.sync_issues()
        for listener in list(self._listeners):
            listener()

    def sync_issues(self) -> None:
        """Open suggestion = repair with a dialog to accept or reject; none for other states or removed plants."""
        for key, plant in self.coordinator.plants.items():
            issue_id = f"{CARE_ISSUE}{key}"
            if self.ai and self.record(key)["state"] == "offen":
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=True,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="care",
                    translation_placeholders={"plant": plant.name},
                    data={"key": key},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        for domain, issue_id in list(ir.async_get(self.hass).issues):
            if (
                domain == DOMAIN
                and issue_id.startswith(CARE_ISSUE)
                and issue_id.removeprefix(CARE_ISSUE) not in self.coordinator.plants
            ):
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def repair_placeholders(self, key: str) -> dict[str, str]:
        """Texts for the repair dialog: current plan, suggested plan, reason of the AI."""
        language = self.hass.config.language
        proposed = self.proposal(key)
        plant = self.coordinator.plants[key]
        return {
            "plant": plant.name,
            "current": care.describe(self.plan(plant), language),
            "proposed": care.describe(proposed, language) if proposed else "",
            "reason": str(self.record(key).get("reason") or ""),
        }
