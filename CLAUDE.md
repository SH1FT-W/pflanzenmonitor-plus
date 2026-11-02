# Pflanzenmonitor Plus – Projektregeln

HA-Integration (`custom_components/pflanzenmonitor_plus`, Domain `pflanzenmonitor_plus`), SH1FT-W/pflanzenmonitor-plus.
Eigenständige Pflanzen-Integration (ersetzt den Plant Monitor, HACS, Olen, Domain `plant`, GPL-3.0; dessen Pflanzen
zieht ein Reparatur-Dialog um). Lizenz Apache 2.0: **keinen Code aus dem Plant Monitor übernehmen**, nur Verhalten
nachbauen (NOTICE).

- Repo ist **öffentlich**: nichts Persönliches ins Repo (keine Namen, echten Entitäts-/Eintrags-IDs, Heimnetz-Details,
  lokalen Pfade). Testdaten bleiben fiktiv.
- README, CHANGELOG, Release-Notizen, Issue-Texte und Commit-Texte auf Englisch.
- Lokal committen ok, Push und Releases nur nach Rückfrage. Die eigene HA-Installation nur auf ausdrücklichen Wunsch.
Jede Änderung = neue Version in `manifest.json` (Privat-Updates installiert über diese Version).
Versionen = Kalender-Schema `YY.M.N` (Jahr, Monat ohne Null, N = laufendes Release im Monat ab 1; ab 26.10.x, kein X.Y.Z mehr): `tools/next-version` rechnet die nächste aus (zählt nur echte, veröffentlichte GitHub-Releases des Monats, auch alte X.Y.Z, bloße Tags nicht; ohne Release im Monat → .1; jedes Release als GitHub-Release veröffentlichen). Tag/Release-Titel = genau diese Nummer ohne „v“.
Platin-Qualität halten: `quality_scale.yaml` bei jeder Änderung mitpflegen.

