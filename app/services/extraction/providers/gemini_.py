"""Google Gemini back-end (Google AI Studio).

Gemini reads PDFs and images directly, so like the Claude back-end there is
no separate OCR step.

Structured output is requested with `response_mime_type="application/json"`
plus a `response_schema`, and the SDK returns a parsed object on
`response.parsed`. We re-validate through Pydantic ourselves regardless, so a
provider that returns a dict rather than a model instance is handled the same
way.

NOTE ON CONFIDENTIALITY: Google AI Studio's free tier may use submitted
content to improve their products; paid tiers do not. Fine for synthetic test
data, not appropriate for real client documents. See SETUP.md.
"""
import json
import logging

from flask import current_app

log = logging.getLogger(__name__)

LABEL = "Gemini"


def available() -> bool:
    return bool(current_app.config.get("GEMINI_API_KEY"))


def model_name() -> str:
    return current_app.config.get("GEMINI_MODEL", "gemini-3.6-flash")


def _client():
    api_key = current_app.config.get("GEMINI_API_KEY")
    if not api_key:
        return None
    from google import genai
    return genai.Client(api_key=api_key)


def _to_contents(parts):
    """Neutral parts -> Gemini content parts."""
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
        raise RuntimeError("GEMINI_API_KEY is not set")

    response = client.models.generate_content(
        model=model_name(),
        contents=_to_contents(parts),
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema_model,
            max_output_tokens=max_tokens,
            # Extraction is a reading task, not a creative one.
            temperature=0.0,
        ),
    )

    # Prefer the SDK's own parsed object; fall back to parsing the JSON text.
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema_model):
        return parsed
    if isinstance(parsed, dict):
        return schema_model.model_validate(parsed)

    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    return schema_model.model_validate(json.loads(text))


def test_connection() -> dict:
    """Cheap round-trip to confirm the key works."""
    try:
        client = _client()
        if client is None:
            return {"ok": False, "error": "GEMINI_API_KEY is not set"}
        client.models.generate_content(
            model=model_name(), contents="Reply with the word OK.")
        return {"ok": True, "error": None, "model": model_name()}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": str(exc)}
