#!/usr/bin/env python3
"""One-shot Read AI re-authorization. Updates tokens in config without wiping other settings."""
import base64, hashlib, json, os, subprocess, urllib.parse, webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

CONFIG_FILE = Path.home() / ".read_ai_sync" / "config.json"
READ_AI_REGISTER_URL = "https://api.read.ai/oauth/register"
READ_AI_AUTH_ENDPOINT = "https://authn.read.ai/oauth2/auth"
READ_AI_TOKEN_URL = "https://authn.read.ai/oauth2/token"
PORT = 8765
REDIRECT_URI = "http://localhost:{}/callback".format(PORT)
SCOPES = "openid email offline_access profile meeting:read"

print("Step 1: Registering new OAuth client with Read AI...")
import requests
resp = requests.post(READ_AI_REGISTER_URL, json={
    "client_name": "Read AI Transcript Sync",
    "redirect_uris": [REDIRECT_URI],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "scope": SCOPES,
    "token_endpoint_auth_method": "client_secret_basic",
}, timeout=30)
resp.raise_for_status()
reg = resp.json()
client_id = reg["client_id"]
client_secret = reg["client_secret"]
print("  client_id:", client_id)

print("\nStep 2: Building auth URL with PKCE...")
verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
digest = hashlib.sha256(verifier.encode()).digest()
challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
state = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()

params = urllib.parse.urlencode({
    "client_id": client_id,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "scope": SCOPES,
    "state": state,
    "code_challenge": challenge,
    "code_challenge_method": "S256",
})
auth_url = "{}?{}".format(READ_AI_AUTH_ENDPOINT, params)

print("\nStep 3: Opening browser. Authorize in the browser, then return here.")
print("URL:", auth_url)
webbrowser.open(auth_url)

print("\nStep 4: Waiting for callback on {}...".format(REDIRECT_URI))
code_holder = {}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in p:
            code_holder["code"] = p["code"][0]
            body = b"Authorization successful! Return to terminal."
        else:
            body = "Error: {}".format(p.get("error", ["unknown"])[0]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

server = HTTPServer(("localhost", PORT), Handler)
server.handle_request()
code = code_holder.get("code", "")
if not code:
    print("ERROR: No authorization code received.")
    exit(1)
print("  Got authorization code.")

print("\nStep 5: Exchanging code for tokens...")
result = subprocess.run([
    "curl", "-s", "-X", "POST", READ_AI_TOKEN_URL,
    "-u", "{}:{}".format(client_id, client_secret),
    "-d", "grant_type=authorization_code",
    "-d", "code={}".format(code),
    "-d", "redirect_uri={}".format(REDIRECT_URI),
    "-d", "code_verifier={}".format(verifier),
], capture_output=True, text=True, timeout=30)
tokens = json.loads(result.stdout)
if "error" in tokens:
    print("ERROR exchanging code:", tokens)
    exit(1)
print("  Got access_token and refresh_token.")

print("\nStep 6: Saving tokens to config...")
config = json.loads(CONFIG_FILE.read_text())
config.update({
    "client_id": client_id,
    "client_secret": client_secret,
    "access_token": tokens["access_token"],
    "refresh_token": tokens["refresh_token"],
    "token_expires_at": (
        datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 600))
    ).isoformat(),
})
CONFIG_FILE.write_text(json.dumps(config, indent=2))
print("  Saved to", CONFIG_FILE)
print("\nDone! Run: python sync_transcripts.py --dry-run")
