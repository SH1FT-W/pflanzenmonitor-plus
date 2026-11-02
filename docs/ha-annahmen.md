# HA-Annahmen

<!-- Vorlage aus repo-vorlagen/vorlagen/ha-annahmen.md. Liegt im Repo als docs/ha-annahmen.md.
     Der HA-Beta-Bericht (repo-vorlagen/ha-beta-bericht.md) gleicht diese Liste jeden Monat mit den
     Neuerungen der HA-Beta ab. Nur festhalten, was bei einer Änderung in HA still kaputtginge. -->

Worauf sich dieses Repo bei Home Assistant verlässt. Bei jeder Änderung am Code mitpflegen.

**Getestet mit:** HA 2026.9.3 · **Mindestversion:** siehe `hacs.json` / `manifest.json` (2026.9.0)

## Annahmen

| Annahme | Wo im Code | Abgesichert durch |
|---|---|---|
| Unterpunkte (Config-Subentries): `async_get_supported_subentry_types`, `ConfigSubentryFlow` mit `reconfigure`, `async_add_subentry`/`async_update_subentry`/`async_remove_subentry` rufen die Update-Listener; Entfernen räumt Gerät und Entitäten des Unterpunkts weg | `config_flow.py` `PlantFlow`, `__init__.py` `_changed`, `migrate.py` `_create` | `tests/test_config_flow.py` `test_add_plant`, `test_reconfigure_plant`; `tests/test_init.py` `test_plants_added_and_removed` |
| `async_add_entities(..., config_subentry_id=…)` hängt Entitäten und Gerät an den Unterpunkt | `sensor.py`, `number.py`, `binary_sensor.py`, `button.py` | `tests/test_init.py` `test_sensors_on_plant_device` |
| Update-Listener laufen eager: ein `async_schedule_reload` kann schon während der nächsten Zeile ablaufen → bei mehreren Änderungen erst am Ende neu laden | `migrate.py` `_create` (`reload_pending`) | `tests/test_migrate.py` `test_migrate_two_plants_without_device` |
| `device_registry.async_get_device` ist verboten (Frame-Fehler); Geräte über `async_get_device_by_identifier(identifier, config_entry_id)` | `plants.py` `device`, `migrate.py` `read` | alle Tests mit Gerät |
| `DeviceInfo.suggested_area` legt den Raum beim ersten Anlegen des Geräts an (seit 2025.x „runtime only“, Ablösung angekündigt) | `entity.py` `device_info` | `tests/test_config_flow.py` `test_add_plant` |
| Entitäts-IDs neuer Entitäten bekommen den Raum vorangestellt (`sensor.schlafzimmer_bogenhanf_…`) | Tests, README | Tests mit IDs |
| `entity_registry.async_get_or_create(..., suggested_object_id=…)` vergibt genau diese ID, wenn sie frei ist (kein Zustand, kein Registereintrag) – darauf beruht „gleiche IDs nach dem Umzug“ | `migrate.py` `_create` | `tests/test_migrate.py` `test_migrate` |
| Entfernen eines Geräts löscht alle Entitäten mit dieser `device_id`, auch fremder Integrationen | `migrate.py` `async_migrate` (Plus-Entitäten vorher lösen) | `tests/test_migrate.py` `test_migrate` |
| Statistik hängt an der `entity_id`: neue Entität mit gleicher ID, Einheit und `state_class` führt den Verlauf weiter | `migrate.py`, `sensor.py` (Einheiten wie im Plant Monitor) | – (nur in Prod beobachtbar) |
| Registry-Optionen `conversation.should_expose`, `sensor.display_precision` liegen in `RegistryEntry.options` und lassen sich mit `async_update_entity_options` setzen | `migrate.py` `_create` | `tests/test_migrate.py` `test_migrate` |
| Repairs-Flow: `async_init` übergibt die Hinweis-Daten als `user_input` an den ersten Schritt | `repairs.py` `MigrateRepair.async_step_init` | `tests/test_migrate.py` `test_migrate` |
| Zweck-Auslöser/-Bedingungen (2026.7): `make_entity_target_state_trigger`, `make_entity_state_condition`, `DomainSpec(value_source=…)`, `entity_filter`, `async_track_target_selector_state_change_event` (async), `trigger: <domain>.<key>` in Automationen | `trigger.py`, `condition.py`, `automation.py` | `tests/test_automation.py` |
| `triggers.yaml`/`conditions.yaml`/`services.yaml` werden über `async_get_all_descriptions` gelesen | Beschreibungsdateien | `tests/test_automation.py` `test_descriptions_valid` |
| Logbuch über die Aktion `logbook.log` (`name`, `message`, `domain`, `entity_id`) | `live.py` `_logbook` | `tests/test_live.py` `test_status_mirror_and_logbook` |
| HA schreibt das Mikro-Zeichen in Einheiten als griechisches μ | `live.py` `ppfd_factor` | `tests/test_live.py` `test_ppfd_source` |
| `NumberEntity` mit `NumberDeviceClass.TEMPERATURE` und `°C` rechnet in die Systemeinheit um | `number.py` | `tests/test_live.py` `test_limits_from_number_entities` |
| `automations_with_entity`/`scripts_with_entity` finden Automationen/Skripte zu einer Entitäts-ID | `migrate.py` `_references` | `tests/test_migrate.py` `test_migrate` |
| Recorder `statistics_during_period(hass, start, None, ids, "hour", None, {"mean","min","max"})` | `coordinator.py` `_fetch` | Tests über `stats` |
| `weather.get_forecasts` mit `type: hourly` und `return_response` | `coordinator.py` `_forecast` | `tests/test_init.py` |
| Issue-Registry: `ir.async_get(hass).issues` ist ein Dict mit Schlüssel `(domain, issue_id)`; `async_delete_issue` ist bei fehlendem Eintrag ein No-op | `advisor.py` `sync_issues`/`_plant`, `repairs.py` `SuggestionRepair`, `__init__.py` `_remove_stale` | `tests/test_repairs.py` `test_issue_of_removed_plant_goes_away`, `test_accept_for_removed_plant` |
| `ai_task.generate_data` mit `task_name`, `instructions`, `structure`, Antwort in `data` | `advisor.py` `_ask`, `careplan.py` `_ask` | `tests/test_advisor.py`, `tests/test_care_calendar.py` |
| To-do-Aktionen `todo.get_items` (mit `return_response`, Antwort `{<entity>: {"items": [{summary, uid, status, due…}]}}`), `todo.add_item` (`item`, `due_date` nur bei `TodoListEntityFeature.SET_DUE_DATE_ON_ITEM`, sonst Fehler), `todo.update_item` (`item` = uid oder Titel, `status: completed`); `add_item` gibt keine uid zurück → danach über `get_items` suchen; Status-Werte `needs_action`/`completed` | `tasks.py` | `tests/test_tasks.py` |
| Die `uid` einer To-do-Aufgabe bleibt über Neustarts gleich (sonst gilt die Aufgabe als vom Nutzer gelöscht und wird beim Gießen nicht mehr abgehakt) | `tasks.py` `_find` | – (je Listen-Integration; lokal geprüft nur mit Test-Liste) |
| Unterstützte Funktionen einer To-do-Liste stehen im Zustandsattribut `supported_features` | `tasks.py` `_add` | `tests/test_tasks.py` `test_deleted_by_user_and_adopted` |
| `CalendarEntity`: `event` (nächster/laufender Termin) und `async_get_events(hass, start, end)`; Ganztagstermine mit `date` (Ende exklusiv); `_async_write_ha_state` plant selbst die Aktualisierung bei Beginn und Ende des Termins (über Mitternacht kein eigener Timer nötig) | `calendar.py` | `tests/test_care_calendar.py` |
| Entität ohne Gerät mit `has_entity_name` bekommt den übersetzten Namen als Entitäts-ID (`calendar.pflanzenpflege`) | `calendar.py` | `tests/test_care_calendar.py`, `tests/test_care_option.py` |
| `SelectSelector` in Formularen liefert Strings (Monate „1“–„12“) | `config_flow.py` `async_step_care`, `care.plan` | `tests/test_config_flow.py` `test_add_plant` |
| `ConfigSubentryFlow._get_entry()` liefert im Pflanzen-Dialog den Eintrag samt Optionen (Schritt „Pflege“ nur mit Pflegekalender) | `config_flow.py` `async_step_problems` | `tests/test_config_flow.py` `test_add_plant`, `test_plant_without_care_step` |
| Eine weitergeleitete Plattform darf keine Entität anlegen (Kalender bei ausgeschaltetem Pflegekalender) und lädt trotzdem sauber ent- und neu; eine per `entity_registry.async_remove` entfernte Entität bekommt beim Wiederanlegen dieselbe Entitäts-ID | `calendar.py`, `button.py`, `__init__.py` `_remove_care`/`_remove_stale` | `tests/test_care_option.py` `test_switch_on_off_on` |

## Fremde Integrationen

- **Plant Monitor** (HACS, Domain `plant`, nur für den Umzug): Einträge mit `data.plant_info` (`name`, `<m>_sensor`,
  `limits`, `limits_temperature_unit`, `lux_to_ppfd`, `entity_picture`, `display_pid`), Optionen (`display_pid`,
  `species`, `entity_picture`, `<m>_trigger`, `moisture_grace_period`), unique_ids `<entry>`, `<entry>-current-<m>`,
  `<entry>-<min|max>-<m>`, `<entry>-lux-to-ppfd`, `<entry>-ppfd-integral`, `<entry>-dli` (Attribut `last_period`),
  Spiegel-Attribut `external_sensor`, Attribute der `plant.*`-Entität (`<m>_status`, `problems`), Gerät
  `("plant", <entry>)` – `migrate.py`.
- **Casora** liest den Zustand der Pflanze (`sensor.<pflanze>`, Plattform `pflanzenmonitor_plus`, unique_id
  `…-status`) wie bisher `plant.*`: Attribute `problems`, `<m>_status`, `species`.

## Prüfbefehl

```sh
keiner
```
