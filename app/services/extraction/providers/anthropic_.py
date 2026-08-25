"""Anthropic (Claude) back-end.

Claude reads PDFs natively as `document` blocks - including scanned ones -
so there is no separate OCR step.
"""
import base64
import json
import logging

from flask import current_app

log = logging.getLogger(__name__)

LABEL = "Claude"


def available() -> bool:
    return bool(current_app.config.get("ANTHROPIC_API_KEY"))


def model_name() -> str:
    return current_app.config.get("ANTHROPIC_MODEL", "claude-opus-5")


def _client():
    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def _to_content(parts):
    """Neutral parts -> Anthropic content blocks."""
    content = []
    for part in parts:
        kind = part["type"]
        if kind == "pdf":
            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(part["data"]).decode(),
                },
            })
        elif kind == "image":
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.get("mime", "image/png"),
                    "data": base64.standard_b64encode(part["data"]).decode(),
                },
            })
        else:
            content.append({"type": "text", "text": part["text"]})
    return content


def structured_call(system, parts, schema_model, max_tokens=16000):
    """Return a validated instance of schema_model, or raise."""
    client = _client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    messages = [{"role": "user", "content": _to_content(parts)}]

    # messages.parse() validates the response against the schema for us.
    # Older SDK releases lack it, so fall back to an explicit json_schema.
    if hasattr(client.messages, "parse"):
        response = client.messages.parse(
            model=model_name(),
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_format=schema_model,
        )
        return response.parsed_output

    schema = schema_model.model_json_schema()
    schema["additionalProperties"] = False
    response = client.messages.create(
        model=model_name(),
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    text = next(b.text for b in response.content if b.type == "text")
    return schema_model.model_validate(json.loads(text))


def test_connection() -> dict:
    """Cheap round-trip to confirm the key works."""
    try:
        client = _client()
        if client is None:
            return {"ok": False, "error": "ANTHROPIC_API_KEY is not set"}
        client.messages.create(
            model=model_name(), max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the word OK."}],
        )
        return {"ok": True, "error": None, "model": model_name()}
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "error": str(exc)}
