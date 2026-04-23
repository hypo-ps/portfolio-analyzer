from __future__ import annotations

from kiteconnect import KiteConnect

from portfolio_analyzer.config import Credentials


def interactive_login(creds: Credentials) -> KiteConnect:
    """Prompt the user to complete Kite login and return an authenticated client."""
    kite = KiteConnect(api_key=creds.api_key)
    print("\n== Kite Connect login ==")
    print(f"Login URL: {kite.login_url()}")
    print("After logging in, copy the `request_token` from the redirect URL.")
    request_token = input("Paste request_token: ").strip()
    if not request_token:
        raise RuntimeError("Empty request_token")
    session = kite.generate_session(request_token, api_secret=creds.api_secret)
    kite.set_access_token(session["access_token"])
    print("Login successful.\n")
    return kite
