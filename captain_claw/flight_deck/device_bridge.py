"""Device bridge — a handheld voice client (M5Stack StickS3 / StopWatch) talks to an agent.

One WebSocket per device: ``/device/ws?token=<cc_pat_…>&device=<id>``
(optional ``agent=<slug|name|port>``, ``lang=<tts language>``, ``speak=0``,
``voice=male|female|<Soniox voice>``,
``sw``/``sh`` = screen size for pictures, default 240×135,
``round=1`` = a round screen: pictures fit inside the circle,
``img=chunks`` = pictures may span several frames, so they can be bigger).

The device records while its button is held and streams the audio here;
Flight Deck transcribes it (Soniox), sends the words to one of the owner's
agents over the glasses channel bus (surface ``"device"``), and returns the
reply as text, spoken audio (Soniox TTS) and any pictures/audio the turn
made — so the stick stays a thin client with no audio codecs or HTTP stack.

Device → Flight Deck
  text  {"type":"hello", ...}                    optional, informational
  text  {"type":"rec_start","rate":16000}        starts a recording (stops playback)
  bin   raw PCM s16le mono at that rate          while recording
  text  {"type":"rec_end"}                       → transcribe → agent
  text  {"type":"rec_cancel"}                    drop the recording
  text  {"type":"text","text":"…"}               send typed words instead
  text  {"type":"stop"}                          stop playback, clear the queue
  text  {"type":"agents"} / {"type":"select","agent":"<slug>"}
  text  {"type":"prefs","voice":"male|female|<name>","speak":bool}   change voice / speaking live
  text  {"type":"ping","t":<device ms>}          echoed back (round-trip time)

Flight Deck → device
  text  {"type":"bound","agent":name,"slug":slug,"linked":bool} | {"type":"agents","items":[…],"current":slug}
  text  {"type":"state","state":"connected|agent_offline|transcribing|thinking|ready"}
  text  {"type":"transcript","text":"…"}         what it heard
  text  {"type":"status","text":"…"}             progress narration
  text  {"type":"activity","text":"…","tool":"…"} what the agent is doing (tools, LLM calls)
  text  {"type":"reply","text":"…","proactive":bool}
  text  {"type":"audio_start","rate":24000} … {"type":"audio_end"}
  bin   0x01 + PCM s16le mono                    audio chunks between those two
  text  {"type":"image","name":"…","w":240,"h":135,"size":n} then bin 0x02 + baseline JPEG
        (img=chunks: bin 0x03 + part … while more follow, then 0x02 + the last part)
  text  {"type":"error","text":"…"} | {"type":"pong","t":<echoed>}

Auth: a personal access token (Profile → MCP agent access) names the owner;
the device only ever reaches that owner's agents. With Flight Deck auth off
(single-user desktop) no token is needed.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import re
import secrets
import shutil
import struct
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from captain_claw.flight_deck import glasses_bridge as gb

log = logging.getLogger(__name__)

router = APIRouter()

# Hidden context for the device's agent binding (first message only — the
# agent keeps it per surface and renders it on device turns).
_DEVICE_SYSTEM_CONTEXT = (
    "[SYSTEM CONTEXT — do not echo, quote, or acknowledge this block in your reply.]\n"
    "The user is talking to you through a tiny handheld voice device: they spoke, "
    "and your reply is read aloud and shown on a {screen} screen.\n"
    "Output rules for every reply on this session:\n"
    "  - Lead with the answer. No preamble, no recap.\n"
    "  - Keep it short and speakable: 1–3 short sentences, or a short list of "
    "items when the task produced several (never drop results to stay short).\n"
    "  - Plain text only: no tables, code blocks, headings or URLs unless asked.\n"
    "  - Pictures you generate with image_gen are shown on the device's screen and "
    "audio you make with pocket_tts is played on it, automatically.\n"
    "  - The transcript of their speech may contain recognition errors; read it "
    "charitably.\n"
    "  - Do not mention the device, this context, or these rules.\n"
    "Apply these rules to this and every subsequent message until told otherwise.\n"
    "---\n"
    "USER MESSAGE:\n"
)

# Binary frame kinds, Flight Deck → device (first byte of each frame).
BIN_AUDIO = 0x01
BIN_JPEG = 0x02        # a picture, or the last part of one
BIN_JPEG_PART = 0x03   # a part of a picture, more follow (devices that ask: img=chunks)

PCM_CHUNK = 4096                 # bytes per audio frame sent to the device
TTS_RATE = gb._STREAM_SAMPLE_RATE
MAX_REC_SECONDS = 120            # a held button can't fill memory
MIN_REC_SECONDS = 0.35
MAX_SPOKEN_CHARS = 1500          # long replies are shown in full, spoken in part
MEDIA_WINDOW_S = 180.0           # turn_media trails the reply; accept it this long
AUDIO_FILE_MAX_S = 300
MAX_JPEG_BYTES = 14000           # the device's WebSocket library takes ≤15 KB frames
MAX_CHUNKED_JPEG_BYTES = 120_000  # a picture sent in parts (img=chunks)


# ── Small pure helpers (tested) ───────────────────────────────────────


def wav_from_pcm(pcm: bytes, rate: int, channels: int = 1) -> bytes:
    """Wrap raw little-endian int16 PCM in a WAV header."""
    byte_rate = rate * channels * 2
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, channels * 2, 16)
    header += b"data" + struct.pack("<I", len(pcm))
    return header + pcm


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_URL = re.compile(r"https?://\S+")
_MD_MARKS = re.compile(r"(\*\*|__|`+|~~|^#{1,6}\s*|^>\s*)", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)
_MD_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.MULTILINE)


def speakable(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """A reply as it should be read aloud: markdown marks, links and URLs
    out, list items as sentences, cut at a sentence end near *limit*."""
    out = _MD_LINK.sub(r"\1", str(text or ""))
    out = _MD_URL.sub("", out)
    out = _MD_TABLE_RULE.sub("", out)
    out = _MD_MARKS.sub("", out)
    out = _MD_BULLET.sub("", out)
    out = out.replace("|", ", ").replace("*", "")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    out = " ".join(ln if ln[-1] in ".!?:;," else ln + "." for ln in lines)
    out = re.sub(r"\s{2,}", " ", out).strip()
    if len(out) > limit:
        cut = out[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        out = cut[: end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + "…"
    return out


def screen_text(text: str, has_dj: bool = False) -> str:
    """A reply as the tiny screen should show it: markdown marks out (the
    device renders plain text), structure kept. *has_dj*: the device's font
    has Đ (the StopWatch's does; the StickS3's has only đ)."""
    out = _MD_LINK.sub(r"\1", str(text or ""))
    out = _MD_TABLE_RULE.sub("", out)
    out = re.sub(r"(\*\*|__|`+|~~)", "", out)
    out = re.sub(r"^#{1,6}\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*[-*]\s+", "• ", out, flags=re.MULTILINE)
    if not has_dj:
        out = out.replace("Đ", "Dj")  # the StickS3's font has đ but not Đ
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# Emoji and symbol marks the device font can't draw (agent activity uses a few).
_NO_GLYPH = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\uFE0F\u200D]")
# Runtime statuses that are turn bookkeeping, not activity worth showing.
_QUIET_STATUSES = {"", "thinking", "ready", "idle", "done", "completed", "agent_connected",
                   "agent_reconnecting", "subscribed"}


def activity_text(text: str, limit: int = 120) -> str:
    """An activity line as the device shows it: one line, no emoji, "…"."""
    out = _NO_GLYPH.sub("", str(text or "")).replace("\n", " ")
    out = re.sub(r"\.\.\.$", "…", re.sub(r"\s{2,}", " ", out).strip())
    return out[:limit]


def round_fit(width: int, height: int, aspect: float) -> tuple[int, int]:
    """The largest picture of *aspect* (w/h) that fits inside the circle a
    round width×height screen shows (its diameter is the shorter side)."""
    d = min(width, height)
    return max(1, int(d * aspect / math.hypot(1, aspect))), max(1, int(d / math.hypot(1, aspect)))


def fit_jpeg(blob: bytes, width: int, height: int, quality: int = 85,
             max_bytes: int = MAX_JPEG_BYTES, round_screen: bool = False) -> bytes:
    """Any picture as a baseline JPEG of exactly width×height (letterboxed on
    black) — what the device's decoder takes — no larger than *max_bytes*
    (the device's WebSocket frame limit; quality steps down to fit). On a
    round screen the picture fits inside the circle, corners and all."""
    from PIL import Image, ImageOps

    im = Image.open(io.BytesIO(blob))
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
        im = Image.alpha_composite(bg, im)
    im = im.convert("RGB")
    box = round_fit(width, height, im.width / im.height) if round_screen else (width, height)
    im = ImageOps.contain(im, box, Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), (0, 0, 0))
    canvas.paste(im, ((width - im.width) // 2, (height - im.height) // 2))
    while True:
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=quality, progressive=False, optimize=False)
        if out.tell() <= max_bytes or quality <= 20:
            return out.getvalue()
        quality -= 10


def jpeg_frames(jpeg: bytes, part: int = MAX_JPEG_BYTES) -> list[tuple[int, bytes]]:
    """A picture as device frames for img=chunks: (0x03, part) while more
    follow, then (0x02, the last part). One that fits a frame is a single
    (0x02, bytes) — exactly what a device without img=chunks gets."""
    parts = [jpeg[i:i + part] for i in range(0, len(jpeg), part)] or [b""]
    return [(BIN_JPEG_PART, p) for p in parts[:-1]] + [(BIN_JPEG, parts[-1])]


def voice_name(choice: str) -> str:
    """The Soniox voice for a device's choice: "male"/"female" map to
    ``DEVICE_VOICE_MALE`` / ``DEVICE_VOICE_FEMALE`` (Adrian / Emma), anything
    else is taken as a voice name; "" = the deck's ``SONIOX_TTS_VOICE``."""
    import os

    c = str(choice or "").strip()
    if c.lower() in ("male", "m"):
        return os.environ.get("DEVICE_VOICE_MALE", "").strip() or "Adrian"
    if c.lower() in ("female", "f"):
        return os.environ.get("DEVICE_VOICE_FEMALE", "").strip() or "Emma"
    return re.sub(r"[^A-Za-z0-9_-]", "", c)[:32]


def _int_param(value: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except Exception:
        return default


def _picture_options(q) -> dict:
    """How pictures are made for a device, from its query (sw, sh, round,
    img); with none of them, the StickS3's 240×135 in one frame."""
    return {
        "screen": (_int_param(q.get("sw", ""), 240, 32, 800), _int_param(q.get("sh", ""), 135, 32, 800)),
        "round_screen": q.get("round", "") in ("1", "true", "on"),
        "chunked": q.get("img", "") == "chunks",
    }


# ── Auth + agent choice ───────────────────────────────────────────────


async def _device_user(token: str) -> dict | None:
    """The owner a device token names (a personal access token), or None."""
    from captain_claw.flight_deck import auth as A

    if not A._fd_auth_enabled():
        return dict(A._LOCAL_USER)
    token = (token or "").strip()
    if not token.startswith(A.PAT_PREFIX):
        return None
    db = A.get_db()
    row = await db.get_pat_by_hash(A.hash_token(token))
    if not row:
        return None
    user = await db.get_user_by_id(row["user_id"])
    if not user:
        return None
    try:
        await db.touch_pat(row["id"])
    except Exception:
        pass
    return user


def _running_agents(user_id: str) -> list[dict]:
    from captain_claw.flight_deck.mcp_server_routes import _user_agents

    return [
        a for a in _user_agents(user_id)
        if a.get("port") and str(a.get("status", "")).lower() in ("running", "up")
    ]


def _pick_agent(user_id: str, selector: str) -> dict | None:
    """The agent a selector names (slug, name or port) among the owner's
    running agents, else their first running one."""
    agents = _running_agents(user_id)
    sel = (selector or "").strip()
    if sel:
        for a in agents:
            if str(a.get("port")) == sel or a.get("slug") == sel or a.get("name") == sel:
                return a
        low = sel.lower()
        for a in agents:
            if low in str(a.get("name", "")).lower() or low in str(a.get("slug", "")).lower():
                return a
    return agents[0] if agents else None


# ── One connected device ──────────────────────────────────────────────


class _DeviceConn:
    def __init__(self, ws: WebSocket, user: dict, device_id: str, *, lang: str,
                 speak: bool, screen: tuple[int, int], voice: str = "",
                 round_screen: bool = False, chunked: bool = False) -> None:
        self.ws = ws
        self.user = user
        self.user_id = str(user.get("id", ""))
        self.device_id = device_id
        self.lang = lang
        self.speak = speak
        self.voice = voice_name(voice)
        self.screen = screen
        self.round_screen = round_screen  # pictures fit inside the circle
        self.chunked = chunked            # pictures may span several frames
        self._image_lock = asyncio.Lock()  # one picture's parts at a time
        self.ch: gb._ChannelState | None = None
        self.agent: dict | None = None
        self._send_lock = asyncio.Lock()
        self.recording = False
        self.rec_rate = 16000
        self.rec = bytearray()
        # A device turn is open from our send until the agent says "ready".
        self.pending = False
        self.last_reply_at = 0.0
        self._last_activity = ""
        self._audio_q: asyncio.Queue = asyncio.Queue()
        self._audio_task: asyncio.Task | None = None
        self._audio_worker: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()

    # ── wire ──
    async def send_json(self, obj: dict) -> None:
        async with self._send_lock:
            await self.ws.send_text(json.dumps(obj, default=str))

    async def send_bin(self, kind: int, payload: bytes) -> None:
        async with self._send_lock:
            await self.ws.send_bytes(bytes([kind]) + payload)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # ── binding ──
    @property
    def channel_id(self) -> str:
        return f"device:{self.user_id}:{self.device_id}"

    async def bind(self, selector: str) -> bool:
        agent = _pick_agent(self.user_id, selector)
        if agent is None:
            await self.send_json({"type": "error", "text": "No running agent. Start one in Flight Deck."})
            return False
        ch = await gb._get_or_create_channel(self.channel_id)
        if self.ch is not ch:
            if self.ch is not None and self.on_bus in self.ch.callback_subscribers:
                self.ch.callback_subscribers.remove(self.on_bus)
            self.ch = ch
        # One device socket per channel listens (a reconnect replaces the old).
        ch.callback_subscribers[:] = [cb for cb in ch.callback_subscribers
                                      if getattr(cb, "__self__", None) is None
                                      or not isinstance(cb.__self__, _DeviceConn)]
        ch.callback_subscribers.append(self.on_bus)
        await gb._ensure_agent_binding(ch, str(agent.get("host") or "localhost"), int(agent["port"]))
        self.agent = agent
        await self.send_json({"type": "bound", "agent": agent.get("name", ""), "slug": agent.get("slug", ""),
                              "linked": ch.agent_ws is not None})
        return True

    def unbind(self) -> None:
        if self.ch is not None and self.on_bus in self.ch.callback_subscribers:
            self.ch.callback_subscribers.remove(self.on_bus)

    # ── agent bus → device ──
    async def on_bus(self, ev: dict) -> None:
        etype = ev.get("type")
        if etype == "agent":
            proactive = bool(ev.get("proactive")) and not ev.get("fd_delivers")
            if not (self.pending or proactive):
                return  # a turn from another surface on the same agent
            text = str(ev.get("text", "") or "")
            if not text.strip():
                return
            self.last_reply_at = time.monotonic()
            # Round screens are the StopWatch, whose font has Đ.
            await self.send_json({"type": "reply", "text": screen_text(text, has_dj=self.round_screen),
                                  "proactive": proactive})
            if self.speak:
                spoken = speakable(text)
                if spoken:
                    await self._audio_q.put(("tts", spoken))
        elif etype == "narration":
            if self.pending and ev.get("text"):
                await self.send_json({"type": "status", "text": str(ev["text"])[:200]})
        elif etype == "activity":
            if self.pending and ev.get("phase") != "done":
                await self._send_activity(str(ev.get("text", "")), str(ev.get("tool", "")))
        elif etype == "status":
            status = str(ev.get("status", ""))
            # "Using <tool>..." duplicates the tool's activity line (which says more).
            tool_start = status.startswith("Using ") and status.endswith("...")
            if self.pending and status.strip().lower() not in _QUIET_STATUSES and not tool_start:
                await self._send_activity(status, "")
            if status == "thinking" and self.pending:
                await self.send_json({"type": "state", "state": "thinking"})
            elif status == "ready" and self.pending:
                self.pending = False
                await self.send_json({"type": "state", "state": "ready"})
            elif status == "agent_connected":
                await self.send_json({"type": "state", "state": "connected"})
            elif status == "agent_reconnecting":
                await self.send_json({"type": "state", "state": "agent_offline"})
                await self.send_json({"type": "status", "text": "Reconnecting to agent…"})
        elif etype == "error":
            if self.pending:
                self.pending = False
                text = ("The agent is busy — try again in a moment." if ev.get("retryable")
                        else str(ev.get("text", "") or "Agent error"))
                await self.send_json({"type": "error", "text": text[:300]})
        elif etype == "media":
            if not (self.pending or time.monotonic() - self.last_reply_at < MEDIA_WINDOW_S):
                return
            for item in ev.get("items") or []:
                kind = str(item.get("kind", ""))
                if kind == "image":
                    self._spawn(self._send_image(item))
                elif kind == "audio":
                    await self._audio_q.put(("file", item))

    async def _send_activity(self, text: str, tool: str) -> None:
        line = activity_text(text)
        if not line or line == self._last_activity:
            return
        self._last_activity = line
        await self.send_json({"type": "activity", "text": line, "tool": tool})

    # ── device → agent ──
    def system_context(self) -> str:
        w, h = self.screen
        screen = f"small round {min(w, h)} px" if self.round_screen else f"{w}×{h} pixel"
        return _DEVICE_SYSTEM_CONTEXT.replace("{screen}", screen)

    async def send_to_agent(self, text: str) -> None:
        ch = self.ch
        if ch is None:
            await self.send_json({"type": "error", "text": "No agent bound."})
            return
        for _ in range(50):  # the first send after binding waits for the link
            if ch.agent_ws is not None:
                break
            await asyncio.sleep(0.1)
        if ch.agent_ws is None:
            await self.send_json({"type": "error", "text": "Agent isn't reachable."})
            return
        async with ch.send_lock:
            content = text if ch.context_sent else self.system_context() + text
            payload = {
                "type": "chat", "content": content, "surface": "device",
                "client_msg_id": "dev_" + secrets.token_hex(6),
            }
            try:
                await ch.agent_ws.send(json.dumps(payload))
                ch.context_sent = True
            except Exception as exc:
                await self.send_json({"type": "error", "text": f"Send failed: {exc}"})
                return
        self.pending = True
        self._last_activity = ""
        await self.send_json({"type": "state", "state": "thinking"})

    async def finish_recording(self) -> None:
        pcm = bytes(self.rec)
        self.rec = bytearray()
        seconds = len(pcm) / (2 * self.rec_rate)
        if seconds < MIN_REC_SECONDS:
            await self.send_json({"type": "error", "text": "Too short — hold the button while you talk."})
            await self.send_json({"type": "state", "state": "ready"})
            return
        await self.send_json({"type": "state", "state": "transcribing"})
        from captain_claw.flight_deck.whatsapp_bridge import _transcribe_soniox

        text, err = await _transcribe_soniox(wav_from_pcm(pcm, self.rec_rate), "audio/wav")
        text = (text or "").strip()
        if not text:
            await self.send_json({"type": "error", "text": err or "Didn't catch that."})
            await self.send_json({"type": "state", "state": "ready"})
            return
        await self.send_json({"type": "transcript", "text": text})
        await self.send_to_agent(text)

    # ── pictures ──
    async def _fetch_from_agent(self, path: str) -> bytes:
        import httpx

        ch = self.ch
        if ch is None or not ch.bound_port:
            raise RuntimeError("no agent")
        auth = await gb._resolve_channel_agent_auth(ch)
        params = {"path": path}
        if auth:
            params["token"] = auth
        url = f"http://{ch.bound_host or 'localhost'}:{ch.bound_port}/api/media"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"agent media {resp.status_code}: {resp.text[:120]}")
        return resp.content

    async def _send_image(self, item: dict) -> None:
        try:
            blob = await self._fetch_from_agent(str(item["path"]))
            w, h = self.screen
            max_bytes = MAX_CHUNKED_JPEG_BYTES if self.chunked else MAX_JPEG_BYTES
            jpeg = await asyncio.to_thread(fit_jpeg, blob, w, h, max_bytes=max_bytes,
                                           round_screen=self.round_screen)
            frames = jpeg_frames(jpeg) if self.chunked else [(BIN_JPEG, jpeg)]
            async with self._image_lock:  # two pictures' parts must not interleave
                await self.send_json({"type": "image", "name": str(item.get("name", "")), "w": w, "h": h,
                                      "size": len(jpeg)})
                for kind, part in frames:
                    await self.send_bin(kind, part)
        except Exception as exc:
            log.warning("device image delivery failed: %s", exc)
            await self.send_json({"type": "status", "text": f"Couldn't show {item.get('name', 'the picture')}."})

    # ── audio queue (one sound at a time) ──
    def start_audio_worker(self) -> None:
        self._audio_worker = asyncio.create_task(self._audio_loop())

    async def _audio_loop(self) -> None:
        while True:
            kind, arg = await self._audio_q.get()
            job = self._play_tts(arg) if kind == "tts" else self._play_file(arg)
            self._audio_task = asyncio.create_task(job)
            try:
                await self._audio_task
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise  # the worker itself is stopping (device gone)
                # else: stop_audio() cancelled just this sound — next one
            except Exception as exc:
                log.warning("device audio failed: %s", exc)
            finally:
                self._audio_task = None

    async def stop_audio(self) -> None:
        while not self._audio_q.empty():
            self._audio_q.get_nowait()
        task = self._audio_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass

    async def _stream_pcm(self, chunks) -> None:
        await self.send_json({"type": "audio_start", "rate": TTS_RATE})
        try:
            pending = b""
            async for chunk in chunks:
                pending += chunk
                while len(pending) >= PCM_CHUNK:
                    await self.send_bin(BIN_AUDIO, pending[:PCM_CHUNK])
                    pending = pending[PCM_CHUNK:]
            if len(pending) % 2:
                pending = pending[:-1]
            if pending:
                await self.send_bin(BIN_AUDIO, pending)
        finally:
            try:
                await self.send_json({"type": "audio_end"})
            except Exception:
                pass

    async def _play_tts(self, text: str) -> None:
        gen = gb.soniox_tts_pcm(text, language=self.lang, voice=self.voice)
        try:
            await self._stream_pcm(gen)
        except gb.SonioxTTSError as exc:
            await self.send_json({"type": "status", "text": f"Voice unavailable: {exc}"[:200]})
        finally:
            await gen.aclose()

    async def _play_file(self, item: dict) -> None:
        if not shutil.which("ffmpeg"):
            await self.send_json({"type": "status", "text": "Can't play audio files (no ffmpeg on the server)."})
            return
        blob = await self._fetch_from_agent(str(item["path"]))
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-t", str(AUDIO_FILE_MAX_S),
            "-f", "s16le", "-ac", "1", "-ar", str(TTS_RATE), "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def _feed() -> None:
            try:
                proc.stdin.write(blob)
                await proc.stdin.drain()
            finally:
                proc.stdin.close()

        async def _chunks():
            while True:
                data = await proc.stdout.read(PCM_CHUNK)
                if not data:
                    return
                yield data

        feeder = asyncio.create_task(_feed())
        try:
            await self._stream_pcm(_chunks())
        finally:
            feeder.cancel()
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()

    async def close(self) -> None:
        self.unbind()
        await self.stop_audio()
        if self._audio_worker is not None:
            self._audio_worker.cancel()
        for task in list(self._bg):
            task.cancel()


# ── Route ─────────────────────────────────────────────────────────────


@router.websocket("/device/ws")
async def device_ws(ws: WebSocket) -> None:
    q = ws.query_params
    user = await _device_user(q.get("token", ""))
    if user is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    device_id = re.sub(r"[^A-Za-z0-9_-]", "", q.get("device", "") or "")[:32] or "stick"
    lang = re.sub(r"[^A-Za-z-]", "", q.get("lang", "") or "")[:8]
    conn = _DeviceConn(
        ws, user, device_id, lang=lang, speak=q.get("speak", "1") not in ("0", "false", "off"),
        voice=q.get("voice", ""), **_picture_options(q),
    )
    conn.start_audio_worker()
    try:
        await conn.bind(q.get("agent", ""))
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                if conn.recording:
                    if len(conn.rec) + len(data) <= MAX_REC_SECONDS * 2 * conn.rec_rate:
                        conn.rec.extend(data)
                continue
            try:
                obj = json.loads(msg.get("text") or "")
            except Exception:
                continue
            mtype = obj.get("type")
            if mtype == "rec_start":
                await conn.stop_audio()
                conn.rec_rate = _int_param(str(obj.get("rate", 16000)), 16000, 8000, 48000)
                conn.rec = bytearray()
                conn.recording = True
            elif mtype == "rec_end":
                if conn.recording:
                    conn.recording = False
                    conn._spawn(conn.finish_recording())
            elif mtype == "rec_cancel":
                conn.recording = False
                conn.rec = bytearray()
            elif mtype == "text":
                text = str(obj.get("text", "")).strip()
                if text:
                    await conn.stop_audio()
                    conn._spawn(conn.send_to_agent(text))
            elif mtype == "stop":
                await conn.stop_audio()
            elif mtype == "agents":
                items = [{"slug": a.get("slug"), "name": a.get("name")} for a in _running_agents(conn.user_id)]
                await conn.send_json({"type": "agents", "items": items,
                                      "current": (conn.agent or {}).get("slug", "")})
            elif mtype == "select":
                await conn.bind(str(obj.get("agent", "")))
            elif mtype == "prefs":
                if "voice" in obj:
                    conn.voice = voice_name(str(obj.get("voice", "")))
                if "speak" in obj:
                    conn.speak = bool(obj.get("speak"))
            elif mtype == "ping":
                pong = {"type": "pong"}
                if isinstance(obj.get("t"), (int, float)):
                    pong["t"] = obj["t"]
                await conn.send_json(pong)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("device ws error: %s", exc)
    finally:
        await conn.close()
