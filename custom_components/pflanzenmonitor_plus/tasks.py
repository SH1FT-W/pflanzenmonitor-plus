"""Watering reminders as tasks on a to-do list (option `todo_list`, off by default).

Once a plant is due ("Water in" reaches today), one task "<plant> gießen" is added, with the due date if the list
supports it. There is never more than one open task per plant, also across restarts (store
`pflanzenmonitor_plus.<entry>.tasks`, and an open task with the same title on the list is adopted instead of adding a
second one). When the soil moisture rises again (a new rise after the task was added), Plus completes the task. If the
user checks the task off or deletes it, Plus leaves the plant alone until the next watering has been seen.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.todo import TodoItemStatus, TodoListEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import model
from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import PlusCoordinator

_LOGGER = logging.getLogger(__name__)

TODO_DOMAIN: Final = "todo"
STORE_VERSION: Final = 1
TITLES: Final = {"de": "{plant} gießen", "en": "Water {plant}"}
# A slow (cloud) list must not hold up the next runs
TODO_TIMEOUT: Final = 30


def title(plant: str, language: str) -> str:
    return TITLES.get(language.split("-")[0], TITLES["en"]).format(plant=plant)


def latest_rise(learned: model.Learned, history: list[model.Sample]) -> datetime | None:
    """Start of the newest confirmed rise of the soil moisture (watering, rain or unknown).

    Stricter than model.confirmed: the hourly statistics must have an hour after the rise, and that hour's mean must
    still lie clearly above the level before the rise. A single outlier reading can lift the time-weighted mean of
    its own hour, but not the one of the next hour.
    """
    found = None
    for rise in learned.rises:
        after = next((s for s in history if s.t > rise.start), None)
        if after is not None and after.mean >= rise.before + model.JUMP / 2 and (found is None or rise.start > found):
            found = rise.start
    return found


def _time(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


class WateringTasks:
    """One record per plant: the task on the list (`uid`, `summary`), `open` or `closed`, and `cycle` = newest rise
    when the task was added (a later rise means: watered since)."""

    def __init__(self, hass: HomeAssistant, coordinator: PlusCoordinator, todo: str) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.todo = todo
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{coordinator.config_entry.entry_id}.tasks"
        )
        self.records: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._seen: object = None

    async def async_load(self, keys: set[str]) -> None:
        stored = await self._store.async_load() or {}
        plants = stored.get("plants") if isinstance(stored, dict) else None
        # Records of removed plants and of another list (option changed) no longer count
        self.records = {
            k: v for k, v in (plants or {}).items() if k in keys and isinstance(v, dict) and v.get("list") == self.todo
        }

    def _due(self, key: str) -> bool:
        due = self.coordinator.due_date(key)
        return due is not None and dt_util.as_local(due).date() <= dt_util.now().date()

    async def _call(self, service: str, data: dict[str, Any], response: bool = False) -> Any:
        try:
            async with asyncio.timeout(TODO_TIMEOUT):
                return await self.hass.services.async_call(
                    TODO_DOMAIN, service, {ATTR_ENTITY_ID: self.todo, **data}, blocking=True, return_response=response
                )
        except TimeoutError as err:
            raise HomeAssistantError(f"{service} timed out") from err

    async def _items(self) -> list[dict[str, Any]]:
        result = await self._call("get_items", {}, response=True)
        items = ((result or {}).get(self.todo) or {}).get("items")
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    @staticmethod
    def _find(items: list[dict[str, Any]], rec: dict[str, Any]) -> dict[str, Any] | None:
        if rec.get("uid"):
            return next((i for i in items if i.get("uid") == rec["uid"]), None)
        return next(
            (i for i in items if i.get("summary") == rec.get("summary") and i.get("status") != "completed"), None
        )

    async def async_run(self) -> None:
        """After new data: add, complete or let go of tasks."""
        data = self.coordinator.data
        if self._lock.locked() or not data or data is self._seen or self.hass.states.get(self.todo) is None:
            return
        self._seen = data
        async with self._lock:
            try:
                await self._run(data)
            except Exception as err:  # noqa: BLE001 - list unavailable or broken: try again with the next data
                # List unavailable or refuses the change: try again with the next data
                _LOGGER.warning("To-do list %s: %s", self.todo, err)

    async def _run(self, data: dict[str, Any]) -> None:
        wanted = [k for k, p in self.coordinator.plants.items() if p.moisture and k in data]
        if not any(self.records.get(k, {}).get("state") == "open" or self._due(k) for k in wanted):
            return
        items = await self._items()
        changed = False
        for key in wanted:
            try:
                changed |= await self._plant(key, data[key].learned, items)
            except Exception as err:  # noqa: BLE001 - whatever a list integration raises, the other plants go on
                # One plant failing (e.g. the list refuses the change) must not hold up the others
                _LOGGER.warning("To-do list %s, %s: %s", self.todo, self.coordinator.plants[key].name, err)
        if changed:
            await self._store.async_save({"plants": self.records})

    async def _plant(self, key: str, learned: model.Learned, items: list[dict[str, Any]]) -> bool:
        """Handle one plant; True if its record changed."""
        rise = latest_rise(learned, self.coordinator.history(self.coordinator.plants[key].moisture))
        rec = self.records.get(key)
        cycle = _time(rec.get("cycle")) if rec else None
        # Rises within MERGE are one watering in the model; the start of such a group can move when the oldest rise
        # leaves the 120-day window or the history source changes, so only a clearly later rise counts as watering
        watered = rec is not None and rise is not None and (cycle is None or rise > cycle + model.MERGE)
        if rec and rec.get("state") == "open":
            found = self._find(items, rec)
            if watered:
                if found is not None and found.get("status") != TodoItemStatus.COMPLETED:
                    item = found.get("uid") or found.get("summary")
                    await self._call("update_item", {"item": item, "status": TodoItemStatus.COMPLETED})
                rec |= {"state": "closed", "closed_by": "watering"}
                return True
            if found is None or found.get("status") == TodoItemStatus.COMPLETED:
                # Checked off or deleted by the user: respected until the next watering
                rec |= {"state": "closed", "closed_by": "user"}
                return True
            return False
        if not self._due(key) or (rec is not None and not watered):
            return False
        await self._add(key, rise, items)
        return True

    def _taken(self, key: str) -> set[str]:
        """Task ids that belong to other plants (plants with the same name must not share a task)."""
        return {str(r["uid"]) for k, r in self.records.items() if k != key and r.get("uid")}

    def _free(self, items: list[dict[str, Any]], summary: str, taken: set[str]) -> dict[str, Any] | None:
        """Open task with this title that no other plant owns."""
        return next(
            (
                i
                for i in items
                if i.get("summary") == summary
                and i.get("status") == TodoItemStatus.NEEDS_ACTION
                and (not i.get("uid") or i["uid"] not in taken)
            ),
            None,
        )

    async def _add(self, key: str, rise: datetime | None, items: list[dict[str, Any]]) -> None:
        summary = title(self.coordinator.plants[key].name, self.hass.config.language)
        taken = self._taken(key)
        # Already on the list (store lost, or the user added it): adopt instead of adding a second one
        found = self._free(items, summary, taken)
        if found is None:
            payload: dict[str, Any] = {"item": summary}
            state = self.hass.states.get(self.todo)
            features = int(state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0) if state else 0
            due = self.coordinator.due_date(key)
            if features & TodoListEntityFeature.SET_DUE_DATE_ON_ITEM and due is not None:
                payload["due_date"] = max(dt_util.as_local(due).date(), dt_util.now().date())
            await self._call("add_item", payload)
            # add_item returns no id: the new task is the open one with this title that was not there before
            known = {str(i["uid"]) for i in items if i.get("uid")}
            found = self._free(await self._items(), summary, taken | known)
        self.records[key] = {
            "list": self.todo,
            "summary": summary,
            "uid": found.get("uid") if found else None,
            "state": "open",
            "cycle": rise.isoformat() if rise else None,
            "created": dt_util.utcnow().isoformat(),
        }
