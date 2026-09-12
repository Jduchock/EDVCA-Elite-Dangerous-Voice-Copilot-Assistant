"""
voice_claude.py - hold a key, talk to Claude, hear her answer.

  Hold Caps Lock   talk. Let go and she answers.
  Tap Caps Lock    while she's talking: shuts her up.
  Tap F10          quit.

Caps Lock is swallowed while this is running, so it never actually toggles
caps - see SUPPRESS_PTT_KEY in config.py.

Everything configurable lives in config.py.

    python voice_claude.py              run it
    python voice_claude.py --voices     list your ElevenLabs voices + IDs
    python voice_claude.py --devices    list microphones and speakers
    python voice_claude.py --mic        record 5s, show the level, play it back
    python voice_claude.py --auth       show which Claude credential is in use, test it
    python voice_claude.py --agents     list ElevenLabs Agents + IDs (this app uses none)
    python voice_claude.py --credits    ElevenLabs quota: used, left, reset date
    python voice_claude.py --exo        exobiology report: banked value, rank, projection
    python voice_claude.py --watch [h]  this watch: jumps, finds, carrier fuel
    python voice_claude.py --webtest [url]  run a real fetch+search, show raw errors
    python voice_claude.py --text       type instead of talk (no mic needed)
    python voice_claude.py --check      verify the setup and exit
"""

from __future__ import annotations

import array
import asyncio
import atexit
import io
import os
import random
import re
import sys
import time
import wave
from datetime import datetime

