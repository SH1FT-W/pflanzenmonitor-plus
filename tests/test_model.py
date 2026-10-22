"""Reine Auswertung: Anstiege, Regen, Zyklen, Vorhersage."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from custom_components.pflanzenmonitor_plus import model
from custom_components.pflanzenmonitor_plus.model import Sample

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def series(*parts: tuple[float, float, int]) -> list[Sample]:
    """Abschnitte (von, bis, Stunden) aneinander; ein Sprung nach oben = Gießen."""
    out: list[Sample] = []
    for start, end, hours in parts:
        for h in range(hours):
            v = start + (end - start) * h / hours
            out.append(Sample(T0 + timedelta(hours=len(out)), v, v - 0.2, v + 0.2))
    return out


def test_detect_rises_merges_steps() -> None:
    s = series((40, 30, 48), (55, 55, 2), (70, 60, 48))
    rises = model.detect_rises(s)
    assert len(rises) == 1
    assert rises[0].before < 31
    assert rises[0].peak > 69


def test_spike_up_is_not_watering() -> None:
    """Eine Stunde mit Höchstwert 100 bei normalem Mittel (Sensor-Ausreißer) ist kein Gießen."""
    s = series((40, 30, 48), (30, 28, 24))
    spike = s[50]
    s[50] = Sample(spike.t, spike.mean, spike.low, 100.0)
    assert model.detect_rises(s) == []
    # Echtes Gießen spät in der Stunde: Mittel noch unten, die Folgestunde bestätigt
    w = series((40, 30, 48), (55, 55, 1), (70, 60, 48))
    late = w[48]
    w[48] = Sample(late.t, 31.0, 30.0, 70.0)
    rises = model.detect_rises(w)
    assert len(rises) == 1 and rises[0].start == late.t
    # Jüngste Stunde ohne Folgestunde zählt vorerst (wird beim nächsten Durchlauf geprüft)
    fresh = series((40, 30, 48))
    fresh.append(Sample(fresh[-1].t + timedelta(hours=1), 31.0, 30.0, 70.0))
    assert len(model.detect_rises(fresh)) == 1


def test_learn_without_data() -> None:
    assert model.learn([], minimum=20).stage == "keine_daten"


def test_learn_cycles_and_prediction() -> None:
    s = series((80, 20, 240), (80, 20, 240), (80, 20, 240), (80, 50, 120))
    learned = model.learn(s, minimum=20)
    assert learned.stage == "bereit"
    # Zyklus = von einem Gießen bis zum nächsten (die Phase davor und der laufende zählen nicht)
    assert learned.cycles == 2
    assert learned.method == "zyklen"
    # Gleiches Tempo wie früher: 50 → 20 dauerte 5 Tage
    assert learned.days is not None
    assert 4.5 <= learned.days <= 5.5
    assert learned.rate is not None
    assert 5.5 <= learned.rate <= 6.5
    assert learned.threshold is not None
    assert 19 <= learned.threshold <= 21
    assert learned.last_watered == s[720].t


def test_learning_and_rate_fallback() -> None:
    """Ein Zyklus reicht nicht zum Lernen; die Vorhersage nimmt dann das aktuelle Tempo."""
    s = series((60, 40, 100), (80, 50, 50), (80, 50, 120))
    learned = model.learn(s, minimum=20)
    assert learned.stage == "lernt"
    assert learned.cycles == 1
    assert learned.method == "tempo"
    assert learned.days is not None
    assert 4.5 <= learned.days <= 5.5


def test_due_now_and_habit_target() -> None:
    s = series((80, 30, 100), (80, 30, 100), (80, 25, 100))
    assert model.learn(s, minimum=30).days == 0.0
    # Ohne Untergrenze: Gießgewohnheit als Ziel
    habit = model.learn(s, minimum=None)
    assert habit.target == habit.threshold


def test_no_target_no_prediction() -> None:
    s = series((80, 50, 100))
    assert model.learn(s, minimum=None).days is None


def test_recent_watering_has_no_rate() -> None:
    s = series((60, 30, 100), (80, 78, 10))
    learned = model.learn(s, minimum=20)
    assert learned.rate is None


def test_rising_moisture_rate_is_zero() -> None:
    s = series((60, 30, 100), (80, 70, 48), (70, 72, 80))
    assert model.learn(s, minimum=20).rate == 0.0


def test_outdoor_rain_classification() -> None:
    s = series((60, 40, 50), (80, 60, 50), (80, 60, 50))
    first, second = s[50].t, s[100].t
    learned = model.learn(s, minimum=20, outdoor=True, rain=[first - timedelta(hours=1)], rain_since=T0)
    assert [r.kind for r in learned.rises] == ["regen", "gegossen"]
    assert learned.last_rain == first
    assert learned.last_watered == second
    # Ohne Regendaten (oder vor ihrem Beginn) weiß man es nicht
    unknown = model.learn(s, minimum=20, outdoor=True, rain=None)
    assert {r.kind for r in unknown.rises} == {"unbekannt"}
    late = model.learn(s, minimum=20, outdoor=True, rain=[], rain_since=second - timedelta(hours=4))
    assert [r.kind for r in late.rises] == ["unbekannt", "gegossen"]


def test_time_between_edges() -> None:
    dry = series((30, 20, 50))
    # Nie so feucht wie jetzt → kein Vergleich
    assert model.time_between(dry, 40, 10) is None
    # Feuchtester Punkt am Ende → Stand nie unterschritten
    assert model.time_between(series((20, 30, 50)), 25, 10) is None
    # Ziel nicht erreicht, aber trocknet weiter → verlängert
    extended = model.time_between(series((80, 40, 96)), 60, 20)
    assert extended is not None
    assert 3.5 <= extended <= 4.5
    # Ziel nicht erreicht und trocknet nicht mehr → keine Aussage
    assert model.time_between(series((80, 40, 48), (40, 40, 80)), 60, 20) is None


def test_time_between_scales_to_current_rate() -> None:
    seg = series((80, 20, 240))
    plain = model.time_between(seg, 50, 20)
    slower = model.time_between(seg, 50, 20, rate_now=3.0)
    assert plain is not None
    assert slower is not None
    assert abs(slower - plain * 2) < 0.3
    # höchstens Faktor 3
    crawl = model.time_between(seg, 50, 20, rate_now=0.5)
    assert crawl is not None
    assert abs(crawl - plain * 3) < 0.3


def test_slope_edges() -> None:
    assert model.slope_per_day([]) is None
    one = Sample(T0, 1, 1, 1)
    assert model.slope_per_day([one, one]) is None


def test_light_summary() -> None:
    day = date(2026, 10, 1)
    assert model.light_summary([]) is None
    # Zu wenige Stunden an einem Tag → zählt nicht
    assert model.light_summary([(day, 100.0, 200.0)] * 5) is None

    def full(d: date, peak: float) -> list[tuple[date, float, float]]:
        # 10 Std. hell (1000 lx Mittel), 14 Std. dunkel; ein kaputter negativer Wert zählt als 0
        return [(d, 1000.0, peak)] * 10 + [(d, 0.0, 0.0)] * 13 + [(d, -5.0, 0.0)]

    one = model.light_summary(full(day, 3000.0), lux_to_ppfd=0.02)
    # 10 × 1000 lx × 0,02 × 3600 s / 1e6 = 0,72 mol/m²/d
    assert one == {
        "days": 1,
        "daily_peak_lux": {"p10": 3000.0, "median": 3000.0, "p90": 3000.0},
        "daily_dli": {"p10": 0.7, "median": 0.7, "p90": 0.7},
    }
    rows = [r for i in range(10) for r in full(date(2026, 10, 1 + i), 1000.0 * (i + 1))]
    many = model.light_summary(rows)
    assert many is not None
    assert many["days"] == 10
    assert many["daily_peak_lux"] == {"p10": 1900.0, "median": 5500.0, "p90": 9100.0}
