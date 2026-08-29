"""LLM client, two providers, one call signature.

A model id starting with "claude-" routes through the native Anthropic
Messages API (never an OpenAI-compatible shim - the two APIs have different
shapes and pretending otherwise silently breaks). Anything else goes through
the OpenAI-compatible chat-completions path, which keeps every non-Anthropic
deployment (OpenRouter, Groq, vLLM, OpenAI itself) working unmodified.

The offline stub (STORYLIVER_LLM_MODE=mock, or simply no key set) is
provider-agnostic and drives the full test suite with no key and no spend.
"""
import json
import random
import re
import time

import httpx

from . import budget, config, db


class LLMError(RuntimeError):
    pass


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _extract_json(text: str):
    """Models occasionally wrap JSON in prose or fences. Recover it."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    raise LLMError("no JSON object in model output")


def _record(user_id, playthrough_id, role, model, in_tok, out_tok, *,
            cache_write_tok=0, cache_read_tok=0):
    pin, pout = config.price_for(model)
    usd = (in_tok / 1_000_000) * pin + (out_tok / 1_000_000) * pout
    usd += (cache_write_tok / 1_000_000) * pin * config.CACHE_WRITE_MULTIPLIER
    usd += (cache_read_tok / 1_000_000) * pin * config.CACHE_READ_MULTIPLIER
    day = db.today()
    db.run(
        "INSERT INTO usage_log (playthrough_id,user_id,role,model,in_tokens,out_tokens,usd,day,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (playthrough_id, user_id, role, model, in_tok + cache_write_tok + cache_read_tok,
         out_tok, usd, day, db.now()),
    )
    db.run(
        "INSERT INTO daily_ledger (user_id,day,usd_spent) VALUES (?,?,?)"
        " ON CONFLICT(user_id,day) DO UPDATE SET usd_spent = usd_spent + excluded.usd_spent",
        (user_id, day, usd),
    )
    return usd


def complete(role, system, user, *, user_id, playthrough_id=None, model=None,
             json_mode=False, max_tokens=700, temperature=0.8, stub=None):
    """One completion. `stub` is a zero-arg callable used when offline.

    Every call passes the per-turn budget gate first: an unlisted role raises,
    and a turn that exceeds its cap raises. The stub path counts too, so the
    tests measure the same bound the live path obeys."""
    budget.record(role)
    model = model or config.MODELS.get(role, config.MODELS["narrator"])

    if not config.live_llm():
        out = stub() if stub else ""
        text = json.dumps(out) if not isinstance(out, str) else out
        _record(user_id, playthrough_id, role, model,
                _estimate_tokens(system + user), _estimate_tokens(text))
        return _extract_json(text) if json_mode else text

    if not config.key_for(model):
        which = "ANTHROPIC_API_KEY" if config.is_claude_model(model) else "OPENAI_API_KEY"
        raise LLMError(f"{which} is not set. Add it to .env and restart.")

    if config.is_claude_model(model):
        text = _complete_anthropic(role, system, user, model=model, user_id=user_id,
                                    playthrough_id=playthrough_id, max_tokens=max_tokens,
                                    temperature=temperature)
    else:
        text = _complete_openai(role, system, user, model=model, user_id=user_id,
                                playthrough_id=playthrough_id, max_tokens=max_tokens,
                                temperature=temperature, json_mode=json_mode)
    return _extract_json(text) if json_mode else text


# ---------------------------------------------------------------------------
# Anthropic - native Messages API
# ---------------------------------------------------------------------------

_anthropic_client = None


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY or config.OPENAI_API_KEY,
            base_url=config.ANTHROPIC_BASE_URL,
            max_retries=config.MAX_RETRIES,
            timeout=config.REQUEST_TIMEOUT,
        )
    return _anthropic_client


def _complete_anthropic(role, system, user, *, model, user_id, playthrough_id,
                        max_tokens, temperature):
    """The SYSTEM instructions for a role are static (they're a module-level
    constant, not built per turn), so they're the one part of every prompt
    genuinely safe to cache: caching a block that changes every call would
    never hit and would just eat the write premium for nothing.

    No `temperature` is sent: current-generation Claude models (Opus 5,
    Sonnet 5, Haiku 4.5's siblings) dropped classic sampling params from the
    API in favour of adaptive generation, and the installed SDK no longer
    exposes the kwarg at all - passing it is a client-side TypeError, not a
    400 to catch and retry around."""
    import anthropic as anthropic_sdk

    client = _anthropic()
    system_blocks = [{"type": "text", "text": system,
                      "cache_control": {"type": "ephemeral"}}]

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max(64, max_tokens),
            system=system_blocks,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic_sdk.RateLimitError as e:
        raise LLMError(f"rate limited: {e}") from e
    except anthropic_sdk.AuthenticationError as e:
        raise LLMError(f"authentication failed: {e}") from e
    except anthropic_sdk.APIStatusError as e:
        raise LLMError(f"{e.status_code}: {str(e)[:200]}") from e
    except anthropic_sdk.APIConnectionError as e:
        raise LLMError(f"connection failed: {e}") from e

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise LLMError("empty completion")

    usage = response.usage
    _record(user_id, playthrough_id, role, model,
            in_tok=usage.input_tokens, out_tok=usage.output_tokens,
            cache_write_tok=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tok=getattr(usage, "cache_read_input_tokens", 0) or 0)
    return text


# ---------------------------------------------------------------------------
# OpenAI-compatible chat-completions path (non-Claude models)
# ---------------------------------------------------------------------------

def _complete_openai(role, system, user, *, model, user_id, playthrough_id,
                     max_tokens, temperature, json_mode):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_completion_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if temperature is not None:
        payload["temperature"] = temperature

    headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY or config.API_KEY}",
               "Content-Type": "application/json"}

    last = None
    for attempt in range(config.MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=config.REQUEST_TIMEOUT) as client:
                r = client.post(f"{config.BASE_URL}/chat/completions",
                                headers=headers, json=payload)
            if r.status_code == 400 and "temperature" in r.text and "temperature" in payload:
                payload.pop("temperature")          # some models pin temperature to 1
                continue
            if r.status_code == 400 and "max_completion_tokens" in r.text:
                payload["max_tokens"] = payload.pop("max_completion_tokens")
                continue
            if r.status_code >= 500 or r.status_code == 429:
                raise LLMError(f"{r.status_code}: {r.text[:200]}")
            if r.status_code >= 400:
                raise LLMError(f"{r.status_code}: {r.text[:300]}")
            data = r.json()
            text = (data["choices"][0]["message"].get("content") or "").strip()
            usage = data.get("usage") or {}
            _record(user_id, playthrough_id, role, model,
                    usage.get("prompt_tokens", _estimate_tokens(system + user)),
                    usage.get("completion_tokens", _estimate_tokens(text)))
            if not text:
                raise LLMError("empty completion")
            return text
        except (httpx.HTTPError, LLMError) as e:
            last = e
            if attempt < config.MAX_RETRIES:
                time.sleep(0.8 * (attempt + 1))
    raise LLMError(f"LLM call failed after retries: {last}")


def rng(*parts) -> random.Random:
    """Stable per-context randomness for the offline stub."""
    return random.Random("|".join(str(p) for p in parts))
