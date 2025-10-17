# backend/transport/twilio_quart.py
import os
import json
from quart import Blueprint, request, Response, websocket
from twilio.rest import Client
from data.store import save_event
from llm.realtime_openai import OpenAIRealtimeBridge

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_CALLER_ID   = os.environ.get("TWILIO_CALLER_ID")
ACQ_LEAD_NUMBER    = os.environ.get("ACQ_LEAD_NUMBER")
PUBLIC_BASE_URL    = os.environ.get("PUBLIC_BASE_URL")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

twilio_bp = Blueprint("twilio", __name__)

# --- REST: start outbound call ---
@twilio_bp.post("/api/call/start")
async def start_call():
    data = await request.get_json()
    to = data["to"]
    url = f"{PUBLIC_BASE_URL}/twilio/voice/answer"
    call = client.calls.create(to=to, from_=TWILIO_CALLER_ID, url=url)
    save_event("OUTBOUND_INITIATED", {"to": to}, call.sid)
    return {"sid": call.sid}

# --- TwiML: answer, begin Media Stream ---
@twilio_bp.post("/voice/answer")
async def answer_call():
    form = await request.form
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")
    ws_url = PUBLIC_BASE_URL.replace("https", "wss") + "/twilio/stream/media"

    save_event("CALL_ANSWERED", {"from": from_number}, call_sid)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Hi, this is Vanessa.</Say>
  <Start>
    <Stream url="{ws_url}" />
  </Start>
</Response>"""
    return Response(twiml, mimetype="text/xml")

# --- WebSocket: Twilio Media Stream ---
@twilio_bp.websocket("/stream/media", subprotocols=["twilio", "audio"])
async def media_stream():
    """
    Twilio will connect with Sec-WebSocket-Protocol 'twilio' or 'audio'.
    Quart will echo a supported one automatically because we list them here.
    """
    call_sid = None
    phone = None
    bridge = None

    async def do_transfer(call_sid_value: str):
        # Redirect live call to transfer TwiML
        client.calls(call_sid_value).update(
            url=f"{PUBLIC_BASE_URL}/twilio/voice/transfer",
            method="POST"
        )

    async def do_callback(window: str, notes: str = ""):
        # DB side-effects handled inside the bridge on tool calls.
        pass

    try:
        while True:
            msg_text = await websocket.receive()
            if msg_text is None:
                break

            msg = json.loads(msg_text)
            if msg.get("event") == "start":
                start_info = msg["start"]
                call_sid = start_info.get("callSid") or start_info.get("callSid".capitalize()) or start_info.get("callSid".upper()) or start_info.get("callSid")
                # Twilio's casing is "callSid" in JSON
                phone = start_info.get("from", "")

                save_event("STREAM_START", {"from": phone}, call_sid or "")

                bridge = OpenAIRealtimeBridge(
                    call_sid=call_sid or "",
                    phone=phone or "",
                    on_transfer=do_transfer,
                    on_callback=do_callback
                )
                await bridge.start()

            if bridge:
                await bridge.handle_twilio_event(msg_text)

    except Exception as e:
        save_event("STREAM_ERROR", {"error": str(e)}, call_sid or "")

    finally:
        if bridge:
            await bridge.close()
        save_event("STREAM_CLOSED", {}, call_sid or "")

# --- TwiML: warm transfer ---
@twilio_bp.post("/voice/transfer")
async def transfer_twiml():
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Connecting you now.</Say>
  <Dial callerId="{TWILIO_CALLER_ID}">{ACQ_LEAD_NUMBER}</Dial>
</Response>"""
    return Response(twiml, mimetype="text/xml")
