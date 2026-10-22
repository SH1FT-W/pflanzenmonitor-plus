"""Test-Haus: zwei Pflanzen (drinnen + Terrasse) mit echten Sensoren, stündliche Statistik, Fake-Wetter."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pflanzenmonitor_plus.const import (
    CONF_AI_TASK,
    CONF_AREA,
    CONF_KEY,
    CONF_NAME,
    CONF_OUTDOOR,
    CONF_RAIN_SENSOR,
    CONF_WEATHER,
    DOMAIN,
    SUBENTRY_PLANT,
)
from custom_components.pflanzenmonitor_plus.limits import Limits

NOW = "2026-10-16 12:00:00+00:00"
INSIDE, OUTSIDE = "PLANTINSIDE01", "PLANTOUTSIDE1"
RAIN = "sensor.regen"


def now() -> datetime:
    parsed = dt_util.parse_datetime(NOW)
    assert parsed is not None
    return parsed


def cycles(
    start: float = 80.0, low: float = 20.0, period: int = 10, count: int = 3, end: float = 50.0
) -> list[dict[str, Any]]:
    """Stündliche Statistik: alle `period` Tage gegossen, dazwischen gleichmäßig abgetrocknet.

    Nach `count` vollen Zyklen folgt ein angefangener, der bis jetzt auf `end` abgetrocknet ist (= aktueller Wert).
    """
    hours = period * 24
    partial = round((start - end) / (start - low) * hours)
    total = hours * count + partial
    first = now() - timedelta(hours=total)
    rows = []
    for i in range(total):
        h = i % hours
        value = start - (start - low) * h / hours
        t = first + timedelta(hours=i)
        rows.append({"start": t.timestamp(), "mean": value, "min": value - 0.2, "max": value + 0.2})
    # Die Stunde des Gießens: Höchstwert schon oben, Mittel noch dazwischen
    for c in range(1, count + 1):
        rows[c * hours]["mean"] = (start + low) / 2
        rows[c * hours]["min"] = low
    return rows


def watered(rows: list[dict[str, Any]], period: int = 10, count: int = 3) -> datetime:
    """Zeitpunkt des letzten Gießens in `cycles`."""
    return dt_util.utc_from_timestamp(rows[period * 24 * count]["start"])


PENDING = "test_pending_plants"


def add_plant(
    hass: HomeAssistant,
    key: str,
    name: str,
    slug: str,
    area: str,
    *,
    minimum: float = 25.0,
    min_temp: float = 2.0,
    moisture: float = 50.0,
    full: bool = False,
    outdoor: bool = False,
) -> dict[str, Any]:
    """Eine Pflanze mit ihren echten Sensoren; sie kommt beim nächsten `setup` als Unterpunkt in den Eintrag.

    Die echten Sensoren heißen wie früher die des Plant Monitor (sensor.<slug>_bodenfeuchtigkeit), die Spiegel von
    Plus bekommen den Raum vorangestellt (sensor.schlafzimmer_bogenhanf_bodenfeuchtigkeit).
    """
    ar.async_get(hass).async_get_or_create(area)
    hass.states.async_set(
        f"sensor.{slug}_bodenfeuchtigkeit", str(moisture), {"unit_of_measurement": "%", "device_class": "moisture"}
    )
    hass.states.async_set(f"sensor.{slug}_temperatur", "19.5", {"unit_of_measurement": "°C"})
    data: dict[str, Any] = {
        CONF_KEY: key,
        CONF_NAME: name,
        CONF_AREA: area,
        CONF_OUTDOOR: outdoor,
        "moisture_sensor": f"sensor.{slug}_bodenfeuchtigkeit",
        "temperature_sensor": f"sensor.{slug}_temperatur",
    }
    limits: dict[str, float] = {"min_moisture": minimum, "min_temperature": min_temp}
    if full:
        # Alle übrigen Sensoren, Grenzwerte wie die Startwerte (Lux-Faktor eigener Wert)
        for field, object_id, value, unit in (
            ("soil_temperature_sensor", "bodentemperatur", "18", "°C"),
            ("humidity_sensor", "luftfeuchtigkeit", "45", "%"),
            ("illuminance_sensor", "beleuchtungsstarke", "300", "lx"),
        ):
            hass.states.async_set(f"sensor.{slug}_{object_id}", value, {"unit_of_measurement": unit})
            data[field] = f"sensor.{slug}_{object_id}"
        limits["lux_to_ppfd"] = 0.02
    hass.data.setdefault(PENDING, []).append((data, limits))
    return data


async def add_now(hass: HomeAssistant, entry: MockConfigEntry, data: dict[str, Any]) -> None:
    """Pflanze im laufenden Betrieb anlegen (wie „Pflanze hinzufügen“)."""
    hass.data.get(PENDING, []).clear()
    hass.config_entries.async_add_subentry(entry, _subentry(data))
    await hass.async_block_till_done(wait_background_tasks=True)


def _subentry(data: dict[str, Any]) -> ConfigSubentry:
    return ConfigSubentry(
        data=MappingProxyType(data), subentry_type=SUBENTRY_PLANT, title=data[CONF_NAME], unique_id=data[CONF_KEY]
    )


def pending(hass: HomeAssistant, key: str) -> dict[str, Any]:
    """Daten einer angelegten, noch nicht eingerichteten Pflanze (im Test änderbar)."""
    return next(data for data, _limits in hass.data[PENDING] if data[CONF_KEY] == key)


def set_limits(hass: HomeAssistant, key: str, **values: float) -> None:
    """Grenzwerte „von Hand“ ändern (wie über die number-Entitäten)."""
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    entry.runtime_data.limits.async_set(key, values)


def limit(hass: HomeAssistant, key: str, name: str) -> float:
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    value: float = entry.runtime_data.limits.get(key, name)
    return value


@pytest.fixture(autouse=True)
def base(request: pytest.FixtureRequest) -> None:
    """Recorder (Abhängigkeit) muss vor `hass` starten; die Statistik selbst kommt aus `stats`."""
    request.getfixturevalue("recorder_mock")
    request.getfixturevalue("enable_custom_integrations")
    request.getfixturevalue("hass")


@pytest.fixture(autouse=True)
async def berlin(base: None, hass: HomeAssistant, freezer: Any) -> None:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Berlin")
    hass.config.language = "de"


@pytest.fixture
def house(hass: HomeAssistant) -> None:
    """Bogenhanf im Schlafzimmer, Olivenbaum auf der Terrasse."""
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", moisture=50.0)
    add_plant(hass, OUTSIDE, "Olivenbaum", "olivenbaum", "Terrasse", minimum=15.0, moisture=60.0, outdoor=True)


@pytest.fixture
def stats() -> Generator[dict[str, list[dict[str, Any]]]]:
    """Statistik je Sensor, im Test änderbar."""
    data: dict[str, list[dict[str, Any]]] = {
        "sensor.bogenhanf_bodenfeuchtigkeit": cycles(),
        "sensor.olivenbaum_bodenfeuchtigkeit": cycles(start=90, low=40, period=7, count=3, end=60),
        "sensor.bogenhanf_temperatur": [
            {"start": (now() - timedelta(hours=h)).timestamp(), "mean": 20.0, "min": 18.0, "max": 22.0}
            for h in range(48, 0, -1)
        ],
    }

    def fake(
        _hass: HomeAssistant, _start: datetime, _end: Any, ids: set[str], *_args: Any
    ) -> dict[str, list[dict[str, Any]]]:
        return {sid: data[sid] for sid in ids if sid in data}

    with patch("custom_components.pflanzenmonitor_plus.coordinator.statistics_during_period", side_effect=fake):
        yield data


@pytest.fixture
def forecast(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Fake-Wetter mit stündlicher Vorhersage (im Test änderbar)."""
    items: list[dict[str, Any]] = []
    for h in range(48):
        t = now() + timedelta(hours=h + 1)
        items.append({"datetime": t.isoformat(), "temperature": 8.0, "precipitation": 0.0})
    hass.states.async_set("weather.haus", "cloudy")

    async def handler(call: ServiceCall) -> ServiceResponse:
        return {"weather.haus": {"forecast": items}}

    hass.services.async_register("weather", "get_forecasts", handler, supports_response=SupportsResponse.ONLY)
    return items


