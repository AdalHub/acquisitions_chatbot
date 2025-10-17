import os, json
from typing import Dict, Any, Optional
from datetime import datetime
from sqlmodel import SQLModel, create_engine, Session, select
from .models import Lead, Callback, CallEvent

DB_URL = os.environ.get("DB_URL", "sqlite:///vanessa.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})

def init_db():
    SQLModel.metadata.create_all(engine)

def save_event(event_type: str, payload: Dict[str, Any], call_sid: Optional[str] = None):
    with Session(engine) as s:
        s.add(CallEvent(call_sid=call_sid or "", event_type=event_type, payload=json.dumps(payload)))
        s.commit()

def upsert_lead(phone: str, **fields) -> Lead:
    with Session(engine) as s:
        lead = s.exec(select(Lead).where(Lead.phone == phone)).first()
        if not lead:
            lead = Lead(phone=phone, **fields)
            s.add(lead)
        else:
            for k, v in fields.items():
                if v not in (None, ""):
                    setattr(lead, k, v)
            lead.updated_at = datetime.utcnow()
        s.commit()
        s.refresh(lead)
        return lead

def mark_qualified(lead_id: int, is_qualified: bool = True):
    with Session(engine) as s:
        lead = s.get(Lead, lead_id)
        if lead:
            lead.qualified = is_qualified
            lead.updated_at = datetime.utcnow()
            s.add(lead)
            s.commit()

def create_callback(lead_id: int, window: str, notes: str = "") -> Callback:
    with Session(engine) as s:
        cb = Callback(lead_id=lead_id, window=window or "next business day", notes=notes)
        s.add(cb)
        s.commit()
        s.refresh(cb)
        return cb

def find_lead_by_phone(phone: str) -> Optional[Lead]:
    with Session(engine) as s:
        return s.exec(select(Lead).where(Lead.phone == phone)).first()
