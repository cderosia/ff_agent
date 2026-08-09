#!/usr/bin/env python3
"""
One-time Yahoo OAuth2 setup — obtains a refresh token and stores it in .env.

Run it twice:

    python3 scripts/yahoo_auth.py              # step 1: opens the consent page
    python3 scripts/yahoo_auth.py <code>       # step 2: exchanges code for tokens

The refresh token is written straight to .env and never printed.
Once you have it, you never run this again — refresh tokens renew themselves.
"""
import sys, base64, json, webbrowser, urllib.parse, pathlib

import requests

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"
AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
DEFAULT_REDIRECT = "https://localhost:8000/callback"


def read_env():
    vals = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def write_env_value(key, value):
    """Set key=value in .env, preserving everything else and all comments."""
    lines = ENV.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n")
    ENV.chmod(0o600)


def main():
    env = read_env()
    cid = env.get("YAHOO_CLIENT_ID", "")
    csec = env.get("YAHOO_CLIENT_SECRET", "")
    redirect = env.get("YAHOO_REDIRECT_URI") or DEFAULT_REDIRECT

    if not cid or not csec:
        sys.exit("ERROR: YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET missing from .env")

    # ---- step 1: send the user to Yahoo's consent page ----------------------
    if len(sys.argv) < 2:
        # Scope handling: Yahoo's app form no longer exposes a Fantasy Sports
        # checkbox, and asking for `fspt-r` on an app without it returns
        # invalid_scope. Omitting scope entirely makes Yahoo issue a token with
        # whatever the app IS provisioned for -- which in practice still
        # reaches the fantasy endpoints. Override with YAHOO_SCOPE if needed.
        params = {
            "client_id": cid,
            "redirect_uri": redirect,
            "response_type": "code",
            "language": "en-us",
        }
        scope = env.get("YAHOO_SCOPE", "").strip()
        if scope:
            params["scope"] = scope
        qs = urllib.parse.urlencode(params)
        url = f"{AUTH_URL}?{qs}"
        print("Opening Yahoo consent page in your browser...\n")
        print("After you click Agree, the browser will fail to load a page at")
        print(f"  {redirect}?code=...")
        print("That failure is EXPECTED — nothing is listening on that port.")
        print("Copy the `code` value out of the URL bar, then run:\n")
        print("  python3 scripts/yahoo_auth.py <code>\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        print("If the browser didn't open, paste this URL manually:\n")
        print(url)
        return

    # ---- step 2: trade the authorization code for tokens --------------------
    code = sys.argv[1].strip()
    # Tolerate someone pasting the whole redirect URL instead of just the code.
    if code.startswith("http"):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
        code = (q.get("code") or [""])[0]
        if not code:
            sys.exit("ERROR: no ?code= found in that URL")

    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
            "code": code,
        },
        timeout=30,
    )

    if r.status_code != 200:
        print(f"FAILED — HTTP {r.status_code}")
        try:
            err = r.json()
            print(json.dumps(err, indent=2))
            desc = str(err)
            if "redirect_uri" in desc:
                print("\n-> The redirect_uri here must EXACTLY match what you")
                print("   registered on the Yahoo app. Set YAHOO_REDIRECT_URI")
                print("   in .env if you used something other than:")
                print(f"   {DEFAULT_REDIRECT}")
            if "invalid_grant" in desc:
                print("\n-> Authorization codes are single-use and expire fast.")
                print("   Re-run step 1 to get a fresh one.")
        except Exception:
            print(r.text[:500])
        sys.exit(1)

    tok = r.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit(f"ERROR: no refresh_token in response: {list(tok)}")

    write_env_value("YAHOO_REFRESH_TOKEN", refresh)
    print("SUCCESS — refresh token written to .env")
    print(f"  granted scope : {tok.get('scope', '(none reported)')}")
    print(f"  access expires: {tok.get('expires_in')}s (auto-renewed from here on)")
    print(f"  yahoo guid    : {tok.get('xoauth_yahoo_guid')}")


if __name__ == "__main__":
    main()