class FakeAI:
    """Fake-KI für ai_task.generate_data: Antwort im Test änderbar, Aufrufe werden mitgeschrieben."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer: Any = {
            "min_moisture": 15,
            "max_moisture": 40,
            "min_temperature": 2,
            "reason_min_moisture": "Gießt meist bei 16 %.",
            "reason_max_moisture": "",
            "reason_min_temperature": "",
            "confidence": "high",
            "summary": "Untergrenze auf 15 % anheben.",
            "species": "",
        }
        self.error: Exception | None = None
        self.hang = False  # antwortet nie (Anbieter hängt)

    async def handler(self, call: ServiceCall) -> ServiceResponse:
        self.calls.append(dict(call.data))
        if self.hang:
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        return {"conversation_id": "x", "data": self.answer}


@pytest.fixture
def numbers() -> Generator[list[tuple[str, str, float]]]:
    """Schreibt jede Änderung der Grenzwerte mit: (Pflanze, Grenzwert, Wert)."""
    calls: list[tuple[str, str, float]] = []
    original = Limits.async_set

    def record(self: Limits, key: str, values: Any) -> None:
        calls.extend((key, name, float(value)) for name, value in values.items())
        original(self, key, values)

    with patch.object(Limits, "async_set", record):
        yield calls


@pytest.fixture
def ai(hass: HomeAssistant) -> FakeAI:
    fake = FakeAI()
    hass.states.async_set("ai_task.claude", "unknown")
    hass.services.async_register("ai_task", "generate_data", fake.handler, supports_response=SupportsResponse.ONLY)
    return fake


@pytest.fixture
def ai_entry(hass: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN,
        title="Pflanzenmonitor Plus",
        data={},
        options={CONF_AI_TASK: "ai_task.claude", CONF_WEATHER: "weather.haus"},
    )
    e.add_to_hass(hass)
    return e


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN,
        title="Pflanzenmonitor Plus",
        data={},
        options={CONF_WEATHER: "weather.haus", CONF_RAIN_SENSOR: RAIN},
    )
    e.add_to_hass(hass)
    return e


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Angelegte Pflanzen als Unterpunkte (Grenzwerte in den Store) übernehmen, dann einrichten."""
    pending = hass.data.pop(PENDING, [])
    if pending:
        store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.limits")
        stored = await store.async_load() or {"plants": {}}
        for data, limits in pending:
            hass.config_entries.async_add_subentry(entry, _subentry(data))
            stored["plants"][data[CONF_KEY]] = limits
        await store.async_save(stored)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
