"""KI-Grenzwert-Vorschläge ohne HA: Anfrage bauen, Antwortformat, Schutzregeln.

Die Schutzregeln stehen hier im Code und nicht im Prompt – was die KI auch antwortet, es wird begrenzt.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Final, Literal

Field = Literal[
    "min_moisture",
    "max_moisture",
    "min_temperature",
    "max_temperature",
    "min_soil_temperature",
    "max_soil_temperature",
    "min_humidity",
    "max_humidity",
    "min_illuminance",
    "max_illuminance",
    "min_dli",
    "max_dli",
]
FIELDS: Final[tuple[Field, ...]] = (
    "min_moisture",
    "max_moisture",
    "min_temperature",
    "max_temperature",
    "min_soil_temperature",
    "max_soil_temperature",
    "min_humidity",
    "max_humidity",
    "min_illuminance",
    "max_illuminance",
    "min_dli",
    "max_dli",
)
# Sensor, ohne den ein Grenzwert sinnlos ist (DLI rechnet Plus aus der Helligkeit)
Sensor = Literal["moisture", "temperature", "soil_temperature", "humidity", "illuminance"]
SENSOR: Final[dict[Field, Sensor]] = {
    "min_moisture": "moisture",
    "max_moisture": "moisture",
    "min_temperature": "temperature",
    "max_temperature": "temperature",
    "min_soil_temperature": "soil_temperature",
    "max_soil_temperature": "soil_temperature",
    "min_humidity": "humidity",
    "max_humidity": "humidity",
    "min_illuminance": "illuminance",
    "max_illuminance": "illuminance",
    "min_dli": "illuminance",
    "max_dli": "illuminance",
}
# Diese Werte sind seit 1.0 dabei und meist von Hand eingestellt: immer nur schrittweise. Alle anderen dürfen beim
# ersten Vorschlag frei springen, solange Plus sie noch nie geschrieben hat (sie stehen dann auf irgendeinem Startwert).
STEPPED: Final[frozenset[Field]] = frozenset({"min_moisture", "max_moisture", "min_temperature"})

# Erlaubte Bereiche je Wert
RANGE: Final[dict[Field, tuple[float, float]]] = {
    "min_moisture": (5.0, 90.0),
    "max_moisture": (15.0, 100.0),
    "min_temperature": (-15.0, 25.0),
    "max_temperature": (15.0, 45.0),
    "min_soil_temperature": (-5.0, 25.0),
    "max_soil_temperature": (15.0, 45.0),
    "min_humidity": (10.0, 90.0),
    "max_humidity": (30.0, 100.0),
    "min_illuminance": (0.0, 50000.0),
    "max_illuminance": (2000.0, 200000.0),
    "min_dli": (0.0, 40.0),
    "max_dli": (2.0, 80.0),
}
# Höchstens so viel Änderung pro Vorschlag (außer beim freien ersten Schritt)
MAX_STEP: Final[dict[Field, float]] = {
    "min_moisture": 15.0,
    "max_moisture": 15.0,
    "min_temperature": 5.0,
    "max_temperature": 5.0,
    "min_soil_temperature": 5.0,
    "max_soil_temperature": 5.0,
    "min_humidity": 15.0,
    "max_humidity": 15.0,
    "min_illuminance": 2000.0,
    "max_illuminance": 20000.0,
    "min_dli": 5.0,
    "max_dli": 10.0,
}
# Gerundet auf (DLI auf 0,1; Helligkeit feiner als der 500er-Regler der Grenzwerte, z. B. 200 lx)
ROUND: Final[dict[Field, float]] = dict.fromkeys(FIELDS, 1.0) | {
    "min_illuminance": 100.0,
    "max_illuminance": 100.0,
    "min_dli": 0.1,
    "max_dli": 0.1,
}
# Untergrenze → Obergrenze und Mindestabstand dazwischen
PAIRS: Final[dict[Field, tuple[Field, float]]] = {
    "min_moisture": ("max_moisture", 10.0),
    "min_temperature": ("max_temperature", 5.0),
    "min_soil_temperature": ("max_soil_temperature", 5.0),
    "min_humidity": ("max_humidity", 10.0),
    "min_illuminance": ("max_illuminance", 2000.0),
    "min_dli": ("max_dli", 2.0),
}
MIN_GAP: Final = PAIRS["min_moisture"][1]
# Draußen steht die Pflanze meist im Kübel: der Wurzelballen friert früher als im Beet – nie tiefer als das
OUTDOOR_FLOOR: Final[dict[Field, float]] = {"min_temperature": -5.0, "min_soil_temperature": 0.0}
# Draußen außerhalb des Sommers kennt der 30-Tage-Verlauf die Sommersonne nicht: diese Werte dann nie senken
OUTDOOR_NO_LOWER: Final[frozenset[Field]] = frozenset({"max_illuminance", "max_dli"})
CONFIDENCE: Final = ("low", "medium", "high")

LANGUAGES: Final = {"de": "German", "en": "English", "fr": "French", "nl": "Dutch", "it": "Italian", "es": "Spanish"}


@dataclass(frozen=True, slots=True)
class Change:
    """Ein geänderter Grenzwert."""

    current: float | None
    proposed: float
    reason: str
    # Ziel der KI, wenn die Schrittgrenze den Wert begrenzt hat (sonst None) – dann geht es schrittweise weiter
    requested: float | None = None
    # Erstmals von Plus eingestellt: durfte ohne Schrittgrenze springen
    free: bool = False


@dataclass(frozen=True, slots=True)
class Advice:
    """Bereinigte Antwort der KI. `ai_summary` ist der Text der KI – er kann Werte vor den Schutzregeln nennen."""

    changes: dict[Field, Change]
    confidence: str
    ai_summary: str
    species: str | None


# Texte für die selbst gebaute Zusammenfassung (Werte nach den Schutzregeln)
TEXTS: Final[dict[str, dict[str, str]]] = {
    "de": {
        "min_moisture": "Bodenfeuchte min",
        "max_moisture": "Bodenfeuchte max",
        "min_temperature": "Temperatur min",
        "max_temperature": "Temperatur max",
        "min_soil_temperature": "Bodentemperatur min",
        "max_soil_temperature": "Bodentemperatur max",
        "min_humidity": "Luftfeuchte min",
        "max_humidity": "Luftfeuchte max",
        "min_illuminance": "Helligkeit min",
        "max_illuminance": "Helligkeit max",
        "min_dli": "Tageslicht min",
        "max_dli": "Tageslicht max",
        "new": "neu",
        "target": "Ziel {value}, schrittweise",
        "first": "erstmals eingestellt",
        "points": "Punkte",
        "overview": "{count} Grenzwerte ändern sich.",
        "overview_one": "1 Grenzwert ändert sich.",
        "step_note": "Große Änderungen macht Plus in Schritten (pro Vorschlag höchstens {steps}). "
        "Nach dem Übernehmen fragt Plus in {days} Tagen nach dem nächsten Schritt.",
        "free_note": "Werte, die Plus zum ersten Mal einstellt, dürfen ganz springen (sie stehen meist noch auf "
        "Startwerten). Bodenfeuchte und Mindesttemperatur ändert Plus immer nur schrittweise.",
        "light_note": "Zum Licht: Helligkeit max vergleicht Plus mit dem aktuellen Wert, Helligkeit min zählt nicht "
        "als Problem. Tageslicht prüft Plus gegen den Vortag; Tageslicht min 0 heißt: keine Warnung bei wenig Licht.",
        "none": "Grenzwerte passen.",
        "low": "niedrig",
        "medium": "mittel",
        "high": "hoch",
    },
    "en": {
        "min_moisture": "Soil moisture min",
        "max_moisture": "Soil moisture max",
        "min_temperature": "Temperature min",
        "max_temperature": "Temperature max",
        "min_soil_temperature": "Soil temperature min",
        "max_soil_temperature": "Soil temperature max",
        "min_humidity": "Humidity min",
        "max_humidity": "Humidity max",
        "min_illuminance": "Illuminance min",
        "max_illuminance": "Illuminance max",
        "min_dli": "Daily light min",
        "max_dli": "Daily light max",
        "new": "new",
        "target": "target {value}, step by step",
        "first": "first tuning",
        "points": "points",
        "overview": "{count} limits change.",
        "overview_one": "1 limit changes.",
        "step_note": "Plus makes large changes in steps (at most {steps} per suggestion). "
        "After you accept, Plus asks for the next step in {days} days.",
        "free_note": "Limits that Plus sets for the first time may change fully (they are usually still start "
        "values). Soil moisture and minimum temperature always change step by step.",
        "light_note": "About light: Plus compares illuminance max with the current value; illuminance min does not "
        "count as a problem. Daily light is checked against the previous day; daily light min 0 means no warning when "
        "light is low.",
        "none": "Limits fit.",
        "low": "low",
        "medium": "medium",
        "high": "high",
    },
}
UNITS: Final[dict[Sensor | Literal["dli"], str]] = {
    "moisture": " %",
    "temperature": " °C",
    "soil_temperature": " °C",
    "humidity": " %",
    "illuminance": " lx",
    "dli": " mol/m²/d",
}


def _texts(language: str) -> dict[str, str]:
    return TEXTS.get(language.split("-")[0], TEXTS["en"])


def _unit(name: Field) -> str:
    return UNITS["dli"] if name.endswith("_dli") else UNITS[SENSOR[name]]


def _fmt(value: float, name: Field, language: str = "en") -> str:
    """Zahl mit Einheit; ab 1000 mit Tausendertrennung, auf Deutsch 50.000 und 0,5 statt 50,000 und 0.5."""
    text = f"{value:,.0f}" if abs(value) >= 1000 and value == int(value) else f"{value:g}"
    if language.split("-")[0] == "de":
        text = text.translate(str.maketrans(",.", ".,"))
    return text + _unit(name)


def _step_text(name: Field, language: str) -> str:
    """Schrittgrenze, bei Prozentwerten in Punkten („±15 Punkte“)."""
    if _unit(name) == " %":
        return f"±{MAX_STEP[name]:g} {_texts(language)['points']}"
    return f"±{_fmt(MAX_STEP[name], name, language)}"


def _round(value: float, name: Field) -> float:
    step = ROUND[name]
    return float(round(round(value / step) * step, 1))


def value_text(name: Field, change: Change, language: str) -> str:
    """„10 % → 18 %“, „neu 20 %“, mit „(Ziel 25 %, schrittweise)“, wenn die Schutzregeln begrenzt haben."""
    t = _texts(language)
    text = (
        f"{t['new']} {_fmt(change.proposed, name, language)}"
        if change.current is None
        else f"{_fmt(change.current, name, language)} → {_fmt(change.proposed, name, language)}"
    )
    if change.requested is not None:
        text += f" ({t['target'].format(value=_fmt(change.requested, name, language))})"
    elif change.free:
        text += f" ({t['first']})"
    return text


def overview(changes: dict[Field, Change], language: str) -> str:
    """Kurzsatz für den Dialog, darunter stehen die Zeilen je Wert."""
    t = _texts(language)
    if not changes:
        return t["none"]
    return t["overview_one"] if len(changes) == 1 else t["overview"].format(count=len(changes))


def describe(changes: dict[Field, Change], language: str) -> str:
    """Zusammenfassung aus den endgültigen Werten, z. B. „Bodenfeuchte max 40 % → 55 %, Temperatur min 2 °C → 5 °C“."""
    t = _texts(language)
    if not changes:
        return t["none"]
    parts = [f"{t[name]} {value_text(name, changes[name], language)}" for name in FIELDS if name in changes]
    return ", ".join(parts) + "."


# Beschreibung je Wert im Antwortformat: (Text, kleinster, größter, Schritt)
_ANSWER: Final[dict[Field, tuple[str, float, float, float]]] = {
    "min_moisture": ("Lower soil moisture limit in % (water at the latest here)", 0, 100, 1),
    "max_moisture": ("Upper soil moisture limit in % (wetter than this is too wet)", 0, 100, 1),
    "min_temperature": ("Minimum air temperature in °C for this plant and season", -20, 30, 1),
    "max_temperature": ("Maximum air temperature in °C the plant tolerates", 10, 50, 1),
    "min_soil_temperature": ("Minimum soil (root zone) temperature in °C", -10, 30, 1),
    "max_soil_temperature": ("Maximum soil (root zone) temperature in °C", 10, 50, 1),
    "min_humidity": ("Minimum relative air humidity in %", 0, 100, 1),
    "max_humidity": ("Maximum relative air humidity in %", 0, 100, 1),
    "min_illuminance": ("Minimum illuminance in lx on THIS sensor's scale", 0, 200000, 100),
    "max_illuminance": ("Maximum illuminance in lx on THIS sensor's scale", 0, 200000, 100),
    "min_dli": ("Minimum daily light integral in mol/m²/d on THIS sensor's scale", 0, 100, 0.1),
    "max_dli": ("Maximum daily light integral in mol/m²/d on THIS sensor's scale", 0, 100, 0.1),
}


def structure(fields: tuple[Field, ...] = FIELDS) -> dict[str, Any]:
    """Antwortformat für ai_task.generate_data – nur die Grenzwerte, die gefragt sind."""

    def number(description: str, low: float, high: float, step: float) -> dict[str, Any]:
        return {
            "description": description,
            "required": True,
            "selector": {"number": {"min": low, "max": high, "step": step, "mode": "box"}},
        }

    def text(description: str, required: bool = True) -> dict[str, Any]:
        return {"description": description, "required": required, "selector": {"text": {}}}

    result: dict[str, Any] = {}
    for name in fields:
        result[name] = number(*_ANSWER[name])
    for name in fields:
        result[f"reason_{name}"] = text("One short sentence why")
    return result | {
        "confidence": {
            "description": "How sure you are",
            "required": True,
            "selector": {"select": {"options": list(CONFIDENCE)}},
        },
        "summary": text("One or two sentences for the owner: what to change and why"),
        "species": text("Botanical species if it can be inferred from the name, else empty", required=False),
    }


def _step(name: Field) -> str:
    return f"{MAX_STEP[name]:g}{_unit(name).replace(' %', ' points')}"


def instructions(
    context: dict[str, Any], language: str, fields: tuple[Field, ...] = FIELDS, free: frozenset[Field] = frozenset()
) -> str:
    """Auftrag an die KI mit allen Zahlen der Pflanze – nur zu den Grenzwerten in `fields`."""
    lang = LANGUAGES.get(language.split("-")[0], "English")
    asked = set(fields)
    rules = [
        "You are an experienced horticulturist. Suggest limits for a plant that is monitored by sensors in a home. "
        "The limits drive reminders and problem alerts, so they must fit THIS plant in THIS home now. "
        f"Only these limits are asked: {', '.join(fields)}.",
        "Consider the species (from the name if not given), indoor/outdoor, the month and the season "
        "(dormancy in winter means drier soil and less light).",
    ]
    if asked & {"min_moisture", "max_moisture"}:
        rules.append(
            "Soil moisture percentages are sensor- and substrate-specific (capacitive sensor). Judge them against the "
            "plant's own history below for the numbers, and against the species for the direction. The lower limit "
            "is where the plant should be watered at the latest. The owner's watering habit is NOT proof of what the "
            "plant needs (you cannot see whether it thrives, and owners often water far too early): for species "
            "that want to dry out (succulents, cacti, Sansevieria, Zamioculcas, Yucca and the like) the lower limit "
            "belongs clearly below the habitual watering level; for species that must never dry out (ferns, "
            "Calathea and the like) it may lie above it. The upper limit is where the soil is too wet for too long. "
            "Right after watering the soil is briefly wetter (typical peak below); the upper limit usually lies "
            "slightly below that peak, so that only lasting wetness counts, and lower than that for species prone "
            "to root rot."
        )
        rules.append(
            "If the owner's watering habit clearly does not fit the species (watered much wetter or drier than it "
            "needs), say so in the reason for min_moisture and in the summary, e.g. that the plant is watered too "
            "early."
        )
    if asked & {"min_temperature", "max_temperature", "min_soil_temperature", "max_soil_temperature"}:
        rules.append(
            "Temperatures: the lowest and highest air or root-zone temperature the species tolerates without damage "
            "in its situation (textbook values of the species are fine here)."
        )
    if asked & {"min_humidity", "max_humidity"}:
        rules.append(
            "Air humidity: the range the species needs. Indoors in the heating season humidity drops; set the lower "
            "limit to what the species really needs, not to an ideal it never gets (compare with the room history)."
        )
    if asked & {"min_illuminance", "max_illuminance", "min_dli", "max_dli"}:
        rules.append(
            "Light: the illuminance sensor may not sit at the plant (e.g. a room sensor), so its lux values depend on "
            "where it is. Always judge lux and DLI against the light history below (daily peaks and daily DLI from "
            "the same sensor), scaled by what the species needs. The maximum illuminance is checked against the "
            "momentary value: set it above the normal daily peaks unless such light really harms the plant. The "
            "minimum illuminance is not checked (it would trigger every night); set it to the level below which "
            "daytime light is too dark for the species. DLI is checked once a day against yesterday's value: set "
            "min/max so that normal days for a healthy plant stay inside. The history covers only the last 30 days: "
            "leave headroom in the maximum illuminance and maximum DLI for brighter seasons (summer sun) unless the "
            "plant is outdoors in full sun already."
        )
    if context.get("outdoor"):
        rules.append(
            "The plant stands outdoors, most likely in a pot: the root ball freezes much sooner than in the ground, "
            f"so never go below {OUTDOOR_FLOOR['min_temperature']:g} °C air or "
            f"{OUTDOOR_FLOOR['min_soil_temperature']:g} °C soil temperature, even for hardy species. Outside "
            "summer never lower the maximum illuminance or maximum DLI (summer sun is not in the history)."
        )
    stepped = [n for n in fields if n not in free]
    if stepped:
        rules.append(
            "Keep a current value when it already fits; do not change values for the sake of it. Change "
            + ", ".join(f"{n} by at most {_step(n)}" for n in stepped)
            + " per suggestion; larger corrections happen over several months."
        )
    if free:
        rules.append(
            f"These limits were never tuned for this plant and may still be generic start values, so replace them "
            f"fully with fitting values: {', '.join(n for n in fields if n in free)}."
        )
    gaps = [f"{low} at least {gap:g} below {high}" for low, (high, gap) in PAIRS.items() if {low, high} & asked]
    rules.append(f"Keep {'; '.join(gaps)}. The summary must name exactly the values you return.")
    rules.append(f"Write all text in {lang}. Be short and concrete.")
    return (
        "\n".join(f"- {r}" for r in rules)
        + f"\n\nPlant data (JSON):\n{json.dumps(context, ensure_ascii=False, indent=1)}"
    )


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def sanitize(
    current: dict[Field, float | None],
    answer: Any,
    settable: set[Field] | None = None,
    free: frozenset[Field] = frozenset(),
    outdoor: bool = False,
    summer: bool = False,
) -> Advice | None:
    """Antwort prüfen und begrenzen. None, wenn sie unbrauchbar ist.

    `settable` = Grenzwerte, die es bei der Pflanze gibt (None = alle); andere werden nie vorgeschlagen.
    `free` = Grenzwerte ohne Schrittbegrenzung (noch nie von Plus geschrieben; Bereich und Abstand gelten trotzdem).
    `outdoor`/`summer` = Pflanze draußen / gerade Sommer: Frostgrenze (OUTDOOR_FLOOR) und außerhalb des Sommers
    kein Senken der Lichtobergrenzen (OUTDOOR_NO_LOWER).
    """
    if not isinstance(answer, dict):
        return None
    proposed: dict[Field, float] = {}
    requested: dict[Field, float] = {}
    for name in FIELDS:
        if settable is not None and name not in settable:
            continue
        raw = _num(answer.get(name))
        if raw is None:
            continue
        low, high = RANGE[name]
        now = current.get(name)
        if outdoor:
            low = max(low, OUTDOOR_FLOOR.get(name, low))
            if not summer and name in OUTDOOR_NO_LOWER and now is not None:
                low = max(low, now)
        # Der Bereich begrenzt endgültig (das Ziel ist nie erreichbar), die Schrittgrenze nur für diesen Vorschlag
        target = _round(min(max(raw, low), high), name)
        value = target
        if now is not None and name not in free:
            step = MAX_STEP[name]
            value = _round(min(max(target, now - step), now + step), name)
        proposed[name] = value
        if value != target:
            requested[name] = target
    if not proposed:
        return None

    # Untergrenze muss deutlich unter der Obergrenze bleiben – sonst beide Werte des Paars verwerfen
    for low_name, (high_name, gap) in PAIRS.items():
        low_v = proposed.get(low_name, current.get(low_name))
        high_v = proposed.get(high_name, current.get(high_name))
        if low_v is not None and high_v is not None and high_v - low_v < gap:
            proposed.pop(low_name, None)
            proposed.pop(high_name, None)

    changes: dict[Field, Change] = {}
    for name, value in proposed.items():
        now = current.get(name)
        if now is None or abs(value - now) >= ROUND[name]:
            reason = str(answer.get(f"reason_{name}") or "").strip()
            jump = now is not None and abs(value - now) > MAX_STEP[name]
            changes[name] = Change(now, value, reason, requested.get(name), name in free and jump)
    confidence = answer.get("confidence")
    species = str(answer.get("species") or "").strip() or None
    return Advice(
        changes=changes,
        confidence=confidence if confidence in CONFIDENCE else "low",
        ai_summary=str(answer.get("summary") or "").strip(),
        species=species,
    )


def reasons(changes: dict[Field, Change], language: str) -> str:
    """Je geändertem Grenzwert eine Zeile „- Bodenfeuchte min 10 % → 18 %: Grund“ (für den Reparatur-Dialog)."""
    t = _texts(language)
    lines = []
    for name in FIELDS:
        if (change := changes.get(name)) is None:
            continue
        line = f"- {t[name]} {value_text(name, change, language)}"
        lines.append(f"{line}: {change.reason}" if change.reason else line)
    return "\n".join(lines)


def confidence_text(confidence: str, language: str) -> str:
    t = _texts(language)
    return t.get(confidence, confidence)


LIGHT: Final[frozenset[Field]] = frozenset({"min_illuminance", "max_illuminance", "min_dli", "max_dli"})


def step_note(changes: dict[Field, Change], language: str, days: int) -> str:
    """Erklärungen für den Dialog: begrenzte Schritte, freie erste Schritte, Licht – leer, wenn nichts davon passt."""
    t = _texts(language)
    notes = []
    limited = [name for name in FIELDS if (c := changes.get(name)) is not None and c.requested is not None]
    if limited:
        steps = ", ".join(f"{t[name]} {_step_text(name, language)}" for name in limited)
        notes.append(t["step_note"].format(steps=steps, days=days))
    if any(c.free for c in changes.values()):
        notes.append(t["free_note"])
    if LIGHT & set(changes):
        notes.append(t["light_note"])
    return "\n\n".join(notes)
