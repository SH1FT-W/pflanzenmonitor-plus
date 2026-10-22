"""Auslöser: Problem erkannt/behoben, je Messgröße zu niedrig/zu hoch/ok geworden, Sensor veraltet/meldet wieder."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, override

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.helpers.automation import DomainSpec
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.target import TargetStateChangedData, async_track_target_selector_state_change_event
from homeassistant.helpers.trigger import (
    EntityTargetStateTriggerBase,
    Trigger,
    TriggerActionRunner,
    TriggerConfig,
    make_entity_target_state_trigger,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import health
from .automation import EXCLUDED, STALE_MEASUREMENTS, STALE_SCHEMA, is_status, source, stale, stale_for

# Still veralteter Sensor: so oft nachsehen, ob er (mit gleichem Wert) wieder meldet
RECHECK: Final = timedelta(minutes=5)
STATUS: Final = {"became_low": health.LOW, "became_high": health.HIGH, "became_ok": health.OK}


def _plus(base: type[EntityTargetStateTriggerBase]) -> type[EntityTargetStateTriggerBase]:
    """Nur Zustands-Entitäten von Plus (die Domain „sensor“ allein reicht nicht)."""

    class PlusTrigger(base):  # type: ignore[valid-type,misc]
        @override
        def entity_filter(self, entities: set[str]) -> set[str]:
            return {e for e in super().entity_filter(entities) if is_status(self._hass, e)}

    return PlusTrigger


@dataclass(slots=True)
class _Watch:
    """Was über einen echten Sensor bekannt ist."""

    # Letzte gültige Meldung (auch mit gleichem Wert; beim Start ohne gültigen Wert: Startzeit)
    good: datetime
    # Seit dem Start schon einmal gültig gemeldet (dann zählt ein Ausfall sofort)
    seen: bool
    stale: bool = False
    timer: CALLBACK_TYPE | None = None


def _stale(measurement: str, *, fresh: bool) -> type[Trigger]:
    """Echter Sensor einer Messgröße wird veraltet (fresh=False) bzw. meldet wieder (fresh=True).

    „Veraltet“ wird bei jeder Meldung und zu jeder Frist neu ausgerechnet (`automation.stale`), ausgelöst wird beim
    Wechsel. Wer schon beim Start ausgefallen ist (Neustart), bekommt die volle Frist, bevor er als veraltet gilt.
    """

    class StaleTrigger(Trigger):
        @classmethod
        async def async_validate_config(cls, hass: HomeAssistant, config: ConfigType) -> ConfigType:
            validated: ConfigType = STALE_SCHEMA(config)
            return validated

        def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
            super().__init__(hass, config)
            assert config.target is not None
            self._target = config.target
            self._duration = stale_for(config.options)

        def _sources(self, entities: set[str]) -> set[str]:
            return {s for e in entities if is_status(self._hass, e) and (s := source(self._hass, e, measurement))}

        async def async_attach_runner(
            self, run_action: TriggerActionRunner, did_not_trigger: Any | None = None
        ) -> CALLBACK_TYPE:
            watches: dict[str, _Watch] = {}

            @callback
            def update(entity_id: str, *, first: bool = False) -> None:
                watch = watches.get(entity_id)
                if watch is None:
                    return
                if watch.timer:
                    watch.timer()
                    watch.timer = None
                state = self._hass.states.get(entity_id)
                now = dt_util.utcnow()
                if state is not None and state.state not in EXCLUDED:
                    watch.good, watch.seen = max(watch.good, state.last_reported), True
                result = stale(state, watch.good, now, self._duration, seen=watch.seen)
                # Neu rechnen zur Frist (eine Meldung mit gleichem Wert schiebt sie hinaus); still veraltet: regelmäßig,
                # denn eine Meldung mit gleichem Wert ist keine Zustandsänderung und käme sonst nie an
                when = None
                if not result.is_stale or not watch.seen:
                    when = watch.good + self._duration
                elif result.reason == "no_update":
                    when = now + min(self._duration, RECHECK)
                if when is not None:

                    @callback
                    def due(_now: datetime) -> None:
                        update(entity_id)

                    watch.timer = async_track_point_in_utc_time(self._hass, due, when)
                changed, watch.stale = watch.stale != result.is_stale, result.is_stale
                if first or not changed or result.is_stale == fresh:
                    return
                data: dict[str, Any] = {ATTR_ENTITY_ID: entity_id, "measurement": measurement}
                if not fresh:
                    data["reason"] = result.reason
                run_action(data, f"{measurement} sensor {entity_id}", None)

            @callback
            def changed(data: TargetStateChangedData) -> None:
                update(data.state_change_event.data["entity_id"])

            @callback
            def targets(added: set[str], removed: set[str], _states: Mapping[str, State | None]) -> None:
                for entity_id in removed:
                    if (watch := watches.pop(entity_id, None)) and watch.timer:
                        watch.timer()
                now = dt_util.utcnow()
                for entity_id in added:
                    watches[entity_id] = _Watch(good=now, seen=False)
                    update(entity_id, first=True)

            unsub = await async_track_target_selector_state_change_event(
                self._hass, self._target, changed, self._sources, targets
            )

            @callback
            def remove() -> None:
                unsub()
                for watch in watches.values():
                    if watch.timer:
                        watch.timer()
                watches.clear()

            return remove

    return StaleTrigger


TRIGGERS: Final[dict[str, type[Trigger]]] = (
    {
        "problem_detected": _plus(make_entity_target_state_trigger("sensor", health.PROBLEM)),
        "problem_cleared": _plus(make_entity_target_state_trigger("sensor", health.OK)),
    }
    | {
        f"{m}_{suffix}": _plus(make_entity_target_state_trigger({"sensor": DomainSpec(value_source=f"{m}_status")}, to))
        for m in health.MEASUREMENTS
        for suffix, to in STATUS.items()
    }
    | {
        f"{m}_sensor_became_{name}": _stale(m, fresh=fresh)
        for m in STALE_MEASUREMENTS
        for name, fresh in (("stale", False), ("fresh", True))
    }
)


async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
    return TRIGGERS