## Prüfen
- venv im Repo: `.venv` (gitignored; anlegen mit `uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt pytest-cov`)
- Tests: `LC_ALL=en_US TZ=UTC .venv/bin/python -m pytest tests -q -p no:logging --cov=custom_components.pflanzenmonitor_plus` (≥ 98 %)
- `uvx ruff check . && uvx ruff format --check .`
- `uvx mypy@2.3.1 --python-executable .venv/bin/python`
- hassfest: `docker run --rm -v "$PWD:/github/workspace" ghcr.io/home-assistant/hassfest` (kein Docker auf dem Mac → läuft in CI; `test_descriptions_valid` prüft die YAML-Beschreibungen lokal)
- Übersetzungen: `python3 tools/pruefe-uebersetzungen.py` (strings.json gegen translations/*.json)
- CI: `.github/workflows/pruefung.yml` (Vorlage repo-vorlagen/vorlagen/ci-ha-integration.yml): ruff, mypy, Übersetzungen,
  pytest mit Abdeckungsgrenze 98 %, hassfest, montags Frühwarnung gegen die neueste HA; Pakete in `requirements_test.txt`
- `tools/next-version` und `tools/pruefe-uebersetzungen.py` sind byteidentische Kopien der Vorlagen (`repo-vorlagen/vorlagen/abgleich.sh`)
- Tests mit `freezer` frieren auch die Loop-Zeit ein; `asyncio.timeout` läuft erst nach `freezer.tick(...)` ab
- HA-Annahmen: `docs/ha-annahmen.md` mitpflegen
- Echte Daten (nur lesend!): `sqlite3` mit `file:<HA-Konfiguration>/home-assistant_v2.db?mode=ro&immutable=1`, Tabelle
  `statistics` (stündlich) über `statistics_meta.statistic_id`; Rückblick-Test gegen die Vorhersage.

## Aufbau
- `model.py` rein, ohne HA: Anstiege (≥ 10 Punkte gegen Tiefstwert der 3 Std. davor, 12 Std. zusammengefasst),
  Zyklen (Anstieg bis Anstieg, ≥ 24 Std.), Vorhersage aus den letzten 3 Zyklen, mit Tempo-Verhältnis über dieselben
  72 Std. vor dem heutigen Stand umgerechnet (max. Faktor 3); Ziel = Untergrenze, sonst Gewohnheit.
  Gewohnheit ist NUR Info (viele gießen weit vor Bedarf).
- Pflanzen = Unterpunkte (Subentries, Typ `plant`) des einen Eintrags (`plants.py`): Name, Art, Bild, Raum,
  `<m>_sensor` (echte Sensoren), `outdoor`, `problems`, `moisture_grace` (s), `key` (umgezogen: Entry-ID im Plant
  Monitor, sonst subentry_id) – Store-Schlüssel und unique_ids hängen am `key`.
- `health.py` rein: Zustand wie Plant Monitor (Low/High/ok, 5 % Spielraum, Licht nur High, DLI gegen gestern, Wartezeit
  unterdrückt nur High), VPD, Lichtsumme. `live.py`: je Pflanze Spiegel (Einheit vom Quellsensor, nach Neustart
  10 Min. letzter Wert), PPFD/DLI (Tick 5 Min. + Mitternacht), Logbuch; Store `<entry>.live`. `limits.py`: Grenzwerte
  in °C, Store `<entry>.limits`, number-Entitäten zeigen sie nur.
- unique_ids: `<key>-status` (Zustand, Name = Gerätename → `sensor.<pflanze>`), `<key>-current-<m>`, `<key>-<min|max>-<m>`,
  `<key>-dli` usw. wie im Plant Monitor (Umzug behält IDs); Gelerntes `<key>_<k>`. Eigene Geräte `(DOMAIN, key)`.
- `migrate.py`: Reparatur „migrate“ → echte Grenzwerte aus den number-Zuständen, Sensoren aus `external_sensor`,
  Register-Einstellungen (Name, Icon, Labels, hidden, user-disabled, alle Optionen inkl. conversation), Live-Stand;
  Plus-Entitäten vom alten Gerät lösen, Olen-Einträge entfernen, dann OHNE await Unterpunkte + Registereinträge mit
  alten IDs, einmal neu laden (Update-Listener laufen eager!). Hinweis „references“ für Automationen mit alten IDs.
- `trigger.py`/`condition.py`: 43 Auslöser, 35 Bedingungen wie Plant Monitor, Ziel = Zustands-Entitäten (Filter über
  Registry, nicht nur Domain sensor). „Veraltet“ eigenständig: `automation.stale()` aus `last_reported` (auch gleicher
  Wert zählt), Neuberechnung je Meldung und Frist, still veraltet alle 5 Min. Aktion `replace_sensor`.
- `coordinator.py`: Lernen aus Statistik (echter Sensor oder Spiegel, der längere), Wetter, KI-Kontext.
- KI-Vorschläge (`advice.py` rein: Anfrage, Antwortformat, Schutzregeln; `advisor.py`: Store
  `pflanzenmonitor_plus.<entry>.advice`, fragen/übernehmen/ablehnen/zurücknehmen) über `ai_task.generate_data`
  (Option `ai_task`); schreibt direkt in `limits.py`. Schutzregeln NUR im Code ändern, nie dem Prompt überlassen. Echter Test
  gegen Claude: Bogenhanf max 40 → 70 (Peak 85 %), Schutzregel macht 55.
- 1.1: alle Grenzwerte mit Sensor (advice.FIELDS, 12 Stück; CO2/VPD bewusst nicht). `STEPPED` (Feuchte, Mindesttemp.)
  immer schrittweise, die übrigen frei, bis Plus sie einmal geschrieben hat (Store `tuned`; Zurücknehmen stellt es
  wieder her). Prod-Werte sind NICHT die Plant-Monitor-Startwerte im Config-Eintrag (10–40 °C, 0–100000 lx) – deshalb
  „noch nie von Plus geschrieben“ statt Vergleich mit Startwert. Licht: Plant Monitor prüft nur max lx (Momentanwert)
  und DLI (Vortag); Kontext `light` = Tagesspitzen + DLI aus Stundenmitteln × lux-to-ppfd. Echter Test 16.10.:
  Bogenhanf max lx 6000 (Spitzen ~1800), DLI 0–5, Art erkannt.
- 26.10.5: draußen (Kübel) Frostgrenze im Code (`OUTDOOR_FLOOR`: Luft ≥ −5 °C, Boden ≥ 0 °C) und außerhalb des Sommers
  (Mai–Aug., Südhalbkugel Nov.–Feb.) max lx/max DLI nie senken (`OUTDOOR_NO_LOWER`); Anlass Olivenbaum 17.10. (KI wollte
  −8 °C und 100000 → 20000 lx). Art = Feld `species` der Pflanze (Umzug: `display_pid`, sonst `species`).
- 26.11.1 Gieß-Aufgaben (`tasks.py`, Option `todo_list`, Store `<entry>.tasks`): fällig = `due_date` ≤ heute; je
  Pflanze ein Eintrag `open`/`closed`, `cycle` = jüngster Anstieg beim Anlegen; späterer Anstieg = gegossen → Plus hakt
  ab; vom Nutzer abgehakt/gelöscht → erst nach neuem Anstieg wieder. Offene Aufgabe gleichen Titels wird übernommen.
- 26.11.1 Pflegekalender (`care.py` rein, `careplan.py` Store `<entry>.care` mit Daten + KI-Vorschlag,
  `calendar.py` eine Entität für alle, unique_id `<entry>-care`): Plan im Subentry (`fertilize_interval`,
  `fertilize_from`, `fertilize_to`, `repot_years`; fehlend = Standard 3–9/21 Tage/2 Jahre, Süd 9–3), Umtopfen im
  ersten Saisonmonat. Knöpfe `<key>_fertilized`/`<key>_repotted`, Aktion `care_done`. KI einmal je Jahreszeit
  (`season_key`), Reparatur `care_<key>`, Übernehmen schreibt ins Subentry (lädt neu). Advisor und Pflegeplan teilen
  EINE Sperre (`advisor.lock`, auch für „KI jetzt fragen“): nie zwei KI-Anfragen. Aufgaben laufen getrennt davon
  (eigene Hintergrundaufgabe, 30 s Timeout je To-do-Aufruf, Fehler je Pflanze abgefangen). Abhaken nach dem Gießen
  erst, wenn die Stundenstatistik eine Stunde NACH dem Anstieg hat UND deren Mittel ≥ before + JUMP/2 liegt
  (`tasks.latest_rise`; HA-Mittel ist zeitgewichtet, ein stehender Ausreißer hebt sonst die eigene Stunde) → 1–2 Std.
  Verzug. Gegossen = Anstieg > `cycle` + MERGE (Gruppenstart kann am 120-Tage-Rand/bei Quellenwechsel wandern).

## Plan
1.0 Lernen + KI-Vorschläge · 26.10.1 (erstes öffentliches Release) eigenständig (alles vom Plant Monitor, Umzug) · danach Casora-Kachel +
Klartext-Benachrichtigung; OpenPlantbook erst auf Wunsch.
