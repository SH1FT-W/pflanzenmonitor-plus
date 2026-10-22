"""Alle 10 Min.: Feuchteverlauf aus der Langzeitstatistik lesen, lernen, Wetter für Pflanzen draußen prüfen.

Messwerte, Zustand und Lichtsumme laufen daneben live (live.py), die Grenzwerte liegen in limits.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN, SERVICE_GET_FORECASTS
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import model
from .const import (
    CONF_RAIN_SENSOR,
    CONF_WEATHER,
    CONTEXT_HISTORY,
    DOMAIN,
    FROST_DEFAULT,
    FROST_HORIZON,
    HISTORY,
    RAIN_AMOUNT,
    RAIN_HORIZON,
    STATS_DELAY,
    UPDATE_INTERVAL,
)
from .limits import Limits
from .live import Live, ppfd_factor
from .plants import Plant, device, entity_id, find_plants
from .util import number_state

if TYPE_CHECKING:
    from .advisor import Advisor

_LOGGER = logging.getLogger(__name__)

# Übrige Messgrößen, deren Verlauf die KI-Zusammenfassung braucht
CONTEXT = ("temperature", "soil_temperature", "humidity", "illuminance")

type PlusConfigEntry = ConfigEntry[PlusCoordinator]


@dataclass(frozen=True, slots=True)
class Rain:
    """Regen in den nächsten Stunden."""

    amount: float
    until: datetime


@dataclass(frozen=True, slots=True)
class Frost:
    """Tiefster Wert der nächsten 48 Std. gegen die Untergrenze der Pflanze."""

    lowest: float
    at: datetime
    limit: float

    @property
    def active(self) -> bool:
        return self.lowest < self.limit


@dataclass(slots=True)
class PlantData:
    """Was die Entitäten einer Pflanze zeigen."""

    learned: model.Learned
    moisture: float | None
    rain: Rain | None = None
    frost: Frost | None = None


def _samples(rows: list[Any]) -> list[model.Sample]:
    return [
        model.Sample(dt_util.utc_from_timestamp(r["start"]), float(r["mean"]), float(r["min"]), float(r["max"]))
        for r in rows
        if r.get("mean") is not None and r.get("min") is not None and r.get("max") is not None
    ]


class PlusCoordinator(DataUpdateCoordinator[dict[str, PlantData]]):
    """Rechnet alle Pflanzen auf einmal; die Statistik wird höchstens stündlich neu gelesen."""

    config_entry: PlusConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PlusConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.plants: dict[str, Plant] = find_plants(entry)
        self.limits = Limits(hass, entry.entry_id)
        self.live = Live(hass, entry.entry_id, self.limits)
        self.weather: str | None = entry.options.get(CONF_WEATHER)
        self.rain_sensor: str | None = entry.options.get(CONF_RAIN_SENSOR)
        self._stats: dict[str, list[model.Sample]] = {}
        self._stats_hour: datetime | None = None
        # Quelle des Verlaufs je echtem Sensor (er selbst oder der Spiegel von Plus, der nach dem Umzug den Verlauf
        # des Plant Monitor weiterführt)
        self.sources: dict[str, str] = {}
        # Tiefstwert der letzten Stunden je Feuchtesensor: springt der aktuelle Wert darüber, sofort neu rechnen
        self._recent_low: dict[str, float] = {}
        # Stundenbeginn, zu dem ein frischer Sprung zuerst gesehen wurde (bis die Stundenstatistik ihn enthält)
        self._fresh: dict[str, datetime] = {}
        # KI-Vorschläge (nur mit KI-Entität in den Optionen)
        self.advisor: Advisor | None = None
        # Neuladen nach einer Änderung ist schon angestoßen
        self.reload_pending = False

    @callback
    def async_track_moisture(self) -> None:
        """Gießen sofort erkennen statt erst beim nächsten Durchlauf."""
        ids = [p.moisture for p in self.plants.values() if p.moisture]
        if not ids:
            return

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            low = self._recent_low.get(event.data["entity_id"])
            value = number_state(self.hass, event.data["entity_id"])
            if low is not None and value is not None and value - low >= model.JUMP:
                # Nur einmal je Sprung neu rechnen, nicht bei jeder weiteren Meldung des Sensors
                self._recent_low[event.data["entity_id"]] = value
                # An den Eintrag gebunden: beim Entladen wird die Neuberechnung mit abgebrochen
                self.config_entry.async_create_task(self.hass, self.async_request_refresh())

        self.config_entry.async_on_unload(async_track_state_change_event(self.hass, ids, changed))

    async def _fetch(self, ids: set[str], start: datetime) -> dict[str, list[Any]]:
        rows: dict[str, list[Any]] = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, None, ids, "hour", None, {"mean", "min", "max"}
        )
        return rows

    async def _history(self, now: datetime) -> None:
        """Stündliche Statistik aller Feuchtesensoren (und des Regensensors) – nur einmal pro Stunde lesen."""
        # Der Recorder fasst eine Stunde erst kurz nach ihrem Ende zusammen – deshalb 2 Min. Vorlauf
        hour = (now - STATS_DELAY).replace(minute=0, second=0, microsecond=0)
        if self._stats_hour == hour:
            return
        moisture = {p.moisture for p in self.plants.values() if p.moisture}
        context: set[str] = set()
        if self.advisor:
            # Für die KI-Vorschläge: Verlauf der übrigen Sensoren, nur für die 30-Tage-Zusammenfassung
            for p in self.plants.values():
                context |= {p.sensors[m] for m in CONTEXT if m in p.sensors}
        own = moisture | context
        # Den echten Sensor und seinen Spiegel lesen und den längeren Verlauf nehmen: der Spiegel führt nach dem Umzug
        # den des Plant Monitor weiter, ein neu gekoppelter echter Sensor hat dagegen oft nur wenige Tage.
        external: dict[str, str] = {}
        for p in self.plants.values():
            for m, sensor in p.sensors.items():
                if sensor in own and (mirror := entity_id(self.hass, "sensor", f"{p.key}-current-{m}")):
                    external[sensor] = mirror
        # Zwei Zeitfenster: Feuchte und Regen lang (Zyklen lernen), Kontextsensoren kurz (spart Recorder-Arbeit)
        long_wanted = moisture | ({self.rain_sensor} if self.rain_sensor else set())
        long_wanted |= {external[s] for s in moisture if s in external}
        short_wanted = (context | {external[s] for s in context if s in external}) - long_wanted
        rows = await self._fetch(long_wanted, now - HISTORY) if long_wanted else {}
        if short_wanted:
            rows |= await self._fetch(short_wanted, now - CONTEXT_HISTORY)
        for sensor in own:
            ext = external.get(sensor)
            longer = ext if ext and len(rows.get(ext) or []) > len(rows.get(sensor) or []) else sensor
            self.sources[sensor] = longer
        self._stats = {sid: _samples(r) for sid, r in rows.items()}
        self._stats_hour = hour

    async def _forecast(self) -> list[dict[str, Any]] | None:
        if not self.weather or not any(p.outdoor for p in self.plants.values()):
            return None
        try:
            response = await self.hass.services.async_call(
                WEATHER_DOMAIN,
                SERVICE_GET_FORECASTS,
                {"entity_id": self.weather, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("Keine stündliche Vorhersage von %s: %s", self.weather, err)
            return None
        forecast = (response or {}).get(self.weather, {})
        items = forecast.get("forecast") if isinstance(forecast, dict) else None
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else None

    def _rain(self, forecast: list[dict[str, Any]], now: datetime) -> Rain | None:
        amount, until = 0.0, None
        for item in forecast:
            when = _when(item)
            if when is None or not now <= when <= now + RAIN_HORIZON:
                continue
            if (mm := _float(item.get("precipitation"))) is not None and mm > 0:
                amount += mm
                until = when
        return Rain(round(amount, 1), until) if amount >= RAIN_AMOUNT and until is not None else None

    def _frost(self, forecast: list[dict[str, Any]], now: datetime, limit: float | None) -> Frost | None:
        # Ohne Mindesttemperatur der Pflanze (kein Temperatursensor): Frost = unter 0 °C
        limit = FROST_DEFAULT if limit is None else limit
        lowest: tuple[float, datetime] | None = None
        for item in forecast:
            when, temp = _when(item), _float(item.get("temperature"))
            if when is None or temp is None or not now <= when <= now + FROST_HORIZON:
                continue
            if lowest is None or temp < lowest[0]:
                lowest = (temp, when)
        return Frost(lowest[0], lowest[1], limit) if lowest else None

    def _learn_all(
        self,
        stamps: dict[str, datetime],
        current: dict[str, float | None],
        minimum: dict[str, float | None],
    ) -> dict[str, model.Learned]:
        """Läuft im Executor: nur rechnen, keine Zustände lesen."""
        rain_rows = self._stats.get(self.rain_sensor or "", [])
        # Ohne Regenverlauf weiß man draußen nicht, ob gegossen wurde oder es geregnet hat
        rain = [s.t for s in rain_rows if s.high > 0] if rain_rows else None
        rain_since = rain_rows[0].t if rain_rows else None
        learned = {}
        for plant in self.plants.values():
            samples = list(self.history(plant.moisture))
            if (value := current[plant.key]) is not None:
                samples.append(model.Sample(stamps[plant.key], value, value, value))
            learned[plant.key] = model.learn(
                samples,
                minimum=minimum[plant.key],
                outdoor=plant.outdoor,
                rain=rain,
                rain_since=rain_since,
            )
        return learned

    async def _async_update_data(self) -> dict[str, PlantData]:
        now = dt_util.utcnow()
        await self._history(now)
        current = {key: self._moisture(plant) for key, plant in self.plants.items()}
        minimum: dict[str, float | None] = {key: self.limits.get(key, "min_moisture") for key in self.plants}
        stamps = {key: self._stamp(plant, current[key], now) for key, plant in self.plants.items()}
        learned = await self.hass.async_add_executor_job(self._learn_all, stamps, current, minimum)
        forecast = await self._forecast()
        # Regen ist für alle Pflanzen draußen derselbe: einmal je Durchlauf rechnen
        rain = self._rain(forecast, now) if forecast is not None else None
        data: dict[str, PlantData] = {}
        for key, plant in self.plants.items():
            item = PlantData(learned=learned[key], moisture=current[key])
            if plant.outdoor and forecast is not None:
                item.rain = rain
                limit = self.limits.get(key, "min_temperature") if plant.has("temperature") else None
                item.frost = self._frost(forecast, now, limit)
            data[key] = item
        return data

    def _moisture(self, plant: Plant) -> float | None:
        """Direkt vom echten Sensor (der Spiegel hat bei einem Sprung evtl. noch den alten Wert); nach einem Neustart
        notfalls der letzte Wert des Spiegels."""
        value = number_state(self.hass, plant.moisture)
        return value if value is not None else self.live.plants[plant.key].values.get("moisture")

    def _stamp(self, plant: Plant, value: float | None, now: datetime) -> datetime:
        """Zeitpunkt für den aktuellen Wert.

        Ist die Feuchte gerade gesprungen (Gießen), bekommt der aktuelle Wert den Stundenbeginn der ersten Erkennung –
        so bleibt „Zuletzt gegossen“ stehen, statt bei jedem Durchlauf mitzuwandern, und passt zur Stundenstatistik,
        die den Sprung später derselben Stunde zuordnet.
        """
        sensor = plant.moisture
        hourly = self.history(sensor)
        if sensor is None or value is None or not hourly:
            return now
        recent = [model.floor(s) for s in hourly if now - s.t <= model.BASE_WINDOW + timedelta(hours=1)]
        if recent and value - min(recent) >= model.JUMP:
            stamp = self._fresh.setdefault(sensor, now.replace(minute=0, second=0, microsecond=0))
            # Sofort-Listener erst wieder bei einem neuen Sprung über diesen Wert
            self._recent_low[sensor] = value
            return max(stamp, hourly[-1].t)
        self._fresh.pop(sensor, None)
        self._recent_low[sensor] = min(model.floor(s) for s in hourly[-3:])
        return now

    def history(self, entity_id: str | None) -> list[model.Sample]:
        """Stündlicher Verlauf eines echten Sensors (oder seines Spiegels, wenn der länger zurückreicht)."""
        return self._stats.get(self.sources.get(entity_id, entity_id), []) if entity_id else []

    def summary(self, entity_id: str | None, days: int = 30) -> dict[str, float] | None:
        """Tiefst-, Mittel- und Höchstwert der letzten Tage."""
        since = dt_util.utcnow() - timedelta(days=days)
        rows = [s for s in self.history(entity_id) if s.t >= since]
        if not rows:
            return None
        return {
            "min": round(min(s.low for s in rows), 1),
            "mean": round(sum(s.mean for s in rows) / len(rows), 1),
            "max": round(max(s.high for s in rows), 1),
        }

    def ppfd(self, plant: Plant) -> bool:
        """Misst der Helligkeitssensor der Pflanze schon PPFD statt lx (Einheit µmol/m²/s)?"""
        return self.live.plants[plant.key].ppfd_source

    def light(self, plant: Plant, days: int = 30) -> dict[str, Any] | None:
        """Lichtverlauf der letzten Tage für die KI: Tageshöchstwerte und Tageslicht (DLI) aus derselben Statistik."""
        since = dt_util.utcnow() - timedelta(days=days)
        # Liefert der Sensor schon PPFD, nur auf µmol/m²/s bringen
        unit = self.live.plants[plant.key].units.get("illuminance")
        source = ppfd_factor(unit)
        factor = source * 1_000_000 if source is not None else self.limits.get(plant.key, "lux_to_ppfd")
        sensor = plant.sensors.get("illuminance")
        rows = [(dt_util.as_local(s.t).date(), s.mean, s.high) for s in self.history(sensor) if s.t >= since]
        return model.light_summary(rows, factor)

    def device_area(self, plant: Plant) -> str | None:
        """Raum des Pflanzengeräts."""
        found = device(self.hass, self.config_entry.entry_id, plant)
        return found.area_id if found else None

    def hours(self, key: str) -> int:
        """Wie viele Stunden Verlauf für eine Pflanze vorliegen."""
        return len(self.history(self.plants[key].moisture))

    def due_date(self, key: str) -> datetime | None:
        """Wann gegossen werden sollte – auf die Stunde gerundet (fällig = heute 0 Uhr), damit der Wert stillsteht."""
        days = self.data[key].learned.days if self.data and key in self.data else None
        if days is None:
            return None
        now = dt_util.now()
        if days <= 0:
            return dt_util.start_of_local_day()
        due = now + timedelta(days=days, minutes=30)
        return due.replace(minute=0, second=0, microsecond=0)

    @property
    def rain_since(self) -> datetime | None:
        """Ab wann es Regenverlauf gibt (None = kein Regensensor oder noch kein Verlauf)."""
        rows = self._stats.get(self.rain_sensor or "", [])
        return rows[0].t if rows else None


def _when(item: dict[str, Any]) -> datetime | None:
    """Zeitpunkt eines Vorhersage-Eintrags; ohne Zeitzone gilt die von HA."""
    when = dt_util.parse_datetime(str(item.get("datetime")))
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=dt_util.get_default_time_zone())
    return when


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None
