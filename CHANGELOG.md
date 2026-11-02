# Changelog

## 26.11.1 – unreleased

- **Watering tasks (optional, off by default):** choose a to-do list in the options. When a plant is due ("Water in"
  reaches today), Plus adds a task like "Water Yucca" (with the due date if the list supports it),
  at most one open task per plant, also across restarts. Plus completes it once the soil moisture rises again
  (confirmed by the hourly statistics, so one outlier reading does not count); if you
  check it off or delete it, no new task comes until the plant has been watered and is due again. Stored in a new
  store `<entry>.tasks`.
- **Care calendar "Plant care":** fertilizing (every N days within the season, winter pause) and repotting (every N
  years in the first month of the season) for every plant, as all-day events. New buttons "Fertilized" and "Repotted"
  per plant and the action `pflanzenmonitor_plus.care_done` (with an optional date) move the dates on. The plan is set
  in a new step "Care" of the plant dialog (default March to September every 21 days, repot every 2 years; southern
  hemisphere September to March). Dates are kept in a new store `<entry>.care`; existing data is unchanged.
- **AI care plan:** with an AI task entity, Plus asks once per season and plant for a care plan for the species; a
  differing plan shows up under Repairs with Accept / Reject. Values are clamped in code.

- **Open AI suggestions no longer block a plant forever.** A suggestion nobody accepts or rejects now expires after
  30 days (the same cadence as the regular check, and the suggestion's data is fully outdated by then); the plant is
  then assessed again. The replaced suggestion is kept in the stored record, the AI suggestion entity shows when it was
  asked (attribute `expired`). If the new request fails, the open suggestion stays. A repair dialog whose suggestion
  was replaced meanwhile does not accept the new one unseen.
- **Accepting or undoing a suggestion of a removed plant** no longer fails with an error: it stops cleanly and the
  suggestion's repair is removed. Repairs of plants that no longer exist are cleaned up.

## 26.10.1 – 22.10.2026

First public release. Pflanzenmonitor Plus is now a complete plant integration of its own and no longer needs
Plant Monitor.

- **Plants of its own:** add each plant under *Pflanzenmonitor Plus → Add plant* (name, species, room, picture,
  sensors, outdoors, which readings count as a problem, wait after watering). Every plant is its own device.
- **Status like `plant.*`:** one status entity per plant (`ok` / `problem`) with the attributes cards and templates
  know: `problems`, `<measurement>_status` (`Low` / `High` / `ok`), `species`. 5 % hysteresis at the limits,
  illuminance only counts when too bright, daily light is checked against yesterday, an optional wait after watering
  keeps "too wet" quiet.
- **Readings and light:** the real sensors are mirrored on the plant's device (unit from the source, last value held
  for up to 10 minutes after a restart), plus VPD, PPFD, total light integral and daily light integral (today,
  yesterday, last 24 hours). Light sensors that already measure PPFD are supported.
- **Limits:** min/max for every measurement and the lux-to-PPFD factor as number entities, stored by Plus,
  temperatures in °C and shown in your unit system.
- **Logbook:** start and end of every problem.
- **Automations:** 43 triggers (problem detected/cleared, every measurement became low/high/OK, a sensor became stale
  or reports again) and 35 conditions, targeting plants directly or by area and label. Stale means unavailable or no
  report for a while; a report with the same value counts.
- **Action `pflanzenmonitor_plus.replace_sensor`** gives a reading another sensor or removes it.
- **Move from Plant Monitor:** a repair takes over all plants with their sensors, the limits currently set, options,
  room, labels, names, icons, display and voice-assistant settings and the live state (light sums, problems). Entity
  IDs stay the same, so history continues; only `plant.<name>` becomes `sensor.<name>`. What Plus has learned and the
  AI history stay. Automations and scripts that still use the old entities or devices are listed under Repairs.
- **AI suggestions** write to the plant's own limits; temperature limits now also work with °F systems.

## Before the public release

Pflanzenmonitor Plus started as a private companion to Plant Monitor (versions 1.0.0 to 26.10.6, October 2026). The
version count starts again with the public repository; everything from that time is part of 26.10.1:

- Learning from the soil moisture history when a plant was watered, how fast it dries out and when it needs water
  again ("Water in", "Watering due", "Last watered", drying rate, learning status); spikes and gaps are ignored.
- AI suggestions for all limits that have a sensor, with safety rules in code, step limits, approval, undo and a
  repair dialog; checked against the species, with a frost floor and summer light rules for plants outdoors.
- Plants outdoors: rain does not count as watering; "Rain expected" and "Frost risk".
