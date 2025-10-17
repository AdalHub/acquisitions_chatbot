# backend/transport/twilio_interface.py
import os
import json
from functools import partial
from flask import Blueprint, request, jsonify, Response
from flask_sock import Sock
from twilio.rest import Client

from data.store import save_event
from llm.realtime_openai import OpenAIRealtimeBridge

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_CALLER_ID   = os.environ.get("TWILIO_CALLER_ID")
ACQ_LEAD_NUMBER    = os.environ.get("ACQ_LEAD_NUMBER")
PUBLIC_BASE_URL    = os.environ.get("PUBLIC_BASE_URL")     # e.g., https://xxxx.ngrok.io

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

twilio_bp = Blueprint("twilio", __name__)
sock = Sock()   # initialized in init_app()

def init_app(app):
    sock.init_app(app)

# === REST: start outbound call ===
@twilio_bp.post("/api/call/start")
def start_call():
    data = request.get_json(force=True)
    to = data["to"]
    url = f"{PUBLIC_BASE_URL}/twilio/voice/answer"
    call = client.calls.create(to=to, from_=TWILIO_CALLER_ID, url=url)
    save_event("OUTBOUND_INITIATED", {"to": to}, call.sid)
    return jsonify({"sid": call.sid})

# === TwiML: answer and start Media Stream ===
@twilio_bp.post("/voice/answer")
def answer_call():
    """
    Returns TwiML that starts a bidirectional media stream to our WS endpoint.
    """
    form = request.form or {}
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")
    ws_url = PUBLIC_BASE_URL.replace("https", "wss") + "/twilio/stream/media"

    save_event("CALL_ANSWERED", {"from": from_number}, call_sid)

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Hi, this is Vanessa.</Say>
  <Start>
    <Stream url="{ws_url}" />
  </Start>
</Response>"""
    return Response(twiml, mimetype="text/xml")

# === WebSocket: Twilio Media Stream ===
@sock.route("/twilio/stream/media", subprotocols=["twilio"])
async def media_stream(ws):
    """
    Receives Twilio media stream messages; bridges to OpenAI Realtime.
    We derive call_sid and caller phone from the initial 'start' frame.
    """
    call_sid = None
    phone = None

    # we lazily construct the bridge after we see the 'start' frame for caller info
    bridge = None

    async def do_transfer(call_sid_value: str):
        # Redirect the live call to transfer TwiML
        client.calls(call_sid_value).update(url=f"{PUBLIC_BASE_URL}/twilio/voice/transfer", method="POST")

    async def do_callback(window: str, notes: str = ""):
        # No live action needed; DB already persisted by the bridge tool handler.
        pass

    try:
        while True:
            msg_text = await ws.receive()
            if msg_text is None:
                break
            msg = json.loads(msg_text)
            if msg.get("event") == "start":
                call_sid = msg["start"]["callSid"]
                phone = msg["start"].get("from", "")
                save_event("STREAM_START", {"from": phone}, call_sid)

                bridge = OpenAIRealtimeBridge(
                    call_sid=call_sid,
                    phone=phone,
                    on_transfer=do_transfer,
                    on_callback=do_callback
                )
                await bridge.start()

            # Forward events to the bridge
            if bridge:
                await bridge.handle_twilio_event(msg_text)

    except Exception as e:
        save_event("STREAM_ERROR", {"error": str(e)}, call_sid or "")

    finally:
        if bridge:
            await bridge.close()
        save_event("STREAM_CLOSED", {}, call_sid or "")

# === TwiML: warm transfer to acquisitions lead ===
@twilio_bp.post("/voice/transfer")
def transfer_twiml():
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Connecting you now.</Say>
  <Dial callerId="{TWILIO_CALLER_ID}">{ACQ_LEAD_NUMBER}</Dial>
</Response>"""
    return Response(twiml, mimetype="text/xml")
