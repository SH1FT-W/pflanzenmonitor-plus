"""KI-Vorschläge für Grenzwerte: fragen (höchstens monatlich), speichern, übernehmen, ablehnen, zurücknehmen.

Nichts gilt ohne Freigabe: erst „Übernehmen“ schreibt in die Grenzwerte der Pflanze.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar, issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import advice
from .const import DOMAIN
from .plants import Plant, entity_id, status_id

if TYPE_CHECKING:
    from .coordinator import PlantData, PlusCoordinator

_LOGGER = logging.getLogger(__name__)

AI_DOMAIN: Final = "ai_task"
EVENT_SUGGESTION: Final = f"{DOMAIN}_suggestion"
# Automatisch höchstens so oft je Pflanze fragen; nach einem Fehler so lange warten
ASK_EVERY: Final = timedelta(days=30)
RETRY: Final = timedelta(hours=6)
# Länger warten wir nicht auf die KI: sonst hängt die Sperre (_lock) und der Knopf meldet nur „beschäftigt“
AI_TIMEOUT: Final = 120
# Nach einem übernommenen, von den Schutzregeln begrenzten Schritt: so bald nach dem nächsten Schritt fragen
NEXT_STEP: Final = timedelta(days=7)
SUGGESTION_ISSUE: Final = "suggestion_"
# Stand der gespeicherten Einträge; ältere (1.0, nur Feuchte und Mindesttemperatur) werden beim Laden neu gefragt
VERSION: Final = 2
STATES: Final = ["keiner", "offen", "keine_aenderung", "uebernommen", "abgelehnt", "zurueckgenommen"]


class Advisor:
    """Hält die Vorschläge aller Pflanzen; gespeichert im Store, damit sie Neustarts überleben."""

    def __init__(self, hass: HomeAssistant, coordinator: PlusCoordinator, ai: str | None) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.ai = ai
        entry = coordinator.config_entry
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.advice")
        self.records: dict[str, dict[str, Any]] = {}
        # Nie zwei KI-Anfragen oder eine Anfrage und ein Übernehmen gleichzeitig
        self._lock = asyncio.Lock()
        self._seen: object = None
        self._unloaded = False

    def shutdown(self) -> None:
        """Nach dem Entladen nichts mehr speichern (eine neue Instanz hat dann das Sagen)."""
        self._unloaded = True

    async def async_load(self, keys: set[str]) -> None:
        """Gespeicherte Vorschläge laden; die von gelöschten Pflanzen verwerfen."""
        stored = await self._store.async_load() or {}
        plants = stored.get("plants") if isinstance(stored, dict) else None
        self.records = {k: v for k, v in (plants or {}).items() if k in keys and isinstance(v, dict)}
        for key, rec in self.records.items():
            if rec.get("version") != VERSION:
                # Vorschlag aus 1.0: kannte nur drei Grenzwerte – verwerfen und bald für alle Werte neu fragen
                if rec.get("state") == "offen":
                    rec |= {"state": "keiner", "changes": {}}
                self.records[key] = rec | {"version": VERSION, "asked": None, "next_step": None}

    def record(self, key: str) -> dict[str, Any]:
        """Gespeicherter Stand einer Pflanze; unbekannte oder kaputte Einträge zählen als „keiner“."""
        rec = dict(self.records.get(key) or {})
        if rec.get("state") not in STATES:
            rec["state"] = "keiner"
        if not isinstance(rec.get("changes"), dict):
            rec["changes"] = {}
        if not isinstance(rec.get("tuned"), list):
            rec["tuned"] = []
        return rec

    def free(self, key: str, settable: set[advice.Field]) -> frozenset[advice.Field]:
        """Grenzwerte ohne Schrittbegrenzung: neu seit 1.1 und noch nie von Plus oder von Hand eingestellt."""
        tuned = set(self.record(key)["tuned"])
        return frozenset(n for n in settable if n not in advice.STEPPED and n not in tuned)

    def _hand_tuned(self, key: str, current: dict[advice.Field, float | None]) -> list[str]:
        """„Schon eingestellt“ plus alle Werte, die seit der letzten Anfrage jemand von Hand geändert hat."""
        rec = self.record(key)
        tuned = set(rec["tuned"])
        seen: dict[str, Any] = rec["seen"] if isinstance(rec.get("seen"), dict) else {}
        for name, value in current.items():
            before = seen.get(name)
            if value is not None and isinstance(before, (int, float)) and abs(value - before) >= advice.ROUND[name]:
                tuned.add(name)
        return sorted(tuned)

    def _species(self, plant: Plant) -> str | None:
        """Eingetragene Art der Pflanze."""
        return plant.species

    def _season(self, plant: Plant) -> dict[str, bool]:
        """Für die Schutzregeln: draußen? Sommer (Mai bis August, auf der Südhalbkugel November bis Februar)?"""
        months = (5, 6, 7, 8) if self.hass.config.latitude >= 0 else (11, 12, 1, 2)
        return {"outdoor": plant.outdoor, "summer": dt_util.now().month in months}

    def next_check(self, key: str) -> datetime | None:
        """Wann automatisch wieder gefragt wird (None = sobald die Pflanze gelernt ist)."""
        rec = self.record(key)
        asked, retry, step = _time(rec.get("asked")), _time(rec.get("retry_after")), _time(rec.get("next_step"))
        # Wurde ein begrenzter Schritt übernommen, kommt der nächste Schritt früher als nach 30 Tagen
        regular = step or (asked + ASK_EVERY if asked else None)
        times = [t for t in (regular, retry) if t]
        return max(times) if times else None

    def _due(self, key: str, data: PlantData, now: datetime) -> bool:
        if data.learned.stage != "bereit":
            return False
        rec = self.record(key)
        if rec["state"] == "offen":
            return False
        # Art eingetragen oder geändert: gleich neu fragen (nur die Fehlersperre gilt)
        if rec.get("asked") and self._species(self.coordinator.plants[key]) != rec.get("plant_species"):
            retry = _time(rec.get("retry_after"))
            return retry is None or now >= retry
        nxt = self.next_check(key)
        return nxt is None or now >= nxt

    async def async_run_due(self) -> None:
        """Nach jedem Durchlauf: höchstens eine fällige Pflanze fragen (verteilt die Anfragen)."""
        data = self.coordinator.data
        # Nur nach neuen Daten (nicht nach eigenem Speichern) – so verteilt sich das Fragen auf die Durchläufe
        if not self.ai or self._lock.locked() or not data or data is self._seen:
            return
        self._seen = data
        now = dt_util.utcnow()
        for key, item in data.items():
            if key in self.coordinator.plants and self._due(key, item, now):
                await self.async_ask(key, manual=False)
                return

    async def async_ask(self, key: str, *, manual: bool = True) -> None:
        """KI fragen; bei `manual` (Knopf) Fehler sichtbar machen."""
        if not self.ai:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_ai")
        data = (self.coordinator.data or {}).get(key)
        if data is None or data.learned.stage == "keine_daten":
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_data")
        if self._lock.locked():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="busy")
        async with self._lock:
            await self._ask(key, data, manual)

    async def _ask(self, key: str, data: PlantData, manual: bool) -> None:
        plant = self.coordinator.plants[key]
        current = self._current(plant)
        settable = self._settable(plant)
        # Von Hand geänderte Werte gelten als eingestellt (nie mehr frei)
        self.records[key] = self.record(key) | {"tuned": self._hand_tuned(key, current)}
        free = self.free(key, settable)
        fields = tuple(name for name in advice.FIELDS if name in settable)
        try:
            async with asyncio.timeout(AI_TIMEOUT):
                response = await self.hass.services.async_call(
                    AI_DOMAIN,
                    "generate_data",
                    {
                        "task_name": f"Pflanzenmonitor Plus: {self._name(plant)}",
                        ATTR_ENTITY_ID: self.ai,
                        "instructions": advice.instructions(
                            self._context(plant, data, current, fields), self.hass.config.language, fields, free
                        ),
                        "structure": advice.structure(fields),
                    },
                    blocking=True,
                    return_response=True,
                )
            # Grenzwerte erst nach der Antwort lesen – sie könnten sich inzwischen geändert haben
            current = self._current(plant)
            result = advice.sanitize(current, (response or {}).get("data"), settable, free, **self._season(plant))
            if result is None:
                raise HomeAssistantError("unbrauchbare Antwort")
        except Exception as err:
            # Jeder Fehler (auch Zeitüberschreitung beim Anbieter) sperrt die Pflanze für RETRY –
            # sonst würde bei jedem Durchlauf erneut kostenpflichtig gefragt.
            _LOGGER.warning("KI-Vorschlag für %s fehlgeschlagen: %s", plant.name, err)
            message = str(err) or type(err).__name__
            self.records[key] = self.record(key) | {
                "retry_after": (dt_util.utcnow() + RETRY).isoformat(),
                "error": message,
            }
            await self._save()
            if manual:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="ask_failed",
                    translation_placeholders={"error": message},
                ) from err
            return

        state = "offen" if result.changes else "keine_aenderung"
        summary = advice.describe(result.changes, self.hass.config.language)
        confidence = result.confidence
        # Ohne eingetragene Art rät die KI nur aus dem Namen – dann höchstens „mittel“
        if not self._species(plant) and confidence == "high":
            confidence = "medium"
        self.records[key] = {
            "state": state,
            "asked": dt_util.utcnow().isoformat(),
            "retry_after": None,
            "error": None,
            "changes": {name: asdict(change) for name, change in result.changes.items()},
            "confidence": confidence,
            "summary": summary,
            "ai_summary": result.ai_summary,
            "species": result.species,
            "applied": self.record(key).get("applied"),
            "next_step": None,
            "tuned": self.record(key)["tuned"],
            # Werte beim Fragen (erkennt später Änderungen von Hand) und eingetragene Art (Änderung = neu fragen)
            "seen": current,
            "plant_species": self._species(plant),
            "version": VERSION,
        }
        await self._save()
        if state == "offen":
            self.hass.bus.async_fire(
                EVENT_SUGGESTION,
                {
                    "entity_id": entity_id(self.hass, "sensor", status_id(key)),
                    "summary": summary,
                    "changes": self.records[key]["changes"],
                },
            )

    async def async_apply(self, key: str) -> None:
        """Vorschlag in die Grenzwerte der Pflanze schreiben (alte Werte für „Zurücknehmen“ merken).

        Die Schutzregeln gelten gegen die Werte von JETZT: wurde inzwischen von Hand etwas geändert und passt der
        Vorschlag nicht mehr, wird nichts geschrieben.
        """
        if self._lock.locked():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="busy")
        async with self._lock:
            rec = self.record(key)
            if rec["state"] != "offen" or not rec["changes"]:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_suggestion")
            plant = self.coordinator.plants[key]
            current = self._current(plant)
            new = {name: float(change["proposed"]) for name, change in rec["changes"].items()}
            settable = self._settable(plant)
            check = advice.sanitize(current, new, settable, self.free(key, settable), **self._season(plant))
            # Dieselbe Schwelle wie in den Schutzregeln: was schon (fast) so eingestellt ist, wird nicht geschrieben
            new = {n: v for n, v in new.items() if (now := current.get(n)) is None or abs(v - now) >= advice.ROUND[n]}
            if check is None or not new or {n: c.proposed for n, c in check.changes.items()} != new:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="suggestion_outdated")
            old = {n: current.get(n) for n in new}
            self.coordinator.limits.async_set(key, new)
            limited = any(change.get("requested") is not None for change in rec["changes"].values())
            self.records[key] = rec | {
                "state": "uebernommen",
                "applied": {"at": dt_util.utcnow().isoformat(), "old": old, "new": new, "tuned": rec["tuned"]},
                "tuned": sorted(set(rec["tuned"]) | set(new)),
                "next_step": (dt_util.utcnow() + NEXT_STEP).isoformat() if limited else None,
            }
            await self._save()
        await self.coordinator.async_request_refresh()

    async def async_reject(self, key: str) -> None:
        """Vorschlag verwerfen; nicht während einer laufenden Anfrage oder Übernahme (die überschriebe es sonst)."""
        if self._lock.locked():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="busy")
        rec = self.record(key)
        if rec["state"] != "offen":
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_suggestion")
        self.records[key] = rec | {"state": "abgelehnt"}
        await self._save()

    async def async_undo(self, key: str) -> None:
        """Letzte Übernahme zurücknehmen – nur direkt danach und nur, wenn seitdem niemand die Werte geändert hat."""
        if self._lock.locked():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="busy")
        async with self._lock:
            rec = self.record(key)
            applied = rec.get("applied")
            if rec["state"] != "uebernommen" or not isinstance(applied, dict):
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="nothing_to_undo")
            plant = self.coordinator.plants[key]
            current = self._current(plant)
            if any(current.get(n) != v for n, v in applied["new"].items()):
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="changed_since")
            self.coordinator.limits.async_set(key, {n: v for n, v in applied["old"].items() if v is not None})
            tuned = applied.get("tuned")
            self.records[key] = rec | {
                "state": "zurueckgenommen",
                "applied": None,
                "tuned": tuned if isinstance(tuned, list) else rec["tuned"],
            }
            await self._save()
        await self.coordinator.async_request_refresh()

    async def _save(self) -> None:
        if self._unloaded:
            return
        await self._store.async_save({"plants": self.records})
        self.sync_issues()
        self.coordinator.async_update_listeners()

    def sync_issues(self) -> None:
        """Offener Vorschlag = Eintrag unter Reparaturen (mit Dialog zum Übernehmen/Ablehnen), sonst keiner."""
        for key, plant in self.coordinator.plants.items():
            issue_id = f"{SUGGESTION_ISSUE}{key}"
            if self.record(key)["state"] == "offen":
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=True,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="suggestion",
                    translation_placeholders={"plant": self._name(plant)},
                    data={"key": key},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def repair_placeholders(self, key: str) -> dict[str, str]:
        """Texte für den Reparatur-Dialog."""
        rec = self.record(key)
        language = self.hass.config.language
        changes = {name: advice.Change(**change) for name, change in rec["changes"].items() if name in advice.FIELDS}
        return {
            "plant": self._name(self.coordinator.plants[key]),
            "summary": advice.overview(changes, language),
            "reasons": advice.reasons(changes, language),
            "confidence": advice.confidence_text(str(rec.get("confidence") or "low"), language),
            "note": advice.step_note(changes, language, NEXT_STEP.days),
        }

    def _settable(self, plant: Plant) -> set[advice.Field]:
        """Grenzwerte, zu denen die Pflanze einen Sensor hat.

        Helligkeit in lx nicht, wenn der Sensor schon PPFD liefert (dann gibt es keine lx-Prüfung).
        """
        ppfd = self.coordinator.ppfd(plant)
        return {
            name
            for name in advice.FIELDS
            if plant.has(advice.SENSOR[name]) and not (ppfd and name in ("min_illuminance", "max_illuminance"))
        }

    def _current(self, plant: Plant) -> dict[advice.Field, float | None]:
        return {name: self.coordinator.limits.get(plant.key, name) for name in advice.FIELDS}

    def _name(self, plant: Plant) -> str:
        return plant.name

    def _context(
        self,
        plant: Plant,
        data: PlantData,
        current: dict[advice.Field, float | None],
        fields: tuple[advice.Field, ...],
    ) -> dict[str, Any]:
        """Was die KI über die Pflanze erfährt: Pflanzenname, Art, Raumname, drinnen/draußen, Monat, Land und Zahlen.

        Keine Koordinaten, Adressen oder Personendaten aus HA – der frei gewählte Pflanzen- und Raumname geht aber mit.
        """
        area = None
        if area_id := self.coordinator.device_area(plant):
            area_entry = ar.async_get(self.hass).async_get_area(area_id)
            area = area_entry.name if area_entry else None
        learned = data.learned
        return {
            "plant_name": self._name(plant),
            "species": self._species(plant),
            "room": area,
            "outdoor": plant.outdoor,
            "month": dt_util.now().strftime("%B"),
            "country": self.hass.config.country,
            "current_limits": {name: current[name] for name in fields},
            "learned": {
                "complete_watering_cycles": learned.cycles,
                "usually_watered_at_moisture": learned.threshold,
                "typical_moisture_right_after_watering": learned.peak,
                "typical_days_between_waterings": learned.cycle_days,
                "drying_rate_percent_per_day_now": learned.rate,
                "days_until_lower_limit": learned.days,
            },
            "moisture_now": data.moisture,
            "last_30_days": {
                "moisture": self.coordinator.summary(plant.moisture),
                "air_temperature": self.coordinator.summary(plant.sensors.get("temperature")),
                "soil_temperature": self.coordinator.summary(plant.sensors.get("soil_temperature")),
                "air_humidity": self.coordinator.summary(plant.sensors.get("humidity")),
                "light": self.coordinator.light(plant),
            },
        }


def _time(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None
