"""IIFL authentication + 1-minute data fetcher.

Starts a local callback server, prints the login URL, waits for the
browser-based OAuth callback, exchanges the auth code for a JWT, then
fetches 1-minute OHLCV for liquid Indian stocks.

Run:  python scripts/iifl_auth.py
"""
from __future__ import annotations
import hashlib
import http.server
import json
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atr.brokers.iifl.auth import Session, SessionStore, build_checksum, login_url
from atr.brokers.iifl.client import IiflClient
from atr.config.settings import get_settings

P = logger.info

settings = get_settings()
APP_KEY = settings.iifl_app_key
APP_SECRET = settings.iifl_app_secret
CLIENT_ID = settings.iifl_client_id
REDIRECT_URL = settings.iifl_redirect_url
CACHE_PATH = ROOT / settings.iifl_session_cache
CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

# Captured during the callback
_auth_code: str | None = None
_client_id_callback: str | None = None
_callback_event = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code, _client_id_callback
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        auth = params.get("authcode", [None])[0] or params.get("authCode", [None])[0] or params.get("auth_code", [None])[0]
        cid = params.get("clientid", [None])[0] or params.get("clientId", [None])[0] or params.get("client_id", [None])[0]

        if auth and cid:
            _auth_code = auth
            _client_id_callback = cid
            _callback_event.set()
            body = b"<html><body><h2>Login successful!</h2><p>You can close this tab.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"<html><body><h2>Login failed</h2><p>No auth code received.</p></body></html>"
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # Suppress default logging


def start_callback_server(port: int = 8000):
    server = socketserver.TCPServer(("127.0.0.1", port), _CallbackHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    P(f"Callback server listening on http://127.0.0.1:{port}")
    return server


def try_cached_session(client: IiflClient) -> Session | None:
    """Try to load a valid cached session."""
    store = SessionStore(str(CACHE_PATH))
    session = store.load()
    if session:
        client.set_session(session)
        return session
    return None


def authenticate() -> Session | None:
    """Try cached session first; if none, start browser login flow."""
    client = IiflClient(
        app_key=APP_KEY,
        app_secret=APP_SECRET,
        base_url=settings.iifl_base_url,
        session_store=SessionStore(str(CACHE_PATH)),
    )

    # 1. Try cached session
    session = try_cached_session(client)
    if session:
        P(f"Using cached session: client_id={session.client_id}, expires={session.expires_at}")
        return session

    # 2. Start callback server and print login URL
    P("No cached session. Starting browser authentication...")
    cb_port = 8765
    server = start_callback_server(port=cb_port)
    url = f"https://markets.iiflcapital.com/?v=1&appkey={APP_KEY}&redirecturl=http://127.0.0.1:{cb_port}"
    P(f"Open this URL in your browser:\n  {url}")
    P("Log in with your IIFL trading credentials + OTP.")

    # 3. Wait for callback (5 minutes max)
    P("Waiting for callback...")
    if not _callback_event.wait(timeout=300):
        P("Timed out waiting for callback.")
        return None

    # 4. Exchange auth code for session
    auth_code = _auth_code or ""
    client_id = _client_id_callback or CLIENT_ID
    P(f"Received callback: client_id={client_id}, auth_code={auth_code[:8]}...")

    try:
        session = client.create_session(client_id, auth_code)
        P(f"Session created: client_id={session.client_id}, expires={session.expires_at}")
        return session
    except Exception as e:
        P(f"Session creation failed: {e}")
        return None
    finally:
        server.shutdown()


def fetch_1min_ohlcv(client: IiflClient, symbol: str, exchange: str = "NSEEQ",
                     from_date: str = "01-Jan-2025", to_date: str = "15-Sep-2026") -> list:
    """Fetch 1-minute candles for a symbol."""
    from atr.brokers.iifl.contracts import InstrumentMaster
    master = InstrumentMaster(client)
    master.sync([exchange])

    instruments = master.search(symbol, exchange=exchange)
    if instruments.empty:
        P(f"No instrument found for {symbol} on {exchange}")
        return []

    conid = str(instruments.iloc[0]["conid"])
    P(f"Fetching {symbol} (conid={conid}, exchange={exchange}, 1m) "
      f"{from_date} → {to_date}...")

    # IIFL returns max ~30 days per request; split into chunks
    from datetime import datetime, timedelta
    from_date_dt = datetime.strptime(from_date, "%d-%b-%Y")
    to_date_dt = datetime.strptime(to_date, "%d-%b-%Y")

    all_candles = []
    chunk_start = from_date_dt
    while chunk_start < to_date_dt:
        chunk_end = min(chunk_start + timedelta(days=25), to_date_dt)
        payload = client.historical_data(
            exchange=exchange,
            instrument_id=conid,
            interval="1m",
            from_date=chunk_start.strftime("%d-%b-%Y"),
            to_date=chunk_end.strftime("%d-%b-%Y"),
        )
        candles = payload.get("result", {}).get("candles", payload if isinstance(payload, list) else [])
        all_candles.extend(candles)
        chunk_start = chunk_end + timedelta(days=1)
        P(f"  Got {len(candles)} candles for {chunk_start.strftime('%Y-%m')}")
        import time; time.sleep(0.1)

    P(f"Total: {len(all_candles)} 1-minute candles for {symbol}")
    return all_candles


if __name__ == "__main__":
    session = authenticate()
    if not session:
        print("\nNo session available. Cannot fetch data.")
        sys.exit(1)

    client = IiflClient(
        app_key=APP_KEY, app_secret=APP_SECRET,
        base_url=settings.iifl_base_url,
    )
    client.set_session(session)

    # Test with a few liquid stocks
    test_symbols = ["RELIANCE", "HDFCBANK", "INFY", "ICICIBANK", "TCS", "NIFTYBEES"]
    P("\nFetching 1-minute data for test symbols...")

    for sym in test_symbols:
        try:
            candles = fetch_1min_ohlcv(client, sym, from_date="01-Jan-2025", to_date="15-Sep-2026")
            if candles:
                P(f"  {sym}: {len(candles)} candles")
        except Exception as e:
            P(f"  {sym}: failed - {e}")

    client.close()
    P("\nDone!")
