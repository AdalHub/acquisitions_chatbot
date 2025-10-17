import json
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from data.store import save_event, upsert_lead, mark_qualified, create_callback, find_lead_by_phone
from .prompt import SYSTEM_PROMPT
from .openai_client import call_llm_text

@dataclass
class ConversationState:
    phone: str
    call_sid: str = ""
    history: List[Dict[str, str]] = field(default_factory=list)  # [{role, content}]
    elapsed_sec: int = 0
    closed: bool = False
    outcome: Optional[str] = None         # transfer | callback | dnc | no_interest | None
    last_response_id: Optional[str] = None

class VanessaBrain:
    def __init__(self, phone: str, call_sid: str = ""):
        self.state = ConversationState(phone=phone, call_sid=call_sid)
        upsert_lead(phone)

    def _lead_snapshot(self) -> Dict[str, Any]:
        lead = find_lead_by_phone(self.state.phone)
        if not lead:
            return {}
        return {
            "id": lead.id, "phone": lead.phone, "interest": lead.interest,
            "price_range": lead.price_range, "timing": lead.timing,
            "condition": lead.condition, "owner_status": lead.owner_status,
            "qualified": lead.qualified
        }

    def _update_lead(self, analysis: Dict[str, Any]):
        upsert_lead(
            self.state.phone,
            interest=analysis.get("interest",""),
            price_range=analysis.get("price_range",""),
            timing=analysis.get("timing",""),
            condition=analysis.get("condition",""),
            owner_status=analysis.get("owner_status","")
        )

    def _append_user(self, text: str):
        self.state.history.append({"role":"user", "content": text})
        save_event("TURN", {"from":"user","text":text,"elapsed":self.state.elapsed_sec}, self.state.call_sid)

    def _decide_and_apply(self, analysis: Dict[str, Any], lead_id: int) -> Optional[Dict[str, Any]]:
        interest = (analysis.get("interest") or "").lower().strip()
        price_ok = bool((analysis.get("price_range") or "").strip())
        time_ok  = bool((analysis.get("timing") or "").strip())

        if interest in ("dnc", "no"):
            save_event("OUTCOME_DNC", {"lead_id": lead_id, "reason": analysis.get("notes","")}, self.state.call_sid)
            return {"type":"dnc", "message":"Understood—removing you from our list. Have a great day."}

        if interest == "later":
            cb = create_callback(lead_id, analysis.get("callback_window","next business day"), analysis.get("notes",""))
            save_event("OUTCOME_CALLBACK", {"lead_id": lead_id, "callback_id": cb.id, "window": cb.window}, self.state.call_sid)
            return {"type":"callback", "callback_id": cb.id, "window": cb.window, "message":"We’ll call you back then. Thank you!"}

        if interest in ("yes","maybe") and (price_ok or time_ok or self.state.elapsed_sec >= 90):
            mark_qualified(lead_id, True)
            save_event("OUTCOME_TRANSFER", {"lead_id": lead_id}, self.state.call_sid)
            return {"type":"transfer", "message":"Let me connect you with my acquisitions lead now for numbers."}

        if self.state.elapsed_sec >= 90:
            save_event("OUTCOME_DNC", {"lead_id": lead_id, "reason":"no_clear_interest_within_timebox"}, self.state.call_sid)
            return {"type":"dnc", "message":"Thanks for your time—no worries, we’ll remove you from our list."}

        return None

    def ingest_user_text(self, text: str, approx_seconds: int = 8) -> Dict[str, Any]:
        if self.state.closed:
            return {"status":"closed", "outcome": self.state.outcome}

        self._append_user(text)
        self.state.elapsed_sec += approx_seconds

        output_text, resp_id = call_llm_text(
            messages=self.state.history[-6:],
            instructions=SYSTEM_PROMPT,
            previous_response_id=self.state.last_response_id
        )
        self.state.last_response_id = resp_id
        save_event("RAW_LLM", {"text": output_text, "response_id": resp_id}, self.state.call_sid)

        try:
            analysis = json.loads(output_text.strip())
            if not isinstance(analysis, dict):
                raise ValueError("not a JSON object")
        except Exception:
            analysis = {"interest":"unknown","price_range":"","timing":"","condition":"","owner_status":"unknown","callback_window":"","notes":""}

        self._update_lead(analysis)
        lead_info = self._lead_snapshot()
        lead_id = lead_info.get("id")

        outcome = self._decide_and_apply(analysis, lead_id) if lead_id else None
        if outcome:
            self.state.outcome = outcome["type"]
            self.state.closed = True
            # NEW: refresh the snapshot so 'qualified' reflects any changes
            lead_info = self._lead_snapshot()

        return {
            "lead": lead_info,
            "analysis": analysis,
            "outcome": outcome or {"type":"continue"},
            "elapsed_sec": self.state.elapsed_sec
        }

