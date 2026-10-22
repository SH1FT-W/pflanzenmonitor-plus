"""Bedingungen: Pflanze hat ein Problem, je Messgröße zu niedrig/zu hoch/ok, Sensor veraltet."""

from __future__ import annotations

from typing import Any, Final, override

from homeassistant.core import HomeAssistant
from homeassistant.helpers.automation import DomainSpec
from homeassistant.helpers.condition import (
    Condition,
    ConditionConfig,
    EntityStateConditionBase,
    make_entity_state_condition,
)
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import health
from .automation import STALE_MEASUREMENTS, STALE_SCHEMA, is_status, source, stale, stale_for

STATUS: Final = {"is_low": health.LOW, "is_high": health.HIGH, "is_ok": health.OK}


def _plus(base: type[EntityStateConditionBase]) -> type[EntityStateConditionBase]:
    """Nur Zustands-Entitäten von Plus."""

    class PlusCondition(base):  # type: ignore[valid-type,misc]
        @override
        def entity_filter(self, entities: set[str]) -> set[str]:
            return {e for e in super().entity_filter(entities) if is_status(self._hass, e)}

    return PlusCondition


def _stale(measurement: str) -> type[Condition]:
    """Wahr, wenn bei einer der Pflanzen der echte Sensor der Messgröße veraltet ist."""

    class StaleCondition(Condition):
        @classmethod
        async def async_validate_config(cls, hass: HomeAssistant, config: ConfigType) -> ConfigType:
            validated: ConfigType = STALE_SCHEMA(config)
            return validated

        def __init__(self, hass: HomeAssistant, config: ConditionConfig) -> None:
            super().__init__(hass, config)
            assert config.target is not None
            self._target = config.target
            self._duration = stale_for(config.options)

        def _async_check(self, **kwargs: Any) -> bool:
            selected = async_extract_referenced_entity_ids(
                self._hass, TargetSelection(self._target), expand_group=False
            )
            now = dt_util.utcnow()
            for entity_id in selected.referenced | selected.indirectly_referenced:
                if not is_status(self._hass, entity_id) or not (sensor := source(self._hass, entity_id, measurement)):
                    continue
                state = self._hass.states.get(sensor)
                good = state.last_reported if state is not None else now
                if stale(state, good, now, self._duration).is_stale:
                    return True
            return False

    return StaleCondition


CONDITIONS: Final[dict[str, type[Condition]]] = (
    {
        "has_problem": _plus(make_entity_state_condition("sensor", health.PROBLEM)),
    }
    | {
        f"{m}_{suffix}": _plus(make_entity_state_condition({"sensor": DomainSpec(value_source=f"{m}_status")}, state))
        for m in health.MEASUREMENTS
        for suffix, state in STATUS.items()
    }
    | {f"{m}_sensor_is_stale": _stale(m) for m in STALE_MEASUREMENTS}
)


async def async_get_conditions(hass: HomeAssistant) -> dict[str, type[Condition]]:
    return CONDITIONS
