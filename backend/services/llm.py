"""Provider-agnostic LLM access, organized in two task tiers.

Every AI generation in Rovyn goes through generate_json() with a tier, so the
cost/latency/quality trade-off lives in one place instead of eleven call sites:

- "fast"     — small, schema-bound generations: nearby-city ideas, flight and
               transport menus, category gap-fills, per-option detail sheets.
- "standard" — the generations that carry the product: destination recs, the
               route, the stay/visits/events menus, the final-plan weave,
               revisions, and trip adaptations.

Each tier resolves its model from the environment as "provider/model":

    LLM_FAST=anthropic/claude-haiku-4-5
    LLM_STANDARD=anthropic/claude-sonnet-5
    LLM_FAST=gemini/gemini-2.5-flash        # requires GEMINI_API_KEY

Anthropic is the primary provider. The Gemini provider speaks the public
generateContent REST API through the stdlib (no new dependency) and, if a call
fails for any reason, falls back to the tier's Anthropic default when an
Anthropic key exists — a bad Gemini key can never take a trip down. When only
GEMINI_API_KEY is configured, both tiers default to Gemini models so the app
remains fully usable without an Anthropic key.
"""

import json
import os
import urllib.error
import urllib.request

TIERS = ("fast", "standard")

_DEFAULTS = {
    "anthropic": {"fast": "claude-haiku-4-5", "standard": "claude-sonnet-5"},
    "gemini": {"fast": "gemini-2.5-flash", "standard": "gemini-2.5-pro"},
}

# Models where an explicit thinking={"type": "disabled"} is accepted — used as
# the empty-completion retry for models that think adaptively by default.
_THINKING_OFF_OK = ("claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7")

_anthropic_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        _anthropic_client = Anthropic()
    return _anthropic_client


def _has_anthropic() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _has_gemini() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def available() -> bool:
    """Whether any AI provider is configured (sample mode otherwise)."""
    return _has_anthropic() or _has_gemini()


def resolve(tier: str) -> tuple[str, str]:
    """(provider, model) for a tier. Env spec wins; otherwise Anthropic
    defaults, or Gemini defaults when Gemini holds the only key."""
    default_provider = "anthropic" if _has_anthropic() else "gemini"
    spec = os.environ.get(f"LLM_{tier.upper()}", "").strip()
    if spec:
        provider, _, model = spec.partition("/")
        provider = provider.strip().lower()
        if provider in _DEFAULTS:
            return provider, (model.strip() or _DEFAULTS[provider][tier])
    return default_provider, _DEFAULTS[default_provider][tier]


def parse_json_reply(text: str):
    """Extract the JSON payload from a model reply, tolerating code fences and
    surrounding prose. Tries the shape the reply starts with first, so an
    object containing arrays parses as the object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    pairs = (("{", "}"), ("[", "]")) if text[:1] == "{" else (("[", "]"), ("{", "}"))
    for opener, closer in pairs:
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return json.loads(text)


def generate_json(prompt: str, tier: str = "standard", max_tokens: int = 8192):
    """One JSON generation on the tier's model. Raises on total failure —
    callers keep their own sample-data fallbacks."""
    provider, model = resolve(tier)
    if provider == "gemini":
        try:
            return parse_json_reply(_gemini_text(model, prompt, max_tokens))
        except Exception:
            if not _has_anthropic():
                raise
            # Gemini misbehaved — serve the request on the Anthropic default
            # for this tier rather than surfacing a provider hiccup to the trip.
            _, model = "anthropic", _DEFAULTS["anthropic"][tier]
    return parse_json_reply(_anthropic_text(model, prompt, max_tokens))


def _anthropic_text(model: str, prompt: str, max_tokens: int) -> str:
    """Streamed request with one connection-error retry.

    Streaming matters: long generations leave a non-streaming HTTP connection
    idle and idle connections get dropped. The thinking parameter is omitted —
    models that think adaptively by default (Sonnet 5) still do, and models
    that don't (Haiku, Opus without the param) answer directly, which is what
    the schema-bound prompts want. The retry turns thinking off explicitly for
    the rare run where the whole token budget went to thinking.
    """
    from anthropic import APIConnectionError, APITimeoutError

    attempts = [None]
    if model.startswith(_THINKING_OFF_OK):
        attempts.append({"type": "disabled"})
    last_err = None
    for thinking in attempts:
        try:
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            if thinking:
                kwargs["thinking"] = thinking
            with _get_anthropic().messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
            text = next((b.text for b in message.content if b.type == "text"), "")
            if not text.strip():
                last_err = RuntimeError("The model returned an empty completion (token budget spent on thinking).")
                continue
            return text
        except (APIConnectionError, APITimeoutError) as exc:
            last_err = exc
    raise last_err


def _gemini_text(model: str, prompt: str, max_tokens: int) -> str:
    """generateContent over raw HTTP. responseMimeType pins the reply to JSON,
    so parse_json_reply sees clean payloads."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={os.environ['GEMINI_API_KEY']}")
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode())
    parts = payload["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        raise RuntimeError("Gemini returned an empty completion.")
    return text
