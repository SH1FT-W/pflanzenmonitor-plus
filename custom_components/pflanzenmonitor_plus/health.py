"""Reine Auswertung ohne HA: Messgrößen, Grenzwerte, Zustand einer Pflanze, Lichtsumme (DLI).

Verhalten wie im Plant Monitor (HACS, Olen), damit Automationen und Karten nach dem Umzug gleich weiterlaufen:
Zustand „ok“/„problem“, je Messgröße „Low“/„High“/„ok“ mit 5 % Spielraum, Licht nur „zu hell“ als Problem, DLI
gegen den Vortag, Feuchte-Wartezeit nach dem Gießen (nur „zu nass“ wird so lange nicht gemeldet).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import math
from typing import Final, Literal

Status = Literal["Low", "High", "ok"]
LOW: Final = "Low"
HIGH: Final = "High"
OK: Final = "ok"
PROBLEM: Final = "problem"

# Messgrößen mit eigenem Sensor (Reihenfolge = Felder beim Einrichten)
SOURCES: Final = ("moisture", "temperature", "soil_temperature", "humidity", "illuminance", "conductivity", "co2")
# Messgrößen mit Zustand, in der Reihenfolge des Plant Monitor (so steht auch `problems` da)
MEASUREMENTS: Final = (
    "moisture",
    "conductivity",
    "temperature",
    "humidity",
    "co2",
    "soil_temperature",
    "illuminance",
    "dli",
    "vpd",
)
TEMPERATURES: Final = frozenset({"temperature", "soil_temperature"})
# Zählen standardmäßig als Problem (VPD wie im Plant Monitor nicht)
DEFAULT_PROBLEMS: Final = tuple(m for m in MEASUREMENTS if m != "vpd")

# Startwerte der Grenzwerte (Temperaturen in °C), wie im Plant Monitor
DEFAULTS: Final[dict[str, float]] = {
    "min_moisture": 20,
    "max_moisture": 60,
    "min_temperature": 10,
    "max_temperature": 40,
    "min_soil_temperature": 10,
    "max_soil_temperature": 40,
    "min_humidity": 20,
    "max_humidity": 60,
    "min_illuminance": 0,
    "max_illuminance": 100000,
    "min_conductivity": 500,
    "max_conductivity": 3000,
    "min_co2": 400,
    "max_co2": 2000,
    "min_dli": 2,
    "max_dli": 30,
    "min_vpd": 0.4,
    "max_vpd": 1.6,
    "lux_to_ppfd": 0.0185,
}
LIMITS: Final = tuple(DEFAULTS)


@dataclass(frozen=True, slots=True)
class Range:
    """Einstellbereich eines Grenzwerts."""

    low: float
    high: float
    step: float


RANGES: Final[dict[str, Range]] = {
    "moisture": Range(0, 100, 1),
    "temperature": Range(-50, 100, 1),
    "soil_temperature": Range(-50, 100, 1),
    "humidity": Range(0, 100, 1),
    "illuminance": Range(0, 200000, 500),
    "conductivity": Range(0, 3000, 50),
    "co2": Range(0, 5000, 50),
    "dli": Range(0, 100, 0.1),
    "vpd": Range(0, 5, 0.1),
    "lux_to_ppfd": Range(0.001, 0.1, 0.0001),
}

# Spielraum an den Grenzen: wer einmal „Low“ ist, bleibt es bis 5 % über der Grenze (höchstens 5 % der Spanne)
HYSTERESIS: Final = 0.05
# So viel Anstieg der Bodenfeuchte gilt als Gießen (startet die Wartezeit)
GRACE_JUMP: Final = 10.0
# Vergleichsbasis für den Anstieg: Tiefstwert so lange zurück
GRACE_WINDOW: Final = timedelta(minutes=30)


def measured(measurement: str, sensors: Mapping[str, str | None]) -> bool:
    """Gibt es die Messgröße bei dieser Pflanze? DLI braucht Licht, VPD Temperatur und Luftfeuchte."""
    if measurement == "dli":
        return bool(sensors.get("illuminance"))
    if measurement == "vpd":
        return bool(sensors.get("temperature") and sensors.get("humidity"))
    return bool(sensors.get(measurement))


def limit_fields(sensors: Mapping[str, str | None]) -> tuple[str, ...]:
    """Grenzwerte, die zu den Sensoren der Pflanze gehören (Lux-Faktor nur mit Lichtsensor)."""
    fields = [f"{side}_{m}" for m in MEASUREMENTS if measured(m, sensors) for side in ("max", "min")]
    if sensors.get("illuminance"):
        fields.append("lux_to_ppfd")
    return tuple(fields)


def check(value: float, low: float | None, high: float | None, current: str | None) -> Status:
    """Zustand einer Messgröße; der Spielraum gilt nur, solange sie schon zu niedrig bzw. zu hoch ist."""
    span = (high - low) * HYSTERESIS if low is not None and high is not None else math.inf
    if low is not None and value < low:
        return LOW
    if high is not None and value > high:
        return HIGH
    if current == LOW and low is not None and value <= low + min(abs(low) * HYSTERESIS, span):
        return LOW
    if current == HIGH and high is not None and value >= high - min(abs(high) * HYSTERESIS, span):
        return HIGH
    return OK


@dataclass(frozen=True, slots=True)
class Result:
    """Zustand der Pflanze: „ok“, „problem“ oder None (nichts bekannt)."""

    state: str | None
    statuses: dict[str, Status | None]
    problems: list[dict[str, str]]


def _text(value: float | None) -> str:
    return "unknown" if value is None else str(float(value))


def evaluate(
    values: Mapping[str, float | None],
    limits: Mapping[str, float],
    previous: Mapping[str, str | None],
    counted: frozenset[str],
    *,
    grace: bool = False,
    ppfd_source: bool = False,
) -> Result:
    """Alle Messgrößen gegen ihre Grenzwerte prüfen.

    `values` je Messgröße (Temperaturen in °C; `dli` = Tageslicht von gestern), `counted` = Messgrößen, die als
    Problem zählen. Licht ist nur zu hell ein Problem (dunkel ist es jede Nacht); misst der Lichtsensor schon PPFD,
    gibt es keine lx-Prüfung. Während der Wartezeit nach dem Gießen zählt „zu nass“ nicht.
    """
    statuses: dict[str, Status | None] = {}
    problems: list[dict[str, str]] = []
    known = False
    for m in MEASUREMENTS:
        value = values.get(m)
        if value is None or (m == "illuminance" and ppfd_source):
            statuses[m] = None
            continue
        known = True
        low, high = limits.get(f"min_{m}"), limits.get(f"max_{m}")
        status: Status = OK if m == "dli" and value <= 0 else check(value, low, high, previous.get(m))
        statuses[m] = status
        if status == OK or m not in counted or (m == "illuminance" and status == LOW):
            continue
        if m == "moisture" and status == HIGH and grace:
            continue
        problems.append(
            {"sensor_type": m, "status": status, "current": _text(value), "min": _text(low), "max": _text(high)}
        )
    if not known:
        return Result(None, statuses, [])
    return Result(PROBLEM if problems else OK, statuses, problems)


def vpd(temperature: float, humidity: float) -> float:
    """Dampfdruckdefizit in kPa aus Lufttemperatur (°C) und relativer Luftfeuchte (%)."""
    saturation = 0.6108 * math.exp(17.27 * temperature / (temperature + 237.3))
    return round(saturation * (1 - humidity / 100), 2)


@dataclass(slots=True)
class Light:
    """Lichtsumme: gesamt, heute, gestern und die letzten 24 Std. (stündliche Töpfe), alles in mol/m².

    PPFD in mol/(s·m²); zwischen zwei Messungen wird trapezförmig aufsummiert, ohne neue Messung gilt der letzte Wert.
    """

    total: float = 0.0
    today: float = 0.0
    day: date | None = None
    yesterday: float | None = None
    last_t: datetime | None = None
    last_ppfd: float | None = None
    hours: dict[str, float] = field(default_factory=dict)

    @property
    def day_24h(self) -> float:
        return sum(self.hours.values())


def _hour(t: datetime) -> str:
    return t.replace(minute=0, second=0, microsecond=0).isoformat()


def advance(light: Light, t: datetime, ppfd: float | None) -> None:
    """Bis `t` aufsummieren und `ppfd` als neuen Wert merken (None = gerade kein Messwert, nichts zählen)."""
    if light.last_t is not None and light.last_ppfd is not None and t > light.last_t:
        end = light.last_ppfd if ppfd is None else ppfd
        amount = (light.last_ppfd + end) / 2 * (t - light.last_t).total_seconds()
        light.total += amount
        light.today += amount
        key = _hour(light.last_t)
        light.hours[key] = light.hours.get(key, 0.0) + amount
    if light.last_t is None or t >= light.last_t:
        light.last_t = t
        light.last_ppfd = ppfd
    cutoff = _hour(t - timedelta(hours=23))
    light.hours = {k: v for k, v in light.hours.items() if k >= cutoff}


def roll(light: Light, day: date) -> None:
    """Neuer Tag: heutige Summe wird zu „gestern“ (nur direkt am Folgetag, sonst ist gestern unbekannt)."""
    if light.day == day:
        return
    if light.day is not None:
        light.yesterday = light.today if day - light.day == timedelta(days=1) else 0.0
    light.day = day
    light.today = 0.0


def dump(light: Light) -> dict[str, object]:
    """Zum Speichern."""
    return {
        "total": light.total,
        "today": light.today,
        "day": light.day.isoformat() if light.day else None,
        "yesterday": light.yesterday,
        "last_t": light.last_t.isoformat() if light.last_t else None,
        "last_ppfd": light.last_ppfd,
        "hours": dict(light.hours),
    }


def load(data: object) -> Light:
    """Gespeicherten Stand lesen; Unbrauchbares wird zum leeren Anfang."""
    if not isinstance(data, dict):
        return Light()

    def number(key: str) -> float | None:
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None

    def when(key: str) -> datetime | None:
        try:
            return datetime.fromisoformat(str(data[key])) if data.get(key) else None
        except ValueError:
            return None

    try:
        day = date.fromisoformat(str(data["day"])) if data.get("day") else None
    except ValueError:
        day = None
    hours = data.get("hours")
    return Light(
        total=number("total") or 0.0,
        today=number("today") or 0.0,
        day=day,
        yesterday=number("yesterday"),
        last_t=when("last_t"),
        last_ppfd=number("last_ppfd"),
        hours={str(k): float(v) for k, v in hours.items() if isinstance(v, (int, float))}
        if isinstance(hours, dict)
        else {},
    )
