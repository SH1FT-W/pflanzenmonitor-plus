# Changelog

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
