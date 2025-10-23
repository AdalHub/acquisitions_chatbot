import os
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Form
from twilio.rest import Client
from dotenv import load_dotenv

from data.store import init_db, save_event

# ------------------------------------------------------------------------------
# Bootstrapping: env + DB + Twilio client
# ------------------------------------------------------------------------------

load_dotenv()
init_db()  # create tables if they don't exist (events/leads/etc.)

# Required env vars (see .env). We default to "" to avoid KeyError, and rely on
# Twilio to throw a clear error if something's missing/malformed.
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_CALLER_ID   = os.environ.get("TWILIO_CALLER_ID", "")   # your Twilio number (E.164)
ACQ_LEAD_NUMBER    = os.environ.get("ACQ_LEAD_NUMBER", "")    # your acquisitions lead's phone
PUBLIC_BASE_URL    = os.environ.get("PUBLIC_BASE_URL", "")    # https://<ngrok>.ngrok-free.dev

# Twilio REST client used for outbound calls and warm transfer.
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ------------------------------------------------------------------------------
# FastAPI app + CORS (allow frontend to call these endpoints locally)
# ------------------------------------------------------------------------------

app = FastAPI(title="Vanessa: Twilio IVR Bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # relax for prototype; lock down in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------------------
# Healthcheck: simple GET so you can test local/public reachability quickly
# ------------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"ok": True}

# ------------------------------------------------------------------------------
# 1) REST: Start an outbound call
#
# - Frontend hits this with { "to": "+1..." }.
# - We tell Twilio to dial `to` and request TwiML from /twilio/voice/answer.
# - We also log the event to SQLite.
# ------------------------------------------------------------------------------

@app.post("/twilio/api/call/start")
async def start_call(payload: dict):
    """
    Body: { "to": "+1XXXXXXXXXX" }
    Returns: { "sid": "CA..." }
    """
    to = payload["to"]
    # Twilio will fetch TwiML from this URL as soon as the callee picks up.
    answer_url = f"{PUBLIC_BASE_URL}/twilio/voice/answer"

    call = client.calls.create(
        to=to,
        from_=TWILIO_CALLER_ID,   # must be a Twilio number on your account
        url=answer_url            # TwiML entrypoint for this call
    )

    save_event("OUTBOUND_INITIATED", {"to": to}, call.sid)
    return {"sid": call.sid}

# ------------------------------------------------------------------------------
# 2) TwiML: Answer the call and begin the IVR
#
# This is a *static* TwiML entrypoint (returned as XML) that:
#   - Greets the caller.
#   - Asks a single qualifying question ("Are you open to an offer?")
#   - Uses <Gather input="speech dtmf"> to capture "yes"/"no"/"later" or 1/2/3.
#   - Sends the result to /twilio/voice/qualify.
#
# Notes:
# - We purposely *do not* start a media stream here. Keeping it IVR-only makes
#   the demo deterministic and avoids bidirectional audio complexity right now.
# - We log "CALL_ANSWERED" in SQLite for your dashboard/metrics.
# ------------------------------------------------------------------------------

@app.post("/twilio/voice/answer", response_class=PlainTextResponse)
async def answer_call(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")
    save_event("CALL_ANSWERED", {"from": from_number}, call_sid)

    return """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">
    Hi, this is Vanessa calling about the home at your address.
    Quick question — are you open to a cash offer if the price and timing made sense?
  </Say>

  <!-- Collect either speech OR DTMF:
       - Say "yes" / "no" / "later", or
       - Press 1 (yes), 2 (later), 3 (no)
       The result posts to /twilio/voice/qualify -->
  <Gather input="speech dtmf"
          action="/twilio/voice/qualify"
          method="POST"
          timeout="6"
          speechTimeout="auto"
          numDigits="1">
    <Say>
      You can say yes, no, or later.
      Or press 1 for yes, 2 for later, 3 for no.
    </Say>
  </Gather>

  <!-- If nothing captured (no input / long silence), end politely -->
  <Say>Sorry, I didn’t catch that. We’ll try again another time. Goodbye.</Say>
  <Hangup/>
</Response>"""

# ------------------------------------------------------------------------------
# 3) TwiML: First branch handler (intent detection)
#
# - Receives either:
#     - SpeechResult: freeform text recognized by Twilio ASR
#     - Digits:       "1"/"2"/"3" if the caller pressed a key
# - We bucket to three outcomes:
#     A) DNC / Not interested → say goodbye and hang up
#     B) Callback later       → acknowledge and hang up
#     C) Qualified (yes/maybe)→ ask two follow-ups, then warm transfer
#
# - All decisions are logged with save_event(...) for your dashboard.
# ------------------------------------------------------------------------------

@app.post("/twilio/voice/qualify", response_class=PlainTextResponse)
async def twilio_qualify(
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    SpeechResult: str = Form(default=""),
    Digits: str = Form(default="")
):
    text = (SpeechResult or "").lower().strip()
    d = (Digits or "").strip()

    def twiml(body: str) -> str:
        """Helper to wrap a TwiML fragment with <Response>...</Response>."""
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'

    # Map DTMF overrides to text intent if present
    if d == "1":
        text = "yes"
    elif d == "2":
        text = "later"
    elif d == "3":
        text = "no"

    # --- A) DNC / Not interested --------------------------------------------
    if any(k in text for k in ["no", "not interested", "remove", "do not call", "stop", "wrong number"]):
        save_event("INTENT_DNC", {"from": From, "speech": text}, CallSid)
        return twiml("""
          <Say>Totally understood. We’ll remove you from our list. Have a great day.</Say>
          <Hangup/>
        """)

    # --- B) Callback later ----------------------------------------------------
    if any(k in text for k in ["later", "busy", "another time", "call back", "tomorrow"]):
        # In a production app, you would parse/normalize the requested window.
        save_event("INTENT_LATER", {"from": From, "speech": text, "window": "today 4 to 6 PM"}, CallSid)
        return twiml("""
          <Say>No problem. I’ll note a call back for later today between 4 and 6 PM. Thank you!</Say>
          <Hangup/>
        """)

    # --- C) Qualified (yes/maybe/depends) ------------------------------------
    if any(k in text for k in ["yes", "maybe", "thinking", "depends", "sure"]):
        save_event("INTENT_QUALIFIED", {"from": From, "speech": text}, CallSid)
        # Ask follow-up #1 (price). On timeout/no-input we Redirect to same handler.
        return twiml("""
          <Gather input="speech dtmf" action="/twilio/voice/followup1" method="POST" timeout="6" speechTimeout="auto">
            <Say>Great. Roughly what price range would you consider for the property?</Say>
          </Gather>
          <Say>Thanks. One moment.</Say>
          <Redirect method="POST">/twilio/voice/followup1</Redirect>
        """)

    # Fallback: one more try, then end
    return twiml("""
      <Gather input="speech dtmf" action="/twilio/voice/qualify" method="POST" timeout="6" speechTimeout="auto" numDigits="1">
        <Say>Please say yes, no, or later. Or press 1 for yes, 2 for later, 3 for no.</Say>
      </Gather>
      <Say>Thanks for your time. Goodbye.</Say>
      <Hangup/>
    """)

# ------------------------------------------------------------------------------
# 4) TwiML: Follow-up #1 (price)
#
# - Captures a freeform price range (e.g., "350 to 380k").
# - Logs it, then asks follow-up #2 (timing).
# ------------------------------------------------------------------------------

@app.post("/twilio/voice/followup1", response_class=PlainTextResponse)
async def twilio_followup1(
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    SpeechResult: str = Form(default="")
):
    price = (SpeechResult or "").strip()
    save_event("PRICE_RANGE", {"from": From, "price": price}, CallSid)

    return """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="speech dtmf" action="/twilio/voice/followup2" method="POST" timeout="6" speechTimeout="auto">
    <Say>Got it. And what timing are you thinking about — in the next 30 to 60 days, or later?</Say>
  </Gather>
  <Redirect method="POST">/twilio/voice/followup2</Redirect>
</Response>"""

# ------------------------------------------------------------------------------
# 5) TwiML: Follow-up #2 (timing) → warm transfer
#
# - Captures timing (“30–60 days”, “ASAP”, etc.), logs it, then <Dial>s your
#   acquisitions lead for a warm transfer.
# - Caller ID uses your TWILIO_CALLER_ID so your lead recognizes the call.
# ------------------------------------------------------------------------------

@app.post("/twilio/voice/followup2", response_class=PlainTextResponse)
async def twilio_followup2(
    CallSid: str = Form(default=""),
    From: str = Form(default=""),
    SpeechResult: str = Form(default="")
):
    timing = (SpeechResult or "").strip()
    save_event("TIMING", {"from": From, "timing": timing}, CallSid)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Thank you. Let me connect you with my acquisitions lead now to discuss numbers.</Say>
  <Dial callerId="{TWILIO_CALLER_ID}">{ACQ_LEAD_NUMBER}</Dial>
</Response>"""

# ------------------------------------------------------------------------------
# 6) TwiML: Transfer endpoint (kept separate for clarity/reuse)
#
# - This is useful if you need to redirect an *already running* call to a new
#   TwiML URL (e.g., from API/LLM tool calls). For the current IVR flow,
#   /followup2 dials directly, but we keep /transfer for parity with earlier
#   designs and quick future reuse.
# ------------------------------------------------------------------------------

@app.post("/twilio/voice/transfer", response_class=PlainTextResponse)
async def transfer_twiml():
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Connecting you now.</Say>
  <Dial callerId="{TWILIO_CALLER_ID}">{ACQ_LEAD_NUMBER}</Dial>
</Response>"""
