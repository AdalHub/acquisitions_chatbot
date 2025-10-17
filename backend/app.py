import os
from dotenv import load_dotenv
from quart import Quart, jsonify
from data.store import init_db
from transport.twilio_quart import twilio_bp  # NEW: quart-based blueprint

load_dotenv()
init_db()

app = Quart(__name__)
app.register_blueprint(twilio_bp, url_prefix="/twilio")

@app.get("/health")
async def health():
    return jsonify({"ok": True})

# Local debug (you'll still run with hypercorn for prod-like behavior)
if __name__ == "__main__":
    app.run(port=8000, debug=True)
