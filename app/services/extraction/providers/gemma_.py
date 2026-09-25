"""Gemma back-end, called through the same Google AI Studio API as Gemini.

This is not a production provider - it exists so `flask compare-gemma` can
run the exact same extraction prompt through Gemma and compare it against
Gemini, before any decision is made to self-host Gemma on our own VPS. The
SDK, endpoint and structured-output mechanism are all identical to Gemini's;
only the API key/model config and the label differ.

NOTE ON CONFIDENTIALITY: same as Gemini - this call still leaves our server
and goes to Google's cloud, so it is fine for synthetic test data only, never
real client documents. Self-hosting is the step that actually stops data
leaving our infrastructure; calling Gemma through this API does not.
"""
import json
import logging
import time

from flask import current_app

from ... import ai_usage

log = logging.getLogger(__name__)

LABEL = "Gemma"


def available() -> bool:
    return bool(current_app.config.get("GEMMA_API_KEY")) \
        and bool(current_app.config.get("GEMMA_MODEL"))


def model_name() -> str:
    return current_app.config.get("GEMMA_MODEL", "")


def _client():
    api_key = current_app.config.get("GEMMA_API_KEY")
    if not api_key:
        return None
    from google import genai
    return genai.Client(api_key=api_key)


def _to_contents(parts):
    """Neutral parts -> Gemini/Gemma content parts."""
    from google.genai import types

    contents = []
    for part in parts:
        kind = part["type"]
        if kind == "pdf":
            contents.append(types.Part.from_bytes(
                data=part["data"], mime_type="application/pdf"))
        elif kind == "image":
            contents.append(types.Part.from_bytes(
                data=part["data"], mime_type=part.get("mime", "image/png")))
        else:
            contents.append(types.Part.from_text(text=part["text"]))
    return contents


def structured_call(system, parts, schema_model, max_tokens=16000):
    """Return a validated instance of schema_model, or raise."""
    from google.genai import types

    client = _client()
    if client is None:
        raise RuntimeError("GEMMA_API_KEY or GEMMA_MODEL is not set")

    started = time.monotonic()
    try:
        response = client.models.generate_content(
            model=model_name(),
            contents=_to_contents(parts),
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema_model,
                max_output_tokens=max_tokens,
                temperature=0.0,
            ),
        )
    except Exception as exc:                       # noqa: BLE001
        ai_usage.record(provider="gemma", model=model_name(),
                        seconds=time.monotonic() - started, ok=False,
                        error=str(exc))
        raise
    ai_usage.record(provider="gemma", model=model_name(),
                    seconds=time.monotonic() - started,
                    **ai_usage.gemini_tokens(response))

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema_model):
        return parsed
    if isinstance(parsed, dict):
        return schema_model.model_validate(parsed)

    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemma returned an empty response")
    return schema_model.model_validate(json.loads(text))


def test_connection() -> dict:
    """Cheap round-trip to confirm the key and model name work."""
    try:
        client = _client()
        if client is None:
            return {"ok": False, "error": "GEMMA_API_KEY or GEMMA_MODEL is not set"}
        client.models.generate_content(
            model=model_name(), contents="Reply with the word OK.")
        return {"ok": True, "error": None, "model": model_name()}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": str(exc)}
