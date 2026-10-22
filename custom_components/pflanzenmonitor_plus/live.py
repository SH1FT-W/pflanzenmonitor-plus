"""Live je Pflanze: echte Sensoren spiegeln, VPD und Lichtsumme rechnen, Zustand prüfen, Logbuch schreiben.

Läuft auf Zustandsänderungen der echten Sensoren (nicht im 10-Minuten-Takt des Koordinators); gespeichert wird im
Store, damit Lichtsumme, Spielraum und die zuletzt gemeldeten Probleme Neustarts überleben.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import math
from typing import Any, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from . import health
from .const import DOMAIN, LIGHT_TICK, RESTORE_HOLD
from .limits import Limits
from .plants import Plant, entity_id, status_id

_LOGGER = logging.getLogger(__name__)

# Live-Stand wird verzögert gespeichert (Sensoren melden oft); beim Beenden von HA schreibt der Store sofort
SAVE_DELAY: Final = 60
# Logbuch: Messgröße in Worten
WORDS: Final[dict[str, dict[str, str]]] = {
    "de": {
        "moisture": "Bodenfeuchte",
        "conductivity": "Leitfähigkeit",
        "temperature": "Temperatur",
        "humidity": "Luftfeuchte",
        "co2": "CO2",
        "soil_temperature": "Bodentemperatur",
        "illuminance": "Helligkeit",
        "dli": "Tageslicht (DLI)",
        "vpd": "VPD",
        "Low": "zu niedrig",
        "High": "zu hoch",
        "now": "jetzt",
        "back": "wieder im Bereich",
    },
    "en": {
        "moisture": "Soil moisture",
        "conductivity": "Conductivity",
        "temperature": "Temperature",
        "humidity": "Air humidity",
        "co2": "CO2",
        "soil_temperature": "Soil temperature",
        "illuminance": "Illuminance",
        "dli": "Daily light (DLI)",
        "vpd": "VPD",
        "Low": "too low",
        "High": "too high",
        "now": "now",
        "back": "back in range",
    },
}
TEMPERATURE_UNITS: Final = {u.value for u in UnitOfTemperature}


def ppfd_factor(unit: str | None) -> float | None:
    """Liefert der Lichtsensor schon PPFD (Einheit mit „mol“): Faktor nach mol/(s·m²), sonst None (lx)."""
    text = str(unit or "")
    if "mol" not in text:
        return None
    prefix = text.split("mol", 1)[0].strip().lower()
    return {"µ": 1e-6, "μ": 1e-6, "u": 1e-6, "m": 1e-3}.get(prefix, 1.0)


class PlantLive:
    """Messwerte und Zustand einer Pflanze."""

    def __init__(
        self, hass: HomeAssistant, plant: Plant, limits: Limits, saved: Any, changed: Callable[[], None]
    ) -> None:
        self.hass = hass
        self.plant = plant
        self.limits = limits
        self._changed = changed
        saved = saved if isinstance(saved, dict) else {}
        self.values: dict[str, float | None] = dict.fromkeys(plant.sensors)
        self.units: dict[str, str | None] = dict.fromkeys(plant.sensors)
        # Letzte Werte vor dem Neustart: gelten, bis der echte Sensor wieder meldet (höchstens RESTORE_HOLD)
        self._restored: dict[str, tuple[float, str | None]] = {}
        for m, item in (saved.get("values") or {}).items():
            if m in plant.sensors and isinstance(item, list) and len(item) == 2 and _number(item[0]) is not None:
                self._restored[m] = (float(item[0]), str(item[1]) if item[1] else None)
        self._holding = bool(self._restored)
        self.vpd: float | None = None
        self.ppfd: float | None = None
        self.light = health.load(saved.get("light"))
        # Ausfallzeit nicht als Licht zählen
        self.light.last_t = self.light.last_ppfd = None
        statuses = saved.get("statuses")
        self._statuses: dict[str, str | None] = dict(statuses) if isinstance(statuses, dict) else {}
        logged = saved.get("logged")
        self._logged: set[str] = {str(m) for m in logged} if isinstance(logged, list) else set()
        self.result = health.Result(None, {}, [])
        self._grace_until: datetime | None = None
        self._recent: list[tuple[datetime, float]] = []
        self._listeners: list[Callable[[], None]] = []

    @callback
    def async_start(self) -> list[CALLBACK_TYPE]:
        """Auf die echten Sensoren, die Grenzwerte und die Uhr hören; einmal sofort rechnen."""
        unsubs = [
            self.limits.async_listen(self.plant.key, self._refresh),
            async_track_time_interval(self.hass, self._tick, LIGHT_TICK),
            # Mitternacht: Tageslicht abschließen (DLI von gestern)
            async_track_time_change(self.hass, self._tick, hour=0, minute=0, second=0),
        ]
        if self.plant.sensors:
            unsubs.append(async_track_state_change_event(self.hass, list(self.plant.sensors.values()), self._source))
        if self._holding:
            unsubs.append(async_call_later(self.hass, RESTORE_HOLD, self._end_hold))
        self._refresh()
        return unsubs

    @callback
    def async_listen(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    @callback
    def _source(self, event: Event[EventStateChangedData]) -> None:
        self._refresh()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._refresh()

    @callback
    def _end_hold(self, _now: datetime) -> None:
        self._restored.clear()
        self._holding = False
        self._refresh()

    def _read(self, measurement: str) -> tuple[float | None, str | None]:
        """Wert und Einheit des echten Sensors; nach einem Neustart notfalls der gespeicherte Wert."""
        state = self.hass.states.get(self.plant.sensors[measurement])
        restored = self._restored.get(measurement)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return restored if restored else (None, self.units.get(measurement))
        unit = state.attributes.get("unit_of_measurement")
        self._restored.pop(measurement, None)
        return _number(state.state), str(unit) if unit else None

    def celsius(self, measurement: str) -> float | None:
        """Temperatur in °C (Grenzwerte und VPD rechnen in °C), andere Werte unverändert."""
        value = self.values.get(measurement)
        unit = self.units.get(measurement)
        if value is None or measurement not in health.TEMPERATURES or unit not in TEMPERATURE_UNITS:
            return value
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)

    @property
    def ppfd_source(self) -> bool:
        return ppfd_factor(self.units.get("illuminance")) is not None

    @callback
    def _refresh(self) -> None:
        now = dt_util.utcnow()
        for m in self.plant.sensors:
            self.values[m], self.units[m] = self._read(m)
        temperature, humidity = self.celsius("temperature"), self.values.get("humidity")
        self.vpd = health.vpd(temperature, humidity) if temperature is not None and humidity is not None else None
        self._light(now)
        self._grace(now)
        limits = self.limits.plant(self.plant.key)
        values: dict[str, float | None] = {m: self.celsius(m) for m in self.plant.sensors}
        values["vpd"] = self.vpd
        if self.plant.has("dli"):
            values["dli"] = self.light.yesterday
        previous = self._statuses
        grace = self._grace_until is not None and now < self._grace_until
        result = health.evaluate(
            values, limits, previous, self.plant.problems, grace=grace, ppfd_source=self.ppfd_source
        )
        self._logbook(result)
        self.result = result
        self._statuses = dict(result.statuses)
        self._changed()
        for listener in list(self._listeners):
            listener()

    def _light(self, now: datetime) -> None:
        if not self.plant.has("dli"):
            return
        lux, unit = self.values.get("illuminance"), self.units.get("illuminance")
        factor = ppfd_factor(unit)
        if lux is None:
            self.ppfd = None
        elif factor is not None:
            self.ppfd = lux * factor
        else:
            self.ppfd = lux * self.limits.get(self.plant.key, "lux_to_ppfd") / 1_000_000
        today = dt_util.as_local(now).date()
        if self.light.day is not None and self.light.day != today:
            # Was bis jetzt aufgelaufen ist, gehört noch zum alten Tag
            health.advance(self.light, now, self.light.last_ppfd)
        health.roll(self.light, today)
        health.advance(self.light, now, self.ppfd)

    def _grace(self, now: datetime) -> None:
        """Feuchte sprunghaft gestiegen (gegossen): so lange wie eingestellt kein „zu nass“ melden."""
        value = self.values.get("moisture")
        if value is None:
            self._recent.clear()
            self._grace_until = None
            return
        self._recent = [(t, v) for t, v in self._recent if now - t <= health.GRACE_WINDOW]
        if self.plant.grace and self._recent and value - min(v for _t, v in self._recent) >= health.GRACE_JUMP:
            self._grace_until = now + timedelta(seconds=self.plant.grace)
            self._recent.clear()
        self._recent.append((now, value))
        if self._grace_until is not None and now >= self._grace_until:
            self._grace_until = None

    def _logbook(self, result: health.Result) -> None:
        """Beginn und Ende eines Problems ins Logbuch (Sensor ausgefallen = weder neu noch behoben)."""
        if result.state is None:
            return
        words = WORDS.get(self.hass.config.language, WORDS["en"])
        messages = []
        for problem in result.problems:
            m = problem["sensor_type"]
            if m in self._logged:
                continue
            self._logged.add(m)
            side = "min" if problem["status"] == health.LOW else "max"
            messages.append(
                f"{words[m]} {words[problem['status']]} – {words['now']} {problem['current']}, {side} {problem[side]}"
            )
        active = {p["sensor_type"] for p in result.problems}
        for m in sorted(self._logged - active):
            if result.statuses.get(m) is None:
                continue
            self._logged.discard(m)
            messages.append(f"{words[m]} {words['back']}")
        target = entity_id(self.hass, "sensor", status_id(self.plant.key))
        if not messages or "logbook" not in self.hass.config.components:
            return
        for message in messages:
            data = {"name": self.plant.name, "message": message, "domain": DOMAIN}
            if target:
                data["entity_id"] = target
            self.hass.async_create_task(self.hass.services.async_call("logbook", "log", data), eager_start=True)

    def dump(self) -> dict[str, Any]:
        return {
            "values": {m: [v, self.units.get(m)] for m, v in self.values.items() if v is not None},
            "light": health.dump(self.light),
            "statuses": self._statuses,
            "logged": sorted(self._logged),
        }


class Live:
    """Alle Pflanzen; ein gemeinsamer Store."""

    def __init__(self, hass: HomeAssistant, entry_id: str, limits: Limits) -> None:
        self.hass = hass
        self.limits = limits
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.live")
        self.plants: dict[str, PlantLive] = {}
        self._started = False
        # Umzug: Stand für Pflanzen, die erst nach dem Neuladen da sind
        self._seeded: dict[str, dict[str, Any]] = {}
        # Speichern angesetzt (async_delay_save verschiebt bei jedem Aufruf, Sensoren melden aber ständig)
        self._pending = False

    async def async_load(self, plants: dict[str, Plant]) -> None:
        stored = await self._store.async_load() or {}
        saved = stored.get("plants") if isinstance(stored, dict) else None
        saved = saved if isinstance(saved, dict) else {}
        self.plants = {
            key: PlantLive(self.hass, plant, self.limits, saved.get(key), self._save) for key, plant in plants.items()
        }

    @callback
    def async_start(self) -> list[CALLBACK_TYPE]:
        unsubs: list[CALLBACK_TYPE] = []
        for plant in self.plants.values():
            unsubs += plant.async_start()
        self._started = True
        return unsubs

    @callback
    def _save(self) -> None:
        if self._started and not self._pending:
            self._pending = True
            self._store.async_delay_save(self._data, SAVE_DELAY)

    def _data(self) -> dict[str, Any]:
        self._pending = False
        return {"plants": self._seeded | {key: plant.dump() for key, plant in self.plants.items()}}

    def seed(self, key: str, data: dict[str, Any]) -> None:
        """Stand einer Pflanze vorgeben, die erst nach dem Neuladen geladen wird (Umzug)."""
        self._seeded[key] = data

    async def async_flush(self) -> None:
        """Beim Entladen sofort speichern (sonst ginge der verzögerte Stand beim Neuladen verloren)."""
        if self._started or self._seeded:
            # Danach nichts mehr ansetzen: ein später gespeicherter alter Stand überschriebe den neu geladenen
            self._started = False
            await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None
