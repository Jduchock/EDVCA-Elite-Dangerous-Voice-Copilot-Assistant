"""
exobio.py - work out exactly where John stands on exobiology.

Reads the Elite Dangerous journals, works out what has been SOLD (fact) and
what is still SITTING IN THE HOLD unsold (the thing that matters for a
zero-to-Elite single turn-in), values it, and writes the answer to
reference/exobiology_status.json for the voice assistant to read.

Run it directly for a readable report:
    python exobio.py

Key mechanics this encodes:
  * Rank comes from lifetime PROFIT selling organic data (Value + Bonus).
  * A First Logged sample pays base + 4x base = 5x total.
  * "First logged" is per body per species, awarded to the first commander to
    SELL - so it cannot be known for certain before the sale.
  * ScanOrganic ScanType "Analyse" is the third and final sample: that's when
    a species is complete and bankable.
  * Scan.WasFootfalled == False means nobody has ever landed there, so nothing
    on that body can have been sold: bonus highly likely.
  * Populated systems don't record footfall, so the signal is meaningless
    there and bonuses are effectively unavailable.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

RANKS = [
    ("Directionless", 0), ("Mostly Directionless", 22_500_000),
    ("Compiler", 83_475_000), ("Collector", 210_560_000),
    ("Cataloguer", 532_800_000), ("Taxonomist", 1_144_000_000),
    ("Ecologist", 2_262_600_000), ("Geneticist", 3_996_000_000),
    ("Elite", 8_425_000_000), ("Elite I", 12_969_000_000),
    ("Elite II", 17_425_000_000), ("Elite III", 21_925_000_000),
    ("Elite IV", 26_320_000_000), ("Elite V", 30_553_600_000),
]
FIRST_LOGGED_MULTIPLIER = 5
HERE = os.path.dirname(os.path.abspath(__file__))


def load_species_values(path: str | None = None) -> dict:
    path = path or os.path.join(HERE, "reference", "species_values.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("values", {})
    except Exception:
        return {}


def rank_for(profit: int):
    """(current rank, next rank, credits still needed for next)."""
    current, nxt, need = RANKS[0][0], None, 0
    for name, threshold in RANKS:
        if profit >= threshold:
            current = name
        else:
            nxt, need = name, threshold - profit
            break
    return current, nxt, need


def journal_files(directory: str) -> list[str]:
    try:
        names = [n for n in os.listdir(directory)
                 if n.startswith("Journal.") and n.endswith(".log")]
    except OSError:
        return []
    return [os.path.join(directory, n) for n in sorted(names)]


def analyse(journal_dir: str, values: dict | None = None) -> dict:
    values = values if values is not None else load_species_values()

    sold_value = sold_bonus = 0
    sold_count = bonus_count = 0
    sold_by_species: dict[str, int] = defaultdict(int)
    observed_value: dict[str, int] = {}

    analysed: dict[str, int] = defaultdict(int)          # species -> completed sets
    analysed_bodies: dict[str, set] = defaultdict(set)   # species -> {(system, body)}
    body_flags: dict[tuple, dict] = {}                   # (system, body) -> flags
    populated: set = set()

    files = journal_files(journal_dir)
    system = body = None

    for path in files:
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line or '"event"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                event = e.get("event")

                if event in ("Location", "FSDJump", "CarrierJump"):
                    system = e.get("StarSystem") or system
                    if e.get("Population"):
                        populated.add(system)
                elif event == "Touchdown" or event == "ApproachBody":
                    body = e.get("Body") or body

                elif event == "Scan":
                    key = (e.get("StarSystem") or system, e.get("BodyName"))
                    if key[1]:
                        body_flags[key] = {
                            "was_discovered": e.get("WasDiscovered"),
                            "was_mapped": e.get("WasMapped"),
                            "was_footfalled": e.get("WasFootfalled"),
                        }

                elif event == "ScanOrganic":
                    name = e.get("Species_Localised") or e.get("Species") or "?"
                    if e.get("ScanType") == "Analyse":
                        analysed[name] += 1
                        analysed_bodies[name].add((system, body))
                        if e.get("WasLogged") is True:
                            body_flags.setdefault((system, body), {})["was_logged"] = True

                elif event == "SellOrganicData":
                    for bio in e.get("BioData", []) or []:
                        name = bio.get("Species_Localised") or bio.get("Species") or "?"
                        value = int(bio.get("Value") or 0)
                        bonus = int(bio.get("Bonus") or 0)
                        sold_value += value
                        sold_bonus += bonus
                        sold_count += 1
                        sold_by_species[name] += 1
                        if bonus > 0:
                            bonus_count += 1
                        if value:
                            observed_value[name] = value

    # His own sales are the most authoritative price source available.
    merged = dict(values)
    merged.update(observed_value)

    banked = {}
    for name, done in analysed.items():
        left = done - sold_by_species.get(name, 0)
        if left > 0:
            banked[name] = left

    unknown = sorted(n for n in banked if n not in merged)
    base_total = sum(merged.get(n, 0) * c for n, c in banked.items())
    banked_count = sum(banked.values())

    # Footfall signal across the bodies holding unsold samples.
    relevant = set()
    for name in banked:
        relevant |= analysed_bodies.get(name, set())
    virgin = touched = unknown_ff = 0
    for key in relevant:
        flags = body_flags.get(key) or {}
        ff = flags.get("was_footfalled")
        if ff is False:
            virgin += 1
        elif ff is True:
            touched += 1
        else:
            unknown_ff += 1
    known_ff = virgin + touched
    footfall_rate = (virgin / known_ff) if known_ff else None

    hit_rate = (bonus_count / sold_count) if sold_count else None

    lifetime = sold_value + sold_bonus
    current, nxt, need = rank_for(lifetime)

    worst = base_total
    best = base_total * FIRST_LOGGED_MULTIPLIER
    if footfall_rate is not None:
        likely = base_total * (1 + 4 * footfall_rate)
        likely_basis = f"{virgin} of {known_ff} bodies never footfalled"
    elif hit_rate is not None:
        likely = base_total * (1 + 4 * hit_rate)
        likely_basis = f"your past sales earned the bonus {hit_rate:.0%} of the time"
    else:
        likely = None
        likely_basis = "no evidence either way yet"

    def projection(total):
        if total is None:
            return None
        rank, _, _ = rank_for(lifetime + total)
        return {"payout": int(total), "rank_after": rank,
                "reaches_elite": (lifetime + total) >= 8_425_000_000}

    return {
        "journal_dir": journal_dir,
        "journals_read": len(files),
        "sold": {
            "samples": sold_count,
            "base_credits": sold_value,
            "bonus_credits": sold_bonus,
            "total_credits": lifetime,
            "first_logged_samples": bonus_count,
            "first_logged_rate": round(hit_rate, 4) if hit_rate is not None else None,
        },
        "rank": {"current": current, "next": nxt, "credits_to_next": need},
        "banked": {
            "samples": banked_count,
            "species": dict(sorted(banked.items(),
                                   key=lambda kv: -merged.get(kv[0], 0) * kv[1])),
            "base_value": base_total,
            "species_without_a_known_price": unknown,
            "bodies_never_footfalled": virgin,
            "bodies_already_footfalled": touched,
            "bodies_footfall_unknown": unknown_ff,
        },
        "projection": {
            "worst_case": projection(worst),
            "likely_case": projection(likely),
            "best_case": projection(best),
            "likely_basis": likely_basis,
            "elite_threshold": 8_425_000_000,
            "credits_short_of_elite_worst": max(0, 8_425_000_000 - (lifetime + worst)),
            "credits_short_of_elite_best": max(0, 8_425_000_000 - (lifetime + best)),
        },
    }


def render(r: dict) -> str:
    def money(n):
        return f"{n:,}" if n is not None else "?"
    out = []
    s, b, p = r["sold"], r["banked"], r["projection"]
    out.append(f"\nJournals read: {r['journals_read']}")
    out.append(f"\nSOLD SO FAR   {s['samples']} samples")
    out.append(f"  base        {money(s['base_credits'])}")
    out.append(f"  bonus       {money(s['bonus_credits'])}")
    out.append(f"  total       {money(s['total_credits'])}   -> rank {r['rank']['current']}")
    if s["first_logged_rate"] is not None:
        out.append(f"  first logged {s['first_logged_samples']}/{s['samples']} "
                   f"({s['first_logged_rate']:.0%})")
    out.append(f"\nIN THE HOLD   {b['samples']} samples, {len(b['species'])} species")
    out.append(f"  base value  {money(b['base_value'])}")
    out.append(f"  bodies never footfalled: {b['bodies_never_footfalled']}  "
               f"already footfalled: {b['bodies_already_footfalled']}  "
               f"unknown: {b['bodies_footfall_unknown']}")
    if b["species_without_a_known_price"]:
        out.append(f"  NO PRICE FOR: {', '.join(b['species_without_a_known_price'])}")
    out.append("\nIF YOU SOLD IT ALL NOW")
    for label in ("worst_case", "likely_case", "best_case"):
        c = p[label]
        if not c:
            continue
        flag = "  *** ELITE ***" if c["reaches_elite"] else ""
        out.append(f"  {label.replace('_',' '):<12} {money(c['payout']):>18}"
                   f"   -> {c['rank_after']}{flag}")
    out.append(f"  ({p['likely_basis']})")
    short = p["credits_short_of_elite_worst"]
    out.append(f"\n  Worst case leaves you {money(short)} short of Elite."
               if short else "\n  Even the worst case clears Elite.")
    return "\n".join(out) + "\n"


def write_status(journal_dir: str, out_path: str | None = None) -> dict:
    result = analyse(journal_dir)
    out_path = out_path or os.path.join(HERE, "reference", "exobiology_status.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    os.replace(tmp, out_path)
    return result


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        sys.path.insert(0, HERE)
        import config
        target = config.EXTRA_DIRS[0]
    print(render(write_status(target)))
