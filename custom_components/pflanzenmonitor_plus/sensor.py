"""Sensoren je Pflanze: Zustand, Messwerte, VPD, Licht (PPFD, DLI) und Gelerntes (Gießen, Lernstand, KI-Vorschlag)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    EntityCategory,
    UnitOfConductivity,
    UnitOfRatio,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import advice, health, model
from .advisor import STATES
from .coordinator import PlantData, PlusConfigEntry, PlusCoordinator
from .entity import LiveEntity, PlantEntity
from .live import PlantLive
from .plants import Plant

PARALLEL_UPDATES = 0
STATUS_OPTIONS = [health.OK, health.PROBLEM]


@dataclass(frozen=True, kw_only=True)
class PlusSensorDescription(SensorEntityDescription):
    """Wert und Zusatzangaben aus dem Gelernten."""

    value: Callable[[PlantData, PlusCoordinator, str], float | str | datetime | date | None]
    attributes: Callable[[PlantData, PlusCoordinator, str], Mapping[str, Any]] = lambda _d, _c, _k: {}


def _present(values: Mapping[str, Any]) -> dict[str, Any]:
    """Leere Angaben weglassen – HA zeigt sie sonst als „Unbekannt“, was nach einem Fehler aussieht."""
    return {k: v for k, v in values.items() if v is not None}


def _due(data: PlantData, coordinator: PlusCoordinator, key: str) -> Mapping[str, Any]:
    due = coordinator.due_date(key)
    return _present(
        {
            "due": due.date().isoformat() if due else None,
            "target": data.learned.target,
            "method": data.learned.method,
        }
    )


def _watered(data: PlantData, coordinator: PlusCoordinator, key: str) -> Mapping[str, Any]:
    """Draußen: letzter Regen und ab wann es Regendaten gibt (davor ist ein Anstieg „unbekannt“)."""
    if not coordinator.plants[key].outdoor:
        return {}
    since = coordinator.rain_since
    return _present(
        {
            "last_rain": data.learned.last_rain.isoformat() if data.learned.last_rain else None,
            # Übersetzt: „Noch kein Regenverlauf“ – erklärt, warum „Zuletzt gegossen“ draußen leer bleibt
            "rain_data": "vorhanden" if since else "fehlt",
            "rain_data_since": since.isoformat() if since else None,
        }
    )


SENSORS: tuple[PlusSensorDescription, ...] = (
    PlusSensorDescription(
        key="learning",
        device_class=SensorDeviceClass.ENUM,
        options=["keine_daten", "lernt", "bereit"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d, _c, _k: d.learned.stage,
        attributes=lambda d, _c, _k: _present(
            {"cycles": d.learned.cycles, "cycles_needed": model.CYCLES_NEEDED, "habit": d.learned.threshold}
        ),
    ),
    PlusSensorDescription(
        key="next_watering",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        value=lambda d, _c, _k: d.learned.days,
        attributes=_due,
    ),
    PlusSensorDescription(
        key="next_watering_due",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda _d, c, k: c.due_date(k),
    ),
    PlusSensorDescription(
        key="last_watered",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d, _c, _k: d.learned.last_watered,
        attributes=_watered,
    ),
    PlusSensorDescription(
        key="drying_rate",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="%/d",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda d, _c, _k: d.learned.rate,
    ),
)


@dataclass(frozen=True, kw_only=True)
class MirrorDescription(SensorEntityDescription):
    """Spiegel eines echten Sensors; die Einheit kommt vom echten Sensor (sonst die übliche)."""

    unit: str
    suggested_display_precision: int | None = 1


MIRRORS: dict[str, MirrorDescription] = {
    d.key: d
    for d in (
        MirrorDescription(key="moisture", device_class=SensorDeviceClass.MOISTURE, unit=PERCENTAGE),
        MirrorDescription(
            key="temperature", device_class=SensorDeviceClass.TEMPERATURE, unit=UnitOfTemperature.CELSIUS
        ),
        MirrorDescription(
            key="soil_temperature", device_class=SensorDeviceClass.TEMPERATURE, unit=UnitOfTemperature.CELSIUS
        ),
        MirrorDescription(key="humidity", device_class=SensorDeviceClass.HUMIDITY, unit=PERCENTAGE),
        MirrorDescription(key="illuminance", device_class=SensorDeviceClass.ILLUMINANCE, unit=LIGHT_LUX),
        MirrorDescription(
            key="conductivity",
            device_class=SensorDeviceClass.CONDUCTIVITY,
            unit=UnitOfConductivity.MICROSIEMENS_PER_CM,
        ),
        MirrorDescription(
            key="co2",
            device_class=SensorDeviceClass.CO2,
            unit=UnitOfRatio.PARTS_PER_MILLION,
            suggested_display_precision=0,
        ),
    )
}


async def async_setup_entry(
    hass: HomeAssistant, entry: PlusConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    for plant in coordinator.plants.values():
        live = coordinator.live.plants[plant.key]
        entities: list[SensorEntity] = [StatusSensor(live)]
        entities += [MirrorSensor(live, MIRRORS[m]) for m in plant.sensors]
        if plant.has("vpd"):
            entities.append(VpdSensor(live))
        if plant.has("dli"):
            entities += [PpfdSensor(live), LightIntegralSensor(live), DliSensor(live), Dli24hSensor(live)]
        if plant.moisture:
            entities += [PlusSensor(coordinator, plant, d) for d in SENSORS]
            if coordinator.advisor:
                entities.append(SuggestionSensor(coordinator, plant))
        async_add_entities(entities, config_subentry_id=plant.subentry_id)


class StatusSensor(LiveEntity, SensorEntity):
    """Zustand der Pflanze wie plant.* im Plant Monitor: ok oder problem, je Messgröße Low/High/ok."""

    _attr_name = None
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS

    def __init__(self, live: PlantLive) -> None:
        super().__init__(live, "status")
        self._attr_entity_picture = live.plant.image

    @property
    def native_value(self) -> str | None:
        return self.live.result.state

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Dieselben Angaben wie der Plant Monitor (Karten und Vorlagen lesen sie)."""
        statuses = self.live.result.statuses
        attrs: dict[str, Any] = {"species": self.plant.species}
        attrs |= {f"{m}_status": statuses.get(m) for m in health.MEASUREMENTS}
        attrs["problems"] = self.live.result.problems
        return attrs


