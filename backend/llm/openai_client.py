import os
from typing import List, Dict, Any
from openai import OpenAI

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
MODEL = os.environ.get("OPENAI_RESPONSES_MODEL", "gpt-5")

_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def llm_available() -> bool:
    return _client is not None

def _fallback(messages: List[Dict[str, str]]) -> str:
    latest = (messages[-1]["content"] if messages else "").lower()
    if any(x in latest for x in ["remove", "do not call", "not selling", "stop calling"]):
        return '{"interest":"dnc","price_range":"","timing":"","condition":"","owner_status":"unknown","callback_window":"","notes":""}'
    if any(x in latest for x in ["later", "busy", "another time", "tomorrow"]):
        return '{"interest":"later","callback_window":"today 4-6pm","price_range":"","timing":"","condition":"","owner_status":"unknown","notes":""}'
    if any(x in latest for x in ["yes", "maybe", "might sell", "consider"]):
        return '{"interest":"maybe","price_range":"350-380k","timing":"30-60 days","condition":"needs paint","owner_status":"owner","callback_window":"","notes":""}'
    return '{"interest":"unknown","price_range":"","timing":"","condition":"","owner_status":"unknown","callback_window":"","notes":""}'

def call_llm_text(
    messages: List[Dict[str, str]],
    instructions: str,
    previous_response_id: str | None = None
) -> tuple[str, str | None]:
    """
    Returns (output_text, response_id)
    Uses Responses API with `instructions` and optional `previous_response_id`.
    """
    if not llm_available():
        return _fallback(messages), None

    kwargs: Dict[str, Any] = {
        "model": MODEL,
        "input": messages,
        "instructions": instructions,
        "temperature": 0.3,
        "store": False,
    }
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id

    resp = _client.responses.create(**kwargs)
    return resp.output_text or "", resp.id
