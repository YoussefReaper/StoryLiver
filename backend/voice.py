"""Voice - opt-in, and free by default.

Two tiers, and the honest split matters:

  * LOCAL: the browser's own speech synthesis reads the passage. It costs
    nothing, sends nothing anywhere, and works offline - so it is free, and we
    do not charge Mana for something that costs us nothing.
  * STUDIO: a real TTS model, only when STORYLIVER_TTS_MODEL is configured.
    That one has a bill attached, so it costs Mana.

Text stays the default in both cases. Nothing autoplays.
"""
from __future__ import annotations

import os

import httpx

from . import config, db, llm

TTS_MODEL = os.getenv("STORYLIVER_TTS_MODEL", "").strip()
TTS_VOICE = os.getenv("STORYLIVER_TTS_VOICE", "onyx").strip()
TTS_FORMAT = os.getenv("STORYLIVER_TTS_FORMAT", "mp3").strip()
# Priced per action, not per character, so it stays legible.
TTS_MANA = int(os.getenv("STORYLIVER_TTS_MANA", "2"))
TTS_USD_PER_1K_CHARS = float(os.getenv("STORYLIVER_TTS_USD_PER_1K", "0.015"))

MAX_CHARS = 1800


def available() -> dict:
    return {
        "local": True,
        "local_note": "Read aloud by your own device. Costs nothing, sends nothing, works offline.",
        "studio": bool(TTS_MODEL and config.live_llm()),
        "studio_model": TTS_MODEL or None,
        "studio_mana": TTS_MANA,
        "studio_note": ("A studio narrator voice." if TTS_MODEL else
                        "Not configured on this deployment. Set STORYLIVER_TTS_MODEL to enable."),
    }


def speak(text: str, *, user_id: str, playthrough_id: str | None = None,
          voice: str | None = None) -> bytes:
    """Studio tier only. Raises if it is not configured."""
    if not TTS_MODEL:
        raise llm.LLMError("studio voice is not configured on this deployment")
    if not config.API_KEY:
        raise llm.LLMError("OPENAI_API_KEY is not set")

    text = " ".join((text or "").split())[:MAX_CHARS]
    if not text:
        raise ValueError("nothing to read")

    with httpx.Client(timeout=config.REQUEST_TIMEOUT) as client:
        r = client.post(
            f"{config.BASE_URL}/audio/speech",
            headers={"Authorization": f"Bearer {config.API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": TTS_MODEL, "voice": voice or TTS_VOICE,
                  "input": text, "response_format": TTS_FORMAT},
        )
    if r.status_code >= 400:
        raise llm.LLMError(f"tts {r.status_code}: {r.text[:200]}")

    usd = len(text) / 1000.0 * TTS_USD_PER_1K_CHARS
    day = db.today()
    db.run(
        "INSERT INTO usage_log (playthrough_id,user_id,role,model,in_tokens,out_tokens,usd,day,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (playthrough_id, user_id, "voice", TTS_MODEL, len(text), 0, usd, day, db.now()))
    db.run(
        "INSERT INTO daily_ledger (user_id,day,usd_spent) VALUES (?,?,?)"
        " ON CONFLICT(user_id,day) DO UPDATE SET usd_spent = usd_spent + excluded.usd_spent",
        (user_id, day, usd))
    return r.content


def media_type() -> str:
    return {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac",
            "flac": "audio/flac", "wav": "audio/wav", "pcm": "audio/pcm"}.get(TTS_FORMAT, "audio/mpeg")
