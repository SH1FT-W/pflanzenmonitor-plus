"""Reine Auswertung ohne HA: Gießen erkennen, Abtrocknung messen, nächstes Gießen vorhersagen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import itertools
from statistics import median, quantiles
from typing import Literal

# Anstieg der Bodenfeuchte um mindestens so viele Prozentpunkte innerhalb einer Stunde = Gießen oder Regen
JUMP = 10.0
# Vergleichsbasis: Tiefstwert der Stunden höchstens so lange davor
BASE_WINDOW = timedelta(hours=3)
# Mehrere Stufen innerhalb dieser Zeit sind ein Ereignis (langsames Gießen, Regen über Stunden)
MERGE = timedelta(hours=12)
# Ab so vielen vollständigen Zyklen (Anstieg bis nächster Anstieg) gilt eine Pflanze als gelernt
CYCLES_NEEDED = 2
# Zyklen kürzer als das zählen nicht (Nachgießen am selben Tag)
MIN_CYCLE = timedelta(hours=24)
# Abtrocknung aus den letzten Stunden des laufenden Zyklus
RATE_WINDOW = timedelta(hours=72)
# Vorhersage aus den letzten so vielen Zyklen
ANALOG_CYCLES = 3
# Langsamer als das gilt als „trocknet nicht ab“
MIN_RATE = 0.1
# Vergleichszyklen werden höchstens um diesen Faktor auf das heutige Tempo umgerechnet
MAX_SCALE = 3.0
# Weiter als das sagen wir nichts voraus
MAX_DAYS = 60.0
# Regen bei Pflanzen draußen: Regen von 3 Std. vorher bis 1 Std. nachher zählt
RAIN_BEFORE = timedelta(hours=3)
RAIN_AFTER = timedelta(hours=1)

# Licht: lx → µmol/m²/s (Sonnenlicht, wie der Plant Monitor ohne eigene Einstellung); Tag zählt ab so vielen Stunden
LUX_TO_PPFD = 0.0185
LIGHT_HOURS = 20

Kind = Literal["gegossen", "regen", "unbekannt"]
Stage = Literal["keine_daten", "lernt", "bereit"]


@dataclass(frozen=True)
class Sample:
    """Eine Stunde aus der Langzeitstatistik (oder der aktuelle Wert)."""

    t: datetime
    mean: float
    low: float
    high: float


@dataclass
class Rise:
    """Ein Anstieg der Bodenfeuchte."""

    start: datetime
    idx: int
    before: float
    peak: float
    kind: Kind = "gegossen"


@dataclass
class Learned:
    """Was wir über eine Pflanze wissen."""

    stage: Stage
    cycles: int
    rises: list[Rise] = field(default_factory=list)
    last_watered: datetime | None = None
    last_rain: datetime | None = None
    threshold: float | None = None
    target: float | None = None
    rate: float | None = None
    days: float | None = None
    method: Literal["zyklen", "tempo"] | None = None
    # Für die KI: typische Feuchte direkt nach dem Gießen, typische Tage zwischen zwei Gießereignissen
    peak: float | None = None
    cycle_days: float | None = None


def floor(sample: Sample) -> float:
    """Tiefstwert einer Stunde – außer er ist ein Ausreißer weit unter dem Stundenmittel.

    Viele Sensoren melden nach einem Neustart kurz 0; das wäre sonst ein Phantom-Gießen.
    """
    return sample.mean if sample.mean - sample.low >= JUMP else sample.low


def confirmed(samples: list[Sample], i: int, base: float) -> bool:
    """Hält der Anstieg? Ein einzelner Ausreißer nach oben (eine Stunde mit Höchstwert 100, Mittel normal) ist
    kein Gießen: entweder liegt schon das Stundenmittel deutlich über dem Ausgangswert oder das der Folgestunde.

    Die jüngste Stunde hat noch keine Folgestunde und zählt vorerst; stimmt es nicht, fällt der Anstieg beim nächsten
    Durchlauf wieder weg.
    """
    half = base + JUMP / 2
    if samples[i].mean >= half or i + 1 >= len(samples):
        return True
    return samples[i + 1].mean >= half


def detect_rises(samples: list[Sample]) -> list[Rise]:
    """Anstiege finden: Höchstwert einer Stunde liegt ≥ JUMP über dem Tiefstwert der drei Stunden davor."""
    rises: list[Rise] = []
    for i in range(1, len(samples)):
        # Nur echte Nachbarstunden vergleichen: nach einer Lücke (Sensor aus, HA aus) ist ein Unterschied kein Gießen
        window = [s for s in samples[max(0, i - 3) : i] if samples[i].t - s.t <= BASE_WINDOW]
        if not window:
            continue
        base = min(floor(s) for s in window)
        if samples[i].high - base < JUMP or not confirmed(samples, i, base):
            continue
        if rises and samples[i].t - rises[-1].start <= MERGE:
            continue
        rises.append(Rise(start=samples[i].t, idx=i, before=base, peak=samples[i].high))
    for rise in rises:
        end = rise.start + MERGE
        rise.peak = max(s.high for s in samples[rise.idx :] if s.t <= end)
    return rises


def classify(rises: list[Rise], rain: list[datetime] | None, outdoor: bool, rain_since: datetime | None = None) -> None:
    """Drinnen ist jeder Anstieg Gießen. Draußen nur, wenn es nachweislich nicht geregnet hat.

    `rain` = Stunden mit Regen, `rain_since` = ab wann es Regendaten gibt (davor weiß man es nicht).
    """
    if not outdoor:
        return
    for rise in rises:
        if rain is None or (rain_since is not None and rise.start - RAIN_BEFORE < rain_since):
            rise.kind = "unbekannt"
        elif any(rise.start - RAIN_BEFORE <= t <= rise.start + RAIN_AFTER for t in rain):
            rise.kind = "regen"


def slope_per_day(points: list[Sample]) -> float | None:
    """Abtrocknung in Prozentpunkten pro Tag (positiv = wird trockener), kleinste Quadrate über die Mittelwerte."""
    if len(points) < 2:
        return None
    t0 = points[0].t
    xs = [(p.t - t0).total_seconds() / 86400 for p in points]
    ys = [p.mean for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return -sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den


def _first_below(points: list[Sample], level: float) -> datetime | None:
    return next((p.t for p in points if p.mean <= level), None)


def time_between(segment: list[Sample], level: float, target: float, rate_now: float | None = None) -> float | None:
    """Wie viele Tage brauchte die Erde in diesem Zyklus von `level` bis `target`?

    Erreicht der Zyklus das Ziel nicht (vorher gegossen), wird mit dem Tempo seiner letzten 72 Std. verlängert.
    Mit `rate_now` wird auf das heutige Tempo umgerechnet (Sommerzyklen trocknen schneller als im Herbst).
    """
    # Erst ab dem feuchtesten Punkt zählen: die Stunde des Anstiegs mittelt noch den trockenen Wert davor ein.
    # War die Erde in diesem Zyklus nie so feucht wie jetzt, taugt er nicht zum Vergleich.
    wettest = max(range(len(segment)), key=lambda i: segment[i].mean)
    if segment[wettest].mean <= level:
        return None
    segment = segment[wettest:]
    start = _first_below(segment, level)
    if start is None:
        return None
    end = _first_below(segment, target)
    if end is not None:
        days = (end - start).total_seconds() / 86400
    else:
        tail = [p for p in segment if p.t >= segment[-1].t - RATE_WINDOW]
        rate = slope_per_day(tail)
        if rate is None or rate <= MIN_RATE:
            return None
        days = (segment[-1].t - start).total_seconds() / 86400 + (segment[-1].mean - target) / rate
    if rate_now is not None and rate_now > MIN_RATE:
        # Tempo über dieselben 72 Std. vor dem Erreichen von `level` wie heute (gleicher Feuchtebereich)
        then = slope_per_day([p for p in segment if start - RATE_WINDOW <= p.t <= start])
        if then is not None and then > MIN_RATE:
            days *= min(max(then / rate_now, 1 / MAX_SCALE), MAX_SCALE)
    return days


def learn(
    samples: list[Sample],
    *,
    minimum: float | None,
    outdoor: bool = False,
    rain: list[datetime] | None = None,
    rain_since: datetime | None = None,
) -> Learned:
    """Alles für eine Pflanze aus dem Feuchteverlauf (stündlich, ältester zuerst, aktueller Wert zuletzt)."""
    if len(samples) < 2:
        return Learned(stage="keine_daten", cycles=0)
    rises = detect_rises(samples)
    classify(rises, rain, outdoor, rain_since)
    watered = [r for r in rises if r.kind == "gegossen"]
    rained = [r for r in rises if r.kind == "regen"]

    # Zyklen: von einem Anstieg bis zum nächsten, mindestens einen Tag lang
    bounds = [r.idx for r in rises] + [len(samples)]
    segments = [samples[a:b] for a, b in itertools.pairwise(bounds)]
    complete = [s for s in segments[:-1] if s[-1].t - s[0].t >= MIN_CYCLE]
    current_segment = segments[-1] if rises else samples

    # Gewohnheit: bei welcher Feuchte wurde zuletzt gegossen? Nur Info – viele gießen deutlich vor dem Bedarf.
    threshold = median(r.before for r in watered[-ANALOG_CYCLES:]) if len(watered) >= CYCLES_NEEDED else None
    # Ziel ist der Bedarf der Pflanze (ihre Untergrenze); ohne Untergrenze die Gewohnheit
    target = minimum if minimum is not None else threshold

    recent = [p for p in current_segment if p.t >= samples[-1].t - RATE_WINDOW]
    # Direkt nach dem Gießen läuft überschüssiges Wasser ab, das ist keine Abtrocknung
    if rises and samples[-1].t - rises[-1].start < timedelta(hours=24):
        recent = []
    rate = slope_per_day(recent) if len(recent) >= 12 else None

    days: float | None = None
    method: Literal["zyklen", "tempo"] | None = None
    now = samples[-1].mean
    if target is not None:
        if now <= target:
            days, method = 0.0, "tempo"
        else:
            past = [d for s in complete[-ANALOG_CYCLES:] if (d := time_between(s, now, target, rate)) is not None]
            if past:
                days, method = median(past), "zyklen"
            elif rate is not None and rate > MIN_RATE:
                days, method = (now - target) / rate, "tempo"
        if days is not None:
            days = round(min(days, MAX_DAYS), 1)

    return Learned(
        stage="bereit" if len(complete) >= CYCLES_NEEDED else "lernt",
        cycles=len(complete),
        rises=rises,
        last_watered=watered[-1].start if watered else None,
        last_rain=rained[-1].start if rained else None,
        threshold=None if threshold is None else round(threshold, 1),
        target=None if target is None else round(target, 1),
        # Steigt die Feuchte gerade (Sensor schwankt, Nachgießen), trocknet nichts ab
        rate=None if rate is None else round(max(rate, 0.0), 2),
        days=days,
        method=method,
        peak=round(median(r.peak for r in watered[-ANALOG_CYCLES:]), 1) if watered else None,
        cycle_days=round(median((s[-1].t - s[0].t).total_seconds() / 86400 for s in complete[-ANALOG_CYCLES:]), 1)
        if complete
        else None,
    )


def _spread(values: list[float], digits: int) -> dict[str, float]:
    """Unteres Zehntel, Mitte, oberes Zehntel (bei wenigen Tagen Tiefst-/Höchstwert)."""
    if len(values) < 3:
        low, high = min(values), max(values)
    else:
        # „inclusive“: nie über den gemessenen Bereich hinaus schätzen (bei wenigen Tagen sonst p90 > Höchstwert)
        deciles = quantiles(values, n=10, method="inclusive")
        low, high = deciles[0], deciles[-1]
    return {"p10": round(low, digits), "median": round(median(values), digits), "p90": round(high, digits)}


def light_summary(rows: list[tuple[date, float, float]], lux_to_ppfd: float = LUX_TO_PPFD) -> dict[str, object] | None:
    """Tageshöchstwerte (lx) und Tageslicht (DLI, mol/m²/d) aus stündlicher Statistik (Tag, Mittel, Höchstwert).

    DLI = Summe der Stundenmittel × Umrechnung × 3600 s; nur Tage mit mindestens LIGHT_HOURS Stunden zählen.
    """
    days: dict[date, list[tuple[float, float]]] = {}
    for day, mean, high in rows:
        days.setdefault(day, []).append((mean, high))
    full = [hours for hours in days.values() if len(hours) >= LIGHT_HOURS]
    if not full:
        return None
    peaks = [max(high for _m, high in hours) for hours in full]
    dli = [sum(max(m, 0.0) for m, _h in hours) * lux_to_ppfd * 3600 / 1e6 for hours in full]
    return {"days": len(full), "daily_peak_lux": _spread(peaks, 0), "daily_dli": _spread(dli, 1)}
