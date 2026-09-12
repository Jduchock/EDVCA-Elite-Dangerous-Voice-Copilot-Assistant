# Mister John's ED Dashboard

A push-to-talk voice assistant — **Nova** — and a live field-log dashboard for
long-range exploration in *Elite Dangerous*. Nova reads the game's own journals,
answers questions out loud, reacts to what happens on the ground, and keeps a
running dashboard of the trip: the exobiology manifest, notable worlds, and the
fleet carrier's fuel, finances and route.

**Live dashboard:** https://jduchock.github.io/Mister-Johns-ED-Dashboard/

---

## What's here

| File | What it does |
|------|--------------|
| `voice_claude.py` | The assistant. Push-to-talk speech in, Claude + ElevenLabs voice out. |
| `config.py` | Nova's personality, her reaction lines, and all the paths/keys wiring. |
| `live.py` | Watches the journal live and Status.json; canned spoken reactions. |
| `carrier.py` | Computes the fleet-carrier section of the dashboard (`--write` updates it). |
| `watch.py` | Digests the current watch — jumps, samples, worlds — into JSON. |
| `exobio.py` | The exobiology manifest: what's banked, what it's worth, rank projections. |
| `caption.py` | Captions Elite screenshots with the species/world from the journal. |
| `make_logbook_docx.js` | Renders the field logbook to a Word document. |
| `publish_prep.py` | Prepares the dashboard HTML for hosting. |
| `docs/` | The published dashboard + log detail (served by GitHub Pages). |
| `reference/` | Static lookup data (species values, exobiology rank thresholds). |

## Running Nova

1. `python -m venv .venv` then activate it
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and add your Anthropic + ElevenLabs keys
4. Double-click `run.bat` (Windows) — hold the push-to-talk key to talk

See `SETUP.md` for the full walkthrough.

## Updating the public dashboard

After Nova rebuilds the dashboard, run `publish-github.ps1` to copy the newest
version into `docs/` and push it. GitHub Pages serves it within a minute.

---

*Built with Nova, aboard the fleet carrier HOME. Log continues.*
