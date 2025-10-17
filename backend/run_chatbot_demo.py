import os
from dotenv import load_dotenv
from data.store import init_db
from transport.twilio_placeholder import simulate_conversation

load_dotenv()
init_db()

DEMO_CASES = {
    "dnc": [
        "hello?",
        "i'm not selling, please remove me from your list",
    ],
    "later": [
        "hi, who is this?",
        "call me later today after 5pm, i'm at work",
    ],
    "qualified": [
        "yeah i might consider an offer",
        "probably around 370k if it's straightforward",
        "timeline maybe 30 to 45 days, no big repairs",
    ],
    "timeout_no_interest": [
        "who is this?",
        "can't talk now",
        "not interested",
        "no thanks",
        "i don't have time",
        "what company is this?",
        "why are you calling?",
        "i'm not selling anything",
        "no",
        "goodbye"
    ]

}

def main():
    print("=== Vanessa Chatbot Demo ===")
    for label, turns in DEMO_CASES.items():
        phone = "+1555" + str(abs(hash(label)) % 1_000_000).zfill(6)
        print(f"\n--- CASE: {label} ({phone}) ---")
        results = simulate_conversation(phone, turns, seconds_per_turn=15)  # 7 turns ~105s
        for step, res in enumerate(results, 1):
            print(f"\nStep {step}:")
            print(" outcome:", res.get("outcome"))
            print(" lead:", res.get("lead"))
            print(" analysis:", res.get("analysis"))
            print(" elapsed:", res.get("elapsed_sec"))

if __name__ == "__main__":
    main()
