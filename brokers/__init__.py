"""
Broker package — exports all broker classes and a factory function.

Usage:
  from brokers import get_broker
  broker = get_broker()                       # reads BROKER env var, default: alpaca
  broker = get_broker("alpaca")               # explicit
  broker = get_broker("zerodha", user_id=1)   # loads creds from dashboard DB
"""

from .base        import BaseBroker
from .alpaca_broker import AlpacaBroker
from .angelone    import AngelOneBroker
from .zerodha     import ZerodhaBroker

__all__ = ["BaseBroker", "AlpacaBroker", "AngelOneBroker", "ZerodhaBroker", "get_broker"]


def get_broker(name: str = None, user_id: int = None) -> BaseBroker:
    """
    Instantiate a broker.

    If user_id is given, credentials are loaded from the dashboard DB
    (Profile → broker connect). Otherwise falls back to env vars.

    BROKER=alpaca    → AlpacaBroker  (env: ALPACA_API_KEY / ALPACA_SECRET_KEY)
    BROKER=angelone  → AngelOneBroker (env: AO_API_KEY / AO_CLIENT_ID / AO_PASSWORD / AO_TOTP_SECRET)
    BROKER=zerodha   → ZerodhaBroker  (db creds preferred; env: ZRD_API_KEY / ZRD_API_SECRET / ZRD_ACCESS_TOKEN)
    """
    import os

    broker_name = (name or os.environ.get("BROKER", "alpaca")).lower()

    if broker_name == "alpaca":
        return AlpacaBroker()

    if broker_name == "angelone":
        api_key      = os.environ.get("AO_API_KEY", "")
        client_id    = os.environ.get("AO_CLIENT_ID", "")
        password     = os.environ.get("AO_PASSWORD", "")
        totp_secret  = os.environ.get("AO_TOTP_SECRET", "")
        if not all([api_key, client_id, password, totp_secret]):
            raise ValueError(
                "Angel One broker requires AO_API_KEY, AO_CLIENT_ID, "
                "AO_PASSWORD, AO_TOTP_SECRET env vars"
            )
        return AngelOneBroker(api_key, client_id, password, totp_secret)

    if broker_name == "zerodha":
        api_key = api_secret = access_token = ""
        # Prefer DB credentials when user_id is given (dashboard-saved)
        if user_id is not None:
            try:
                import auth as _auth
                creds = _auth.get_zerodha_creds(int(user_id))
                if creds:
                    api_key      = creds.get("api_key", "")
                    api_secret   = creds.get("api_secret", "")
                    access_token = creds.get("access_token", "")
            except Exception as _e:
                print(f"[get_broker] DB cred load failed: {_e}", flush=True)
        # Fall back to env vars if DB had nothing
        if not api_key:
            api_key      = os.environ.get("ZRD_API_KEY", "")
            api_secret   = os.environ.get("ZRD_API_SECRET", "")
            access_token = os.environ.get("ZRD_ACCESS_TOKEN", "")
        if not api_key or not api_secret:
            raise ValueError(
                "Zerodha broker needs api_key/api_secret. "
                "Save them via dashboard Profile → Zerodha, or set ZRD_API_KEY/ZRD_API_SECRET env vars."
            )
        if not access_token:
            raise ValueError(
                "Zerodha access_token missing. Complete the daily Kite login from Profile → Zerodha."
            )
        return ZerodhaBroker(api_key, api_secret, access_token)

    raise ValueError(
        f"Unknown broker '{broker_name}'. Valid options: alpaca, angelone, zerodha"
    )
