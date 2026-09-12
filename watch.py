"""
watch.py - digest the current watch straight out of the journals.

Nova was spending whole minutes grepping journals to answer "what did we do
tonight", which is slow, expensive, and times out. This does the same work in
Python in a fraction of a second and writes the answer to
reference/watch_status.json for her to read.

    python watch.py            summarise the last WATCH_HOURS
    python watch.py 24         summarise the last 24 hours
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))

# Events worth the parse. Anything else is skipped before json.loads, which is
# what keeps this fast over hundreds of megabytes.
WANTED = (
    b'"Fileheader"', b'"LoadGame"', b'"Commander"', b'"FSDJump"', b'"CarrierJump"',
    b'"Location"', b'"Scan"', b'"SAAScanComplete"', b'"FSSAllBodiesFound"',
    b'"ScanOrganic"', b'"SellOrganicData"', b'"Docked"', b'"Died"',
    b'"CarrierStats"', b'"CarrierDepositFuel"', b'"CarrierJumpRequest"',
    b'"MarketSell"', b'"MultiSellExplorationData"', b'"SellExplorationData"',
    b'"Bounty"', b'"Touchdown"', b'"ApproachBody"',
)

NOTABLE = {
    "Earthlike body": "earthlike",
    "Water world": "water",
    "Ammonia world": "ammonia",
}


UPDATES_FILE = os.path.join(HERE, "reference", "dashboard_updates.json")


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def record_update(dashboard_path: str, keep: int = 20) -> list:
    """Track when the dashboard was last rebuilt.

    We use the FILE'S OWN mtime rather than asking her to remember. She cannot
    forget to record it, and it cannot drift out of step with reality: if the
    dashboard changed, it was rebuilt.
    """
    try:
        with open(UPDATES_FILE, encoding="utf-8") as fh:
            history = json.load(fh)
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    try:
        stamp = _iso(os.path.getmtime(dashboard_path))
    except OSError:
        return history                      # never built yet

    if not history or history[-1] != stamp:
        history.append(stamp)
        history = history[-keep:]
        try:
            os.makedirs(os.path.dirname(UPDATES_FILE), exist_ok=True)
            with open(UPDATES_FILE, "w", encoding="utf-8") as fh:
                json.dump(history, fh, indent=1)
        except OSError:
            pass
    return history


def checkpoint(history: list) -> str | None:
    """Where to resume from: TWO updates back, not one.

    Going back only to the last rebuild risks missing anything written to the
    journal in the seconds around it. One extra update of overlap costs
    almost nothing to re-read and closes that gap.
    """
    if len(history) >= 2:
        return history[-2]
    if history:
        return history[-1]
    return None


def journal_files(directory: str, hours: float | None, since: str | None = None):
    try:
        names = [n for n in os.listdir(directory)
                 if n.startswith("Journal.") and n.endswith(".log")]
    except OSError:
        return []
    paths = [os.path.join(directory, n) for n in names]
    cutoff = None
    if since:
        # a file whose last write predates the checkpoint holds nothing new
        try:
            cutoff = time.mktime(time.strptime(since, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        except ValueError:
            cutoff = None
    if cutoff is None and hours:
        cutoff = time.time() - hours * 3600
    if cutoff is not None:
        paths = [p for p in paths if os.path.getmtime(p) >= cutoff - 60]
    return sorted(paths, key=os.path.getmtime)


def digest(journal_dir: str, hours: float = 12, since: str | None = None) -> dict:
    files = journal_files(journal_dir, hours, since)

    cmdr = ship = ship_name = system = body = None
    credits_now = None
    jumps = carrier_jumps = 0
    distance = 0.0
    systems: list[str] = []
    bodies_scanned = 0
    mapped = 0
    deaths = 0
    docked_at: list[str] = []
    notable = defaultdict(list)
    terraformable = []
    organics = Counter()
    organic_bodies = set()
    sold_value = sold_bonus = sold_count = 0
    fuel = None
    fuel_history = []
    jump_range = None
    first_ts = last_ts = None
    landed_on = set()

    for path in files:
        try:
            handle = open(path, "rb")
        except OSError:
            continue
        with handle:
            for raw in handle:
                if not any(w in raw for w in WANTED):
                    continue
                try:
                    e = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                ev = e.get("event")
                ts = e.get("timestamp")
                if since and ts and ts < since:
                    continue          # already counted in an earlier pass
                if ts:
                    first_ts = first_ts or ts
                    last_ts = ts

                if ev == "LoadGame":
                    cmdr = e.get("Commander") or cmdr
                    ship = e.get("Ship_Localised") or e.get("Ship") or ship
                    ship_name = e.get("ShipName") or ship_name
                    if e.get("Credits") is not None:
                        credits_now = e["Credits"]
                elif ev == "FSDJump":
                    jumps += 1
                    distance += float(e.get("JumpDist") or 0)
                    system = e.get("StarSystem") or system
                    if system and (not systems or systems[-1] != system):
                        systems.append(system)
                elif ev == "CarrierJump":
                    carrier_jumps += 1
                    system = e.get("StarSystem") or system
                elif ev == "Location":
                    system = e.get("StarSystem") or system
                elif ev in ("Touchdown", "ApproachBody"):
                    body = e.get("Body") or body
                    if ev == "Touchdown":
                        landed_on.add((system, body))
                elif ev == "Scan":
                    bodies_scanned += 1
                    name = e.get("BodyName")
                    cls = e.get("PlanetClass")
                    if cls in NOTABLE and name:
                        entry = {"body": name,
                                 "distance_ls": round(float(e.get("DistanceFromArrivalLS") or 0)),
                                 "gravity": round(float(e.get("SurfaceGravity") or 0) / 9.81, 2),
                                 "was_discovered": e.get("WasDiscovered"),
                                 "terraformable": bool(e.get("TerraformState"))}
                        if entry not in notable[NOTABLE[cls]]:
                            notable[NOTABLE[cls]].append(entry)
                    if cls == "Water world" and e.get("TerraformState") and name:
                        if name not in terraformable:
                            terraformable.append(name)
                elif ev == "SAAScanComplete":
                    mapped += 1
                elif ev == "ScanOrganic" and e.get("ScanType") == "Analyse":
                    organics[e.get("Species_Localised") or e.get("Species") or "?"] += 1
                    organic_bodies.add((system, body))
                elif ev == "SellOrganicData":
                    for bio in e.get("BioData", []) or []:
                        sold_count += 1
                        sold_value += int(bio.get("Value") or 0)
                        sold_bonus += int(bio.get("Bonus") or 0)
                elif ev == "Docked":
                    name = e.get("StationName")
                    if name and name not in docked_at:
                        docked_at.append(name)
                elif ev == "Died":
                    deaths += 1
                elif ev == "CarrierStats":
                    fuel = e.get("FuelLevel", fuel)
                    jump_range = e.get("JumpRangeCurr", jump_range)
                    if fuel is not None:
                        fuel_history.append(fuel)

    # tritium burn measured from his own carrier, not a formula
    burn = None
    if len(fuel_history) > 1:
        drops = [a - b for a, b in zip(fuel_history, fuel_history[1:]) if a > b]
        if drops:
            burn = round(sum(drops) / len(drops))

    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "window_hours": hours,
        "since": since,
        "journals_read": len(files),
        "first_event": first_ts,
        "last_event": last_ts,
        "commander": cmdr,
        "ship": ship,
        "ship_name": ship_name,
        "current_system": system,
        "current_body": body,
        "credits": credits_now,
        "travel": {
            "fsd_jumps": jumps,
            "carrier_jumps": carrier_jumps,
            "light_years": round(distance, 1),
            "systems_visited": len(systems),
            "system_list": systems[-40:],
            "docked_at": docked_at,
            "deaths": deaths,
        },
        "exploration": {
            "bodies_scanned": bodies_scanned,
            "bodies_mapped": mapped,
            "earthlikes": notable["earthlike"],
            "water_worlds": notable["water"],
            "ammonia_worlds": notable["ammonia"],
            "terraformable_water": terraformable,
        },
        "exobiology": {
            "samples_completed": sum(organics.values()),
            "species": dict(organics.most_common()),
            "bodies_sampled": len(organic_bodies),
            "landed_bodies": len(landed_on),
            "sold_this_window": {
                "samples": sold_count,
                "base": sold_value,
                "bonus": sold_bonus,
                "total": sold_value + sold_bonus,
            },
        },
        "carrier": {
            "tritium": fuel,
            "jump_range_ly": jump_range,
            "tritium_per_jump_observed": burn,
            "jumps_remaining_estimate": (round(fuel / burn) if fuel and burn else None),
        },
    }


def render(d: dict) -> str:
    t, x, b, c = d["travel"], d["exploration"], d["exobiology"], d["carrier"]
    out = [
        f"\nWATCH — last {d['window_hours']}h, {d['journals_read']} journal(s)",
        f"  {d.get('commander') or '?'} in {d.get('ship_name') or d.get('ship') or '?'}"
        f"  ·  {d.get('current_system') or '?'}",
        "",
        f"  jumps        {t['fsd_jumps']} FSD, {t['carrier_jumps']} carrier"
        f"   ·   {t['light_years']:,} ly   ·   {t['systems_visited']} systems",
        f"  scanned      {x['bodies_scanned']} bodies, {x['bodies_mapped']} mapped",
        f"  notable      {len(x['earthlikes'])} ELW, {len(x['water_worlds'])} WW,"
        f" {len(x['ammonia_worlds'])} AW, {len(x['terraformable_water'])} terraformable",
        f"  organics     {b['samples_completed']} samples,"
        f" {len(b['species'])} species, off {b['bodies_sampled']} bodies",
    ]
    if b["sold_this_window"]["samples"]:
        s = b["sold_this_window"]
        out.append(f"  SOLD         {s['samples']} for {s['total']:,} cr"
                   f" ({s['bonus']:,} bonus)")
    if c["tritium"] is not None:
        line = f"  carrier      {c['tritium']:,} t tritium"
        if c["tritium_per_jump_observed"]:
            line += (f", ~{c['tritium_per_jump_observed']:,} t per jump"
                     f" -> about {c['jumps_remaining_estimate']} jumps left")
        out.append(line)
    if t["deaths"]:
        out.append(f"  deaths       {t['deaths']}")
    if b["species"]:
        out.append("\n  species this watch:")
        for name, n in list(b["species"].items())[:12]:
            out.append(f"    {n:>3}  {name}")
    return "\n".join(out) + "\n"


def write_status(journal_dir: str, hours: float = 12,
                 dashboard: str | None = None, out_path: str | None = None) -> dict:
    """Full watch, plus only-what's-new-since-the-dashboard-was-last-built."""
    result = digest(journal_dir, hours)

    if dashboard:
        history = record_update(dashboard)
        mark = checkpoint(history)
        result["dashboard"] = {
            "last_built": history[-1] if history else None,
            "resume_from": mark,
            "builds_recorded": len(history),
        }
        if mark:
            fresh = digest(journal_dir, None, since=mark)
            for key in ("generated", "window_hours", "commander", "ship",
                        "ship_name", "credits"):
                fresh.pop(key, None)

            # Sanity check. If the delta is empty but the watch clearly is
            # not, the checkpoint cannot be trusted - a clock skew between
            # file mtimes and journal timestamps would silently hide a whole
            # session. Say so rather than reporting "nothing happened".
            empty = (fresh["travel"]["fsd_jumps"] == 0
                     and fresh["exploration"]["bodies_scanned"] == 0
                     and fresh["exobiology"]["samples_completed"] == 0)
            busy = (result["travel"]["fsd_jumps"] > 0
                    or result["exploration"]["bodies_scanned"] > 0)
            fresh["trustworthy"] = not (empty and busy)
            if empty and busy:
                fresh["note"] = ("Checkpoint looks wrong - the watch has activity "
                                 "but the delta is empty. Use the full watch "
                                 "figures instead of these.")
            result["new_since_last_dashboard"] = fresh

    out_path = out_path or os.path.join(HERE, "reference", "watch_status.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    os.replace(tmp, out_path)
    return result


if __name__ == "__main__":
    hrs = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].replace(".", "").isdigit() else 12
    sys.path.insert(0, HERE)
    import config
    target = config.EXTRA_DIRS[0]
    r = write_status(target, hrs, getattr(config, "FIELD_LOG", None))
    print(render(r))
    d = r.get("dashboard") or {}
    if d.get("last_built"):
        print(f"  dashboard last built {d['last_built']}"
              f"  ·  resuming from {d.get('resume_from')}"
              f"  ({d.get('builds_recorded')} builds recorded)")
        n = r.get("new_since_last_dashboard")
        if n:
            t, x, b = n["travel"], n["exploration"], n["exobiology"]
            print(f"  NEW since then: {t['fsd_jumps']} jumps, "
                  f"{x['bodies_scanned']} bodies, "
                  f"{b['samples_completed']} samples\n")
