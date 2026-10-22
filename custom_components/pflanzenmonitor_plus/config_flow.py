"""Einrichtung: ein Eintrag (KI, Wetter, Regensensor – alles optional), darunter je Pflanze ein Unterpunkt."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)
import voluptuous as vol

from . import health
from .const import (
    CONF_AI_TASK,
    CONF_AREA,
    CONF_GRACE,
    CONF_IMAGE,
    CONF_NAME,
    CONF_OUTDOOR,
    CONF_PROBLEMS,
    CONF_RAIN_SENSOR,
    CONF_SPECIES,
    CONF_WEATHER,
    DOMAIN,
    OUTDOOR_WORDS,
    SUBENTRY_PLANT,
)
from .plants import sensor_key

# Geräteklasse je Sensor (Auswahl nur passender Sensoren); Licht auch ohne Klasse (PPFD-Sensoren haben keine)
DEVICE_CLASSES: dict[str, list[str] | None] = {
    "moisture": [SensorDeviceClass.MOISTURE],
    "temperature": [SensorDeviceClass.TEMPERATURE],
    "soil_temperature": [SensorDeviceClass.TEMPERATURE],
    "humidity": [SensorDeviceClass.HUMIDITY],
    "illuminance": None,
    "conductivity": [SensorDeviceClass.CONDUCTIVITY],
    "co2": [SensorDeviceClass.CO2],
}


def _schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_AI_TASK, description={"suggested_value": current.get(CONF_AI_TASK)}): EntitySelector(
                EntitySelectorConfig(domain="ai_task")
            ),
            vol.Optional(CONF_WEATHER, description={"suggested_value": current.get(CONF_WEATHER)}): EntitySelector(
                EntitySelectorConfig(domain="weather")
            ),
            vol.Optional(CONF_RAIN_SENSOR, description={"suggested_value": current.get(CONF_RAIN_SENSOR)}): (
                EntitySelector(
                    # Nur Regenintensität (mm/h): Mengen-Sensoren zählen hoch bzw. bleiben bis Mitternacht > 0
                    EntitySelectorConfig(domain="sensor", device_class=SensorDeviceClass.PRECIPITATION_INTENSITY)
                )
            ),
        }
    )


class PlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ein Eintrag; Pflanzen kommen als Unterpunkte dazu (oder per Umzug aus dem Plant Monitor)."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Pflanzenmonitor Plus", data={}, options=user_input)
        weathers = sorted(self.hass.states.async_entity_ids("weather"))
        tasks = sorted(self.hass.states.async_entity_ids("ai_task"))
        suggestion = {CONF_AI_TASK: tasks[0] if tasks else None, CONF_WEATHER: weathers[0] if weathers else None}
        return self.async_show_form(step_id="user", data_schema=_schema(suggestion))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PlusOptionsFlow:
        return PlusOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_PLANT: PlantFlow}


class PlusOptionsFlow(OptionsFlow):
    """Dieselben Felder wie beim Einrichten (Plus lädt danach neu)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            # Bis zum Umzug steht „draußen“ noch in den Optionen (Liste der plant.*-Entitäten) – nicht verlieren
            if outdoor := self.config_entry.options.get(CONF_OUTDOOR):
                user_input = {**user_input, CONF_OUTDOOR: outdoor}
            return self.async_create_entry(data=user_input)
        return self.async_show_form(step_id="init", data_schema=_schema(dict(self.config_entry.options)))


def _outdoor(hass: HomeAssistant, area_id: str | None) -> bool:
    """Räume wie Terrasse, Balkon, Garten gelten als draußen."""
    area = ar.async_get(hass).async_get_area(area_id) if area_id else None
    return area is not None and any(word in area.name.lower() for word in OUTDOOR_WORDS)


class PlantFlow(ConfigSubentryFlow):
    """Pflanze anlegen oder ändern: Name und Art, Sensoren, was als Problem zählt."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        return await self._step_plant(user_input, area=True)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if not self.data:
            self.data = dict(self._get_reconfigure_subentry().data)
        return await self._step_plant(user_input, area=False)

    async def _step_plant(self, user_input: dict[str, Any] | None, *, area: bool) -> SubentryFlowResult:
        if user_input is not None:
            self.data |= {CONF_SPECIES: "", CONF_IMAGE: ""} | user_input
            self.data[CONF_NAME] = str(user_input[CONF_NAME]).strip()
            return await self.async_step_sensors()
        fields: dict[Any, Any] = {
            vol.Required(CONF_NAME, description={"suggested_value": self.data.get(CONF_NAME)}): TextSelector(),
            vol.Optional(CONF_SPECIES, description={"suggested_value": self.data.get(CONF_SPECIES)}): TextSelector(),
        }
        if area:
            fields[vol.Optional(CONF_AREA)] = AreaSelector()
        fields[vol.Optional(CONF_IMAGE, description={"suggested_value": self.data.get(CONF_IMAGE)})] = TextSelector()
        return self.async_show_form(step_id="reconfigure" if not area else "user", data_schema=vol.Schema(fields))

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            self.data |= {sensor_key(m): user_input.get(sensor_key(m)) for m in health.SOURCES}
            self.data[CONF_OUTDOOR] = bool(user_input.get(CONF_OUTDOOR))
            return await self.async_step_problems()
        fields: dict[Any, Any] = {}
        for m in health.SOURCES:
            key = sensor_key(m)
            config = EntitySelectorConfig(domain="sensor")
            if classes := DEVICE_CLASSES[m]:
                config["device_class"] = classes
            fields[vol.Optional(key, description={"suggested_value": self.data.get(key)})] = EntitySelector(config)
        outdoor = self.data.get(CONF_OUTDOOR, _outdoor(self.hass, self.data.get(CONF_AREA)))
        fields[vol.Optional(CONF_OUTDOOR, default=outdoor)] = BooleanSelector()
        return self.async_show_form(step_id="sensors", data_schema=vol.Schema(fields))

    async def async_step_problems(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            self.data[CONF_PROBLEMS] = list(user_input.get(CONF_PROBLEMS) or [])
            self.data[CONF_GRACE] = int(float(user_input.get(CONF_GRACE) or 0) * 60)
            return self._finish()
        problems = self.data.get(CONF_PROBLEMS, list(health.DEFAULT_PROBLEMS))
        schema = vol.Schema(
            {
                vol.Optional(CONF_PROBLEMS, default=problems): SelectSelector(
                    SelectSelectorConfig(
                        options=list(health.MEASUREMENTS), multiple=True, translation_key="measurement"
                    )
                ),
                vol.Optional(CONF_GRACE, default=int(self.data.get(CONF_GRACE) or 0) // 60): NumberSelector(
                    NumberSelectorConfig(
                        min=0, max=1440, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="min"
                    )
                ),
            }
        )
        return self.async_show_form(step_id="problems", data_schema=schema)

    def _finish(self) -> SubentryFlowResult:
        name = self.data[CONF_NAME]
        # Raum als Name: HA legt das Gerät damit gleich im Raum an (danach gilt der Raum des Geräts)
        if self.source != SOURCE_RECONFIGURE and (area_id := self.data.pop(CONF_AREA, None)):
            area = ar.async_get(self.hass).async_get_area(area_id)
            self.data[CONF_AREA] = area.name if area else None
        if self.source == SOURCE_RECONFIGURE:
            subentry = self._get_reconfigure_subentry()
            return self.async_update_and_abort(self._get_entry(), subentry, title=name, data=self.data)
        return self.async_create_entry(title=name, data=self.data)