# Windows needs the Proactor event loop to spawn the Claude subprocess.
# It has been the DEFAULT on Windows since Python 3.8, so on modern versions we
# leave it alone: set_event_loop_policy is deprecated and goes away in 3.16.
# We only force it on older Pythons, where a library may have switched it.
if sys.platform == "win32" and sys.version_info < (3, 14):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def load_env_file(name: str = ".env") -> list[str]:
    """Read KEY=value lines from a .env sitting next to this script.

    These deliberately OVERRIDE anything already in the Windows environment,
    so this project uses its own key even when a different ANTHROPIC_API_KEY
    is set system-wide. That precedence is the whole point: it's what stops a
    stale global key from silently winning.

    Obvious placeholders are ignored, so an unedited .env can't break a setup
    that was otherwise working.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    if not os.path.isfile(path):
        return []
    loaded: list[str] = []
    try:
        # utf-8-sig: Notepad writes a BOM, which would otherwise become part
        # of the first variable's name.
        with open(path, encoding="utf-8-sig") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("set "):      # tolerate pasted `set X=y`
                    line = line[4:].strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not key or not value:
                    continue
                # Only an unedited placeholder, not any key that happens to
                # contain these letters somewhere in the middle.
                if value.lower().startswith("paste") or value.startswith("<"):
                    continue
                os.environ[key] = value
                loaded.append(key)
    except OSError:
        return []
    return loaded


ENV_FROM_FILE = load_env_file()   # must happen before config reads os.environ


def rehome_oauth_token() -> bool:
    """Move a subscription token out of the API-key slot.

    A token starting sk-ant-oat comes from `claude setup-token` and is a
    SUBSCRIPTION credential. Handed to the server as ANTHROPIC_API_KEY it is
    rejected as invalid - a 401 that reads like a dead key or an empty
    balance and sends you hunting for the wrong thing.

    It is never correct for an sk-ant-oat value to sit in ANTHROPIC_API_KEY,
    so rather than fail we just put it where it belongs, for this process
    only. Nothing on the machine is changed.
    """
    token = os.environ.get("ANTHROPIC_API_KEY", "")
    if not token.startswith("sk-ant-oat"):
        return False
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", token)
    return True


OAUTH_REHOMED = rehome_oauth_token()

import config  # noqa: E402


# ---------------------------------------------------------------------------
# small console helpers
# ---------------------------------------------------------------------------

DIM, BOLD, CYAN, YELLOW, RED, GREEN, RESET = (
    "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[32m", "\033[0m"
)


def say_console(tag: str, msg: str, color: str = "") -> None:
    print(f"{color}{tag:<10}{RESET}{msg}", flush=True)


def fatal(msg: str) -> "None":
    print(f"\n{RED}{msg}{RESET}\n", file=sys.stderr)
    sys.exit(1)


_STATUS_LEN = 0
SPINNER = "|/-\\"


def status(msg: str) -> None:
    """Rewrite the current console line in place. Local printing: zero tokens."""
    global _STATUS_LEN
    plain = re.sub(r"\033\[[0-9;]*m", "", msg)
    pad = max(0, _STATUS_LEN - len(plain))
    sys.stdout.write("\r" + msg + " " * pad)
    sys.stdout.flush()
    _STATUS_LEN = len(plain)


def clear_status() -> None:
    global _STATUS_LEN
    if _STATUS_LEN:
        sys.stdout.write("\r" + " " * _STATUS_LEN + "\r")
        sys.stdout.flush()
        _STATUS_LEN = 0


def human(n) -> str:
    if n is None:
        return "?"
    n = float(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return f"{n:.0f}"


# Running totals. The numbers ride along with every reply, so reading them
# costs nothing extra.
SESSION = {"turns": 0, "in": 0, "out": 0, "cache_read": 0, "cache_write": 0,
           "cost": 0.0, "tools": 0}


def note_usage(usage, cost) -> dict:
    if usage is None:
        usage = {}
    if not isinstance(usage, dict):
        usage = {k: getattr(usage, k, None) for k in
                 ("input_tokens", "output_tokens",
                  "cache_read_input_tokens", "cache_creation_input_tokens")}
    got = {"in": usage.get("input_tokens") or 0,
           "out": usage.get("output_tokens") or 0,
           "cache_read": usage.get("cache_read_input_tokens") or 0,
           "cache_write": usage.get("cache_creation_input_tokens") or 0,
           "cost": float(cost) if isinstance(cost, (int, float)) else 0.0}
    for k, v in got.items():
        SESSION[k] += v
    return got


def log_transcript(who: str, text: str) -> None:
    path = getattr(config, "TRANSCRIPT_FILE", None)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {who}: {text}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# imports that can fail with a useful message
# ---------------------------------------------------------------------------

try:
    import sounddevice as sd
except Exception as exc:  # pragma: no cover
    fatal(f"Could not load sounddevice ({exc}).\nRun:  pip install sounddevice")

try:
    import keyboard
except Exception as exc:  # pragma: no cover
    fatal(f"Could not load the keyboard library ({exc}).\nRun:  pip install keyboard")

try:
    from elevenlabs.client import ElevenLabs
    from elevenlabs import VoiceSettings
except Exception as exc:  # pragma: no cover
    fatal(f"Could not load the elevenlabs library ({exc}).\nRun:  pip install elevenlabs")

try:
    from claude_agent_sdk import (  # noqa: F401
        ClaudeSDKClient,
        ClaudeAgentOptions,
    )
except Exception as exc:  # pragma: no cover
    fatal(f"Could not load claude-agent-sdk ({exc}).\nRun:  pip install claude-agent-sdk")


# ---------------------------------------------------------------------------
# the push-to-talk key
# ---------------------------------------------------------------------------

def caps_lock_is_on() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)
    except Exception:
        return False


class PushToTalk:
    """Tracks whether the talk key is held down.

    Toggle keys - Caps Lock, Scroll Lock, Num Lock - can't just be polled:
    holding one down flips its state, so you'd end up SHOUTING AT EVERYONE
    every time you spoke. So when SUPPRESS_PTT_KEY is on we install a
    low-level hook that eats the key outright. Windows never sees it, your
    game never sees it, and the caps light never comes on. We track the
    up/down state ourselves instead.
    """

    def __init__(self, key: str, suppress: bool):
        self.key = key
        # A scan code identifies the physical key. A name does not: the
        # keyboard library calls numpad minus "-", the same as the main-row
        # hyphen, so binding by name would fire the mic on every dash.
        self.code = getattr(config, "PTT_SCANCODE", None) or key
        self.suppress = suppress
        self._down = False
        self._handle = None
        self._caps_was_on = caps_lock_is_on()

    def start(self) -> None:
        if not self.suppress:
            if "lock" in self.key.lower():
                say_console("warning",
                            f"{self.key} is a toggle key and SUPPRESS_PTT_KEY is "
                            f"off - talking will flip it", YELLOW)
            return
        self._handle = keyboard.hook_key(self.code, self._on_event, suppress=True)

    def _on_event(self, event) -> None:
        if event.event_type == keyboard.KEY_DOWN:
            self._down = True
        elif event.event_type == keyboard.KEY_UP:
            self._down = False

    @property
    def down(self) -> bool:
        return self._down if self.suppress else keyboard.is_pressed(self.code)

    def wait_for_release(self) -> None:
        while self.down:
            time.sleep(0.02)

    def stop(self) -> None:
        if self._handle is not None:
            try:
                keyboard.unhook(self._handle)
            except Exception:
                pass
            self._handle = None
        # Belt and braces: if caps somehow got flipped, flip it back.
        if self.key.lower() == "caps lock" and caps_lock_is_on() != self._caps_was_on:
            try:
                keyboard.send("caps lock")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# microphone
# ---------------------------------------------------------------------------

def record_while_held(ptt: "PushToTalk") -> bytes:
    """Capture 16-bit mono PCM for as long as the talk key is held down."""
    frames: list[bytes] = []

    def callback(indata, _frames, _time, status):
        if status:
            pass  # overflows are normal and harmless here
        frames.append(bytes(indata))

    stream = sd.RawInputStream(
        samplerate=config.MIC_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        device=config.INPUT_DEVICE,
        callback=callback,
        blocksize=1024,
    )
    started = time.monotonic()
    with stream:
        while ptt.down:
            if time.monotonic() - started > config.MAX_RECORDING_SECONDS:
                say_console("", "(hit the recording length limit)", YELLOW)
                break
            time.sleep(0.02)
    return b"".join(frames)


def pcm_to_wav(pcm: bytes, samplerate: int) -> io.BytesIO:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(samplerate)
        wav.writeframes(pcm)
    buf.seek(0)
    buf.name = "speech.wav"  # the SDK sniffs the extension
    return buf


def seconds_of(pcm: bytes, samplerate: int) -> float:
    return len(pcm) / 2 / samplerate


def audio_level(pcm: bytes) -> tuple[float, float]:
    """(peak, rms) of 16-bit PCM, both 0.0 to 1.0. Silence reads near zero."""
    if len(pcm) < 2:
        return 0.0, 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0.0, 0.0
    peak = max(max(samples), -min(samples)) / 32768.0
    step = max(1, len(samples) // 20000)          # subsample; this is only a meter
    window = samples[::step]
    rms = (sum(s * s for s in window) / len(window)) ** 0.5 / 32768.0
    return peak, rms


def level_bar(peak: float, width: int = 24) -> str:
    filled = min(width, int(peak * width * 2))
    return "#" * filled + "." * (width - filled)


# ---------------------------------------------------------------------------
# ElevenLabs: speech in, speech out
# ---------------------------------------------------------------------------

class Voice:
    def __init__(self, api_key: str, voice_id: str):
        self.client = ElevenLabs(api_key=api_key)
        self.voice_id = voice_id
        self.rate = config.TTS_SAMPLE_RATE
        self.first_audio_after: float | None = None
        self._settings = VoiceSettings(
            stability=config.TTS_STABILITY,
            similarity_boost=config.TTS_SIMILARITY,
            style=config.TTS_STYLE,
            use_speaker_boost=True,
            speed=config.TTS_SPEED,
        )

    # -- speech to text ----------------------------------------------------
    def transcribe(self, pcm: bytes) -> str:
        base = dict(
            model_id=config.STT_MODEL,
            tag_audio_events=False,   # no "(laughter)" junk in the text
            diarize=False,
        )
        if config.STT_LANGUAGE:
            base["language_code"] = config.STT_LANGUAGE

        # Sending raw 16 kHz mono PCM is documented as lower latency than an
        # encoded waveform - the server skips decoding. Fall back to a real
        # WAV, then to the older model, if anything objects.
        attempts = [
            dict(base, file=io.BytesIO(pcm), file_format="pcm_s16le_16"),
            dict(base, file=pcm_to_wav(pcm, config.MIC_SAMPLE_RATE)),
        ]
        if config.STT_MODEL != "scribe_v1":
            attempts.append(dict(base, file=pcm_to_wav(pcm, config.MIC_SAMPLE_RATE),
                                 model_id="scribe_v1"))

        last: Exception | None = None
        for kwargs in attempts:
            try:
                result = self.client.speech_to_text.convert(**kwargs)
                return (getattr(result, "text", "") or "").strip()
            except Exception as exc:
                last = exc
        raise last if last else RuntimeError("transcription failed")

    # -- text to speech ----------------------------------------------------
    def speak(self, text: str, interrupt: "PushToTalk | None") -> bool:
        """Stream audio to the speakers. Returns False if it was cut short."""
        if not text.strip():
            return True

        stream = sd.RawOutputStream(
            samplerate=self.rate,
            channels=1,
            dtype="int16",
            device=config.OUTPUT_DEVICE,
        )
        stream.start()
        leftover = b""
        finished = True
        self.first_audio_after = None
        began = time.monotonic()
        try:
            audio = self.client.text_to_speech.stream(
                voice_id=self.voice_id,
                text=text,
                model_id=config.TTS_MODEL,
                output_format=f"pcm_{self.rate}",
                voice_settings=self._settings,
            )
            for chunk in audio:
                if not chunk:
                    continue
                if self.first_audio_after is None:
                    self.first_audio_after = time.monotonic() - began
                data = leftover + chunk
                # writes must be a whole number of 16-bit samples
                usable, leftover = data[: len(data) - len(data) % 2], data[len(data) - len(data) % 2:]
                for i in range(0, len(usable), 4096):
                    if interrupt is not None and interrupt.down:
                        finished = False
                        break
                    stream.write(usable[i:i + 4096])
                if not finished:
                    break
        finally:
            try:
                if finished:
                    stream.stop()   # let the tail play out
                else:
                    stream.abort()  # cut immediately
            finally:
                stream.close()
        return finished

    def list_voices(self) -> list[tuple[str, str, str]]:
        result = self.client.voices.get_all()
        out = []
        for v in getattr(result, "voices", []):
            out.append((getattr(v, "name", "?"),
                        getattr(v, "voice_id", "?"),
                        getattr(v, "category", "")))
        return out


# ---------------------------------------------------------------------------
# making Claude's text sound like speech instead of a README
# ---------------------------------------------------------------------------

def clean_for_speech(text: str) -> str:
    t = text
    t = re.sub(r"```.*?```", " ", t, flags=re.S)              # code fences
    t = re.sub(r"`([^`]*)`", r"\1", t)                        # inline code
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)       # headings
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)        # bold
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)
    # bullets and numbered lists become sentences, so they don't run together
    lines = []
    for line in t.split("\n"):
        stripped = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
        if stripped != line and stripped.strip() and stripped.rstrip()[-1] not in ".!?:;":
            stripped = stripped.rstrip() + "."
        lines.append(stripped)
    t = "\n".join(lines)

    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)            # links
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"^\s*[|>-]{3,}\s*$", " ", t, flags=re.M)      # rules / tables
    t = t.replace("*", "").replace("#", "")
    t = re.sub(r"\n{2,}", ". ", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\.\s*(?=\.)", "", t)                         # collapse ". ."
    t = re.sub(r"([:;,])\s*\.", r"\1", t)                     # and ":."
    return t.strip()


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def resolve_tools() -> list[str]:
    """Turn ALLOWED_TOOLS + WEB_ALLOWLIST into the exact rule list the SDK gets.

    A bare "WebFetch" permits every domain. With an allowlist configured we
    replace it with one WebFetch(domain:X) rule per site, which is the
    documented way to scope that tool.
    """
    tools = [str(t) for t in config.ALLOWED_TOOLS]
    sites = [str(d).strip().lower() for d in getattr(config, "WEB_ALLOWLIST", [])
             if str(d).strip()]
    if not sites:
        return tools

    if "WebFetch" in tools:
        tools.remove("WebFetch")
        tools.extend(f"WebFetch(domain:{d})" for d in sites)
    if "WebSearch" in tools and not getattr(config, "WEB_SEARCH_WITH_ALLOWLIST", True):
        tools.remove("WebSearch")
    return tools


# Windows caps a whole command line at 32,767 characters, and the SDK passes
# --system-prompt as one argument. Her prompt is past 30k, so passing it
# inline dies with WinError 206 before the CLI even starts. Write it to a
# file and hand over --system-prompt-file instead: then the prompt can be any
# length and nothing here has to be rationed.
PROMPT_FILE = "system_prompt.txt"
CMDLINE_SAFE = 30000


def system_prompt_arg():
    """Return the system prompt as the SDK wants it - inline, or via a file."""
    text = config.SYSTEM_PROMPT
    if len(text) < CMDLINE_SAFE:
        return text
    path = os.path.abspath(PROMPT_FILE)
    try:
        with io.open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return {"type": "file", "path": path}
    except Exception as exc:
        # Better a truncated prompt than a dead app - say so loudly.
        say_console("warning", f"couldn't write {PROMPT_FILE} ({exc}) - "
                               f"trimming the prompt to fit the command line",
                    YELLOW)
        return text[:CMDLINE_SAFE]


def build_options() -> ClaudeAgentOptions:
    kwargs: dict = {
        "cwd": config.WORK_DIR,
        "allowed_tools": resolve_tools(),
        "system_prompt": system_prompt_arg(),
        "max_turns": config.MAX_TURNS,
        "permission_mode": config.PERMISSION_MODE,
    }
    if config.MODEL:
        kwargs["model"] = config.MODEL
    if config.EXTRA_DIRS:
        kwargs["add_dirs"] = list(config.EXTRA_DIRS)

    # SDK builds differ slightly in which fields they accept. If one is
    # rejected, drop it and carry on rather than dying on startup.
    optional = ("add_dirs", "permission_mode", "max_turns", "model", "system_prompt")
    for _ in range(len(optional)):
        try:
            return ClaudeAgentOptions(**kwargs)
        except TypeError as exc:
            culprit = next((k for k in optional if k in kwargs and k in str(exc)), None)
            if culprit is None:
                raise
            say_console("", f"this SDK build doesn't take {culprit} - skipping it", YELLOW)
            kwargs.pop(culprit)
    return ClaudeAgentOptions(**kwargs)


def describe_tool(name: str, params: dict) -> str:
    """One readable line per tool call.

    Shell commands are NOT paths - running basename() over them chopped
    everything before the last slash, which is why bash lines used to start
    mid-string. Commands get shown from the front and trimmed at the end.
    """
    low = name.lower()

    if low == "bash":
        cmd = " ".join(str(params.get("command", "")).split())
        # a leading `cd "long/path" &&` tells you nothing useful
        cmd = re.sub(r'^cd\s+("[^"]*"|\S+)\s*&&\s*', "", cmd)
        return f"bash  {cmd[:96] + ('...' if len(cmd) > 96 else '')}"

    if low in ("webfetch", "websearch"):
        target = str(params.get("url") or params.get("query") or "")
        if target.startswith("http"):
            target = re.sub(r"^https?://", "", target).split("/")[0]
        return f"{low}  {target[:72]}"

    # for grep the PATTERN is the interesting half, not the folder
    fields = (("pattern", "file_path", "path", "query") if low == "grep"
              else ("file_path", "path", "pattern", "query"))
    target = ""
    for field in fields:
        if params.get(field):
            target = str(params[field])
            break
    if low in ("read", "write", "edit", "glob") and ("/" in target or "\\" in target):
        target = os.path.basename(target.replace("\\", "/"))
    if len(target) > 60:
        target = target[:57] + "..."

    extra = ""
    if low == "grep":
        where = params.get("path") or params.get("glob")
        if where:
            extra = "  in " + os.path.basename(str(where).replace("\\", "/"))
    return f"{low}  {target}{extra}".strip()


async def ask_claude(client: ClaudeSDKClient, prompt: str,
                     on_tool=None, on_result=None) -> tuple[str, dict]:
    """Returns (answer, stats) - stats carry tool count and token usage."""
    await client.query(prompt)
    parts: list[str] = []
    stats = {"tools": 0, "in": 0, "out": 0, "cache_read": 0,
             "cache_write": 0, "cost": 0.0}

    async for message in client.receive_response():
        for block in getattr(message, "content", None) or []:
            kind = type(block).__name__
            if kind == "TextBlock":
                parts.append(getattr(block, "text", ""))
            elif kind == "ToolUseBlock":
                stats["tools"] += 1
                if on_tool is not None:
                    try:
                        on_tool(describe_tool(getattr(block, "name", "?"),
                                              getattr(block, "input", None) or {}))
                    except Exception:
                        pass
            elif kind == "ToolResultBlock" and on_result is not None:
                body = getattr(block, "content", "")
                if isinstance(body, list):
                    body = " ".join(str(getattr(x, "text", x)) for x in body)
                try:
                    on_result(str(body), bool(getattr(block, "is_error", False)))
                except Exception:
                    pass

        if type(message).__name__ == "ResultMessage":
            stats.update(note_usage(getattr(message, "usage", None),
                                    getattr(message, "total_cost_usd", None)))

    SESSION["turns"] += 1
    SESSION["tools"] += stats["tools"]
    return "\n".join(p for p in parts if p).strip(), stats


async def thinking_ticker(started: float, voice, announce_after: float,
                          live: dict) -> None:
    """Live single-line status, plus the occasional spoken filler."""
    fillers = [str(f) for f in getattr(config, "FILLER_MESSAGES", []) if str(f).strip()]
    if not fillers:
        fillers = ["Working on it."]
    if getattr(config, "FILLER_RANDOMIZE", True):
        fillers = random.sample(fillers, len(fillers))

    cap = max(0, int(getattr(config, "MAX_FILLERS_PER_TURN", 2)))
    gap = float(getattr(config, "FILLER_INTERVAL_SECONDS", 25))
    spoken, due_at, tick = 0, announce_after, 0

    while True:
        await asyncio.sleep(0.25)
        tick += 1
        elapsed = time.monotonic() - started
        doing = live.get("doing") or "thinking"
        n = live.get("tools", 0)
        line = f"   {SPINNER[tick % len(SPINNER)]} {elapsed:5.1f}s  {doing}"
        if n:
            line += f"   [{n} lookup{'s' * (n != 1)}]"
        status(f"{DIM}{line}{RESET}")

        if (voice is not None and announce_after and spoken < cap
                and elapsed >= due_at):
            line_out = fillers[spoken % len(fillers)]
            spoken += 1
            due_at = elapsed + gap
            try:
                await asyncio.to_thread(voice.speak, line_out, None)
            except Exception:
                pass


def print_cost(stats: dict) -> None:
    """What the turn cost, and the running total, read off the reply itself."""
    fresh, cached, written = stats["in"], stats["cache_read"], stats["cache_write"]
    total_in = fresh + cached + written
    if not (total_in or stats["out"]):
        return                      # subscription runs may report nothing

    bits = [f"in {human(total_in)}"]
    if cached:
        bits.append(f"{human(cached)} cached ({cached/total_in:.0%})")
    bits.append(f"out {human(stats['out'])}")
    line = "  ·  ".join(bits)
    if stats["cost"]:
        line += f"   ${stats['cost']:.4f}"
    say_console("tokens", line, DIM)

    grand = SESSION["in"] + SESSION["cache_read"] + SESSION["cache_write"]
    run = (f"session {SESSION['turns']} turn{'s' * (SESSION['turns'] != 1)}"
           f"  ·  in {human(grand)}  ·  out {human(SESSION['out'])}"
           f"  ·  {SESSION['tools']} lookups")
    if SESSION["cost"]:
        run += f"  ·  ${SESSION['cost']:.4f}"
    say_console("", f"   {run}", DIM)


def mode_key() -> None:
    """Print the name and scan code of whichever key is pressed.

    Names are ambiguous - the keyboard library calls numpad minus and the
    main-row hyphen both "-". Scan codes are not. Whatever this prints as
    the code is what belongs in PTT_SCANCODE.
    """
    print()
    say_console("keys", "press any key to see how it is identified, "
                        "ESC to stop", CYAN)
    print()
    while True:
        ev = keyboard.read_event()
        if ev.event_type != keyboard.KEY_DOWN:
            continue
        if ev.name == "esc":
            break
        pad = " (numpad)" if ev.is_keypad else ""
        say_console("", f"name {BOLD}{ev.name}{RESET}{pad}   "
                        f"scan code {BOLD}{ev.scan_code}{RESET}")
    print()
    say_console("", f"put the scan code in PTT_SCANCODE in config.py", DIM)


def mode_devices() -> None:
    print(sd.query_devices())
    print(f"\nDefault input / output: {sd.default.device}")


def mask(secret: str) -> str:
    if len(secret) <= 14:
        return secret[:4] + "..." + secret[-2:]
    return f"{secret[:12]}...{secret[-4:]}  ({len(secret)} chars)"


def mode_auth() -> None:
    """Show where the Anthropic credential comes from, then actually test it."""
    import json
    import urllib.error
    import urllib.request

    print()
    key = os.environ.get("ANTHROPIC_API_KEY")
    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    claude_dir = os.path.expanduser("~/.claude")

    src = " (from .env)" if "ANTHROPIC_API_KEY" in ENV_FROM_FILE else " (from Windows)"
    say_console("key", "ANTHROPIC_API_KEY       " +
                (mask(key) + src if key else "(not set)"), CYAN if key else DIM)
    say_console("key", "CLAUDE_CODE_OAUTH_TOKEN " +
                (mask(oauth) if oauth else "(not set)"), CYAN if oauth else DIM)
    if OAUTH_REHOMED:
        say_console("fixed", "found a subscription token (sk-ant-oat...) sitting in "
                             "ANTHROPIC_API_KEY", GREEN)
        say_console("", "  moved it to CLAUDE_CODE_OAUTH_TOKEN for this run. Nothing on", DIM)
        say_console("", "  your machine was changed - to make it permanent, clear the", DIM)
        say_console("", "  stray variable:", DIM)
        say_console("", '  [Environment]::SetEnvironmentVariable('
                        '"ANTHROPIC_API_KEY", $null, "User")', DIM)
        print()

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if ENV_FROM_FILE:
        say_console("file", f".env supplied: {', '.join(ENV_FROM_FILE)}", GREEN)
    elif os.path.isfile(env_path):
        say_console("file", ".env exists but every line in it is commented out "
                            "or empty - nothing was loaded from it", YELLOW)
    else:
        say_console("file", "no .env file in this folder", DIM)
    say_console("disk", f"{claude_dir}  " +
                ("exists - Claude Code is logged in here"
                 if os.path.isdir(claude_dir) else "not found - no subscription login"),
                GREEN if os.path.isdir(claude_dir) else DIM)
    print()

    if key and (oauth or os.path.isdir(claude_dir)):
        say_console("note", "both are present - the API key WINS. Clear it with "
                            "setx ANTHROPIC_API_KEY \"\" to use the subscription.",
                    YELLOW)
        print()

    if not key:
        say_console("", "No API key set, so nothing to test. If Claude Code is "
                        "logged in above, you're running on your subscription.", DIM)
        print()
        return

    if key.startswith("sk-ant-oat"):
        print()
        say_console("WRONG SLOT", "that is an OAuth token (sk-ant-oat...), not an API "
                                  "key. It is in the wrong variable.", RED)
        say_console("", "  'claude setup-token' produces this, and it belongs in", DIM)
        say_console("", "  CLAUDE_CODE_OAUTH_TOKEN. Sent as ANTHROPIC_API_KEY the", DIM)
        say_console("", "  server rejects it as invalid - which is your 401.", DIM)
        say_console("fix", "delete the variable and use the login already on disk:", GREEN)
        say_console("", '  [Environment]::SetEnvironmentVariable('
                        '"ANTHROPIC_API_KEY", $null, "User")', GREEN)
        say_console("", "  then open a NEW PowerShell window.", DIM)
        print()
        return

    say_console("...", "asking api.anthropic.com whether this key works", DIM)
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={"x-api-key": key,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    status, payload = None, ""
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, payload = response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read().decode(errors="replace")
    except Exception as exc:
        print()
        say_console("PROBLEM", f"couldn't reach the API at all: {exc}", RED)
        print()
        return

    detail = payload
    try:
        parsed = json.loads(payload)
        err = parsed.get("error") or {}
        detail = err.get("message") or payload
        kind = err.get("type", "")
    except Exception:
        kind = ""

    print()
    low = f"{kind} {detail}".lower()
    if status == 200:
        say_console("GOOD", "key is valid and has credit. This is not your problem.", GREEN)
    elif "credit balance" in low or "billing" in low:
        say_console("OUT OF CREDIT", "the key is valid, but the balance is empty.", RED)
        say_console("", "  Top up at console.anthropic.com > Plans & Billing,", DIM)
        say_console("", "  or clear the key and use your Claude subscription instead.", DIM)
    elif status == 401 or "authentication" in low:
        say_console("BAD KEY", "the API rejected it - wrong, revoked, or truncated.", RED)
        say_console("", "  A stray quote in setx is the usual cause. Check the length "
                        "above.", DIM)
    elif status == 404 or "model" in low:
        say_console("KEY OK", "authentication passed (the test model name is just "
                              "out of date), so the key itself is fine.", GREEN)
    elif status == 429:
        say_console("KEY OK", "valid, but currently rate limited.", YELLOW)
    else:
        say_console("HTTP " + str(status), detail[:300], YELLOW)
    print()


def refresh_exo(verbose: bool = False):
    """Recompute the exobiology status file the assistant reads.

    Cheap and deterministic - real Python doing arithmetic over the journals,
    rather than asking a language model to add up thousands of numbers by
    grepping. Failures here are never fatal: she just falls back to reading
    the journals herself.
    """
    try:
        import exobio
    except Exception as exc:
        if verbose:
            say_console("", f"exobio unavailable: {exc}", YELLOW)
        return None
    target = config.EXTRA_DIRS[0] if config.EXTRA_DIRS else config.WORK_DIR
    try:
        return exobio.write_status(target)
    except Exception as exc:
        if verbose:
            say_console("", f"exobio failed: {exc}", YELLOW)
        return None


def refresh_carrier(verbose: bool = False):
    """Recompute the carrier status file. Same bargain as the other two -
    real Python over the journals, so she never has to grep for tritium or
    add up a route in her head."""
    try:
        import carrier
    except Exception as exc:
        if verbose:
            say_console("", f"carrier unavailable: {exc}", YELLOW)
        return None
    target = config.EXTRA_DIRS[0] if config.EXTRA_DIRS else config.WORK_DIR
    try:
        return carrier.write_status(target)
    except Exception as exc:
        if verbose:
            say_console("", f"carrier failed: {exc}", YELLOW)
        return None


def mode_carrier() -> None:
    import carrier
    target = config.EXTRA_DIRS[0] if config.EXTRA_DIRS else config.WORK_DIR
    d = carrier.write_status(target)
    print()
    print(carrier.summary(d))
    print()
    say_console("", "wrote reference/carrier_status.json", DIM)


def refresh_watch(verbose: bool = False):
    """Recompute the watch digest. Pure local Python - fast and free."""
    try:
        import watch
    except Exception as exc:
        if verbose:
            say_console("", f"watch unavailable: {exc}", YELLOW)
        return None
    target = config.EXTRA_DIRS[0] if config.EXTRA_DIRS else config.WORK_DIR
    try:
        return watch.write_status(target, getattr(config, "WATCH_HOURS", 14),
                                  getattr(config, "FIELD_LOG", None))
    except Exception as exc:
        if verbose:
            say_console("", f"watch failed: {exc}", YELLOW)
        return None


def mode_watch() -> None:
    import watch
    hours = getattr(config, "WATCH_HOURS", 14)
    for a in sys.argv[1:]:
        if a.replace(".", "").isdigit():
            hours = float(a)
    result = refresh_watch(verbose=True)
    if result is None:
        fatal("Could not read the journals. Check EXTRA_DIRS in config.py.")
    if hours != getattr(config, "WATCH_HOURS", 14):
        result = watch.write_status(config.EXTRA_DIRS[0], hours,
                                    getattr(config, "FIELD_LOG", None))
    print(watch.render(result))


def mode_exo() -> None:
    import exobio
    result = refresh_exo(verbose=True)
    if result is None:
        fatal("Could not analyse the journals. Check EXTRA_DIRS in config.py.")
    print(exobio.render(result))


async def _webtest(url: str) -> None:
    """Run one real query through her and print EVERY block, verbatim.

    Existing modes only show what she chose to say. This shows the raw tool
    calls and, crucially, the raw tool RESULTS - so a 403, a permission
    refusal and a network error stop looking identical.
    """
    options = build_options()
    prompt = (
        f"Use the WebFetch tool on exactly this URL: {url}\n"
        "Then use the WebSearch tool for: Elite Dangerous exobiology Stratum Tectonicas value\n"
        "Report verbatim what each tool returned, including any error text or "
        "status code. Do not summarise, do not guess why, and do not work "
        "around a failure - I need the raw outcome of both tools."
    )
    say_console("", f"asking her to fetch {url} and run a search...", DIM)
    print()
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            kind = type(message).__name__
            for block in getattr(message, "content", None) or []:
                name = type(block).__name__
                if name == "TextBlock":
                    print(f"{GREEN}[say]{RESET} {getattr(block,'text','')}")
                elif name == "ToolUseBlock":
                    params = getattr(block, "input", None) or {}
                    print(f"{CYAN}[call]{RESET} {getattr(block,'name','?')}  {params}")
                elif name == "ToolResultBlock":
                    body = getattr(block, "content", "")
                    if isinstance(body, list):
                        body = " ".join(str(getattr(x, "text", x)) for x in body)
                    body = str(body)
                    bad = getattr(block, "is_error", False)
                    tag = f"{RED}[ERROR]{RESET}" if bad else f"{YELLOW}[result]{RESET}"
                    print(f"{tag} {body[:1500]}")
                    print()
                else:
                    print(f"{DIM}[{name}]{RESET} {str(block)[:300]}")
            if kind == "ResultMessage":
                print(f"{DIM}[done] subtype={getattr(message,'subtype','?')}{RESET}")


def mode_webtest(args: list[str]) -> None:
    url = next((a for a in args if a.startswith("http")),
               "https://www.edsm.net/api-v1/system?systemName=Colonia&showCoordinates=1")
    print()
    say_console("tools", ", ".join(r for r in resolve_tools() if "Web" in r) or "(no web tools!)",
                CYAN)
    print()
    try:
        asyncio.run(_webtest(url))
    except Exception as exc:
        say_console("failed", str(exc), RED)
    print()


def mode_tools() -> None:
    """Print the exact permission list handed to the SDK."""
    tools = resolve_tools()
    sites = [d for d in getattr(config, "WEB_ALLOWLIST", []) if str(d).strip()]

    print(f"\n  {BOLD}Tools she will be given{RESET}\n")
    for rule in tools:
        print(f"    {rule}")

    print(f"\n  {BOLD}Web access{RESET}\n")
    fetch = [r for r in tools if r.startswith("WebFetch")]
    if not fetch:
        print(f"    {RED}WebFetch is OFF{RESET} - she cannot open any web page.")
        print(f"    {DIM}Add \"WebFetch\" to ALLOWED_TOOLS in config.py.{RESET}")
    elif sites:
        print(f"    WebFetch is ON, limited to {len(sites)} domains:")
        for d in sites:
            print(f"      - {d}")
        print(f"    {DIM}Anything else is refused. Empty WEB_ALLOWLIST to allow all.{RESET}")
    else:
        print("    WebFetch is ON for every domain (no allowlist set).")

    if any(r == "WebSearch" for r in tools):
        print("    WebSearch is ON" + (" (not limited by the allowlist - it can't be)"
                                       if sites else ""))
    else:
        print("    WebSearch is OFF")

    print(f"\n  {BOLD}Permission mode{RESET}  {config.PERMISSION_MODE}")
    if config.PERMISSION_MODE == "dontAsk":
        print(f"    {DIM}Anything not listed above is silently refused - she is never{RESET}")
        print(f"    {DIM}prompted. If she claims a site is paywalled or unreachable,{RESET}")
        print(f"    {DIM}check this list first: a refusal looks the same to her.{RESET}")
    print()


def mode_credits() -> None:
    """How much ElevenLabs quota is left, and when it resets."""
    import json
    import urllib.error
    import urllib.request
    from datetime import datetime as _dt

    key = config.ELEVENLABS_API_KEY
    if not key:
        fatal("No ELEVENLABS_API_KEY set. Put it in .env or set it with setx.")

    def get(path):
        req = urllib.request.Request("https://api.elevenlabs.io" + path,
                                     headers={"xi-api-key": key})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    print()
    say_console("key", "ELEVENLABS_API_KEY  " + mask(key), CYAN)
    say_console("...", "asking api.elevenlabs.io", DIM)
    try:
        sub = get("/v1/user/subscription")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        print()
        if "missing_permission" in body or "user_read" in body:
            say_console("SCOPED KEY", "your key works for speech, it just is not "
                                      "allowed to read usage.", YELLOW)
            say_console("", "  It is missing the 'user_read' permission. This does NOT", DIM)
            say_console("", "  affect Nova talking - only this quota check.", DIM)
            say_console("", "  Enable user_read on the key at elevenlabs.io, or just", DIM)
            say_console("", "  read your balance off their dashboard.", DIM)
        else:
            say_console("HTTP " + str(exc.code), body[:300], RED)
            if exc.code in (401, 403):
                say_console("", "  auth failure - the key is wrong or revoked", DIM)
        print()
        return
    except Exception as exc:
        fatal(f"Couldn't reach ElevenLabs: {exc}")

    used = sub.get("character_count")
    limit = sub.get("character_limit")
    tier = sub.get("tier") or sub.get("subscription", {}).get("tier") or "?"
    reset = sub.get("next_character_count_reset_unix")

    print()
    say_console("plan", str(tier), BOLD)
    if isinstance(used, int) and isinstance(limit, int) and limit > 0:
        left = limit - used
        pct = used / limit
        bar = "#" * int(pct * 30) + "." * (30 - int(pct * 30))
        say_console("used", f"[{bar}] {pct:.0%}", DIM)
        say_console("", f"{used:,} of {limit:,} characters", DIM)
        colour = RED if left < 2000 else (YELLOW if left < 10000 else GREEN)
        say_console("LEFT", f"{left:,} characters", colour)
        # a spoken reply runs roughly 150-400 characters
        say_console("", f"  roughly {left // 250} more replies at ~250 chars each", DIM)
        if left <= 0:
            say_console("EMPTY", "she will think and print, but not speak.", RED)
    else:
        say_console("", "usage fields not in the response; raw follows", YELLOW)
        print("  " + json.dumps(sub)[:600])

    if isinstance(reset, int) and reset > 0:
        try:
            say_console("resets", _dt.fromtimestamp(reset).strftime("%d %b %Y, %H:%M"), DIM)
        except Exception:
            pass
    print()


def mode_agents() -> None:
    """List the ElevenLabs Agents on this account and print their IDs.

    NOTE: this app does not use Agents. It calls the speech-to-text and
    text-to-speech endpoints directly. This is here purely so you can find an
    agent_id for something else you built in the ElevenLabs dashboard.
    """
    import json
    import urllib.error
    import urllib.request

    key = config.ELEVENLABS_API_KEY
    if not key:
        fatal("No ELEVENLABS_API_KEY set.")

    request = urllib.request.Request(
        "https://api.elevenlabs.io/v1/convai/agents?page_size=100",
        headers={"xi-api-key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        print()
        say_console("HTTP " + str(exc.code), body, RED)
        if exc.code in (401, 403):
            say_console("", "  that's an auth failure - check your ElevenLabs key", DIM)
        print()
        return
    except Exception as exc:
        fatal(f"Couldn't reach the ElevenLabs API: {exc}")

    agents = data.get("agents", [])
    print()
    if not agents:
        say_console("none", "there are no ElevenLabs Agents on this account.", YELLOW)
        say_console("", "  So there's no agent_id to patch - nothing here is used by", DIM)
        say_console("", "  this app. Filler phrases live in FILLER_MESSAGES in", DIM)
        say_console("", "  config.py instead.", DIM)
        print()
        return

    width = max(len(str(a.get("name", "?"))) for a in agents)
    print(f"  {BOLD}Your ElevenLabs Agents{RESET}\n")
    for agent in agents:
        print(f"  {str(agent.get('name', '?')):<{width}}  {agent.get('agent_id', '?')}")
    if data.get("has_more"):
        print(f"\n  {DIM}(more exist; this shows the first 100){RESET}")

    first = agents[0].get("agent_id", "AGENT_ID")
    print(f"\n  {DIM}To set a filler phrase on one of these (PowerShell):{RESET}\n")
    print(f'    curl -X PATCH "https://api.elevenlabs.io/v1/convai/agents/{first}" ^')
    print('      -H "xi-api-key: %ELEVENLABS_API_KEY%" ^')
    print('      -H "content-type: application/json" ^')
    print('      -d "{\\"conversation_config\\":{\\"turn\\":{\\"soft_timeout_config\\":'
          '{\\"timeout_seconds\\":3.0,\\"use_llm_generated_message\\":true}}}}"')
    print(f"\n  {YELLOW}Reminder: this app ignores Agents entirely. Patching one will{RESET}")
    print(f"  {YELLOW}not change anything you hear from voice_claude.{RESET}\n")


def mode_mic() -> None:
    """Record a few seconds with a live level meter, then play it back."""
    seconds = 5
    try:
        info_in = sd.query_devices(config.INPUT_DEVICE, "input")
        info_out = sd.query_devices(config.OUTPUT_DEVICE, "output")
    except Exception as exc:
        fatal(f"Could not open an audio device: {exc}\n"
              "Run  python voice_claude.py --devices  to see what's available.")

    print(f"\n  in : {BOLD}{info_in['name']}{RESET}")
    print(f"  out: {BOLD}{info_out['name']}{RESET}")
    print(f"\n  Talk normally for {seconds} seconds...\n")

    frames: list[bytes] = []

    def callback(indata, _f, _t, _s):
        frames.append(bytes(indata))

    with sd.RawInputStream(samplerate=config.MIC_SAMPLE_RATE, channels=1,
                           dtype="int16", device=config.INPUT_DEVICE,
                           callback=callback, blocksize=1024):
        for _ in range(seconds * 10):
            time.sleep(0.1)
            recent = b"".join(frames[-12:])
            peak, _rms = audio_level(recent)
            print(f"\r  [{level_bar(peak, 40)}] {peak:.3f}  ", end="", flush=True)

    pcm = b"".join(frames)
    peak, rms = audio_level(pcm)
    print(f"\n\n  {seconds_of(pcm, config.MIC_SAMPLE_RATE):.1f}s captured   "
          f"peak {peak:.3f}   rms {rms:.3f}\n")

    if peak < 0.015:
        print(f"  {RED}That is silence.{RESET} The app is recording, but nothing is "
              f"reaching it.\n"
              f"  - Check Windows Settings > System > Sound > Input, and that the\n"
              f"    right device is the default (or set INPUT_DEVICE in config.py).\n"
              f"  - Check Privacy & security > Microphone is allowed for desktop apps.\n"
              f"  - If a game or voice chat has the mic in exclusive mode, close it.\n")
        return
    if rms < 0.02:
        print(f"  {YELLOW}Very quiet.{RESET} Something is coming through, but it is "
              f"probably too faint\n  to transcribe. Move the mic closer, or raise its "
              f"level in Windows sound settings.\n")
    else:
        print(f"  {GREEN}Mic looks good.{RESET}\n")

    print("  Playing it back...\n")
    try:
        out = sd.RawOutputStream(samplerate=config.MIC_SAMPLE_RATE, channels=1,
                                 dtype="int16", device=config.OUTPUT_DEVICE)
        out.start()
        for i in range(0, len(pcm) - len(pcm) % 2, 4096):
            out.write(pcm[i:i + 4096])
        out.stop()
        out.close()
    except Exception as exc:
        print(f"  {YELLOW}Could not play it back: {exc}{RESET}\n")
        return
    print("  If you heard yourself clearly, the mic half is fine.\n")


def mode_voices(voice: Voice) -> None:
    rows = voice.list_voices()
    if not rows:
        print("No voices came back. Check your API key.")
        return
    width = max(len(n) for n, _, _ in rows)
    print(f"\n{BOLD}Your ElevenLabs voices{RESET}\n")
    for name, vid, cat in rows:
        print(f"  {name:<{width}}  {vid}  {DIM}{cat}{RESET}")
    print("\nPut the ID of the one you like into VOICE_ID in config.py.\n")


def mode_check() -> None:
    ok = True
    print()
    if not config.ELEVENLABS_API_KEY:
        say_console("MISSING", "ELEVENLABS_API_KEY is not set", RED); ok = False
    else:
        say_console("ok", "ElevenLabs API key found", GREEN)

    if not config.VOICE_ID:
        say_console("MISSING", "VOICE_ID is empty - run --voices", RED); ok = False
    else:
        say_console("ok", f"voice {config.VOICE_ID}", GREEN)

    if not os.path.isdir(config.WORK_DIR):
        say_console("MISSING", f"WORK_DIR does not exist: {config.WORK_DIR}", RED); ok = False
    else:
        n = sum(len(f) for _, _, f in os.walk(config.WORK_DIR))
        say_console("ok", f"WORK_DIR has {n} files", GREEN)

    for d in config.EXTRA_DIRS:
        if not os.path.isdir(d):
            say_console("MISSING", f"EXTRA_DIRS entry does not exist: {d}", RED); ok = False

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    logged_in = os.path.isdir(os.path.expanduser("~/.claude"))

    if ENV_FROM_FILE:
        say_console("ok", f".env loaded: {', '.join(ENV_FROM_FILE)}", GREEN)

    if api_key:
        where = ".env" if "ANTHROPIC_API_KEY" in ENV_FROM_FILE else "Windows environment"
        say_console("note", f"using ANTHROPIC_API_KEY from {where} - this is PAY PER "
                            f"TOKEN, a separate balance from your subscription.", YELLOW)
        say_console("", "  'credit balance is too low' means this wallet is empty;", DIM)
        say_console("", "  paying for Claude Pro or Max does NOT top it up.", DIM)
        if oauth or logged_in:
            say_console("", "  You have a subscription login available. To use it "
                            "instead, clear", YELLOW)
            say_console("", "  the key:  setx ANTHROPIC_API_KEY \"\"   then reopen "
                            "PowerShell.", YELLOW)
            say_console("", "  (the API key always wins while it is set)", DIM)
    elif oauth:
        say_console("ok", "using your Claude subscription (CLAUDE_CODE_OAUTH_TOKEN)", GREEN)
    elif logged_in:
        say_console("ok", "using the Claude Code login on this machine", GREEN)
    else:
        say_console("PROBLEM", "no Claude credential at all. Run  claude setup-token  "
                               "to use your subscription, or set ANTHROPIC_API_KEY "
                               "to pay per token.", RED); ok = False

    suppress = getattr(config, "SUPPRESS_PTT_KEY", True)
    if "lock" in config.PTT_KEY.lower() and not suppress:
        say_console("PROBLEM", f"{config.PTT_KEY} is a toggle key - set "
                               f"SUPPRESS_PTT_KEY = True", RED); ok = False
    else:
        say_console("ok", f"talk key: {config.PTT_KEY}"
                          f"{' (swallowed)' if suppress else ''}", GREEN)

    if suppress:
        try:
            probe = getattr(config, "PTT_SCANCODE", None) or config.PTT_KEY
            handle = keyboard.hook_key(probe, lambda e: None, suppress=True)
            keyboard.unhook(handle)
            say_console("ok", "can grab the talk key", GREEN)
        except Exception as exc:
            say_console("PROBLEM", f"can't grab {config.PTT_KEY} ({exc}) - try "
                                   f"running as administrator", RED); ok = False

    try:
        sd.check_input_settings(device=config.INPUT_DEVICE,
                                channels=1, dtype="int16",
                                samplerate=config.MIC_SAMPLE_RATE)
        say_console("ok", "microphone accepts 16 kHz mono", GREEN)
    except Exception as exc:
        say_console("PROBLEM", f"microphone: {exc}", RED); ok = False

    try:
        sd.check_output_settings(device=config.OUTPUT_DEVICE,
                                 channels=1, dtype="int16",
                                 samplerate=config.TTS_SAMPLE_RATE)
        say_console("ok", f"speakers accept {config.TTS_SAMPLE_RATE} Hz mono", GREEN)
    except Exception as exc:
        say_console("PROBLEM", f"speakers: {exc}", RED); ok = False

    print()
    print(f"{GREEN}Looks good.{RESET}\n" if ok else f"{RED}Fix the above, then run --check again.{RESET}\n")


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The public page is published by a scheduled task in the cloud, which has no
# way to talk to this process. It leaves a small JSON file in the synced Drive
# folder instead; we watch that file and say when the page actually moved.

PUBLISH_POLL_SECONDS = 30
_PUB = {"busy": False, "seen": None}


def _read_publish_status():
    """Read the status file the cloud publisher drops in the Drive folder."""
    import json
    path = getattr(config, "PUBLISH_STATUS", None)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        # missing, half-written, or G: not mounted - all normal, stay quiet
        return None


async def publish_watcher(voice, ptt, text_mode: bool) -> None:
    """Announce, once, each time the cloud job republishes the public page.

    Seeds from whatever is already on disk at startup, so a publish that
    happened while she was closed is not announced as fresh news. Never speaks
    while she is mid-answer - it waits for the next tick instead.
    """
    _PUB["seen"] = (_read_publish_status() or {}).get("published_at")
    while True:
        try:
            await asyncio.sleep(PUBLISH_POLL_SECONDS)
            info = _read_publish_status()
            if not info:
                continue
            stamp = info.get("published_at")
            if not stamp or stamp == _PUB["seen"] or _PUB["busy"]:
                continue
            _PUB["seen"] = stamp

            bits = []
            if info.get("day"):
                bits.append(f"day {info['day']}")
            if info.get("distance"):
                bits.append(f"{info['distance']} light years flown")
            if info.get("samples"):
                bits.append(f"{info['samples']} samples")
            detail = ", ".join(bits)
            line = "Public page updated" + (f" - {detail}." if detail else ".")

            await _say_unprompted(voice, ptt, text_mode, "published",
                                  line, CYAN)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


# One speaker, several things that might want it. Without this the publish
# announcement and a live reaction can start talking over each other.
# Created on first use rather than at import, so it always binds to the
# loop that is actually running.
_SPEECH = {"lock": None}


async def _say_unprompted(voice, ptt, text_mode: bool, tag: str,
                          line: str, color: str) -> None:
    """Speak a line she was not asked for. Never interrupts her own answer."""
    if _SPEECH["lock"] is None:
        _SPEECH["lock"] = asyncio.Lock()
    async with _SPEECH["lock"]:
        clear_status()
        say_console(tag, line, color)
        if voice and not text_mode:
            await asyncio.to_thread(voice.speak, line, ptt)


async def live_watcher(voice, ptt, text_mode: bool) -> None:
    """React out loud to what the game is doing, and keep a live snapshot.

    Reactions are canned lines from config.REACTIONS - straight to the
    speakers, no model call, no tokens. The snapshot goes to
    reference/live_context.json so she can answer "where are we" without
    grepping journals.

    This never touches the dashboard's resume cursor. See the header of
    live.py for why that matters and what it guarantees.
    """
    if not getattr(config, "LIVE_REACTIONS", False):
        return
    try:
        import live
    except Exception as exc:
        say_console("", f"live watcher unavailable: {exc}", YELLOW)
        return

    journal_dir = (config.EXTRA_DIRS[0] if getattr(config, "EXTRA_DIRS", None)
                   else config.WORK_DIR)
    try:
        # seed_to_end: start from the end of the current journal, so a heat
        # warning from three days ago is not announced as breaking news.
        tailer = live.Tailer(journal_dir, seed_to_end=True)
        reactor = live.Reactor(getattr(config, "REACTIONS", {}),
                               getattr(config, "REACTION_COOLDOWNS", {}))
        reactor.star_wait = float(getattr(config, "CARRIER_STAR_WAIT", 20))
    except Exception as exc:
        say_console("", f"live watcher failed to start: {exc}", YELLOW)
        return

    gap = float(getattr(config, "LIVE_POLL_SECONDS", 1.0))
    recent = []
    while True:
        try:
            await asyncio.sleep(gap)
            events = await asyncio.to_thread(tailer.poll)
            for ev in events:
                name = ev.get("event")
                if name:
                    recent.append({"at": ev.get("timestamp"), "event": name})
                    del recent[:-40]
                hit = reactor.react(ev)
                if not hit:
                    continue
                trigger, line = hit
                if _PUB["busy"]:
                    continue        # she is mid-answer; let it go by
                await _say_unprompted(voice, ptt, text_mode,
                                      trigger.lower(), line, YELLOW)
            # A held line - currently only the carrier arrival, which waits
            # a few seconds to see whether the primary star gets scanned.
            held = reactor.pending_due()
            if held and not _PUB["busy"]:
                await _say_unprompted(voice, ptt, text_mode,
                                      held[0].lower(), held[1], YELLOW)

            await asyncio.to_thread(live.write_context, journal_dir, list(recent))
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


async def run(text_mode: bool = False) -> None:
    voice = None
    if not text_mode or config.VOICE_ID:
        voice = Voice(config.ELEVENLABS_API_KEY, config.VOICE_ID)

    options = build_options()

    ptt = PushToTalk(config.PTT_KEY, getattr(config, "SUPPRESS_PTT_KEY", True))
    if not text_mode:
        try:
            ptt.start()
            atexit.register(ptt.stop)
        except Exception as exc:
            fatal(f"Could not grab the {config.PTT_KEY} key ({exc}).\n"
                  "On Windows this usually means you need to run as "
                  "administrator - right-click run.bat, Run as administrator.")

    print()
    say_console("ready", f"hold {BOLD}{config.PTT_KEY.upper()}{RESET} to talk, "
                         f"tap {BOLD}{config.QUIT_KEY.upper()}{RESET} to quit", CYAN)
    if ptt.suppress and not text_mode:
        say_console("", f"{config.PTT_KEY} is swallowed while this runs - "
                        f"it won't toggle or reach your games", DIM)
    say_console("", f"working in {config.WORK_DIR}", DIM)
    print()

    async with ClaudeSDKClient(options=options) as client:
        asyncio.create_task(publish_watcher(voice, ptt, text_mode))
        asyncio.create_task(live_watcher(voice, ptt, text_mode))
        while True:
            _PUB["busy"] = False
            # ---- get the user's words ------------------------------------
            if text_mode:
                try:
                    said = await asyncio.to_thread(input, f"{CYAN}you  > {RESET}")
                except (EOFError, KeyboardInterrupt):
                    break
                if said.strip().lower() in {"quit", "exit"}:
                    break
                if not said.strip():
                    continue
            else:
                pressed = await asyncio.to_thread(wait_for_key, ptt)
                if pressed == "quit":
                    break

                pcm = await asyncio.to_thread(record_while_held, ptt)
                length = seconds_of(pcm, config.MIC_SAMPLE_RATE)
                if length < config.MIN_RECORDING_SECONDS:
                    continue

                peak, rms = audio_level(pcm)
                if peak < 0.015:
                    say_console("silence", f"[{level_bar(peak)}] nothing on the mic "
                                           f"({length:.1f}s, peak {peak:.3f}). Run "
                                           f"--mic to test it.", RED)
                    continue

                say_console("...", f"[{level_bar(peak)}] {length:.1f}s, transcribing", DIM)
                clock = time.monotonic()
                try:
                    said = await asyncio.to_thread(voice.transcribe, pcm)
                    heard_in = time.monotonic() - clock
                except Exception as exc:
                    say_console("error", f"transcription failed: {exc}", RED)
                    continue
                if not said:
                    if rms < 0.02:
                        say_console("...", f"heard sound but no speech - mic level is "
                                           f"very low (rms {rms:.3f}). Move it closer or "
                                           f"turn it up in Windows sound settings.", YELLOW)
                    else:
                        say_console("...", "no words came back - try speaking a little "
                                           "longer, or check STT_LANGUAGE in config.py", YELLOW)
                    continue
                say_console("you", said, CYAN)

            log_transcript("you", said)
            _PUB["busy"] = True

            # ---- ask Claude ---------------------------------------------
            # keep both digests current before she answers. Both are plain
            # Python over the journals - milliseconds, and no tokens.
            await asyncio.to_thread(refresh_exo)
            await asyncio.to_thread(refresh_watch)
            await asyncio.to_thread(refresh_carrier)

            clock = time.monotonic()
            live = {"doing": "thinking", "tools": 0}

            def saw_tool(desc):
                live["tools"] += 1
                live["doing"] = desc.split("  ")[0]
                clear_status()
                say_console("", f"   {CYAN}>{RESET} {DIM}{desc}{RESET}")

            def saw_result(body, is_error):
                rows = body.count("\n") + 1 if body else 0
                summary = ("something went wrong" if is_error
                           else f"{rows} line{'s' * (rows != 1)}, {human(len(body))} chars")
                clear_status()
                say_console("", f"     {DIM}{summary}{RESET}")
                live["doing"] = "thinking"

            ticker = asyncio.create_task(thinking_ticker(
                clock,
                None if text_mode else voice,
                getattr(config, "THINKING_ANNOUNCE_SECONDS", 12),
                live,
            ))
            limit = getattr(config, "TURN_TIMEOUT_SECONDS", 300) or None
            try:
                reply, stats = await asyncio.wait_for(
                    ask_claude(client, said, on_tool=saw_tool, on_result=saw_result),
                    timeout=limit,
                )
                thought_in = time.monotonic() - clock
                tool_calls = stats["tools"]
            except asyncio.TimeoutError:
                clear_status()
                say_console("timeout", f"stopped her after {limit:.0f}s - that request "
                                       f"was too big. Try narrowing it to one night, "
                                       f"or one kind of thing.", RED)
                if voice and not text_mode:
                    try:
                        await asyncio.to_thread(
                            voice.speak,
                            "Sorry, that one was taking too long so I stopped. "
                            "Try asking for a smaller slice of it.", ptt)
                    except Exception:
                        pass
                continue
            except Exception as exc:
                clear_status()
                say_console("error", f"Claude failed: {exc}", RED)
                continue
            finally:
                ticker.cancel()
                clear_status()
            if not reply:
                if tool_calls >= config.MAX_TURNS - 1:
                    say_console("cut off", f"she used all {config.MAX_TURNS} of her tool "
                                           f"rounds hunting through files and never got "
                                           f"to an answer. Narrow the question, or raise "
                                           f"MAX_TURNS in config.py.", RED)
                    if voice and not text_mode:
                        try:
                            await asyncio.to_thread(
                                voice.speak,
                                "I ran out of steps digging through your journals "
                                "before I could answer. Ask me for a smaller slice.",
                                ptt)
                        except Exception:
                            pass
                else:
                    say_console("...", f"no answer came back ({tool_calls} file lookups, "
                                       f"{thought_in:.0f}s)", YELLOW)
                continue

            say_console("claude", reply, GREEN)
            log_transcript("claude", reply)

            # ---- say it out loud ----------------------------------------
            if voice and config.VOICE_ID:
                spoken = clean_for_speech(reply)
                try:
                    finished = await asyncio.to_thread(
                        voice.speak, spoken, None if text_mode else ptt
                    )
                    if not finished:
                        say_console("", "(cut off)", DIM)
                        # wait for the key to come back up so the interrupt
                        # press doesn't immediately start a recording
                        await asyncio.to_thread(ptt.wait_for_release)

                    bits = [f"heard {heard_in:.1f}s"]
                    thinking = f"thought {thought_in:.1f}s"
                    if tool_calls:
                        thinking += f" ({tool_calls} file lookup{'s' * (tool_calls > 1)})"
                    bits.append(thinking)
                    if voice.first_audio_after is not None:
                        bits.append(f"voice {voice.first_audio_after:.1f}s")
                    say_console("timing", "  ·  ".join(bits), DIM)
                    print_cost(stats)
                except Exception as exc:
                    say_console("error", f"speech failed: {exc}", RED)


def wait_for_key(ptt: "PushToTalk") -> str:
    """Block until push-to-talk is held or quit is tapped."""
    while True:
        if ptt.down:
            return "talk"
        if keyboard.is_pressed(config.QUIT_KEY):
            while keyboard.is_pressed(config.QUIT_KEY):
                time.sleep(0.02)
            return "quit"
        time.sleep(0.02)


# ---------------------------------------------------------------------------

def main() -> None:
    args = set(sys.argv[1:])

    if "--key" in args:
        mode_key()
        return

    if "--devices" in args:
        mode_devices()
        return

    if "--check" in args:
        mode_check()
        return

    if "--mic" in args:
        mode_mic()
        return

    if "--auth" in args:
        mode_auth()
        return

    if "--agents" in args:
        mode_agents()
        return

    if "--credits" in args:
        mode_credits()
        return

    if "--tools" in args:
        mode_tools()
        return

    if "--exo" in args:
        mode_exo()
        return

    if "--carrier" in args:
        mode_carrier()
        return
    if "--watch" in args:
        mode_watch()
        return

    if "--webtest" in args:
        mode_webtest(sys.argv[1:])
        return

    if "--voices" in args:
        if not config.ELEVENLABS_API_KEY:
            fatal("Set ELEVENLABS_API_KEY in config.py (or as an environment "
                  "variable) first.")
        mode_voices(Voice(config.ELEVENLABS_API_KEY, ""))
        return

    text_mode = "--text" in args

    if not config.ELEVENLABS_API_KEY:
        fatal("ELEVENLABS_API_KEY is empty. Put your key in config.py, or set "
              "it as an environment variable.")
    if not config.VOICE_ID:
        fatal("VOICE_ID is empty. Run:  python voice_claude.py --voices\n"
              "then paste the ID of the voice you want into config.py.")
    if not os.path.isdir(config.WORK_DIR):
        fatal(f"WORK_DIR does not exist:\n  {config.WORK_DIR}\n"
              "Point it at the folder with your game logs in config.py.")

    try:
        asyncio.run(run(text_mode=text_mode))
    except KeyboardInterrupt:
        pass
    print(f"\n{DIM}bye{RESET}\n")


if __name__ == "__main__":
    main()
