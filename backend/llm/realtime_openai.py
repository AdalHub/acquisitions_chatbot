# backend/llm/realtime_openai.py
import os
import json
import asyncio
import websockets

from data.store import save_event, upsert_lead, mark_qualified, create_callback

OPENAI_REALTIME_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
OPENAI_REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

SYSTEM_PROMPT = """
You are Vanessa, a warm, upbeat acquisitions assistant calling single-family homeowners.
Within 90–120 seconds, determine seller intent and gather key details:
- interest: yes | maybe | later | no | dnc
- price_range, timing, condition, owner_status (owner | tenant | relative | agent | unknown)

Tools you can call:
- lead_detect(...)  -> emit current fields {interest, price_range, timing, condition, owner_status, callback_window?, notes?}
- request_transfer(consent: boolean) -> when caller agrees to connect to acquisitions lead.

Rules:
- If they ask removal or clearly not selling => interest="dnc", end politely.
- If later, suggest a concise callback window.
- Be concise and human; keep call under 180s unless transferring.
"""

class OpenAIRealtimeBridge:
    """
    Bridges Twilio Media Stream <-> OpenAI Realtime.
    Tracks the current Twilio Call SID to persist events and initiate transfer.
    """
    def __init__(self, call_sid: str, phone: str, on_transfer, on_callback):
        self.call_sid = call_sid
        self.phone = phone
        self.on_transfer = on_transfer      # callback(call_sid)
        self.on_callback = on_callback      # callback(window, notes)
        self.ai_ws = None
        self.qualified = False

    async def start(self):
        headers = [("Authorization", f"Bearer {OPENAI_API_KEY}")]
        self.ai_ws = await websockets.connect(OPENAI_REALTIME_URL, extra_headers=headers)

        # Configure the session: instructions + audio formats + tools
        await self.ai_ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "instructions": SYSTEM_PROMPT,
                "input_audio_format": {"type": "g711_ulaw", "sampling_rate_hz": 8000},
                "output_audio_format": {"type": "g711_ulaw", "sampling_rate_hz": 8000},
                "tools": [
                    {
                        "type": "function",
                        "name": "lead_detect",
                        "description": "Capture current seller intent and fields.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "interest": {"type": "string", "enum": ["yes","maybe","later","no","dnc","unknown"]},
                                "price_range": {"type":"string"},
                                "timing": {"type":"string"},
                                "condition": {"type":"string"},
                                "owner_status": {"type":"string"},
                                "callback_window": {"type":"string"},
                                "notes": {"type":"string"}
                            },
                            "required": ["interest"]
                        }
                    },
                    {
                        "type":"function",
                        "name":"request_transfer",
                        "description":"Request warm transfer to acquisitions lead.",
                        "parameters":{"type":"object","properties":{"consent":{"type":"boolean"}},"required":["consent"]}
                    }
                ]
            }
        }))

    async def handle_twilio_event(self, msg_text: str):
        """
        Twilio sends {"event":"start"|"media"|"stop"|...}
        media.payload is base64 g711-ulaw
        """
        msg = json.loads(msg_text)
        event = msg.get("event")

        if event == "start":
            save_event("CALL_START", {"info": msg.get("start", {})}, self.call_sid)
            # init input buffer
            await self.ai_ws.send(json.dumps({"type":"input_audio_buffer.create"}))

        elif event == "media":
            # pipe audio → OpenAI
            await self.ai_ws.send(json.dumps({
                "type":"input_audio_buffer.append",
                "audio": msg["media"]["payload"]
            }))
            # prompt model to respond (text+audio)
            await self.ai_ws.send(json.dumps({
                "type":"response.create",
                "response":{"modalities":["audio","text"]}
            }))
            await self._drain_ai_events()

        elif event == "stop":
            await self.ai_ws.send(json.dumps({"type":"input_audio_buffer.commit"}))
            await self._drain_ai_events(final=True)
            save_event("CALL_STOP", {"info": msg.get("stop", {})}, self.call_sid)

    async def _drain_ai_events(self, final=False):
        """
        Listen for OpenAI events: transcripts, tool calls, etc.
        """
        try:
            while True:
                raw = await asyncio.wait_for(self.ai_ws.recv(), timeout=0.02 if not final else 0.5)
                ev = json.loads(raw)
                t = ev.get("type")

                if t == "response.output_text.delta":
                    # Optional: stream partial text to dashboard
                    pass

                elif t == "response.function_call":
                    fn = ev["name"]
                    args = ev.get("arguments", {})
                    save_event("AI_TOOL_CALL", {"name": fn, "args": args}, self.call_sid)

                    if fn == "lead_detect":
                        # persist fields to DB
                        upsert_lead(
                            self.phone,
                            interest=args.get("interest",""),
                            price_range=args.get("price_range",""),
                            timing=args.get("timing",""),
                            condition=args.get("condition",""),
                            owner_status=args.get("owner_status","")
                        )
                        if args.get("interest") in ("yes","maybe"):
                            self.qualified = True
                            mark_qualified(upsert_lead(self.phone).id, True)
                        if args.get("interest") == "later" and args.get("callback_window"):
                            create_callback(upsert_lead(self.phone).id, args["callback_window"], args.get("notes",""))
                            save_event("OUTCOME_CALLBACK", {"window": args["callback_window"]}, self.call_sid)

                    elif fn == "request_transfer":
                        if args.get("consent") and self.qualified:
                            save_event("OUTCOME_TRANSFER", {"consent": True}, self.call_sid)
                            await self.on_transfer(self.call_sid)

                elif t == "response.completed":
                    # end of this response cycle
                    break

                elif t == "response.error":
                    save_event("AI_ERROR", {"error": ev}, self.call_sid)
                    break

                # If model sends audio back (response.output_audio.delta), Twilio can play it
                # with bidirectional streams; Twilio expects audio chunks on the same WS.
                # For simplicity we let model speak via its own path; you can extend here.

        except asyncio.TimeoutError:
            return

    async def close(self):
        if self.ai_ws:
            await self.ai_ws.close()
