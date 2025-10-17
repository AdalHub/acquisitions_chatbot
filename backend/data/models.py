from typing import Optional
from sqlmodel import SQLModel, Field
from datetime import datetime

class Lead(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str
    owner_name: str = ""
    property_address: str = ""
    interest: str = ""           # yes | maybe | later | no | dnc | unknown
    price_range: str = ""
    timing: str = ""
    condition: str = ""
    owner_status: str = ""       # owner | tenant | relative | agent | unknown
    qualified: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class Callback(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: int = Field(index=True)
    window: str = ""             # e.g. "today 3–5pm" or ISO
    notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)

class CallEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    call_sid: str = ""           # optional in demo
    event_type: str              # TURN | RAW_LLM | OUTCOME_*
    payload: str = "{}"
    ts: datetime = Field(default_factory=datetime.utcnow)
