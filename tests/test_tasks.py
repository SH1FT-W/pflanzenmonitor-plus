"""Watering tasks on a to-do list: one open task per due plant, completed on watering, user check-off respected."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.todo import (
    DATA_COMPONENT,
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pflanzenmonitor_plus.const import CONF_TODO, CONF_WEATHER, DOMAIN

from .conftest import INSIDE, OUTSIDE, add_now, add_plant, cycles, set_limits, setup

pytestmark = pytest.mark.usefixtures("stats")

LIST = "todo.garten"
MOISTURE = "sensor.bogenhanf_bodenfeuchtigkeit"
FULL = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
)


class FakeList(TodoListEntity):
    """In-memory to-do list; `with_uid=False` behaves like a list that hands out no ids."""

    _attr_name = "Garten"

    def __init__(self, features: TodoListEntityFeature = FULL) -> None:
        self._attr_supported_features = features
        self._attr_todo_items = []
        self.entity_id = LIST
        self.count = 0

    async def async_create_todo_item(self, item: TodoItem) -> None:
        self.count += 1
        assert self._attr_todo_items is not None
        self._attr_todo_items.append(
            TodoItem(summary=item.summary, uid=f"id{self.count}", status=TodoItemStatus.NEEDS_ACTION, due=item.due)
        )
        self.async_write_ha_state()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        assert self._attr_todo_items is not None
        self._attr_todo_items = [item if i.uid == item.uid else i for i in self._attr_todo_items]
        self.async_write_ha_state()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        assert self._attr_todo_items is not None
        self._attr_todo_items = [i for i in self._attr_todo_items if i.uid not in uids]
        self.async_write_ha_state()

    @property
    def items(self) -> list[TodoItem]:
        return list(self._attr_todo_items or [])


async def add_list(hass: HomeAssistant, features: TodoListEntityFeature = FULL) -> FakeList:
    assert await async_setup_component(hass, "todo", {})
    todo = FakeList(features)
    await hass.data[DATA_COMPONENT].async_add_entities([todo])
    return todo


@pytest.fixture
def due_house(hass: HomeAssistant) -> None:
    """Bogenhanf is due (lower limit above the current moisture), Olivenbaum is not."""
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", minimum=60.0, moisture=50.0)
    add_plant(hass, OUTSIDE, "Olivenbaum", "olivenbaum", "Terrasse", minimum=15.0, moisture=60.0, outdoor=True)
    add_plant(hass, "PLANTBARE0001", "Nackt", "nackt", "Flur")
    hass.states.async_remove("sensor.nackt_bodenfeuchtigkeit")


@pytest.fixture
def todo_entry(hass: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, title="Pflanzenmonitor Plus", data={}, options={CONF_TODO: LIST})
    e.add_to_hass(hass)
    return e


async def next_round(hass: HomeAssistant, minutes: int) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done(wait_background_tasks=True)


Water = Callable[..., Awaitable[None]]


@pytest.fixture
def water(hass: HomeAssistant, stats: dict[str, list[dict[str, Any]]], freezer: FrozenDateTimeFactory) -> Water:
    """Soil moisture rises (or one outlier reading); two hours later the hourly statistics contain the rise and the
    hour after it."""

    async def run(*, outlier: bool = False) -> None:
        start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        rows = stats[MOISTURE]
        if outlier:
            # One reading of 100 that stood for a few minutes: it lifts the time-weighted mean of its hour above
            # half a jump, but the next hour is back to normal
            values = [(57.0, 49.8, 100.0), (50.0, 49.8, 50.2)]
        else:
            hass.states.async_set(MOISTURE, "85", {"unit_of_measurement": "%"})
            await hass.async_block_till_done(wait_background_tasks=True)
            values = [(85.0, 50.0, 85.0), (84.5, 84.0, 85.0)]
        for h, (mean, low, high) in enumerate(values):
            t = start + timedelta(hours=h)
            rows.append({"start": t.timestamp(), "mean": mean, "min": low, "max": high})
        later = start + timedelta(hours=2, minutes=5)
        freezer.move_to(later)
        async_fire_time_changed(hass, later)
        await hass.async_block_till_done(wait_background_tasks=True)
        await next_round(hass, 11)

    return run


async def test_one_task_with_due_date_also_after_restart(hass: HomeAssistant, todo_entry: MockConfigEntry) -> None:
    todo = await add_list(hass)
    hass.data["test_pending_plants"] = []
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", minimum=60.0, moisture=50.0)
    add_plant(hass, OUTSIDE, "Olivenbaum", "olivenbaum", "Terrasse", minimum=15.0, moisture=60.0, outdoor=True)
    await setup(hass, todo_entry)
    assert [(i.summary, i.due, i.status) for i in todo.items] == [
        ("Bogenhanf gießen", date(2026, 10, 16), TodoItemStatus.NEEDS_ACTION)
    ]
    rec = todo_entry.runtime_data.tasks.records[INSIDE]
    assert (rec["state"], rec["uid"], rec["list"]) == ("open", "id1", LIST)

    await next_round(hass, 10)
    assert await hass.config_entries.async_reload(todo_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await next_round(hass, 20)
    assert len(todo.items) == 1


@pytest.mark.usefixtures("due_house")
async def test_completed_on_watering_and_again_when_due(
    hass: HomeAssistant, todo_entry: MockConfigEntry, water: Water
) -> None:
    todo = await add_list(hass)
    await setup(hass, todo_entry)
    assert len(todo.items) == 1
    await water()
    assert todo.items[0].status == TodoItemStatus.COMPLETED
    rec = todo_entry.runtime_data.tasks.records[INSIDE]
    assert (rec["state"], rec["closed_by"]) == ("closed", "watering")

    # Due again (lower limit above the new moisture): a new task
    set_limits(hass, INSIDE, min_moisture=90, max_moisture=100)
    await next_round(hass, 15)
    assert [(i.summary, i.status) for i in todo.items] == [
        ("Bogenhanf gießen", TodoItemStatus.COMPLETED),
        ("Bogenhanf gießen", TodoItemStatus.NEEDS_ACTION),
    ]


@pytest.mark.usefixtures("due_house")
@pytest.mark.usefixtures("due_house")
async def test_outlier_does_not_complete(hass: HomeAssistant, todo_entry: MockConfigEntry, water: Water) -> None:
    """A single high reading is no watering: the task stays open, also when it happens twice in the same hour."""
    todo = await add_list(hass)
    await setup(hass, todo_entry)
    hass.states.async_set(MOISTURE, "85", {"unit_of_measurement": "%"})
    await hass.async_block_till_done(wait_background_tasks=True)
    await next_round(hass, 1)
    hass.states.async_set(MOISTURE, "50", {"unit_of_measurement": "%"})
    await next_round(hass, 12)
    hass.states.async_set(MOISTURE, "86", {"unit_of_measurement": "%"})
    await hass.async_block_till_done(wait_background_tasks=True)
    await next_round(hass, 13)
    assert todo.items[0].status == TodoItemStatus.NEEDS_ACTION
    hass.states.async_set(MOISTURE, "50", {"unit_of_measurement": "%"})
    await water(outlier=True)
    assert todo.items[0].status == TodoItemStatus.NEEDS_ACTION
    assert todo_entry.runtime_data.tasks.records[INSIDE]["state"] == "open"


@pytest.mark.usefixtures("due_house")
async def test_same_name_gets_own_task(
    hass: HomeAssistant, todo_entry: MockConfigEntry, stats: dict[str, list[dict[str, Any]]], water: Water
) -> None:
    """Two plants called the same: two tasks, each completed on its own."""
    todo = await add_list(hass)
    stats["sensor.zwilling_bodenfeuchtigkeit"] = cycles()
    add_plant(hass, "PLANTTWIN0001", "Bogenhanf", "zwilling", "Bad", minimum=60.0, moisture=50.0)
    await setup(hass, todo_entry)
    records = todo_entry.runtime_data.tasks.records
    assert [i.summary for i in todo.items] == ["Bogenhanf gießen", "Bogenhanf gießen"]
    assert records[INSIDE]["uid"] != records["PLANTTWIN0001"]["uid"]
    await water()
    assert [i.status for i in todo.items].count(TodoItemStatus.COMPLETED) == 1
    assert records["PLANTTWIN0001"]["state"] == "open"


@pytest.mark.usefixtures("due_house")
async def test_checked_off_by_user_is_respected(hass: HomeAssistant, todo_entry: MockConfigEntry, water: Water) -> None:
    todo = await add_list(hass)
    await setup(hass, todo_entry)
    await hass.services.async_call(
        "todo", "update_item", {"entity_id": LIST, "item": "Bogenhanf gießen", "status": "completed"}, blocking=True
    )
    await next_round(hass, 10)
    rec = todo_entry.runtime_data.tasks.records[INSIDE]
    assert (rec["state"], rec["closed_by"]) == ("closed", "user")
    # Still due, but no new task until the plant has been watered
    await next_round(hass, 20)
    assert len(todo.items) == 1
    # The start of the stored rise moved by a few hours (oldest rise left the window, other history source):
    # still the same watering, no new task
    rec["cycle"] = "2026-10-11T06:00:00+00:00"
    await next_round(hass, 25)
    assert len(todo.items) == 1
    await water()
    set_limits(hass, INSIDE, min_moisture=90, max_moisture=100)
    await next_round(hass, 30)
    assert len(todo.items) == 2


@pytest.mark.usefixtures("due_house")
async def test_deleted_by_user_and_adopted(hass: HomeAssistant, todo_entry: MockConfigEntry) -> None:
    todo = await add_list(hass, TodoListEntityFeature.CREATE_TODO_ITEM | TodoListEntityFeature.DELETE_TODO_ITEM)
    # Already on the list (e.g. store lost): adopted instead of a second task; the list has no due dates
    await hass.services.async_call("todo", "add_item", {"entity_id": LIST, "item": "Bogenhanf gießen"}, blocking=True)
    await setup(hass, todo_entry)
    assert [(i.uid, i.due) for i in todo.items] == [("id1", None)]
    assert todo_entry.runtime_data.tasks.records[INSIDE]["uid"] == "id1"
    await hass.services.async_call("todo", "remove_item", {"entity_id": LIST, "item": "id1"}, blocking=True)
    await next_round(hass, 10)
    assert todo_entry.runtime_data.tasks.records[INSIDE]["closed_by"] == "user"
    assert todo.items == []


@pytest.mark.usefixtures("due_house")
async def test_list_without_ids_and_errors(hass: HomeAssistant, todo_entry: MockConfigEntry, water: Water) -> None:
    # No list yet: nothing happens
    await setup(hass, todo_entry)
    assert todo_entry.runtime_data.tasks.records == {}
    # A list that refuses new items: warning, try again later
    todo = await add_list(hass, TodoListEntityFeature(0))
    await next_round(hass, 10)
    assert todo_entry.runtime_data.tasks.records == {}
    todo._attr_supported_features = FULL
    original = todo.async_create_todo_item

    async def no_uid(item: TodoItem) -> None:
        await original(item)
        assert todo._attr_todo_items is not None
        todo._attr_todo_items[-1].uid = None

    todo.async_create_todo_item = no_uid  # type: ignore[method-assign]
    await next_round(hass, 20)
    rec = todo_entry.runtime_data.tasks.records[INSIDE]
    assert (rec["state"], rec["uid"]) == ("open", None)
    # Tracked by its title; watering completes it via the title
    todo._attr_todo_items[0].uid = "later"  # type: ignore[index]
    await water()
    assert todo.items[0].status == TodoItemStatus.COMPLETED


@pytest.mark.usefixtures("due_house")
async def test_failing_plant_and_slow_list(
    hass: HomeAssistant,
    todo_entry: MockConfigEntry,
    stats: dict[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    water: Water,
) -> None:
    """A list that cannot update items: the task stays open, the other plants go on; a hanging list times out."""
    todo = await add_list(hass, TodoListEntityFeature.CREATE_TODO_ITEM)
    await setup(hass, todo_entry)
    await water()
    assert todo_entry.runtime_data.tasks.records[INSIDE]["state"] == "open"
    assert todo.items[0].status == TodoItemStatus.NEEDS_ACTION

    async def hang(item: TodoItem) -> None:
        raise TimeoutError

    todo.async_create_todo_item = hang  # type: ignore[method-assign]
    stats["sensor.zwilling_bodenfeuchtigkeit"] = cycles()
    await add_now(hass, todo_entry, add_plant(hass, "PLANTTWIN0001", "Zwilling", "zwilling", "Bad"))
    set_limits(hass, "PLANTTWIN0001", min_moisture=60, max_moisture=100)
    await next_round(hass, 30)
    assert "PLANTTWIN0001" not in todo_entry.runtime_data.tasks.records

    # The list cannot be read at all: nothing changes, next run tries again
    async def down() -> list[dict[str, Any]]:
        raise HomeAssistantError("down")

    monkeypatch.setattr(todo_entry.runtime_data.tasks, "_items", down)
    await next_round(hass, 40)
    assert todo_entry.runtime_data.tasks.records[INSIDE]["state"] == "open"


async def test_stored_records_filtered(hass: HomeAssistant, todo_entry: MockConfigEntry) -> None:
    store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{todo_entry.entry_id}.tasks")
    await store.async_save(
        {
            "plants": {
                INSIDE: {"list": "todo.alt", "state": "open", "summary": "Bogenhanf gießen"},
                OUTSIDE: {"list": LIST, "state": "closed"},
                "GONE": {"list": LIST, "state": "open"},
            }
        }
    )
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer")
    add_plant(hass, OUTSIDE, "Olivenbaum", "olivenbaum", "Terrasse", outdoor=True)
    await setup(hass, todo_entry)
    assert set(todo_entry.runtime_data.tasks.records) == {OUTSIDE}


async def test_off_by_default(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Pflanzenmonitor Plus", data={}, options={CONF_WEATHER: None})
    entry.add_to_hass(hass)
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", minimum=60.0)
    await setup(hass, entry)
    assert entry.runtime_data.tasks is None
