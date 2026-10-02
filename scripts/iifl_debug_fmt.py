"""Debug IIFL API response format."""
import sys; sys.path.insert(0, "src")
import pandas as pd
from datetime import datetime
from atr.brokers.iifl.auth import SessionStore
from atr.brokers.iifl.client import IiflClient, _fmt_date
from atr.brokers.iifl.contracts import InstrumentMaster
from atr.config.settings import get_settings

s = get_settings()
store = SessionStore(s.iifl_session_cache)
session = store.load()
client = IiflClient(app_key=s.iifl_app_key, app_secret=s.iifl_app_secret, session_store=store, timeout=30)
client.set_session(session)

master = InstrumentMaster(client)
master.sync(["NSEEQ"])
inst = master.search("NIFTYBEES", exchange="NSEEQ")
conid = str(inst.iloc[0]["conid"])

from_dt = datetime(2026, 9, 10)
to_dt = datetime(2026, 9, 12)
payload = client.historical_data(
    exchange="NSEEQ", instrument_id=conid, interval="1m",
    from_date=_fmt_date(from_dt), to_date=_fmt_date(to_dt),
)

import json
print(f"Type: {type(payload)}")
print(f"Keys: {list(payload.keys()) if isinstance(payload, dict) else 'N/A'}")
print(f"Full response:\n{json.dumps(payload, indent=2, default=str)[:3000]}")
client.close()
