"""
Broker package — exports all broker classes and a factory function.

Usage:
  from brokers import get_broker
  broker = get_broker()          # reads BROKER env var, default: alpaca
  broker = get_broker("alpaca")  # explicit
"""

from .base        import BaseBroker
from .alpaca_broker import AlpacaBroker
from .angelone    import AngelOneBroker
from .zerodha     import ZerodhaBroker

__all__ = ["BaseBroker", "AlpacaBroker", "AngelOneBroker", "ZerodhaBroker", "get_broker"]


def get_broker(name: str = None) -> BaseBroker:
    """
    Instantiate a broker from env vars.

    BROKER=alpaca    → AlpacaBroker  (reads ALPACA_API_KEY / ALPACA_SECRET_KEY)
    BROKER=angelone  → AngelOneBroker (reads AO_API_KEY / AO_CLIENT_ID / AO_PASSWORD / AO_TOTP_SECRET)
    BROKER=zerodha   → ZerodhaBroker  (reads ZRD_API_KEY / ZRD_API_SECRET / ZRD_ACCESS_TOKEN)
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
        api_key      = os.environ.get("ZRD_API_KEY", "")
        api_secret   = os.environ.get("ZRD_API_SECRET", "")
        access_token = os.environ.get("ZRD_ACCESS_TOKEN", "")
        if not all([api_key, api_secret]):
            raise ValueError(
                "Zerodha broker requires ZRD_API_KEY, ZRD_API_SECRET env vars"
            )
        return ZerodhaBroker(api_key, api_secret, access_token)

    raise ValueError(
        f"Unknown broker '{broker_name}'. Valid options: alpaca, angelone, zerodha"
    )
