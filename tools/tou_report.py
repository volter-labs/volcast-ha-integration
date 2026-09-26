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


def run(entries: list[dict], ns: list[int], profile: Profile) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for n in ns:
        prof = with_programs(profile, n)
        lost, merges, degraded, zero_cost, skipped = [], [], [], 0, 0
        for e in entries:
            try:
                sch = parse_schedule(e["schedule"])
            except InvalidSchedule:
                skipped += 1
                continue
            if not sch.slots:
                skipped += 1
                continue
            now = max(sch.generated_at or sch.slots[0].start, sch.slots[0].start).astimezone(timezone.utc)
            reserve = sch.fallback.soc_reserve
            rated = float(e.get("max_charge_rate_w") or 5000)
            try:
                r = compress(sch, now, prof, soc_reserve=reserve, rated_power_w=rated, tz=WAW)
            except ValueError:
                skipped += 1
                continue
            lost.append(r.lost_value_pln)
            merges.append(float(len(r.merges)))
            degraded.append(r.degrade_loss_pln)
            zero_cost += sum(1 for m in r.merges if m.lost_value_pln == 0
                              and {m.kept_intent, m.absorbed_intent} == {"standby", "self_consume"})
        out[n] = {"plans": len(lost), "skipped": skipped, "lost": summarize(lost),
                  "merges": summarize(merges), "degrade": summarize(degraded),
                  "standby_self_zero_cost_merges": zero_cost}
    return out


def to_markdown(result: dict[int, dict]) -> str:
    rows = ["| N | plany | pominięte | strata mediana zł | p90 | max | udział 0 | scalenia (med.) | degradacja (med.) |",
            "|---|---|---|---|---|---|---|---|---|"]
    for n, r in sorted(result.items()):
        lo, me, de = r["lost"], r["merges"], r["degrade"]
        rows.append(f"| {n} | {r['plans']} | {r['skipped']} | {lo.get('median', '-')} | {lo.get('p90', '-')} | "
                    f"{lo.get('max', '-')} | {lo.get('zero_share', '-')} | {me.get('median', '-')} | "
                    f"{de.get('median', '-')} |")
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
