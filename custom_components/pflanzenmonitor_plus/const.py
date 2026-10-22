"""Konstanten."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "pflanzenmonitor_plus"
# Der Plant Monitor (HACS, Olen/homeassistant-plant) registriert sich unter der Domain „plant“; nur für den Umzug
LEGACY_DOMAIN: Final = "plant"
# Unterpunkt je Pflanze
SUBENTRY_PLANT: Final = "plant"

CONF_WEATHER: Final = "weather"
CONF_RAIN_SENSOR: Final = "rain_sensor"
CONF_OUTDOOR: Final = "outdoor"
CONF_AI_TASK: Final = "ai_task"
# Je Pflanze (Unterpunkt); Sensoren als „<Messgröße>_sensor“
CONF_NAME: Final = "name"
CONF_SPECIES: Final = "species"
CONF_IMAGE: Final = "image"
CONF_AREA: Final = "area"
CONF_PROBLEMS: Final = "problems"
CONF_GRACE: Final = "moisture_grace"
# Schlüssel der Pflanze (umgezogen: Config-Entry-ID im Plant Monitor, damit Gelerntes und KI-Verlauf bleiben)
CONF_KEY: Final = "key"

UPDATE_INTERVAL: Final = timedelta(minutes=10)
# Nach einem Neustart hält ein Spiegelsensor seinen letzten Wert so lange, bis der echte Sensor wieder meldet
RESTORE_HOLD: Final = timedelta(minutes=10)
# So oft wird die Lichtsumme auch ohne neue Messung fortgeschrieben
LIGHT_TICK: Final = timedelta(minutes=5)
# Stundenstatistik ist erst so lange nach Stundenende sicher da
STATS_DELAY: Final = timedelta(minutes=2)
# So viel Verlauf wird ausgewertet (Langzeitstatistik, stündlich)
HISTORY: Final = timedelta(days=120)
# Die übrigen Sensoren (Temperatur, Luftfeuchte, Licht) braucht nur die KI-Zusammenfassung der letzten Tage
CONTEXT_HISTORY: Final = timedelta(days=30)
# Regen: so weit voraus und ab so viel Menge
RAIN_HORIZON: Final = timedelta(hours=12)
RAIN_AMOUNT: Final = 1.0
# Frost: so weit voraus; Grenze, wenn die Pflanze keine Mindesttemperatur hat
FROST_HORIZON: Final = timedelta(hours=48)
FROST_DEFAULT: Final = 0.0

# Raumnamen, bei denen eine neue Pflanze als „draußen“ vorgeschlagen wird
OUTDOOR_WORDS: Final = (
    "terrasse",
    "balkon",
    "garten",
    "draußen",
    "außen",
    "aussen",
    "outdoor",
    "patio",
    "garden",
    "balcony",
    "terrace",
)

# Entitäten je Pflanze (Schlüssel = Ende der unique_id, „<Pflanze>_<key>“); danach räumt das Einrichten verwaiste weg
SENSOR_KEYS: Final = ("learning", "next_watering", "next_watering_due", "last_watered", "drying_rate")
AI_KEYS: Final = ("suggestion", "accept", "reject", "undo", "ask")
OUTDOOR_KEYS: Final = ("rain_expected", "frost")
