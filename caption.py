"""
caption.py - label Elite screenshots from the journals.

Steam names each screenshot with the local time it was taken. The journals
record, to the second, where you were and what you sampled. Line the two up
and every screenshot can caption itself: species, colour, body, system.

    python caption.py            last 3 days
    python caption.py 7          last 7 days
    python caption.py --dry      list what it would write, change nothing

Originals are never touched. Captioned copies go to a "captioned" subfolder.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
STAMP = re.compile(r"(\d{8})(\d{6})_")          # 20260906204410_1.jpg

# Elite Dangerous runs 1286 years ahead of real time: 2026 -> 3312.
GAME_YEAR_OFFSET = 1286


def game_stamp(when: datetime) -> str:
    """Real date -> in-game date, the way the galaxy map would show it."""
    return (f"{when.day:02d} {when.strftime('%b').upper()} "
            f"{when.year + GAME_YEAR_OFFSET}  \u00b7  {when:%H:%M}")

WANTED = (b'"ScanOrganic"', b'"FSDJump"', b'"Location"', b'"CarrierJump"',
          b'"Touchdown"', b'"ApproachBody"', b'"Disembark"', b'"Scan"',
          b'"SupercruiseExit"')


# Files you have already put on Inara get "_uploaded" added to the name.
# That marker must not make an already-captioned shot look unprocessed.
UPLOADED = "_uploaded"


def base_key(name: str) -> str:
    """Filename with any _uploaded marker removed, for comparing folders."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if stem.endswith(UPLOADED):
        stem = stem[: -len(UPLOADED)]
    return stem + ("." + ext if ext else "")


# ---------------------------------------------------------------- journals

