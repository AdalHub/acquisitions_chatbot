from typing import List
from llm.vanessa import VanessaBrain

def simulate_conversation(phone: str, utterances: List[str], seconds_per_turn: int = 8):
    brain = VanessaBrain(phone=phone, call_sid="SIM-"+phone[-4:])
    results = []
    for text in utterances:
        res = brain.ingest_user_text(text, approx_seconds=seconds_per_turn)
        results.append(res)
        if res.get("outcome",{}).get("type") in ("dnc","callback","transfer"):
            break
    return results
