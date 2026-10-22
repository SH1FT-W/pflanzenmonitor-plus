"""Umzug aus dem Plant Monitor: Pflanzen, echte Grenzwerte, Register-Einstellungen, gleiche Entitäts-IDs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
    label_registry as lr,
)
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration

from custom_components.pflanzenmonitor_plus.const import CONF_AI_TASK, CONF_OUTDOOR, CONF_WEATHER, DOMAIN
from custom_components.pflanzenmonitor_plus.migrate import async_migrate

pytestmark = pytest.mark.usefixtures("stats", "forecast")


KEY = "01JPLANT0000000000BOGENHANF"


@pytest.fixture(autouse=True)
def plant_monitor(hass: HomeAssistant) -> dict[str, bool]:
    """Der Plant Monitor als Integration, deren Einträge sich entladen lassen (unload=False: Entladen scheitert)."""
    control = {"unload": True}

    async def unload_entry(_hass: HomeAssistant, _entry: Any) -> bool:
        return control["unload"]

    mock_integration(hass, MockModule("plant", async_unload_entry=unload_entry))
    return control


async def start(hass: HomeAssistant) -> None:
    """Alle Plant-Monitor-Einträge als geladen markieren (wie nach dem Start von HA)."""
    for entry in hass.config_entries.async_entries("plant"):
        assert isinstance(entry, MockConfigEntry)
        entry.mock_state(hass, ConfigEntryState.LOADED)


def legacy_plant(hass: HomeAssistant, *, key: str = KEY, name: str = "Bogenhanf", slug: str = "bogenhanf") -> None:
    """Eine Pflanze, wie der Plant Monitor sie anlegt (Eintrag, Gerät, Entitäten, Zustände)."""
    entry = MockConfigEntry(
        domain="plant",
        entry_id=key,
        title=name,
        data={
            "plant_info": {
                "name": name,
                "species": "",
                "display_pid": "",
                # Veraltet: der Spiegel kennt den aktuellen Sensor (external_sensor)
                "moisture_sensor": "sensor.alt",
                "temperature_sensor": "sensor.bogen_temp",
                "conductivity_sensor": None,
                "entity_picture": "/local/bogen.jpg",
                # Nur Startwerte; die echten stehen in den number-Entitäten
                "limits": {"min_moisture": 20, "max_moisture": 60, "max_dli": 30},
            }
        },
        options={
            "display_pid": "Dracaena trifasciata",
            "species": "dracaena trifasciata",
            "moisture_trigger": True,
            "temperature_trigger": False,
            "moisture_grace_period": 1800,
        },
    )
    entry.add_to_hass(hass)
    area = ar.async_get(hass).async_get_or_create("Schlafzimmer")
    label = lr.async_get(hass).async_create("Pflanzen")
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(config_entry_id=key, identifiers={("plant", key)}, name=name)
    devices.async_update_device(device.id, area_id=area.id, labels={label.label_id})
    registry = er.async_get(hass)

    def make(domain: str, unique: str, object_id: str, **kwargs: Any) -> str:
        return registry.async_get_or_create(
            domain, "plant", unique, config_entry=entry, device_id=device.id, suggested_object_id=object_id, **kwargs
        ).entity_id

    plant = make("plant", key, slug)
    registry.async_update_entity(plant, labels={label.label_id})
    problem = {"sensor_type": "moisture", "status": "High", "current": "52.0", "min": "8", "max": "45"}
    hass.states.async_set(plant, "problem", {"friendly_name": name, "moisture_status": "High", "problems": [problem]})
    lux = make("sensor", f"{key}-current-illuminance", f"{slug}_beleuchtungsstarke")
    hass.states.async_set(lux, "100", {"unit_of_measurement": "lx", "external_sensor": "sensor.bogen_lux"})
    hidden = er.RegistryEntryHider.INTEGRATION
    hass.states.async_set(
        make("sensor", f"{key}-ppfd-integral", f"{slug}_total_ppfd_mol_integral", hidden_by=hidden), "123.4"
    )
    hass.states.async_set(make("sensor", f"{key}-dli", f"{slug}_dli"), "1.5", {"last_period": "3.2"})
    moisture = make("sensor", f"{key}-current-moisture", f"{slug}_bodenfeuchtigkeit")
    registry.async_update_entity(moisture, labels={label.label_id}, icon="mdi:cup-water", name="Feuchte")
    registry.async_update_entity_options(moisture, "sensor", {"display_precision": 0})
    registry.async_update_entity_options(moisture, "conversation", {"should_expose": False})
    hass.states.async_set(moisture, "50", {"unit_of_measurement": "%", "external_sensor": "sensor.bogen_feuchte"})
    temperature = make("sensor", f"{key}-current-temperature", f"{slug}_temperatur")
    hass.states.async_set(temperature, "20", {"unit_of_measurement": "°C", "external_sensor": "sensor.bogen_temp"})
    hass.states.async_set(make("number", f"{key}-min-moisture", f"{slug}_min_bodenfeuchtigkeit"), "8")
    hass.states.async_set(make("number", f"{key}-max-moisture", f"{slug}_max_bodenfeuchtigkeit"), "45")
    # Temperaturgrenze in °F (Systemeinheit beim Anlegen) → 10 °C
    hass.states.async_set(
        make("number", f"{key}-min-temperature", f"{slug}_min_temperatur"), "50", {"unit_of_measurement": "°F"}
    )
    hass.states.async_set(make("number", f"{key}-max-temperature", f"{slug}_max_temperatur"), "unavailable")
    # Ohne Sensor vom Plant Monitor abgeschaltet: zieht nicht mit um
    disabled = er.RegistryEntryDisabler.INTEGRATION
    make("sensor", f"{key}-current-conductivity", f"{slug}_leitfahigkeit", disabled_by=disabled)
    # Echte Sensoren
    hass.states.async_set("sensor.bogen_feuchte", "52", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.bogen_temp", "21", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.bogen_lux", "100", {"unit_of_measurement": "lx"})


async def test_migrate(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "repairs", {})
    legacy_plant(hass)
    old_device = dr.async_get(hass).async_get_device_by_identifier(("plant", KEY), KEY).id  # type: ignore[union-attr]
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "Gießen",
                    "triggers": [{"trigger": "state", "entity_id": "plant.bogenhanf", "to": "problem"}],
                    "actions": [],
                },
                {
                    "alias": "Gerät",
                    "triggers": [{"trigger": "state", "entity_id": "sensor.bogen_feuchte"}],
                    "actions": [{"action": "homeassistant.update_entity", "target": {"device_id": old_device}}],
                },
            ]
        },
    )
    await start(hass)
    plus = MockConfigEntry(
        domain=DOMAIN,
        title="Pflanzenmonitor Plus",
        data={},
        options={CONF_AI_TASK: "ai_task.claude", CONF_WEATHER: "weather.haus", CONF_OUTDOOR: ["plant.bogenhanf"]},
    )
    plus.add_to_hass(hass)
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_device_by_identifier(("plant", KEY), KEY)
    assert device is not None
    # Entität von Plus am Gerät des Plant Monitor (wie bis 26.10)
    learning = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{KEY}_learning",
        config_entry=plus,
        device_id=device.id,
        suggested_object_id="schlafzimmer_bogenhanf_lernstand",
    )
    assert await hass.config_entries.async_setup(plus.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    # Noch nicht umgezogen: Hinweis da, nichts weggeräumt
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "migrate")
    assert issue is not None
    assert issue.translation_placeholders == {"plants": "Bogenhanf"}
    assert registry.async_get(learning.entity_id) is not None

    manager = repairs_flow_manager(hass)
    assert manager is not None
    result: Any = await manager.async_init(DOMAIN, data={"issue_id": "migrate"})
    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"] == {"plants": "Bogenhanf"}
    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Plant Monitor leer, Hinweis weg
    assert not hass.config_entries.async_entries("plant")
    assert ir.async_get(hass).async_get_issue(DOMAIN, "migrate") is None
    assert registry.async_get("plant.bogenhanf") is None

    # Pflanze mit allem, was dazugehört
    subentry = next(iter(plus.subentries.values()))
    assert subentry.unique_id == KEY
    assert dict(subentry.data) == {
        "key": KEY,
        "name": "Bogenhanf",
        "species": "Dracaena trifasciata",
        "image": "/local/bogen.jpg",
        "outdoor": True,
        "moisture_sensor": "sensor.bogen_feuchte",
        "temperature_sensor": "sensor.bogen_temp",
        "soil_temperature_sensor": None,
        "humidity_sensor": None,
        "illuminance_sensor": "sensor.bogen_lux",
        "conductivity_sensor": None,
        "co2_sensor": None,
        "problems": ["moisture", "conductivity", "humidity", "co2", "soil_temperature", "illuminance", "dli"],
        "moisture_grace": 1800,
    }
    assert "outdoor" not in plus.options
    assert plus.data["migrated"] == {"plant.bogenhanf": "sensor.bogenhanf"}
    limits = plus.runtime_data.limits
    assert limits.get(KEY, "min_moisture") == 8
    assert limits.get(KEY, "max_moisture") == 45
    assert limits.get(KEY, "min_temperature") == 10
    # Nicht verfügbar → Startwert aus dem Eintrag bzw. der übliche
    assert limits.get(KEY, "max_temperature") == 40
    assert limits.get(KEY, "max_dli") == 30

    # Gleiche Entitäts-IDs, jetzt von Plus; plant.* wird zum Zustand
    status = registry.async_get("sensor.bogenhanf")
    assert status is not None
    assert status.platform == DOMAIN
    assert status.labels
    moisture = registry.async_get("sensor.bogenhanf_bodenfeuchtigkeit")
    assert moisture is not None
    assert moisture.platform == DOMAIN
    assert (moisture.name, moisture.icon) == ("Feuchte", "mdi:cup-water")
    assert moisture.options["sensor"]["display_precision"] == 0
    assert moisture.options["conversation"] == {"should_expose": False}
    state = hass.states.get("sensor.bogenhanf_bodenfeuchtigkeit")
    assert state is not None
    assert state.state == "52.0"
    assert registry.async_get("number.bogenhanf_min_bodenfeuchtigkeit") is not None
    assert hass.states.get("number.bogenhanf_min_temperatur").state == "10.0"  # type: ignore[union-attr]
    assert registry.async_get("sensor.bogenhanf_leitfahigkeit") is None
    plant_state = hass.states.get("sensor.bogenhanf")
    assert plant_state is not None
    # 52 % über der übernommenen Obergrenze 45 % (Temperatur zählt laut Plant Monitor nicht)
    assert plant_state.state == "problem"
    assert plant_state.attributes["moisture_status"] == "High"
    assert plant_state.attributes["species"] == "Dracaena trifasciata"
    # Live-Stand übernommen: Lichtsumme, Tageslicht heute und gestern (gegen das geprüft wird)
    assert hass.states.get("sensor.bogenhanf_dli").state == "1.5"  # type: ignore[union-attr]
    assert hass.states.get("sensor.bogenhanf_dli").attributes["last_period"] == 3.2  # type: ignore[union-attr]
    assert plant_state.attributes["dli_status"] == "ok"
    integral = registry.async_get("sensor.bogenhanf_total_ppfd_mol_integral")
    assert integral is not None
    assert integral.hidden_by is er.RegistryEntryHider.INTEGRATION
    assert float(hass.states.get(integral.entity_id).state) >= 123.4  # type: ignore[union-attr]

    # Gerät von Plus mit Raum und Label; die alte Entität von Plus hängt jetzt daran
    new_device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, KEY), plus.entry_id)
    assert new_device is not None
    assert new_device.area_id == "schlafzimmer"
    assert new_device.labels
    moved = registry.async_get(learning.entity_id)
    assert moved is not None
    assert moved.device_id == new_device.id
    assert moved.config_subentry_id == subentry.subentry_id

    # Automation nutzt noch plant.bogenhanf → Hinweis
    refs = ir.async_get(hass).async_get_issue(DOMAIN, "references")
    assert refs is not None
    assert refs.translation_placeholders == {
        "references": f"- Gerät: device_id {old_device} → {new_device.id}\n- Gießen: plant.bogenhanf → sensor.bogenhanf"
    }

    # Nichts mehr umzuziehen (auch nicht, wenn ein alter Hinweis hängt)
    assert await async_migrate(hass, plus) == []
    ir.async_create_issue(
        hass, DOMAIN, "migrate", is_fixable=True, severity=ir.IssueSeverity.WARNING, translation_key="migrate"
    )
    result = await manager.async_init(DOMAIN, data={"issue_id": "migrate"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "nothing_to_migrate"


async def test_migrate_two_plants_without_device(hass: HomeAssistant) -> None:
    """Zwei Pflanzen in einem Rutsch; eine ohne Gerät und ohne Zustände (Startwerte aus dem Eintrag)."""
    legacy_plant(hass)
    bare = MockConfigEntry(
        domain="plant",
        entry_id="BARE",
        title="Kaktus",
        data={"plant_info": {"name": "Kaktus", "moisture_sensor": "sensor.kaktus", "lux_to_ppfd": 0.03}},
        options={},
    )
    bare.add_to_hass(hass)
    await start(hass)
    plus = MockConfigEntry(domain=DOMAIN, title="Pflanzenmonitor Plus", data={}, options={})
    plus.add_to_hass(hass)
    assert await hass.config_entries.async_setup(plus.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sorted(await async_migrate(hass, plus)) == ["Bogenhanf", "Kaktus"]
    await hass.async_block_till_done(wait_background_tasks=True)
    assert set(plus.runtime_data.plants) == {KEY, "BARE"}
    kaktus = plus.runtime_data.plants["BARE"]
    assert kaktus.moisture == "sensor.kaktus"
    assert kaktus.problems == frozenset(
        {"moisture", "conductivity", "temperature", "humidity", "co2", "soil_temperature", "illuminance", "dli"}
    )
    assert plus.runtime_data.limits.get("BARE", "lux_to_ppfd") == 0.03
    # Ohne Automationen kein Hinweis
    assert ir.async_get(hass).async_get_issue(DOMAIN, "references") is None


async def test_no_migration_without_plant_monitor(hass: HomeAssistant) -> None:
    other = MockConfigEntry(domain="plant", entry_id="YAML", data={}, title="Import")
    other.add_to_hass(hass)
    plus = MockConfigEntry(domain=DOMAIN, title="Pflanzenmonitor Plus", data={}, options={})
    plus.add_to_hass(hass)
    assert await hass.config_entries.async_setup(plus.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "migrate") is None
    assert await async_migrate(hass, plus) == []


async def test_migrate_refuses_unloaded_or_stuck(hass: HomeAssistant, plant_monitor: dict[str, bool]) -> None:
    """Nicht geladen (echte Grenzwerte fehlen) oder Entladen scheitert: nichts wird entfernt."""
    assert await async_setup_component(hass, "repairs", {})
    legacy_plant(hass)
    plus = MockConfigEntry(domain=DOMAIN, title="Pflanzenmonitor Plus", data={}, options={})
    plus.add_to_hass(hass)
    assert await hass.config_entries.async_setup(plus.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    manager = repairs_flow_manager(hass)
    assert manager is not None

    async def run() -> Any:
        result: Any = await manager.async_init(DOMAIN, data={"issue_id": "migrate"})
        return await manager.async_configure(result["flow_id"], {})

    result = await run()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "legacy_not_loaded"
    await start(hass)
    plant_monitor["unload"] = False
    result = await run()
    assert result["reason"] == "legacy_unload_failed"
    assert hass.config_entries.async_entries("plant")
    assert not plus.subentries
    assert plus.runtime_data.plants == {}


async def test_options_keep_outdoor_until_migrated(hass: HomeAssistant) -> None:
    legacy_plant(hass)
    await start(hass)
    plus = MockConfigEntry(
        domain=DOMAIN, title="Pflanzenmonitor Plus", data={}, options={CONF_OUTDOOR: ["plant.bogenhanf"]}
    )
    plus.add_to_hass(hass)
    assert await hass.config_entries.async_setup(plus.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    result = await hass.config_entries.options.async_init(plus.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert plus.options == {CONF_OUTDOOR: ["plant.bogenhanf"]}
    # Umbenannt → Liste zieht mit
    er.async_get(hass).async_update_entity("plant.bogenhanf", new_entity_id="plant.bogen")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert plus.options[CONF_OUTDOOR] == ["plant.bogen"]
