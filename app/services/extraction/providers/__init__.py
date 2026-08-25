"""AI provider back-ends.

Each provider exposes the same two functions, so the rest of the application
never learns which engine actually read a document:

    available() -> bool
    structured_call(system, parts, schema_model, max_tokens) -> schema_model

`parts` is a provider-neutral list describing what to send:

    {"type": "pdf",   "data": b"..."}
    {"type": "image", "data": b"...", "mime": "image/png"}
    {"type": "text",  "text": "..."}

Each back-end converts that into its own SDK's format. Adding a third
provider means writing one module here and nothing else.
"""
from flask import current_app


def get_provider(name=None):
    """Return the configured provider module."""
    name = (name or current_app.config.get("AI_PROVIDER", "anthropic")).lower()

    if name == "gemini":
        from . import gemini_ as provider
    elif name == "anthropic":
        from . import anthropic_ as provider
    else:
        raise ValueError(
            f"Unknown AI_PROVIDER {name!r} - expected 'anthropic' or 'gemini'")

    return provider


def provider_name() -> str:
    return (current_app.config.get("AI_PROVIDER") or "anthropic").lower()
