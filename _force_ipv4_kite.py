"""
Force IPv4 outbound for kite.trade endpoints.
Zerodha's NSE-mandated IP whitelist only accepts IPv4 addresses.
Without this, requests may go out over IPv6 and get rejected with:
    "IP (<v6_addr>) is not allowed to place orders for this app"
"""
import socket

_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host=None, port=None, family=0, type=0, proto=0, flags=0):
    """Force AF_INET (IPv4) when resolving kite.trade / zerodha.com hostnames."""
    if host and ("kite.trade" in str(host) or "zerodha.com" in str(host)):
        family = socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo
