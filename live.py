"""
live.py - watch the game as it happens.

Two sources, both written by Elite Dangerous itself:

    Status.json     rewritten continuously (many times a second). A flag
                    bitfield plus pips, fuel, cargo, balance, destination,
                    and - on foot or on a surface - health and position.
    Journal.*.log   appended a line at a time. The narrative: scans, jumps,
                    heat, combat, kills, landings.

This module does two separate jobs and keeps them strictly apart:

  1. REACTIONS. Certain events deserve an immediate spoken line. Those lines
     are canned (config.REACTIONS) and go straight to the speakers. No model
     call, so they are instant and they cost nothing. See voice_claude.py's
     live_watcher().

  2. CONTEXT. A rolling snapshot of what is true right now, written to
     reference/live_context.json, so she can answer "where are we" without
     re-parsing a hundred megabytes of journal.

WHAT THIS MODULE MUST NEVER TOUCH
---------------------------------
The dashboard's resume cursor. watch.py decides where the next dashboard
delta starts, and it does so from the FIELD LOG FILE'S OWN MTIME via
reference/dashboard_updates.json. That design is what stops her double
posting or leaving gaps, and it only works if nothing else writes to either
of those. So:

    - this module never writes dashboard_updates.json
    - this module never writes watch_status.json
    - this module never writes, touches or restats the field log HTML
    - its own tail position lives in memory only, and reseeds to the end of
      the journal on every launch - it persists nothing that could drift

Nothing here can move the dashboard's idea of "last built". If live.py were
deleted mid-watch the dashboard would carry on exactly as before.

    python live.py --selftest     decode Status.json into English, once
    python live.py --tail         print reactions as they fire (no speech)
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONTEXT_FILE = os.path.join(HERE, "reference", "live_context.json")

# Frontier's Status.json flag bits. Treat this as a best-effort decode:
# --selftest prints it in English so it can be checked against the game
# rather than believed on faith.
FLAGS = [
    (0,  "docked"),            (1,  "landed"),          (2,  "gear down"),
    (3,  "shields up"),        (4,  "supercruise"),     (5,  "flight assist off"),
    (6,  "hardpoints out"),    (7,  "in wing"),         (8,  "lights on"),
    (9,  "cargo scoop out"),   (10, "silent running"),  (11, "fuel scooping"),
    (12, "srv handbrake"),     (13, "srv turret"),      (14, "srv under ship"),
    (15, "srv drive assist"),  (16, "fsd mass locked"), (17, "fsd charging"),
    (18, "fsd cooldown"),      (19, "LOW FUEL"),        (20, "OVERHEATING"),
    (21, "has lat/long"),      (22, "IN DANGER"),       (23, "BEING INTERDICTED"),
    (24, "in main ship"),      (25, "in fighter"),      (26, "in srv"),
    (27, "analysis mode"),     (28, "night vision"),    (29, "alt from avg radius"),
    (30, "fsd jump"),          (31, "srv high beam"),
]


# --------------------------------------------------------------------------
# lookup tables - turning the journal's numbers and codes into English
# --------------------------------------------------------------------------

COMBAT_RANK = ["Harmless", "Mostly Harmless", "Novice", "Competent", "Expert",
               "Master", "Dangerous", "Deadly", "Elite"]

PROMO = {
    "Combat":  ("Combat", COMBAT_RANK),
    "Trade":   ("Trade", ["Penniless","Mostly Penniless","Peddler","Dealer",
                "Merchant","Broker","Entrepreneur","Tycoon","Elite"]),
    "Explore": ("Exploration", ["Aimless","Mostly Aimless","Scout","Surveyor",
                "Trailblazer","Pathfinder","Ranger","Pioneer","Elite"]),
    "Soldier": ("Mercenary", ["Defenceless","Mostly Defenceless","Rookie",
                "Soldier","Gunslinger","Warrior","Gladiator","Deadeye","Elite"]),
    "Exobiologist": ("Exobiology", ["Directionless","Mostly Directionless",
                "Compiler","Collector","Cataloguer","Taxonomist","Ecologist",
                "Geneticist","Elite"]),
    "Federation": ("Federal Navy", ["Recruit","Cadet","Midshipman",
                "Petty Officer","Chief Petty Officer","Warrant Officer",
                "Ensign","Lieutenant","Lieutenant Commander","Post Commander",
                "Post Captain","Rear Admiral","Vice Admiral","Admiral"]),
    "Empire":  ("Imperial Navy", ["Outsider","Serf","Master","Squire","Knight",
                "Lord","Baron","Viscount","Count","Earl","Marquis","Duke",
                "Prince","King"]),
    "CQC":     ("CQC", ["Helpless","Mostly Helpless","Amateur",
                "Semi Professional","Professional","Champion","Hero","Legend",
                "Elite"]),
}

# One clause about the primary, for the carrier-arrival line. Keyed on the
# journal's StarType. Everything he has actually arrived under is covered.
STAR_DESC = {
    "O": "Primary's a class O giant. Blue, savage, and rare as anything.",
    "B": "Primary's a class B. Blue-white and very hot.",
    "A": "Primary's a class A. White, hot, and good for scooping.",
    "F": "Primary's a class F. Yellow-white and obliging.",
    "G": "Primary's a class G, same sort of star as Sol. Homely, that.",
    "K": "Primary's a class K orange dwarf. Cool, steady, long-lived.",
    "M": "Primary's an M red dwarf. Dim, cold, and the commonest thing "
         "in the galaxy.",
    "L": "Primary's an L brown dwarf. Barely a star at all.",
    "T": "Primary's a T brown dwarf. Cold, dark, and not much use to anyone.",
    "Y": "Primary's a Y dwarf. Colder than some planets. No fuel here.",
    "TTS": "Primary's a T Tauri star. Young, unsettled, still making up "
           "its mind.",
    "AeBe": "Primary's a Herbig Ae Be. Very young and very bright.",
    "N": "Primary's a neutron star. Careful, John. Lovely boost, "
         "nasty cone.",
    "H": "Primary's a black hole. Mind the approach.",
    "SupermassiveBlackHole": "Primary is a supermassive black hole. "
         "Take a moment with that one.",
    "W": "Primary's a Wolf-Rayet. Shedding itself into space as we watch.",
    "C": "Primary's a carbon star. Deep red, and sooty with it.",
    "MS": "Primary's an MS star. Red, with a touch of heavy metal.",
    "S": "Primary's an S-type. Red and strange.",
    "D": "Primary's a white dwarf. A dead star, still glowing.",
}

# Giants and supergiants share a letter with the dwarfs but are nothing like
# them, so they are matched BEFORE the bare letter falls through.
STAR_SUFFIX = {
    "RedSuperGiant": "Primary's a red supergiant. Enormous, and not long "
                     "for this world.",
    "BlueWhiteSuperGiant": "Primary's a blue-white supergiant. Vast, "
                     "furious, and short-lived.",
    "RedGiant": "Primary's a red giant. Swollen and cooling. It will not "
                "look like that forever.",
    "SuperGiant": "Primary's a supergiant. Enormous.",
    "Giant": "Primary's a giant star. Old, swollen, and worth a look.",
}


def star_sentence(star_type: str | None) -> str:
    """One clause about the primary, or '' if we do not know it."""
    if not star_type:
        return ""
    if star_type in STAR_DESC:
        return STAR_DESC[star_type]
    for suffix, text in STAR_SUFFIX.items():
        if star_type.endswith(suffix):
            return text
    base = star_type.split("_")[0]
    if base.startswith("D"):
        return STAR_DESC["D"]
    if base.startswith("W"):
        return STAR_DESC["W"]
    if base.startswith("C"):
        return STAR_DESC["C"]
    return STAR_DESC.get(base, "")


# Vista Genomics base prices, loaded once. A first-logged sample pays
# base + 4x base = 5x total, which is the figure he actually cares about.
_VALUES = {"loaded": False, "v": {}, "mult": 5}


def species_value(species: str):
    """(value_with_bonus, base) for a species, or (None, None) if unpriced."""
    if not _VALUES["loaded"]:
        _VALUES["loaded"] = True
        try:
            with open(os.path.join(HERE, "reference", "species_values.json"),
                      encoding="utf-8-sig") as fh:
                raw = json.load(fh)
            _VALUES["v"] = raw.get("values") or {}
            _VALUES["mult"] = raw.get("first_logged_multiplier") or 5
        except Exception:
            pass
    base = _VALUES["v"].get(species)
    if not base:
        return None, None
    return base * _VALUES["mult"], base


_KNOWN = {"loaded": False, "set": set()}


def known_species() -> set:
    """Species already banked, from the exobiology status file.

    Used to tell a genuine first find from a codex entry that is only new
    to the region Nova happens to be flying through.
    """
    if not _KNOWN["loaded"]:
        _KNOWN["loaded"] = True
        try:
            with open(os.path.join(HERE, "reference", "exobiology_status.json"),
                      encoding="utf-8-sig") as fh:
                _KNOWN["set"] = set((json.load(fh).get("banked") or {}).get("species") or {})
        except Exception:
            pass
    return _KNOWN["set"]


def money(n) -> str:
    """Credits as they should be SPOKEN, not printed.

    "ninety-five million" beats "ninety-five million fifty-four thousand"
    out loud, and "95,054,000" is worse still - TTS reads every digit.
    """
    if not n:
        return ""
    n = float(n)
    if n >= 1e9:
        s = f"{n / 1e9:.1f}"
        return (s[:-2] if s.endswith(".0") else s) + " billion"
    if n >= 1e7:
        return f"{round(n / 1e6):,} million"
    if n >= 1e6:
        s = f"{n / 1e6:.1f}"
        return (s[:-2] if s.endswith(".0") else s) + " million"
    if n >= 1e3:
        return f"{round(n / 1e3):,} thousand"
    return f"{round(n):,}"


NUMBER_WORD = ["no", "one", "two", "three", "four", "five", "six", "seven",
               "eight", "nine", "ten", "eleven", "twelve"]


def spoken(n: int) -> str:
    """Numbers read better than digits in TTS, up to a point."""
    return NUMBER_WORD[n] if isinstance(n, int) and 0 <= n < len(NUMBER_WORD) else str(n)


def readable(code: str) -> str:
    """collidedAtSpeedInNoFireZone -> 'collided at speed in a no fire zone'."""
    if not code:
        return "something"
    out = re.sub(r"(?<!^)(?=[A-Z])", " ", str(code)).lower()
    return out.replace("no fire", "a no fire")


# Bodies worth announcing, keyed on the journal's own PlanetClass string.
NOTABLE_CLASS = {
    "Earthlike body": "earthlike",
    "Ammonia world":  "ammonia",
}


# --------------------------------------------------------------------------
# journal tailing
# --------------------------------------------------------------------------

def newest_journal(journal_dir: str) -> str | None:
    try:
        names = [n for n in os.listdir(journal_dir)
                 if n.startswith("Journal.") and n.endswith(".log")]
    except Exception:
        return None
    if not names:
        return None
    # Journal names sort chronologically; mtime breaks ties on restart.
    names.sort(key=lambda n: (os.path.getmtime(os.path.join(journal_dir, n)), n))
    return os.path.join(journal_dir, names[-1])


class Tailer:
    """Follow the newest journal, yielding whole events as they land.

    Holds a byte offset and a leftover buffer, because the game flushes
    mid-line and a partial line is not JSON. Handles the file rolling over
    when the game restarts.
    """

    def __init__(self, journal_dir: str, seed_to_end: bool = True):
        self.dir = journal_dir
        self.path = None
        self.offset = 0
        self.leftover = b""
        self.seed_to_end = seed_to_end
        self._adopt(newest_journal(journal_dir), fresh=not seed_to_end)

    def _adopt(self, path: str | None, fresh: bool) -> None:
        self.path = path
        self.leftover = b""
        if not path:
            self.offset = 0
            return
        try:
            self.offset = 0 if fresh else os.path.getsize(path)
        except Exception:
            self.offset = 0

    def poll(self) -> list[dict]:
        """Return every complete event appended since the last call."""
        latest = newest_journal(self.dir)
        if latest and latest != self.path:
            # Game restarted into a new journal - read the new one in full.
            self._adopt(latest, fresh=True)
        if not self.path:
            return []

        out = []
        try:
            size = os.path.getsize(self.path)
            if size < self.offset:          # truncated; start over
                self.offset, self.leftover = 0, b""
            if size == self.offset:
                return []
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
            self.offset += len(chunk)
            data = self.leftover + chunk
            lines = data.split(b"\n")
            self.leftover = lines.pop()     # trailing partial line, if any
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except Exception:
                    continue                # half-written line; skip it
        except Exception:
            return out
        return out


# --------------------------------------------------------------------------
# reactions
# --------------------------------------------------------------------------

class Reactor:
    """Decide which events deserve a spoken line, and which line.

    Two guards stop her machine-gunning:
      cooldown  - one line per trigger per N seconds (heat repeats hard)
      seen      - a body is only ever announced once
    """

    def __init__(self, reactions: dict, cooldowns: dict | None = None):
        self.reactions = reactions or {}
        self.cooldowns = cooldowns or {}
        self.last_spoke = {}
        self.seen_bodies = set()
        self.seen_signals = set()
        # Some events only make sense in the light of an earlier one. A
        # destroyed fighter reads very differently depending on who was
        # flying it, and the journal only says that at launch.
        self.ctx = {"fighter_player": None, "crew": None}
        # Threshold crossings: last value seen, per tracked quantity. A value
        # that sits at 30% must not re-fire every tick - only the moment it
        # crosses a line going down is worth saying out loud.
        self.levels = {}
        # Burst tallies: things that fire far too fast to narrate one at a
        # time (kills, incoming fire). Collect, go quiet, then say it once.
        self.bursts = {}
        # A carrier arrival waits a moment for the primary star to be
        # scanned before it speaks. See _on_CarrierJump / pending_due.
        self.pending = None
        self.star_wait = 20

    # -- threshold crossings -------------------------------------------
    def crossed_down(self, key: str, value: float, marks: tuple) -> float | None:
        """Return the highest mark just crossed going DOWN, else None.

        Fires once per crossing. Going back up re-arms it, so a hull
        repaired above 50% will announce again if it drops back through.
        """
        prev = self.levels.get(key)
        self.levels[key] = value
        if prev is None:
            return None
        for mark in sorted(marks, reverse=True):
            if prev > mark >= value:
                return mark
        return None

    # -- burst tallies -------------------------------------------------
    def add_to_burst(self, key: str, credits: float = 0.0) -> None:
        b = self.bursts.setdefault(key, {"n": 0, "credits": 0.0, "last": 0.0})
        b["n"] += 1
        b["credits"] += credits or 0.0
        b["last"] = time.monotonic()

    def flush_bursts(self, quiet_for: float = 20.0) -> list:
        """Call every poll. Returns [(key, n, credits)] for bursts that have
        gone quiet - i.e. the fight is over and it is safe to speak."""
        out, now = [], time.monotonic()
        for key, b in list(self.bursts.items()):
            if b["n"] and (now - b["last"]) >= quiet_for:
                out.append((key, b["n"], b["credits"]))
                del self.bursts[key]
        return out

    def _pick(self, key: str, **fmt) -> str | None:
        lines = self.reactions.get(key)
        if not lines:
            return None
        now = time.monotonic()
        gap = self.cooldowns.get(key, 0)
        if gap and (now - self.last_spoke.get(key, -1e9)) < gap:
            return None
        self.last_spoke[key] = now
        try:
            line = random.choice(list(lines)).format(**fmt)
        except Exception:
            line = random.choice(list(lines))
        # An optional clause that came back empty leaves a double space or a
        # trailing one. Harmless on screen, audible as a stumble in TTS.
        line = re.sub(r"\s{2,}", " ", line).strip()
        # A line beginning with a substitution ("five signals on...") comes
        # out lowercase. Only the first character is touched, so names and
        # deliberate capitals inside the line are left alone.
        return line[:1].upper() + line[1:] if line else line

    def _track(self, event: dict) -> None:
        """Remember the handful of facts later events depend on."""
        name = event.get("event")
        if name == "LaunchFighter":
            self.ctx["fighter_player"] = bool(event.get("PlayerControlled"))
        elif name == "CrewLaunchFighter":
            self.ctx["crew"] = event.get("Crew")
            self.ctx["fighter_player"] = False

    def react(self, event: dict) -> tuple[str, str] | None:
        """Return (trigger_key, line) or None. One line per call, at most."""
        name = event.get("event")
        self._track(event)
        fn = getattr(self, "_on_" + name, None)
        if fn is None:
            return None
        return fn(event)

    def _out(self, key: str, **fmt):
        line = self._pick(key, **fmt)
        return (key, line) if line else None

    # ---- heat ---------------------------------------------------------
    def _on_HeatWarning(self, e):   return self._out("HeatWarning")
    def _on_HeatDamage(self, e):    return self._out("HeatDamage")
    def _on_CockpitBreached(self, e): return self._out("CockpitBreached")

    # ---- notable worlds ------------------------------------------------
    def _on_Scan(self, e):
        # Remember the primary so a carrier arrival can describe it.
        if e.get("StarType") and e.get("BodyID") == 0:
            self.ctx["star_type"] = e.get("StarType")
            self.ctx["star_system"] = e.get("StarSystem")
        kind = NOTABLE_CLASS.get(e.get("PlanetClass"))
        if not kind:
            return None
        body = e.get("BodyName") or "that body"
        if body in self.seen_bodies:
            return None                 # AutoScan then Detailed = one body
        self.seen_bodies.add(body)
        if len(self.seen_bodies) > 4000:
            self.seen_bodies = set(list(self.seen_bodies)[-2000:])
        return self._out(kind, body=body)

    # ---- exobiology ----------------------------------------------------
    def _on_CodexEntry(self, e):
        """Do NOT speak here.

        The codex entry lands on the FIRST sample of a new plant, which is
        two samples and several minutes before the set is finished. He wants
        the celebration when it actually completes, so all this does is
        remember that this variant was new. _on_ScanOrganic spends it.

        Non-biology codex entries (geology, astronomy) have no three-sample
        cycle, so they stay silent entirely.
        """
        if not e.get("IsNewEntry"):
            return None
        cat = e.get("Category_Localised") or ""
        if "Biolog" not in cat and "Organic" not in (e.get("SubCategory_Localised") or ""):
            return None
        variant = e.get("Name_Localised") or e.get("Name")
        if not variant:
            return None
        # IsNewEntry means new FOR THIS REGION, not new to the manifest. He
        # holds 26 Stratum Tectonicas; crossing into a new region flags it
        # "new" again. Only celebrate a species he has never banked.
        if known_species() and variant.split(" - ")[0] in known_species():
            return None
        self.ctx.setdefault("codex_pending", {})[variant] = {
            "name": variant,
            "category": e.get("SubCategory_Localised") or cat or "biology",
        }
        return None

    def _on_ScanOrganic(self, e):
        """Only the third scan - ScanType Analyse - is a finished sample."""
        if e.get("ScanType") != "Analyse":
            return None
        species = e.get("Species_Localised") or e.get("Species") or "that one"
        variant = e.get("Variant_Localised") or ""
        # Variant reads "Stratum Tectonicas - Emerald"; the colour is the tail.
        colour = variant.split(" - ")[-1] if " - " in variant else ""

        worth, base = species_value(species)
        val = money(worth)

        pending = self.ctx.setdefault("codex_pending", {})
        info = pending.pop(variant, None)
        if info:
            # New to the codex AND now complete - this is the big one.
            key = "codex_new" if val else "codex_new_plain"
            hit = self._out(key, name=info["name"],
                            category=info["category"], value=val)
            if hit:
                return hit

        # A species with no price in the table still gets announced, just
        # without a figure - never a line reading "worth nothing".
        key = "sample_complete" if val else "sample_complete_plain"
        return self._out(key, species=species,
                         colour=colour or "no colour listed", value=val)

    # ---- surface survey -------------------------------------------------
    def _on_SAASignalsFound(self, e):
        """Probes are in. Say what is living down there.

        Only speaks when there are biologicals - roughly one mapped body in
        nine comes back with none, and "no life here" is not worth hearing.
        """
        bio = sum(sig.get("Count", 0) for sig in (e.get("Signals") or [])
                  if "Biolog" in (sig.get("Type_Localised") or ""))
        if not bio:
            return None
        body = e.get("BodyName") or "that body"
        if body in self.seen_signals:
            return None                 # re-mapping must not re-announce
        self.seen_signals.add(body)
        if len(self.seen_signals) > 4000:
            self.seen_signals = set(list(self.seen_signals)[-2000:])
        return self._out("saa_bio", body=body, n=spoken(bio),
                         s="" if bio == 1 else "s")

    # ---- rank ----------------------------------------------------------
    def _on_Promotion(self, e):
        for key, (label, ladder) in PROMO.items():
            if key not in e:
                continue
            n = e.get(key)
            rank = ladder[n] if isinstance(n, int) and 0 <= n < len(ladder) else str(n)
            return self._out("Promotion", ladder=label, rank=rank)
        return None

    def _on_PowerplayRank(self, e):
        return self._out("PowerplayRank", power=e.get("Power") or "your power",
                         rank=e.get("Rank"))

    # ---- being interfered with -----------------------------------------
    def _on_Interdicted(self, e):
        n = e.get("CombatRank")
        rank = COMBAT_RANK[n] if isinstance(n, int) and 0 <= n < len(COMBAT_RANK) else "unknown"
        return self._out("Interdicted",
                         who=e.get("Interdictor") or "someone unnamed",
                         kind="a commander" if e.get("IsPlayer") else "an N P C",
                         rank=rank)

    def _on_Scanned(self, e):
        kinds = {"Cargo": "cargo", "Crime": "criminal record",
                 "Cabin": "passenger cabin", "Data": "data",
                 "Unknown": "everything"}
        return self._out("Scanned",
                         kind=kinds.get(e.get("ScanType"), readable(e.get("ScanType"))))

    def _on_CrimeVictim(self, e):
        return self._out("CrimeVictim",
                         offender=e.get("Offender") or "somebody",
                         crime=readable(e.get("CrimeType")))

    def _on_CommitCrime(self, e):
        fine, bounty = e.get("Fine"), e.get("Bounty")
        pen = ""
        if fine:
            pen = f" That's a {fine:,} credit fine."
        elif bounty:
            pen = f" There's a {bounty:,} credit bounty on us now."
        return self._out("CommitCrime", crime=readable(e.get("CrimeType")),
                         faction=e.get("Faction") or "the locals", penalty=pen)

    # ---- fighters ------------------------------------------------------
    def _on_LaunchFighter(self, e):
        key = ("fighter_launch_you" if e.get("PlayerControlled")
               else "fighter_launch_nova")
        return self._out(key)

    def _on_CrewLaunchFighter(self, e):
        return self._out("CrewLaunchFighter",
                         crew=e.get("Crew") or "your gunner")

    def _on_DockFighter(self, e):
        return self._out("DockFighter")

    def _on_FighterDestroyed(self, e):
        who = self.ctx.get("fighter_player")
        crew = self.ctx.get("crew") or "your gunner"
        key = ("fighter_lost_you" if who is True else
               "fighter_lost_crew" if who is False else "fighter_lost")
        hit = self._out(key, crew=crew)
        return hit or self._out("fighter_lost", crew=crew)

    # ---- crew ----------------------------------------------------------
    def _on_CrewMemberJoins(self, e):
        return self._out("CrewMemberJoins", crew=e.get("Crew") or "somebody")

    def _on_CrewMemberQuits(self, e):
        return self._out("CrewMemberQuits", crew=e.get("Crew") or "somebody")

    # ---- killing a real person -----------------------------------------
    def _on_PVPKill(self, e):
        n = e.get("CombatRank")
        rank = COMBAT_RANK[n] if isinstance(n, int) and 0 <= n < len(COMBAT_RANK) else "unknown"
        return self._out("PVPKill", victim=e.get("Victim") or "they", rank=rank)

    # ---- the carrier ----------------------------------------------------
    def _on_CarrierJump(self, e):
        """Hold the line briefly, in case the primary gets scanned.

        From his own logs the star scan lands 5-8 seconds after the jump
        when he is actually there to honk it. So we wait, and if it turns
        up we can say something about the star; if it does not, the line
        still reads correctly with that clause empty.
        """
        self.ctx["star_type"] = None
        self.pending = {"key": "CarrierJump",
                        "system": e.get("StarSystem") or "somewhere new",
                        "due": time.monotonic() + self.star_wait}
        return None

    def pending_due(self):
        """Call every poll. Returns a held line once its moment arrives."""
        p = getattr(self, "pending", None)
        if not p:
            return None
        star = star_sentence(self.ctx.get("star_type"))
        if not star and time.monotonic() < p["due"]:
            return None                 # still hoping for the star scan
        self.pending = None
        return self._out("CarrierJump", system=p["system"], star=star)


# --------------------------------------------------------------------------
# live context
# --------------------------------------------------------------------------

def read_status(journal_dir: str) -> dict:
    try:
        with open(os.path.join(journal_dir, "Status.json"),
                  encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return {}


def decode_flags(flags: int) -> list[str]:
    return [name for bit, name in FLAGS if flags & (1 << bit)]


def describe(journal_dir: str, recent: list | None = None) -> dict:
    """A snapshot of right now, for her to read instead of grepping."""
    st = read_status(journal_dir)
    flags = int(st.get("Flags") or 0)
    on = decode_flags(flags)
    pips = st.get("Pips") or []
    fuel = (st.get("Fuel") or {}).get("FuelMain")
    dest = (st.get("Destination") or {}).get("Name")

    if "in srv" in on:
        where = "in the SRV"
    elif "in fighter" in on:
        where = "in a fighter"
    elif "docked" in on:
        where = "docked"
    elif "landed" in on:
        where = "landed"
    elif "supercruise" in on:
        where = "in supercruise"
    elif "fsd jump" in on:
        where = "in witchspace"
    elif st:
        where = "in normal space"
    else:
        where = "unknown - game not running"

    trouble = [f for f in ("IN DANGER", "BEING INTERDICTED", "OVERHEATING",
                           "LOW FUEL") if f in on]

    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "game_running": bool(st),
        "status_age_seconds": _age(os.path.join(journal_dir, "Status.json")),
        "where": where,
        "in_trouble": trouble,
        "flags_on": on,
        "pips": {"sys": pips[0] / 2 if len(pips) > 2 else None,
                 "eng": pips[1] / 2 if len(pips) > 2 else None,
                 "wep": pips[2] / 2 if len(pips) > 2 else None},
        "fuel_main_t": round(fuel, 2) if isinstance(fuel, (int, float)) else None,
        "cargo_t": st.get("Cargo"),
        "balance": st.get("Balance"),
        "legal_state": st.get("LegalState"),
        "destination": dest,
        "recent_events": recent or [],
        "NOTE": ("Live telemetry only. This is NOT the dashboard delta - "
                 "for building or updating the field log, use "
                 "watch_status.json -> new_since_last_dashboard, which is "
                 "the only thing that tracks what has already been posted."),
    }


def _age(path: str):
    try:
        return round(time.time() - os.path.getmtime(path), 1)
    except Exception:
        return None


def write_context(journal_dir: str, recent: list | None = None) -> dict:
    snap = describe(journal_dir, recent)
    try:
        os.makedirs(os.path.dirname(CONTEXT_FILE), exist_ok=True)
        tmp = CONTEXT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, indent=1)
        os.replace(tmp, CONTEXT_FILE)       # atomic; she never sees a half file
    except Exception:
        pass
    return snap


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def _journal_dir() -> str:
    import config
    return (config.EXTRA_DIRS[0] if getattr(config, "EXTRA_DIRS", None)
            else config.WORK_DIR)


def selftest() -> None:
    jd = _journal_dir()
    print(f"journal dir : {jd}")
    print(f"newest log  : {os.path.basename(newest_journal(jd) or '-')}")
    st = read_status(jd)
    if not st:
        print("Status.json : NOT READABLE - is the game running?")
        return
    flags = int(st.get("Flags") or 0)
    print(f"Status.json : {_age(os.path.join(jd,'Status.json'))}s old, "
          f"Flags={flags}")
    print("decoded     : " + (", ".join(decode_flags(flags)) or "(nothing set)"))
    snap = describe(jd)
    print(f"where       : {snap['where']}")
    print(f"pips        : {snap['pips']}")
    print(f"fuel        : {snap['fuel_main_t']} t     dest: {snap['destination']}")
    print(f"trouble     : {snap['in_trouble'] or 'none'}")
    print()
    print("Check that line against what the game is showing. If anything")
    print("disagrees, the flag map in live.py needs correcting.")


def tail(speak: bool = False) -> None:
    import config
    jd = _journal_dir()
    t = Tailer(jd, seed_to_end=True)
    r = Reactor(getattr(config, "REACTIONS", {}),
                getattr(config, "REACTION_COOLDOWNS", {}))
    print(f"tailing {os.path.basename(t.path or '-')} - ctrl-c to stop")
    print("(reactions print here; nothing is spoken in this mode)\n")
    while True:
        for ev in t.poll():
            hit = r.react(ev)
            if hit:
                print(f"  [{hit[0]}]  {hit[1]}")
        write_context(jd)
        time.sleep(1.0)


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    if "--selftest" in sys.argv:
        selftest()
    elif "--tail" in sys.argv:
        try:
            tail()
        except KeyboardInterrupt:
            print("\nstopped")
    else:
        print(__doc__)
