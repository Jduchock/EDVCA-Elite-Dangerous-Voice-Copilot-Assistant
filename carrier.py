"""
carrier.py - everything about the fleet carrier, computed rather than recalled.

Nova was being asked to update the carrier half of the dashboard by hand. That
meant grepping hundreds of megabytes of journal for CarrierStats, working out
tritium margins in her head, and hand-authoring SVG path data for the route
map. All three are things a language model does slowly, expensively, and
sometimes wrongly. So Python does them instead.

    python carrier.py            human-readable summary
    python carrier.py --json     write reference/carrier_status.json
    python carrier.py --html     write reference/carrier_block.html
    python carrier.py --write    update the dashboard's carrier section in place

WHAT --write DOES, AND WHY IT IS SAFE
-------------------------------------
The dashboard carries two marker comments:

    <!-- CARRIER:BEGIN --> ... <!-- CARRIER:END -->

--write replaces only what sits between them. Everything else on the page -
the header, the exobiology half, the manifest, the footer - is untouched, so
Nova can regenerate the carrier section without risking the rest of the page.

It does NOT touch reference/dashboard_updates.json or watch_status.json. The
dashboard's resume cursor is driven by the field log file's own mtime, and
rewriting the page is a real rebuild, so that is the correct behaviour.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, "reference")

# Landmarks, in the game's own coordinates.
SOL    = (0.0, 0.0, 0.0)
SGR_A  = (25.21875, -20.90625, 25899.96875)
COLONIA = (-9530.5, -910.28125, 19808.125)

# Only these events are parsed. Everything else is skipped before json.loads,
# which is what keeps this fast over a few hundred megabytes.
WANTED = (b'"CarrierStats"', b'"CarrierJump"', b'"CarrierLocation"',
          b'"FSDJump"', b'"Location"', b'"Scan"')

DEPARTURE = "2026-08-23"       # nothing before this is part of this run
TRITIUM_PER_JUMP = 120         # observed, not the book figure
TANK_CAPACITY = 1000
BASELINE_TRITIUM = 17088       # what was aboard at the Sagittarius A* mark
GAP = 510                      # a leg longer than this spans offline jumps


def journal_files(directory: str, since: str = DEPARTURE) -> list:
    try:
        names = [n for n in os.listdir(directory)
                 if n.startswith("Journal.") and n.endswith(".log")]
    except Exception:
        return []
    keep = [n for n in names if n[8:18] >= since]
    return [os.path.join(directory, n) for n in sorted(keep)]


def collect(journal_dir: str) -> dict:
    """One pass over the journals. Returns raw carrier facts."""
    coords = {}          # SystemAddress -> StarPos
    positions = []       # where the carrier actually was, in order
    stats = None
    for path in journal_files(journal_dir):
        try:
            fh = open(path, "rb")
        except Exception:
            continue
        with fh:
            for raw in fh:
                if not any(w in raw for w in WANTED):
                    continue
                try:
                    e = json.loads(raw.decode("utf-8", "ignore"))
                except Exception:
                    continue
                sa, pos = e.get("SystemAddress"), e.get("StarPos")
                if sa and pos:
                    coords[sa] = pos
                ev = e.get("event")
                if ev == "CarrierStats":
                    stats = e
                elif ev in ("CarrierJump", "CarrierLocation"):
                    # CarrierJumpRequest is deliberately NOT used: it is a
                    # booking for a system the carrier has not reached, and
                    # mixing it in makes the track appear to oscillate.
                    positions.append({"t": e["timestamp"], "sa": sa,
                                      "sys": e.get("StarSystem"),
                                      "pos": pos})
    return {"coords": coords, "positions": positions, "stats": stats}


def build_track(raw: dict) -> list:
    resolved = []
    for p in raw["positions"]:
        pos = p["pos"] or raw["coords"].get(p["sa"])
        if pos and p["sys"]:
            resolved.append({"t": p["t"], "sys": p["sys"], "pos": pos})
    resolved.sort(key=lambda r: r["t"])

    track = []
    for r in resolved:
        if track and track[-1]["sys"] == r["sys"]:
            continue
        track.append(r)
    # drop A -> B -> A flickers, then collapse any repeat left behind
    clean = [r for i, r in enumerate(track)
             if not (0 < i < len(track) - 1
                     and track[i - 1]["sys"] == track[i + 1]["sys"])]
    out, cum = [], 0.0
    for r in clean:
        if out and out[-1]["sys"] == r["sys"]:
            continue
        p = r["pos"]
        leg = math.dist(out[-1]["pos"], p) if out else 0.0
        cum += leg
        out.append({"i": len(out), "t": r["t"], "sys": r["sys"], "pos": p,
                    "x": round(p[0], 1), "y": round(p[1], 1), "z": round(p[2], 1),
                    "leg": round(leg, 1), "cum": round(cum, 1),
                    "sol": round(math.dist(SOL, p), 1),
                    "sgr": round(math.dist(SGR_A, p), 1),
                    "col": round(math.dist(COLONIA, p), 1),
                    "gap": leg > GAP})
    return out


def fuel_to(ly: float, jump_range: float) -> tuple:
    jumps = math.ceil(ly / jump_range) if ly > 0 else 0
    return jumps, jumps * TRITIUM_PER_JUMP


def status(journal_dir: str) -> dict:
    raw = collect(journal_dir)
    track = build_track(raw)
    st = raw["stats"] or {}
    su = st.get("SpaceUsage") or {}
    fin = st.get("Finance") or {}
    tank = st.get("FuelLevel") or 0
    hold = su.get("Cargo") or 0
    total = tank + hold
    rng = st.get("JumpRangeCurr") or 500.0
    now = track[-1] if track else None

    d = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "name": st.get("Name"), "callsign": st.get("Callsign"),
        "fuel": {
            "tank_t": tank, "tank_capacity_t": TANK_CAPACITY,
            "hold_t": hold, "total_t": total,
            "per_jump_t": TRITIUM_PER_JUMP,
            "jumps_from_tank": tank // TRITIUM_PER_JUMP,
            "jumps_from_hold": hold // TRITIUM_PER_JUMP,
            "tank_low": tank < 500,
        },
        "turnaround": {
            "baseline_t": BASELINE_TRITIUM,
            "turn_at_t": BASELINE_TRITIUM // 2,
            "margin_t": total - BASELINE_TRITIUM // 2,
            "margin_jumps": (total - BASELINE_TRITIUM // 2) // TRITIUM_PER_JUMP,
            "percent_of_baseline": round(total / BASELINE_TRITIUM * 100, 1),
            "vs_baseline_t": total - BASELINE_TRITIUM,
        },
        "capacity": {"total_t": su.get("TotalCapacity"), "cargo_t": hold,
                     "crew_t": su.get("Crew"), "free_t": su.get("FreeSpace")},
        "finance": {"balance": fin.get("CarrierBalance"),
                    "available": fin.get("AvailableBalance"),
                    "reserve": fin.get("ReserveBalance"),
                    "reserve_percent": fin.get("ReservePercent"),
                    "tax_percent": fin.get("TaxRate_refuel")},
        "services": [c.get("CrewRole") for c in (st.get("Crew") or [])
                     if c.get("Enabled")],
        "jump_range_ly": rng,
        "track": {"stops": len(track),
                  "distance_ly": round(track[-1]["cum"]) if track else 0,
                  "gap_legs": sum(1 for p in track if p["gap"]),
                  "current_system": now["sys"] if now else None,
                  "from_sol_ly": now["sol"] if now else None,
                  "from_sgr_a_ly": now["sgr"] if now else None,
                  "from_colonia_ly": now["col"] if now else None},
        "stops": [{k: v for k, v in p.items() if k != "pos"} for p in track],
    }
    if now:
        jc, tc = fuel_to(now["col"], rng)
        js, ts = fuel_to(now["sol"], rng)
        d["getting_home"] = {
            "colonia_jumps": jc, "colonia_tritium_t": tc,
            "bubble_jumps": js, "bubble_tritium_t": ts,
            # every jump further out also buys the jump back, hence the 2x
            "further_out_keeping_colonia_jumps": max(0, (total - tc) // (TRITIUM_PER_JUMP * 2)),
            "further_out_keeping_bubble_jumps": max(0, (total - ts) // (TRITIUM_PER_JUMP * 2)),
            "can_still_reach_colonia": total >= tc,
            "can_still_reach_bubble": total >= ts,
        }
    return d


# --------------------------------------------------------------------------
# rendering - the HTML block the dashboard shows
# --------------------------------------------------------------------------

BEGIN = "<!-- CARRIER:BEGIN -->"
END = "<!-- CARRIER:END -->"


def _n(x):
    return f"{round(x):,}"


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _galaxy_svg(stops, w=300, h=330, pad=16):
    X0, X1, Z0, Z1 = -52000, 52000, -26000, 78000
    s = min((w - pad * 2) / (X1 - X0), (h - pad * 2) / (Z1 - Z0))
    px = lambda x: pad + (x - X0) * s + ((w - pad * 2) - (X1 - X0) * s) / 2
    py = lambda z: h - pad - (z - Z0) * s
    route = " ".join(("M" if i == 0 else "L") + f"{px(p['x']):.1f} {py(p['z']):.1f}"
                     for i, p in enumerate(stops))
    rings = "".join(
        f'<circle cx="{px(0):.1f}" cy="{py(0):.1f}" r="{r*s:.1f}" fill="none" '
        f'stroke="#3a2a11" stroke-width="1" stroke-dasharray="2 5"/>'
        for r in (10000, 20000, 30000, 40000))
    marks = ""
    for lx, lz, lbl, anch in ((0, 0, "Sol", "start"),
                              (COLONIA[0], COLONIA[2], "Colonia", "end"),
                              (SGR_A[0], SGR_A[2], "Sgr A*", "start")):
        dx = 6 if anch == "start" else -6
        marks += (f'<circle cx="{px(lx):.1f}" cy="{py(lz):.1f}" r="2.6" fill="#43cfc4" '
                  f'stroke="#0c0e11" stroke-width="1.2"/>'
                  f'<text x="{px(lx)+dx:.1f}" y="{py(lz)+3:.1f}" fill="#43cfc4" '
                  f'font-size="8" font-family="\'Chakra Petch\',sans-serif" '
                  f'letter-spacing="0.08em" text-anchor="{anch}">{lbl}</text>')
    now = stops[-1]
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Top-down galaxy map of '
            f'HOME\'s route out past the core.">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#0c0e11"/>'
            f'<circle cx="{px(SGR_A[0]):.1f}" cy="{py(SGR_A[2]):.1f}" r="{49000*s:.1f}" '
            f'fill="#101318" stroke="#25292f"/>'
            f'<circle cx="{px(SGR_A[0]):.1f}" cy="{py(SGR_A[2]):.1f}" r="{22000*s:.1f}" fill="#13161c"/>'
            f'<circle cx="{px(SGR_A[0]):.1f}" cy="{py(SGR_A[2]):.1f}" r="{5200*s:.1f}" fill="#1a1d24"/>'
            f'{rings}{marks}'
            f'<path d="{route}" fill="none" stroke="#ffb02e" stroke-width="1.6" stroke-linejoin="round"/>'
            f'<circle cx="{px(now["x"]):.1f}" cy="{py(now["z"]):.1f}" r="6" fill="none" '
            f'stroke="#ffb02e" opacity=".45"/>'
            f'<circle cx="{px(now["x"]):.1f}" cy="{py(now["z"]):.1f}" r="3.4" fill="#ffb02e" '
            f'stroke="#0c0e11" stroke-width="1.2"/>'
            f'<text x="{px(now["x"])+9:.1f}" y="{py(now["z"])+3:.1f}" fill="#ffb02e" '
            f'font-size="8.5" font-family="\'Chakra Petch\',sans-serif" '
            f'letter-spacing="0.09em">HOME</text></svg>')


def _run_svg(stops, w=1060, h=250, L=42, R=84, T=18, B=32):
    zs = [p["z"] for p in stops]; xs = [p["x"] for p in stops]
    z0, z1 = min(zs), max(zs); x0, x1 = min(xs), max(xs)
    padx = (x1 - x0) * 0.16 or 400
    px = lambda z: L + (z - z0) / (z1 - z0) * (w - L - R)
    py = lambda x: T + ((x1 + padx) - x) / ((x1 + padx) - (x0 - padx)) * (h - T - B)
    grid = ""
    for z in range(0, int(z1) + 1, 5000):
        if z < z0:
            continue
        grid += (f'<line x1="{px(z):.1f}" y1="{T}" x2="{px(z):.1f}" y2="{h-B}" '
                 f'stroke="#25292f" stroke-width="1"/>'
                 f'<text x="{px(z):.1f}" y="{h-B+14}" fill="#55524b" font-size="9" '
                 f'font-family="\'IBM Plex Mono\',monospace" text-anchor="middle">{z//1000}k</text>')
    sgr = ""
    if z0 <= SGR_A[2] <= z1:
        sgr = (f'<line x1="{px(SGR_A[2]):.1f}" y1="{T}" x2="{px(SGR_A[2]):.1f}" y2="{h-B}" '
               f'stroke="#1f5f5c" stroke-width="1" stroke-dasharray="3 4"/>'
               f'<text x="{px(SGR_A[2])+5:.1f}" y="{T+11}" fill="#43cfc4" font-size="9" '
               f'font-family="\'Chakra Petch\',sans-serif" letter-spacing="0.10em">SGR A*</text>')
    dash = 'stroke-dasharray="4 4" '
    legs = ""
    for a, b in zip(stops, stops[1:]):
        legs += (f'<line x1="{px(a["z"]):.1f}" y1="{py(a["x"]):.1f}" '
                 f'x2="{px(b["z"]):.1f}" y2="{py(b["x"]):.1f}" '
                 f'stroke="{"#8a6220" if b["gap"] else "#ffb02e"}" '
                 f'stroke-width="{1.4 if b["gap"] else 2}" '
                 + (dash if b["gap"] else "") + 'stroke-linecap="round"/>')
    dots = ""
    for p in stops:
        last = p["i"] == len(stops) - 1
        dots += (f'<circle cx="{px(p["z"]):.1f}" cy="{py(p["x"]):.1f}" r="{5 if last else 3.4}" '
                 f'fill="{"#ffb02e" if last else "#43cfc4"}" stroke="#0c0e11" stroke-width="1.3">'
                 f'<title>{_esc(p["sys"])} &#10;{p["t"][:10]} &#10;{_n(p["sol"])} ly from Sol</title></circle>')
    mid = (T + h - B) / 2
    now = stops[-1]
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Carrier route, core-ward '
            f'distance across and lateral drift vertically, {len(stops)} stops.">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#0c0e11"/>{grid}{sgr}{legs}{dots}'
            f'<text x="{L}" y="{h-4}" fill="#8b8578" font-size="9" '
            f'font-family="\'Chakra Petch\',sans-serif" letter-spacing="0.16em">CORE-WARD DISTANCE (LY)</text>'
            f'<text x="13" y="{mid:.0f}" fill="#8b8578" font-size="9" '
            f'font-family="\'Chakra Petch\',sans-serif" letter-spacing="0.16em" '
            f'text-anchor="middle" transform="rotate(-90 13 {mid:.0f})">LATERAL DRIFT</text>'
            f'<text x="{px(now["z"])-8:.1f}" y="{py(now["x"])-11:.1f}" fill="#ffb02e" font-size="10" '
            f'font-family="\'Chakra Petch\',sans-serif" letter-spacing="0.10em" '
            f'text-anchor="end">HOME</text></svg>')


def render(d: dict) -> str:
    """The whole carrier section, ready to drop between the markers."""
    st = [dict(s) for s in d["stops"]]
    f, t, cap, fin, tr = d["fuel"], d["turnaround"], d["capacity"], d["finance"], d["track"]
    gh = d.get("getting_home") or {}
    now = st[-1]
    tank_pct = min(100.0, f["tank_t"] / f["tank_capacity_t"] * 100)
    cargo_pct = cap["cargo_t"] / cap["total_t"] * 100
    crew_pct = cap["crew_t"] / cap["total_t"] * 100
    free_pct = cap["free_t"] / cap["total_t"] * 100
    jt = f["jumps_from_tank"]

    alert = ""
    if f["tank_low"]:
        alert = (f'<div class="alert"><b>Tank is low.</b> {jt} jump'
                 f'{"" if jt == 1 else "s"} left at the observed {f["per_jump_t"]} t '
                 f'per jump. There are <b>{_n(f["hold_t"])} t</b> sitting in the hold '
                 f'&mdash; transfer some across before the next plot, or HOME goes '
                 f'nowhere.</div>')
    services = "".join(f'<span class="svc">{_esc(s)}</span>' for s in d["services"])
    recent = "".join(
        f'<tr><td class="n">{p["t"][5:10]}</td><td class="sys">{_esc(p["sys"])}</td>'
        f'<td class="r{" gapleg" if p["gap"] else ""}">'
        f'{_n(p["leg"]) if p["leg"] else "&mdash;"}</td>'
        f'<td class="r n">{_n(p["sol"])}</td></tr>' for p in reversed(st[-14:]))
    sign = "+" if t["vs_baseline_t"] >= 0 else "&minus;"

    return f"""{BEGIN}
<div class="effort carrier">
  <h2>Fleet Carrier <span>{_esc(d["name"])}</span></h2><div class="rule"></div>
  <div class="note">{_esc(d["callsign"])} &nbsp;·&nbsp; logistics, fuel and the road behind us</div>
</div>

<div class="row r5">
  <div class="tile t2"><div class="k">Tritium in Tank</div><div class="v">{_n(f["tank_t"])} <em>t</em></div><div class="u">{jt} jump{"" if jt==1 else "s"} at {f["per_jump_t"]} t each</div></div>
  <div class="tile t2"><div class="k">Tritium in Hold</div><div class="v">{_n(f["hold_t"])} <em>t</em></div><div class="u">{f["jumps_from_hold"]} more jumps once transferred</div></div>
  <div class="tile"><div class="k">Jump Range</div><div class="v">{d["jump_range_ly"]:.0f} <em>ly</em></div><div class="u">tritium burns at {f["per_jump_t"]} t a jump</div></div>
  <div class="tile"><div class="k">Carrier Track</div><div class="v">{_n(tr["distance_ly"])} <em>ly</em></div><div class="u">{tr["stops"]} recorded stops</div></div>
  <div class="tile"><div class="k">Available Balance</div><div class="v">{fin["available"]/1e9:.2f} <em>B</em></div><div class="u">of {fin["balance"]/1e9:.2f} B, {fin["reserve_percent"]}% reserved</div></div>