class MirrorSensor(LiveEntity, SensorEntity):
    """Messwert des echten Sensors; nach einem Neustart bis zu 10 Min. der letzte Wert, bis er wieder meldet."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, live: PlantLive, description: MirrorDescription) -> None:
        super().__init__(live, description.key, f"current-{description.key.replace('_', '-')}")
        self.entity_description = description
        self.measurement = description.key
        self.default_unit = description.unit

    @property
    def device_class(self) -> SensorDeviceClass | None:
        # Ein Lichtsensor mit PPFD (µmol/m²/s) ist keine Beleuchtungsstärke
        if self.measurement == "illuminance" and self.live.ppfd_source:
            return None
        return self.entity_description.device_class

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self.live.units.get(self.measurement) or self.default_unit

    @property
    def native_value(self) -> float | None:
        return self.live.values.get(self.measurement)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        return {"external_sensor": self.plant.sensors[self.measurement]}


class VpdSensor(LiveEntity, SensorEntity):
    """Dampfdruckdefizit aus Temperatur und Luftfeuchte."""

    _attr_native_unit_of_measurement = "kPa"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, live: PlantLive) -> None:
        super().__init__(live, "vpd", "current-vpd")

    @property
    def native_value(self) -> float | None:
        return self.live.vpd


class PpfdSensor(LiveEntity, SensorEntity):
    """Photonenflussdichte aus der Helligkeit (lx × Faktor) oder direkt vom Sensor."""

    _attr_native_unit_of_measurement = "mol/s⋅m²"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_visible_default = False

    def __init__(self, live: PlantLive) -> None:
        super().__init__(live, "ppfd", "current-ppfd")

    @property
    def native_value(self) -> float | None:
        return self.live.ppfd


class LightIntegralSensor(LiveEntity, SensorEntity):
    """Lichtsumme seit Beginn (mol/m²)."""

    _attr_native_unit_of_measurement = "mol/m²"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_visible_default = False

    def __init__(self, live: PlantLive) -> None:
        super().__init__(live, "total_light_integral", "ppfd-integral")

    @property
    def native_value(self) -> float:
        return round(self.live.light.total, 4)


class DliSensor(LiveEntity, SensorEntity):
    """Tageslicht heute (beginnt um Mitternacht bei 0); gestern steht in „last_period“ (das prüft der Zustand)."""

    _attr_native_unit_of_measurement = "mol/d⋅m²"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2

    def __init__(self, live: PlantLive) -> None:
        super().__init__(live, "dli")

    @property
    def native_value(self) -> float:
        return round(self.live.light.today, 4)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        yesterday = self.live.light.yesterday
        return {"last_period": None if yesterday is None else round(yesterday, 2)}


class Dli24hSensor(LiveEntity, SensorEntity):
    """Lichtsumme der letzten 24 Std."""

    _attr_native_unit_of_measurement = "mol/d⋅m²"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_visible_default = False

    def __init__(self, live: PlantLive) -> None:
        super().__init__(live, "dli_24h", "dli-24h")

    @property
    def native_value(self) -> float:
        return round(self.live.light.day_24h, 4)


class PlusSensor(PlantEntity, SensorEntity):
    """Ein Sensor je Pflanze und Merkmal."""

    entity_description: PlusSensorDescription

    def __init__(self, coordinator: PlusCoordinator, plant: Plant, description: PlusSensorDescription) -> None:
        super().__init__(coordinator, plant, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | str | datetime | date | None:
        item = self.item
        return None if item is None else self.entity_description.value(item, self.coordinator, self.plant.key)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        item = self.item
        return None if item is None else self.entity_description.attributes(item, self.coordinator, self.plant.key)


class SuggestionSensor(PlantEntity, SensorEntity):
    """KI-Vorschlag für die Grenzwerte: Zustand + was sich ändern soll und warum."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES

    def __init__(self, coordinator: PlusCoordinator, plant: Plant) -> None:
        super().__init__(coordinator, plant, "suggestion")
        assert coordinator.advisor is not None
        self.advisor = coordinator.advisor

    @property
    def native_value(self) -> str:
        return str(self.advisor.record(self.plant.key)["state"])

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        """Lesbar statt verschachtelt: je Grenzwert „alt → neu“ und der Grund der KI."""
        rec = self.advisor.record(self.plant.key)
        nxt = self.advisor.next_check(self.plant.key)
        applied = rec.get("applied")
        language = self.hass.config.language
        changes = {n: advice.Change(**c) for n, c in rec["changes"].items() if n in advice.FIELDS}
        # Zusammenfassung beim Anzeigen bauen (aktuelle Bezeichnungen und Zahlenformat)
        attrs: dict[str, Any] = {
            "summary": advice.describe(changes, language) if rec["state"] == "offen" else rec.get("summary")
        }
        for name, change in rec["changes"].items():
            if name not in advice.FIELDS:
                continue
            attrs[name] = advice.value_text(name, changes[name], language)
            attrs[f"{name}_reason"] = change.get("reason") or None
        attrs |= {
            "confidence": rec.get("confidence"),
            "species": rec.get("species"),
            "asked": rec.get("asked"),
            "next_check": nxt.isoformat() if nxt else None,
            "applied": applied.get("at") if isinstance(applied, dict) else None,
            "error": rec.get("error"),
        }
        return _present(attrs)