def timeline(journal_dir: str, since: datetime, until: datetime) -> list:
    """Every event that tells us where he was or what he sampled."""
    events = []
    lo = (since - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (until + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        names = sorted(n for n in os.listdir(journal_dir)
                       if n.startswith("Journal.") and n.endswith(".log"))
    except OSError:
        return events

    cutoff = (since - timedelta(days=2)).timestamp()
    for name in names:
        path = os.path.join(journal_dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                continue
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
                ts = e.get("timestamp")
                if not ts or ts < lo or ts > hi:
                    continue
                events.append(e)
    events.sort(key=lambda e: e["timestamp"])
    return events


def state_at(events: list, moment: datetime, window_min: float = 6) -> dict:
    """Where he was, and what he was working, at that instant.

    He photographs a plant as he samples it, so the right match is the organic
    scan NEAREST the shot - which is often a few seconds AFTER it, because the
    first Log fires as he walks up to the thing he just photographed. Taking
    the last completed sample BEFORE the shot instead is what put the previous
    body's species on the caption.
    """
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    system = body = None
    organics = []          # every scan, with the body he was standing on

    for e in events:
        ev = e.get("event")
        if ev in ("FSDJump", "CarrierJump", "Location", "SupercruiseExit"):
            new_system = e.get("StarSystem") or system
            if new_system != system:
                body = None          # old body belongs to the old system
            system = new_system
            if ev == "SupercruiseExit" and e.get("BodyType") == "Planet":
                body = e.get("Body") or body
        elif ev in ("Touchdown", "ApproachBody", "Disembark"):
            body = e.get("Body") or e.get("BodyName") or body
        elif ev == "ScanOrganic":
            var = e.get("Variant_Localised") or ""
            organics.append({
                "t": e["timestamp"],
                "species": e.get("Species_Localised") or e.get("Species"),
                "genus": e.get("Genus_Localised"),
                "variant": var.split(" - ")[-1].strip() if " - " in var else (var or None),
                "body": body, "system": system,
            })
        if e["timestamp"] <= stamp:
            here = {"system": system, "body": body}

    # position as of the shot
    system = here.get("system") if "here" in dir() else system
    body = here.get("body") if "here" in dir() else body

    # nearest organic scan either side of the shot
    best, best_gap = None, None
    for o in organics:
        try:
            when = datetime.strptime(o["t"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc)
        except ValueError:
            continue
        gap = abs((moment - when).total_seconds())
        if gap <= window_min * 60 and (best_gap is None or gap < best_gap):
            best, best_gap = o, gap

    if best is None:
        return {"system": system, "body": body, "species": None,
                "variant": None, "genus": None}

    # the species belongs to the body it was taken on, not wherever he ended up
    return {"system": best["system"] or system, "body": best["body"] or body,
            "species": best["species"], "variant": best["variant"],
            "genus": best["genus"]}


# ---------------------------------------------------------------- images

def shot_time(path: str) -> datetime | None:
    """Steam names the file with LOCAL wall-clock time; journals are UTC.

    We convert using the machine's own timezone rules via mktime, which
    handles daylight saving correctly for the date in question. We do NOT
    derive the offset from the file's mtime: copying or moving a file
    rewrites mtime and would silently shift every caption by hours.
    """
    m = STAMP.search(os.path.basename(path))
    if not m:
        return None
    try:
        local = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    try:
        epoch = time.mktime(local.timetuple())      # local -> epoch, DST aware
        return datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, ValueError):
        return local.replace(tzinfo=timezone.utc)


def load_font(size: int):
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "arial.ttf",
                 "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def caption_lines(info: dict, when: datetime) -> list:
    top = ""
    if info["species"]:
        top = info["species"]
        if info["variant"]:
            top += f"  \u2014  {info['variant']}"
    elif info["genus"]:
        top = info["genus"]

    where = info["body"] or info["system"] or ""
    if info["body"] and info["system"] and not str(info["body"]).startswith(
            str(info["system"])):
        where = f"{info['body']}  \u00b7  {info['system']}"
    bottom = where
    if when:
        bottom = (bottom + "   \u00b7   " if bottom else "") + game_stamp(when)
    return [l for l in (top, bottom) if l]


def draw_caption(src: str, dst: str, lines: list, scale: float = 2.0) -> None:
    im = Image.open(src).convert("RGB")
    W, H = im.size
    big = load_font(max(20, int(H / 30 * scale)))
    small = load_font(max(15, int(H / 44 * scale)))

    pad = int(H / 36 * scale)
    heights, widths = [], []
    tmp = ImageDraw.Draw(im)
    fonts = [big] + [small] * (len(lines) - 1)
    for text, font in zip(lines, fonts):
        box = tmp.textbbox((0, 0), text, font=font)
        widths.append(box[2] - box[0])
        heights.append(box[3] - box[1])

    block = sum(heights) + pad // 2 * (len(lines) - 1)
    x = pad * 2
    y = H - block - pad * 2

    # A dark plate so the text reads over any terrain. At 2x it also lands
    # over the suit status panel, so a fairly opaque plate does double duty:
    # legible caption, and one less bit of HUD showing.
    plate = Image.new("RGBA", (max(widths) + pad * 3, block + pad * 2),
                      (0, 0, 0, 175))
    im.paste(Image.alpha_composite(
        im.crop((x - pad, y - pad, x - pad + plate.width,
                 y - pad + plate.height)).convert("RGBA"), plate).convert("RGB"),
        (x - pad, y - pad))

    draw = ImageDraw.Draw(im)
    cy = y
    for i, (text, font) in enumerate(zip(lines, fonts)):
        colour = (255, 176, 46) if i == 0 else (233, 229, 220)
        draw.text((x + 2, cy + 2), text, font=font, fill=(0, 0, 0))
        draw.text((x, cy), text, font=font, fill=colour)
        cy += heights[i] + pad // 2

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    im.save(dst, quality=92)


# ---------------------------------------------------------------- driver

def run(shots_dir: str, journal_dir: str, days: float = 3, dry: bool = False,
        out_dir: str | None = None, scale: float = 2.0,
        force: bool = False) -> dict:
    """Caption everything recent that has not been captioned already.

    Captioned copies live in their own folder, so comparing the two folders
    tells you at a glance what is still missing.
    """
    out_dir = out_dir or os.path.join(shots_dir, "captioned")
    os.makedirs(out_dir, exist_ok=True)

    every = sorted(f for f in glob.glob(os.path.join(shots_dir, "*.jpg"))
                   + glob.glob(os.path.join(shots_dir, "*.png"))
                   if os.path.isfile(f))
    done = {base_key(os.path.basename(f)) for f in
            glob.glob(os.path.join(out_dir, "*.jpg"))
            + glob.glob(os.path.join(out_dir, "*.png"))}

    cutoff = time.time() - days * 86400
    recent = [f for f in every if os.path.getmtime(f) >= cutoff]
    todo = recent if force else [f for f in recent
                                 if base_key(os.path.basename(f)) not in done]

    report = {"out_dir": out_dir,
              "originals": len(every),
              "captioned_already": len(done),
              "in_window": len(recent),
              "to_do": len(todo),
              "uploaded": sum(1 for f in
                              glob.glob(os.path.join(out_dir, "*.jpg"))
                              + glob.glob(os.path.join(out_dir, "*.png"))
                              if UPLOADED in os.path.basename(f)),
              "missing_overall": sorted(os.path.basename(f) for f in every
                                        if base_key(os.path.basename(f)) not in done),
              "rows": []}
    if not todo:
        return report

    times = [t for t in (shot_time(f) for f in todo) if t]
    if not times:
        return report
    events = timeline(journal_dir, min(times), max(times))

    for path in todo:
        when = shot_time(path)
        if not when:
            continue
        info = state_at(events, when)
        m = STAMP.search(os.path.basename(path))
        try:
            shown = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except Exception:
            shown = when
        lines = caption_lines(info, shown)
        dst = os.path.join(out_dir, os.path.basename(path))
        if not dry:
            try:
                draw_caption(path, dst, lines, scale)
            except Exception as exc:
                report["rows"].append({"file": os.path.basename(path),
                                       "error": str(exc)})
                continue
        report["rows"].append({"file": os.path.basename(path),
                               "taken": when.strftime("%Y-%m-%d %H:%M:%SZ"),
                               "caption": "  |  ".join(lines) or "(nothing matched)",
                               "species": info["species"], "body": info["body"],
                               "system": info["system"],
                               "out": None if dry else dst})
    return report


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    import config
    args = sys.argv[1:]
    dry = "--dry" in args
    force = "--force" in args
    days = next((float(a) for a in args if a.replace(".", "").isdigit()), 3)

    shots = getattr(config, "SCREENSHOT_DIR", "")
    if not shots or not os.path.isdir(shots):
        sys.exit(f"Set SCREENSHOT_DIR in config.py. Not found: {shots!r}")
    out = getattr(config, "CAPTION_OUT", None)
    scale = float(getattr(config, "CAPTION_SCALE", 2.0))

    r = run(shots, config.EXTRA_DIRS[0], days, dry, out, scale, force)

    print(f"\n  source     {r['originals']:>4} screenshots")
    print(f"  captioned  {r['captioned_already']:>4} already done"
          f"   ({r.get('uploaded', 0)} marked uploaded)")
    print(f"  window     {r['in_window']:>4} in the last {days:g} days")
    print(f"  to do      {r['to_do']:>4}{'  (dry run)' if dry else ''}")
    print(f"\n  out: {r['out_dir']}\n")

    for row in r["rows"]:
        print(f"  {row['file']}   {row.get('taken','')}")
        print(f"      {row.get('caption', row.get('error'))}")

    missing = r["missing_overall"]
    if missing:
        print(f"\n  {len(missing)} screenshot(s) still without a caption overall.")
        if len(missing) > len(r["rows"]):
            print(f"  Older than {days:g} days - run with a bigger number to include them,")
            print("  e.g.  caption.bat 30")
    else:
        print("\n  Every screenshot has a caption.")
    print()
