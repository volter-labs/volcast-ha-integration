"""Raport offline: ile kosztuje ściśnięcie planów do N programów TOU.

Wejście: lista planów w kontrakcie urządzenia (jak eksportuje warstwa
chmurowa). Dane wejściowe NIE trafiają do repozytorium — tylko zagregowany
wynik.

Uwaga: importujemy silnik jako pakiet `core` (nie
`custom_components.volcast.core`), żeby narzędzie dało się uruchomić bez
zainstalowanej integracji Home Assistant — patrz `sys.path.insert` niżej.
"""
from __future__ import annotations

import argparse
import copy
import json
import statistics
import sys
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_VOLCAST_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "volcast"
if str(_VOLCAST_DIR) not in sys.path:
    sys.path.insert(0, str(_VOLCAST_DIR))

from core.engines.time_window import compress  # noqa: E402
from core.profile import Profile, load_builtin, profile_from_dict  # noqa: E402
from core.slot import InvalidSchedule, parse_schedule  # noqa: E402

WAW = ZoneInfo("Europe/Warsaw")


def with_programs(profile: Profile, n: int) -> Profile:
    raw = copy.deepcopy(dict(profile.raw))     # raw to MappingProxyType — json.dumps by go nie przyjął
    raw["tou"]["programs"] = n
    raw["write"]["tou_program"]["count"] = n
    raw["capabilities"]["time_windows"] = n
    return profile_from_dict(raw)


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    s = sorted(values)
    q = statistics.quantiles(s, n=10, method="inclusive") if len(s) > 1 else [s[0]] * 9
    return {"n": len(s), "median": round(statistics.median(s), 4), "p90": round(q[8], 4),
             "max": round(s[-1], 4), "mean": round(statistics.fmean(s), 4),
             "zero_share": round(sum(1 for v in s if v == 0) / len(s), 4)}


# Kryterium „N programów wystarcza" liczone na stracie ze SCALEŃ (jedyna część zależna
# od N). Strata z degradacji intencji, których sprzęt nie ma, jest osobną kolumną.
MERGE_MEDIAN_MAX_PLN = 0.10
MERGE_P90_MAX_PLN = 0.50
# Moc znamionowa, gdy wpis jej nie podaje (NULL/0) — jawnie liczona w wyniku.
DEFAULT_RATED_W = 5000


def verdict(s: dict[str, float]) -> str:
    if not s.get("n"):
        return "BRAK DANYCH"
    ok = s["median"] < MERGE_MEDIAN_MAX_PLN and s["p90"] < MERGE_P90_MAX_PLN
    return "PASS" if ok else "FAIL"


def run(entries: list[dict], ns: list[int], profile: Profile) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for n in ns:
        prof = with_programs(profile, n)
        lost, merge_loss, merges, degraded = [], [], [], []
        zero_cost, assumed = 0, 0
        skipped = {"missing": 0, "invalid": 0, "empty": 0, "engine": 0}
        for e in entries:
            if not isinstance(e, dict) or "schedule" not in e:
                skipped["missing"] += 1
                continue
            try:
                sch = parse_schedule(e["schedule"])
            except InvalidSchedule:
                skipped["invalid"] += 1
                continue
            if not sch.slots:
                skipped["empty"] += 1
                continue
            now = max(sch.generated_at or sch.slots[0].start, sch.slots[0].start).astimezone(timezone.utc)
            reserve = sch.fallback.soc_reserve
            rated = e.get("max_charge_rate_w")
            if not rated:
                assumed += 1
                rated = DEFAULT_RATED_W
            try:
                r = compress(sch, now, prof, soc_reserve=reserve, rated_power_w=float(rated), tz=WAW)
            except ValueError:
                skipped["engine"] += 1
                continue
            lost.append(r.lost_value_pln)
            merge_loss.append(r.merge_loss_pln)
            merges.append(float(len(r.merges)))
            degraded.append(r.degrade_loss_pln)
            zero_cost += sum(1 for m in r.merges if m.lost_value_pln == 0
                              and {m.kept_intent, m.absorbed_intent} == {"standby", "self_consume"})
        ml = summarize(merge_loss)
        out[n] = {"plans": len(lost), "skipped": skipped,
                  "assumed_rated_w": {"count": assumed, "value": DEFAULT_RATED_W},
                  "merge_loss": ml, "verdict": verdict(ml),
                  "lost": summarize(lost), "merges": summarize(merges), "degrade": summarize(degraded),
                  "standby_self_zero_cost_merges": zero_cost}
    return out


def to_markdown(result: dict[int, dict]) -> str:
    rows = [f"Kryterium (strata ze scaleń): mediana < {MERGE_MEDIAN_MAX_PLN} zł i p90 < {MERGE_P90_MAX_PLN} zł na plan.",
            "",
            "| N | plany | pominięte (brak/kontrakt/puste/silnik) | domyślna moc | scalenia: strata mediana zł | p90 | max "
            "| udział 0 | wynik | liczba scaleń (med.) | strata łączna (med.) | degradacja (med.) |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, r in sorted(result.items()):
        ml, lo, me, de = r["merge_loss"], r["lost"], r["merges"], r["degrade"]
        sk, ar = r["skipped"], r["assumed_rated_w"]
        rows.append(f"| {n} | {r['plans']} | {sk['missing']}/{sk['invalid']}/{sk['empty']}/{sk['engine']} | "
                    f"{ar['count']}× {ar['value']} W | {ml.get('median', '-')} | {ml.get('p90', '-')} | "
                    f"{ml.get('max', '-')} | {ml.get('zero_share', '-')} | {r['verdict']} | "
                    f"{me.get('median', '-')} | {lo.get('median', '-')} | {de.get('median', '-')} |")
    return "\n".join(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("plans")
    ap.add_argument("--programs", default="4,6,8")
    ap.add_argument("--profile", default="deye-sg")
    a = ap.parse_args()
    entries = json.loads(Path(a.plans).read_text())
    result = run(entries, [int(x) for x in a.programs.split(",")], load_builtin(a.profile))
    print(to_markdown(result))
    print("\n```json\n" + json.dumps(result, indent=1) + "\n```")


if __name__ == "__main__":
    main()