</div>

<div class="cgrid">

  <div class="cw">
    <div class="h">Fuel State</div>
    <div class="fuel{' low' if f["tank_low"] else ''}" title="Tritium in the carrier's own tank. The hold carries {_n(f['hold_t'])} t more, but it has to be transferred in before it can be burned.">
      <i style="width:{tank_pct:.1f}%"></i>
      <b class="l">{_n(f["tank_t"])} t in tank</b><b class="r">tank holds {_n(f["tank_capacity_t"])} t</b>
    </div>
    {alert}
    <div class="h" style="margin-top:18px">Tritium Aboard &mdash; Turnaround Check</div>
    <div class="tri" title="{_n(t['baseline_t'])} t aboard at the Sagittarius A* baseline. The teal marker is half of that, {_n(t['turn_at_t'])} t: burn past it and there is not enough left to get home. Tank {_n(f['tank_t'])} t plus {_n(f['hold_t'])} t in the hold = {_n(f['total_t'])} t.">
      <div class="tfill" style="width:{t["percent_of_baseline"]:.1f}%"></div>
      <div class="thalf" style="left:50%"></div>
      <b class="l">Tritium {_n(f["total_t"])} t</b>
      <b class="m">Turn at {_n(t["turn_at_t"])}</b>
      <b class="r">{sign}{_n(abs(t["vs_baseline_t"]))} t vs baseline</b>
    </div>
    <div class="trinote">
      Tank plus hold, against the <b>{_n(t["baseline_t"])} t</b> you were carrying at the
      Sagittarius A* mark. The teal line is half of that &mdash; <b>{_n(t["turn_at_t"])} t</b>
      &mdash; the point where you have burned as much as it would take to get back.
      You are <b>{_n(t["margin_t"])} t</b> clear of it, roughly
      <b>{t["margin_jumps"]} carrier jumps</b> at the observed {f["per_jump_t"]} t a jump.
    </div>
    <dl class="kv" style="margin-top:11px">
      <dt>Fuel to reach Colonia &mdash; {gh.get("colonia_jumps")} jumps</dt><dd class="t">{_n(gh.get("colonia_tritium_t", 0))} t</dd>
      <dt>Fuel to reach the bubble &mdash; {gh.get("bubble_jumps")} jumps</dt><dd class="{"a" if gh.get("can_still_reach_bubble") else ""}">{_n(gh.get("bubble_tritium_t", 0))} t</dd>
      <dt>Further out and still able to reach Colonia</dt><dd class="t">{gh.get("further_out_keeping_colonia_jumps")} jumps &nbsp;<span style="color:var(--faint)">{_n(gh.get("further_out_keeping_colonia_jumps",0)*d["jump_range_ly"])} ly</span></dd>
      <dt>Further out and still able to reach the bubble</dt><dd class="a">{gh.get("further_out_keeping_bubble_jumps")} jumps &nbsp;<span style="color:var(--faint)">{_n(gh.get("further_out_keeping_bubble_jumps",0)*d["jump_range_ly"])} ly</span></dd>
    </dl>
    <div class="trinote">
      Straight-line distance at the carrier's full {d["jump_range_ly"]:.0f} ly range and the
      observed {f["per_jump_t"]} t a jump. The last two lines are halved on purpose: every
      jump further out also has to be paid for on the way back.
    </div>
    <div class="h" style="margin-top:16px">Capacity &mdash; {_n(cap["total_t"])} t</div>
    <div class="bar2">
      <i style="width:{cargo_pct:.1f}%;background:var(--amber-dim)"></i>
      <i style="width:{crew_pct:.1f}%;background:var(--teal-dim)"></i>
      <i style="width:{free_pct:.1f}%;background:#1b1e22"></i>
    </div>
    <div class="seg">
      <span><u style="background:var(--amber-dim)"></u>Cargo <b>{_n(cap["cargo_t"])} t</b></span>
      <span><u style="background:var(--teal-dim)"></u>Crew &amp; services <b>{_n(cap["crew_t"])} t</b></span>
      <span><u style="background:#1b1e22;border:1px solid var(--line)"></u>Free <b>{_n(cap["free_t"])} t</b></span>
    </div>
    <div class="h" style="margin-top:18px">The Run Outward &mdash; every recorded carrier stop</div>
    <div class="runwrap">{_run_svg(st)}</div>
    <div class="trinote">
      Left to right is distance toward the core. Dashed legs are jumps made while
      he was logged out. Hover any stop for the system and the date.
    </div>
    <div class="h" style="margin-top:18px">Finance</div>
    <dl class="kv">
      <dt>Carrier balance</dt><dd class="a">{_n(fin["balance"])} CR</dd>
      <dt>Available to spend</dt><dd>{_n(fin["available"])} CR</dd>
      <dt>Held in reserve</dt><dd class="t">{_n(fin["reserve"])} CR &nbsp;<span style="color:var(--faint)">{fin["reserve_percent"]}%</span></dd>
      <dt>Upkeep tax, all services</dt><dd>{fin["tax_percent"]}%</dd>
    </dl>
  </div>

  <div class="cw">
    <div class="h">Where HOME Has Been &mdash; {tr["stops"]} stops, {_n(tr["distance_ly"])} ly</div>
    <div class="mapwrap">{_galaxy_svg(st)}</div>
    <dl class="kv" style="margin-top:12px">
      <dt>Now at</dt><dd class="t">{_esc(now["sys"])}</dd>
      <dt>From Sol</dt><dd class="a">{_n(now["sol"])} ly</dd>
      <dt>From Sagittarius A*</dt><dd>{_n(now["sgr"])} ly</dd>
      <dt>From Colonia</dt><dd>{_n(now["col"])} ly</dd>
    </dl>
    <div class="h" style="margin-top:18px">Services Online &mdash; {len(d["services"])}</div>
    <div>{services}</div>
    <div class="h" style="margin-top:18px">Last Fourteen Stops</div>
    <table class="ctrack">{recent}</table>
    <div class="alert" style="border-left-color:var(--amber-dim)">
      Legs in <b style="color:var(--amber)">amber</b> are longer than one
      {d["jump_range_ly"]:.0f} ly jump &mdash; HOME moved while he was logged out, so those
      intermediate stops were never written to the journal.
      <b style="color:var(--amber)">{tr["gap_legs"]}</b> of {tr["stops"]-1} legs are like that.
    </div>
  </div>

