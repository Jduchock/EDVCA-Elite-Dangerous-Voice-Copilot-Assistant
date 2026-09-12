# Talking to Claude on digbox21

Hold **F9**, talk. Let go. She reads your game logs and answers out loud in your
ElevenLabs voice. Tap **F9** while she's talking to cut her off. Tap **F10** to quit.

---

## What this actually is

Four pieces glued together, all on your PC:

| Step | What does it | Cost |
|---|---|---|
| Your voice → text | ElevenLabs **Scribe v2** | $0.22 / hour of audio |
| Text → answer | **Claude Agent SDK** (real Claude Code, with file tools) | your Claude plan, or API tokens |
| Answer → speech | ElevenLabs **Flash v2.5**, your voice | $0.05 / 1,000 characters |
| Push-to-talk key | `keyboard`, a global Windows hook | free |

The Agent SDK is the important choice. It's not a chatbot with a text box — it's
the same engine as Claude Code, so it has `Read`, `Glob` and `Grep` and can go
open your log files itself. You say "why did it crash last night", it greps the
folder and tells you.

A single exchange runs roughly a third of a cent of ElevenLabs credit. Heavy use
is a couple of dollars a month.

---

## Setup

### 1. Prerequisites

- **Python 3.10 or newer.** Check with `python --version`. If it's missing, get
  it from python.org and tick *"Add Python to PATH"* during install.
- **Node.js**, if you don't already have it — the Claude Agent SDK bundles its
  own CLI binary, but some installs still want Node present. `node --version`.

### 2. Unzip and install

Put the folder somewhere sensible, e.g. `C:\Tools\voice-claude`. Open PowerShell
there and run:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell refuses to run the activate script:

```powershell
Set-ExecutionPolicy -Scope Process RemoteSigned
```

(That only affects the current window.)

### 3. Log Claude in

If you already use Claude Code on this machine, you're done — the SDK picks up
that login. If not, either:

```powershell
claude setup-token          # one-year token tied to your Claude subscription
```

or set an API key (billed per token, separate from your subscription):

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
```

Note: if `ANTHROPIC_API_KEY` is set it always wins over the subscription token.
Unset it if you want to use your plan.

### 4. ElevenLabs key and your voice

Get a key at elevenlabs.io → your profile → API Keys. Then either paste it into
`config.py` or set it once in PowerShell:

```powershell
setx ELEVENLABS_API_KEY "sk_..."
```

Close and reopen PowerShell so `setx` takes effect, then:

```powershell
python voice_claude.py --voices
```

That prints every voice on your account with its ID. Copy the ID of the one you
like into `VOICE_ID` in `config.py`.

### 5. Point it at your game logs

Open `config.py` and set:

```python
WORK_DIR = r"C:\Users\John\AppData\LocalLow\WhateverGame"
```

Keep the `r` prefix — it stops Windows backslashes from being read as escape
codes. If your logs live in more than one place, add the others to `EXTRA_DIRS`.

### 6. Check and run

```powershell
python voice_claude.py --check
```

That verifies the key, the voice, the folder, the mic and the speakers, and
tells you exactly what's wrong if anything is. Then:

```powershell
python voice_claude.py
```

Or just double-click **run.bat**, which creates the venv and installs everything
on first run.

---

## Things worth knowing

**The hotkey while gaming.** The `keyboard` library installs a global Windows
hook, so F9 works even when a game has focus — *unless* the game runs as
administrator. Windows won't let a normal-privilege process hook keys for an
elevated one. If your game is elevated, right-click `run.bat` → Run as
administrator and it'll work. Also pick a key the game doesn't already use;
`right ctrl`, `scroll lock` and `insert` are usually safe bets. Change it with
`PTT_KEY` in `config.py`.

**She keeps context.** The whole session is one conversation. "What crashed?"
then "and before that?" works the way you'd expect. Restarting the script starts
fresh.

**She won't touch your files.** `ALLOWED_TOOLS` is read-only — `Read`, `Glob`,
`Grep` — and `PERMISSION_MODE = "dontAsk"` means anything not on that list gets
silently refused rather than stopping to ask you (which you can't answer with
your hands on a mouse). Add `"Bash"`, `"Write"` or `"Edit"` if you decide you
want more.

**Making her sound better.** The big lever is `SYSTEM_PROMPT` in `config.py` —
it's what stops her reading bullet points and file paths at you. Tweak the tone
there, not in the code. For the voice itself: lower `TTS_STABILITY` for more
expression, raise it for consistency, and `TTS_SPEED` takes 0.7 to 1.2.

**If she's too slow.** `eleven_flash_v2_5` is already the fastest model. Most of
the wait is Claude thinking, especially when she's grepping through logs. Asking
narrower questions helps more than any setting.

**If she sounds better but slower.** Try `TTS_MODEL = "eleven_v3_conversational"`
— noticeably more expressive, about 280ms to first audio instead of 75ms.

**Audio device trouble.** `python voice_claude.py --devices` lists everything;
put the number into `INPUT_DEVICE` / `OUTPUT_DEVICE`. If `--check` complains
your speakers won't take 24000 Hz, try `TTS_SAMPLE_RATE = 22050` or `16000`.
Don't use 44100 unless you're on an ElevenLabs Pro plan — it's gated.

**Testing without a mic.** `python voice_claude.py --text` lets you type the
input and still hear the answer. Good for checking the Claude half works before
you fight with audio drivers.

**Transcript.** Everything said both ways gets appended to `conversation.log`
next to the script. Set `TRANSCRIPT_FILE = None` to turn that off.

---

## Files

```
voice_claude.py    the app
config.py          every setting - this is the only file you need to edit
requirements.txt   pip dependencies
run.bat            double-click launcher, sets up the venv on first run
SETUP.md           this
```

---

## Sources

- [ElevenLabs Speech-to-Text API](https://elevenlabs.io/docs/api-reference/speech-to-text/convert)
- [ElevenLabs streaming TTS](https://elevenlabs.io/docs/api-reference/text-to-speech/stream)
- [ElevenLabs models and latency](https://elevenlabs.io/docs/overview/models)
- [ElevenLabs API pricing](https://elevenlabs.io/pricing/api)
- [Claude Agent SDK for Python](https://code.claude.com/docs/en/agent-sdk/python)
- [Claude Agent SDK quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart)
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)
