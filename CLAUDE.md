# Pflanzenmonitor Plus – Projektregeln

HA-Integration (`custom_components/pflanzenmonitor_plus`, Domain `pflanzenmonitor_plus`), SH1FT-W/pflanzenmonitor-plus.
Eigenständige Pflanzen-Integration (ersetzt den Plant Monitor, HACS, Olen, Domain `plant`, GPL-3.0; dessen Pflanzen
zieht ein Reparatur-Dialog um). Lizenz Apache 2.0: **keinen Code aus dem Plant Monitor übernehmen**, nur Verhalten
nachbauen (NOTICE).

- Repo ist **öffentlich**: nichts Persönliches ins Repo (keine Namen, echten Entitäts-/Eintrags-IDs, Heimnetz-Details,
  lokalen Pfade). Testdaten bleiben fiktiv.
- README, CHANGELOG, Release-Notizen, Issue-Texte und Commit-Texte auf Englisch.
- Lokal committen ok, Push und Releases nur nach Rückfrage. Die eigene HA-Installation nur auf ausdrücklichen Wunsch.
- Jede Änderung = neue Version in `manifest.json` (Privat-Updates installiert über diese Version). Versionen über
  `tools/next-version` (globale Regeln), Tag/Release-Titel ohne „v“.
- Platin-Qualität halten: `quality_scale.yaml` bei jeder Änderung mitpflegen.

## Prüfen
- venv im Repo: `.venv` (gitignored; anlegen mit `uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt pytest-cov`)
- Tests: `LC_ALL=en_US TZ=UTC .venv/bin/python -m pytest tests -q -p no:logging --cov=custom_components.pflanzenmonitor_plus` (≥ 98 %)
- `uvx ruff@0.16.9 check . && uvx ruff@0.16.9 format --check .` (Version wie in `pruefung.yml`)
- `uvx mypy@2.3.1 --python-executable .venv/bin/python`
- hassfest: `docker run --rm -v "$PWD:/github/workspace" ghcr.io/home-assistant/hassfest` (läuft auch in CI;
  `test_descriptions_valid` prüft die YAML-Beschreibungen lokal)
- Übersetzungen: `python3 tools/pruefe-uebersetzungen.py` (strings.json gegen translations/*.json)
- CI: `.github/workflows/pruefung.yml` (Vorlage repo-vorlagen/vorlagen/ci-ha-integration.yml), montags Frühwarnung
  gegen die neueste HA; Pakete in `requirements_test.txt`
- `tools/next-version` und `tools/pruefe-uebersetzungen.py` sind byteidentische Kopien der Vorlagen (`repo-vorlagen/vorlagen/abgleich.sh`)
- Tests mit `freezer` frieren auch die Loop-Zeit ein; `asyncio.timeout` läuft erst nach `freezer.tick(...)` ab
- Echte Daten (nur lesend!): `sqlite3` mit `file:<HA-Konfiguration>/home-assistant_v2.db?mode=ro&immutable=1`, Tabelle
  `statistics` (stündlich) über `statistics_meta.statistic_id`; Rückblick-Test gegen die Vorhersage.

## Aufbau
- Rein, ohne HA (zuerst hier testen): `model.py` (Anstiege, Zyklen, Vorhersage), `health.py` (Zustand wie Plant Monitor,
  VPD, Lichtsumme), `advice.py` (KI-Anfrage, Antwortformat, Schutzregeln), `care.py` (Pflegeplan).
- Pflanzen = Unterpunkte (Subentries, Typ `plant`) des einen Eintrags (`plants.py`); `key` = umgezogen: Entry-ID im
  Plant Monitor, sonst subentry_id. Store-Schlüssel und unique_ids hängen am `key`.
- `live.py` Spiegel/PPFD/DLI/Logbuch, `limits.py` Grenzwerte (number-Entitäten zeigen sie nur), `coordinator.py` Lernen
  aus Statistik, Wetter, KI-Kontext, `advisor.py` KI-Vorschläge, `careplan.py` + `calendar.py` Pflegekalender,
  `tasks.py` Gieß-Aufgaben, `migrate.py` Umzug, `trigger.py`/`condition.py`/`automation.py` Auslöser/Bedingungen.

## Warum es so ist (steht nicht im Code)
- Gewohnheit ist NUR Info (viele gießen weit vor Bedarf); Ziel der Vorhersage ist die Untergrenze.
- unique_ids wie im Plant Monitor, damit der Umzug die IDs behält – nie ändern.
- Umzug: Unterpunkte + Registereinträge mit alten IDs OHNE await anlegen, dann einmal neu laden (Update-Listener
  laufen eager!). Automationen mit alten IDs bekommen den Hinweis „references“.
- Trigger/Conditions zielen auf Zustands-Entitäten über die Registry, nicht nur Domain sensor. „Veraltet“ aus
  `last_reported` (auch gleicher Wert zählt als Meldung).
- KI-Schutzregeln NUR im Code ändern, nie dem Prompt überlassen. Echter Test gegen Claude: Bogenhanf max 40 → 70
  (Peak 85 %), Schutzregel macht 55.
- Grenzwerte mit Sensor (`advice.FIELDS`), CO2/VPD bewusst nicht. `STEPPED` immer schrittweise, die übrigen frei, bis
  Plus sie einmal geschrieben hat (Store `tuned`). Prod-Werte sind NICHT die Plant-Monitor-Startwerte im
  Config-Eintrag – deshalb „noch nie von Plus geschrieben“ statt Vergleich mit Startwert.
- Licht: Plant Monitor prüft nur max lx (Momentanwert) und DLI (Vortag); darum Kontext `light` = Tagesspitzen + DLI aus
  Stundenmitteln. Echter Test 16.10.: Bogenhanf max lx 6000 (Spitzen ~1800), DLI 0–5, Art erkannt.
- Draußen: `OUTDOOR_FLOOR`/`OUTDOOR_NO_LOWER`, Anlass Olivenbaum 17.10. (KI wollte −8 °C und 100000 → 20000 lx).
- Advisor und Pflegeplan teilen EINE Sperre (`advisor.lock`, auch für „KI jetzt fragen“): nie zwei KI-Anfragen.
  Aufgaben laufen getrennt davon, Fehler je Pflanze abgefangen.
- Gieß-Aufgabe abhaken erst, wenn die Stundenstatistik eine Stunde NACH dem Anstieg hat und deren Mittel hoch genug
  ist (`tasks.latest_rise`): HA-Mittel ist zeitgewichtet, ein stehender Ausreißer hebt sonst die eigene Stunde → 1–2
  Std. Verzug ist gewollt. Gegossen = Anstieg > `cycle` + `MERGE`, weil der Gruppenstart am 120-Tage-Rand bzw. bei
  Quellenwechsel wandern kann. Vom Nutzer abgehakt/gelöscht → erst nach neuem Anstieg wieder.
- Pflegekalender (Option `care_calendar`, Standard aus): ausgeschaltet räumt `_remove_care` Entitäten und
  `care_`-Reparaturen weg, Store `<entry>.care` und Plan im Subentry bleiben.

## Plan
Offen: Casora-Kachel, Klartext-Benachrichtigung; OpenPlantbook erst auf Wunsch.