</div>
{END}"""


def write_status(journal_dir: str) -> dict:
    d = status(journal_dir)
    os.makedirs(REF, exist_ok=True)
    tmp = os.path.join(REF, "carrier_status.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, os.path.join(REF, "carrier_status.json"))
    return d


def write_block(journal_dir: str) -> str:
    d = status(journal_dir)
    block = render(d)
    with open(os.path.join(REF, "carrier_block.html"), "w", encoding="utf-8") as fh:
        fh.write(block)
    return block


def write_into_dashboard(journal_dir: str, dashboard: str) -> str:
    """Swap only what lies between the markers. Everything else is untouched."""
    with open(dashboard, encoding="utf-8") as fh:
        page = fh.read()
    if BEGIN not in page or END not in page:
        raise SystemExit(
            f"markers not found in {dashboard}.\n"
            f"The carrier section must be wrapped in {BEGIN} ... {END} "
            f"before this can update it in place.")
    d = status(journal_dir)
    i = page.index(BEGIN); j = page.index(END) + len(END)
    page = page[:i] + render(d) + page[j:]
    tmp = dashboard + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(page)
    os.replace(tmp, dashboard)
    return d


def summary(d: dict) -> str:
    f, t, tr = d["fuel"], d["turnaround"], d["track"]
    gh = d.get("getting_home") or {}
    L = [f'{d["name"]} ({d["callsign"]})',
         f'  tritium   tank {f["tank_t"]:,} t ({f["jumps_from_tank"]} jumps) · '
         f'hold {f["hold_t"]:,} t ({f["jumps_from_hold"]} jumps) · total {f["total_t"]:,} t',
         f'  turnaround  {t["percent_of_baseline"]}% of the {t["baseline_t"]:,} t baseline · '
         f'turn at {t["turn_at_t"]:,} · {t["margin_t"]:,} t clear ({t["margin_jumps"]} jumps)',
         f'  track     {tr["stops"]} stops · {tr["distance_ly"]:,} ly · {tr["gap_legs"]} legs span offline jumps',
         f'  now at    {tr["current_system"]} · {tr["from_sol_ly"]:,.0f} ly from Sol · '
         f'{tr["from_colonia_ly"]:,.0f} from Colonia']
    if gh:
        L.append(f'  getting home  Colonia {gh["colonia_jumps"]} jumps / {gh["colonia_tritium_t"]:,} t · '
                 f'bubble {gh["bubble_jumps"]} jumps / {gh["bubble_tritium_t"]:,} t')
        L.append(f'  can still push {gh["further_out_keeping_colonia_jumps"]} jumps further out and reach '
                 f'Colonia, {gh["further_out_keeping_bubble_jumps"]} and reach the bubble')
    if f["tank_low"]:
        L.append(f'  !! TANK LOW - transfer tritium from the hold before plotting')
    return "\n".join(L)


def _journal_dir():
    import config
    return (config.EXTRA_DIRS[0] if getattr(config, "EXTRA_DIRS", None)
            else config.WORK_DIR)


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    import config
    jd = _journal_dir()
    if "--write" in sys.argv:
        d = write_into_dashboard(jd, config.FIELD_LOG)
        print("dashboard carrier section updated.\n")
        print(summary(d))
    elif "--html" in sys.argv:
        write_block(jd)
        print("wrote reference/carrier_block.html")
    elif "--json" in sys.argv:
        d = write_status(jd)
        print("wrote reference/carrier_status.json\n")
        print(summary(d))
    else:
        print(summary(status(jd)))
