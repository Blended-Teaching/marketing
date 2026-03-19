#!/usr/bin/env python3
"""Sync Read AI transcripts to Google Drive, mirroring the Read AI folder structure.

- Creates GDrive subfolders matching Read AI folder names.
- Moves GDrive files when transcripts are moved in Read AI.
- Renames GDrive folders when Read AI folder names change.
- Skips re-uploading unchanged transcripts (tracks by meeting ID).
"""

import argparse
import json
import logging
import subprocess
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_DIR = Path.home() / ".read_ai_sync"
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_FILE = CONFIG_DIR / "gdrive_token.json"
GDRIVE_CREDENTIALS_FILE = CONFIG_DIR / "gdrive_credentials.json"

GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------------------------
# Read AI OAuth 2.1 constants
# ---------------------------------------------------------------------------
READ_AI_BASE = "https://api.read.ai/v1"
READ_AI_REGISTER_URL = "https://api.read.ai/oauth/register"
READ_AI_AUTH_ENDPOINT = "https://authn.read.ai/oauth2/auth"
READ_AI_TOKEN_URL = "https://authn.read.ai/oauth2/token"

OAUTH_CALLBACK_PORT = 8765
OAUTH_REDIRECT_URI = "http://localhost:{}/callback".format(OAUTH_CALLBACK_PORT)
OAUTH_SCOPES = "openid email offline_access profile meeting:read"

# Folder name used for meetings not assigned to any folder in Read AI
UNFILED_FOLDER = "Unfiled"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
def load_config():
    if not CONFIG_FILE.exists():
        sys.exit(
            "Config not found at {}.\nRun:  python sync_transcripts.py --setup".format(CONFIG_FILE)
        )
    return json.loads(CONFIG_FILE.read_text())


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


# ---------------------------------------------------------------------------
# Read AI OAuth 2.1 helpers
# ---------------------------------------------------------------------------
def register_oauth_client():
    log.info("Registering OAuth client with Read AI...")
    resp = requests.post(
        READ_AI_REGISTER_URL,
        json={
            "client_name": "Read AI Transcript Sync",
            "redirect_uris": [OAUTH_REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": OAUTH_SCOPES,
            "token_endpoint_auth_method": "client_secret_basic",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _catch_auth_code():
    code_holder = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                code_holder["code"] = params["code"][0]
                body = b"Authorization successful! You can close this tab and return to the terminal."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                error = params.get("error", ["unknown"])[0]
                body = "Authorization failed: {}".format(error).encode()
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", OAUTH_CALLBACK_PORT), _Handler)
    server.handle_request()
    return code_holder.get("code", "")


def browser_auth_flow(client_id):
    import base64, hashlib, os as _os
    verifier = base64.urlsafe_b64encode(_os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    state = base64.urlsafe_b64encode(_os.urandom(16)).rstrip(b"=").decode()

    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    auth_url = "{}?{}".format(READ_AI_AUTH_ENDPOINT, params)

    print("\nOpening browser for Read AI authorization...")
    print("URL: {}\n".format(auth_url))
    webbrowser.open(auth_url)
    print("Waiting for callback on http://localhost:{}/callback ...".format(OAUTH_CALLBACK_PORT))

    code = _catch_auth_code()
    return code, verifier


def exchange_code_for_tokens(client_id, client_secret, code, verifier):
    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST", READ_AI_TOKEN_URL,
            "-u", "{}:{}".format(client_id, client_secret),
            "-d", "grant_type=authorization_code",
            "-d", "code={}".format(code),
            "-d", "redirect_uri={}".format(OAUTH_REDIRECT_URI),
            "-d", "code_verifier={}".format(verifier),
        ],
        capture_output=True, text=True, timeout=30,
    )
    data = json.loads(result.stdout)
    if "error" in data:
        sys.exit("Token exchange failed: {}".format(data))
    return data


def _do_refresh(client_id, client_secret, refresh_token):
    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST", READ_AI_TOKEN_URL,
            "-u", "{}:{}".format(client_id, client_secret),
            "-d", "grant_type=refresh_token",
            "-d", "refresh_token={}".format(refresh_token),
        ],
        capture_output=True, text=True, timeout=30,
    )
    data = json.loads(result.stdout)
    if "error" in data:
        raise RuntimeError("Token refresh failed: {}".format(data))
    return data


def get_valid_access_token(config):
    expires_at_str = config.get("token_expires_at")
    if expires_at_str:
        expires_at = datetime.fromisoformat(expires_at_str)
        if datetime.now(timezone.utc) < expires_at - timedelta(seconds=60):
            return config["access_token"], config

    log.info("Access token expiring — refreshing...")
    tokens = _do_refresh(config["client_id"], config["client_secret"], config["refresh_token"])
    config["access_token"] = tokens["access_token"]
    if "refresh_token" in tokens:
        config["refresh_token"] = tokens["refresh_token"]
    expires_in = tokens.get("expires_in", 600)
    config["token_expires_at"] = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    ).isoformat()
    save_config(config)
    return config["access_token"], config


# ---------------------------------------------------------------------------
# Interactive setup
# ---------------------------------------------------------------------------
def setup(folder_id=None):
    print("=== Read AI -> Google Drive Sync — Setup ===\n")

    # Load existing config if available so we don't wipe meeting_map, slack keys, etc.
    existing_config = {}
    if CONFIG_FILE.exists():
        try:
            existing_config = json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass

    try:
        reg = register_oauth_client()
    except requests.HTTPError as e:
        sys.exit("Failed to register OAuth client: {}\nResponse: {}".format(e, e.response.text))

    client_id = reg.get("client_id")
    client_secret = reg.get("client_secret")
    if not client_id or not client_secret:
        sys.exit("Unexpected registration response: {}".format(reg))

    print("\nOAuth client registered. client_id: {}".format(client_id))

    code, verifier = browser_auth_flow(client_id)
    if not code:
        sys.exit("No authorization code received. Setup failed.")

    tokens = exchange_code_for_tokens(client_id, client_secret, code, verifier)
    expires_in = tokens.get("expires_in", 600)

    # Merge new auth tokens into existing config, preserving everything else
    existing_config.update({
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        ).isoformat(),
    })
    if folder_id:
        existing_config["gdrive_root_folder_id"] = folder_id
    if "gdrive_root_folder_id" not in existing_config:
        sys.exit("No Drive folder ID found. Pass it with --folder-id <ID>")
    existing_config.setdefault("folder_map", {})
    existing_config.setdefault("meeting_map", {})

    save_config(existing_config)
    print("\nSetup complete! Tokens saved to {}".format(CONFIG_FILE))
    print("Run:  python sync_transcripts.py --dry-run")


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
def get_drive_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GDRIVE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GDRIVE_CREDENTIALS_FILE.exists():
                sys.exit(
                    "Google Drive credentials file not found at {}.\n"
                    "See README.md for setup instructions.".format(GDRIVE_CREDENTIALS_FILE)
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GDRIVE_CREDENTIALS_FILE), GDRIVE_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_or_create_gdrive_folder(service, root_folder_id, folder_name, folder_map, dry_run=False):
    """Return GDrive folder ID for the given Read AI folder name, creating it if needed."""
    if folder_name in folder_map:
        return folder_map[folder_name]

    # Check Drive in case we lost our state
    escaped = folder_name.replace("'", "\\'")
    results = service.files().list(
        q="name='{}' and '{}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false".format(
            escaped, root_folder_id
        ),
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = results.get("files", [])
    if files:
        folder_id = files[0]["id"]
        log.info("Found existing GDrive folder: %s", folder_name)
    elif dry_run:
        log.info("  [DRY RUN] Would create GDrive folder: %s", folder_name)
        return None
    else:
        metadata = {
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [root_folder_id],
        }
        f = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
        folder_id = f["id"]
        log.info("Created GDrive folder: %s", folder_name)

    folder_map[folder_name] = folder_id
    return folder_id


def move_gdrive_file(service, file_id, old_folder_id, new_folder_id):
    service.files().update(
        fileId=file_id,
        addParents=new_folder_id,
        removeParents=old_folder_id,
        fields="id, parents",
        supportsAllDrives=True,
    ).execute()


def upload_gdoc(service, folder_id, title, content):
    """Upload plain text as a Google Doc."""
    metadata = {
        "name": title,
        "parents": [folder_id],
        "mimeType": "application/vnd.google-apps.document",
    }
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    f = service.files().create(body=metadata, media_body=media, fields="id", supportsAllDrives=True).execute()
    return f["id"]


# ---------------------------------------------------------------------------
# Read AI API
# ---------------------------------------------------------------------------
def fetch_meetings_page(access_token, cursor):
    params = {"limit": 10}
    if cursor:
        params["cursor"] = cursor
    params["expand[]"] = "transcript"

    resp = requests.get(
        "{}/meetings".format(READ_AI_BASE),
        headers={
            "Authorization": "Bearer {}".format(access_token),
            "Accept": "application/json",
        },
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Transcript formatting
# ---------------------------------------------------------------------------
def _parse_dt(raw):
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return raw


def _ts_label(raw):
    try:
        secs = int(float(raw))
        m, s = divmod(secs, 60)
        return "[{:02d}:{:02d}] ".format(m, s)
    except (TypeError, ValueError):
        return ""


def format_transcript(meeting):
    title = meeting.get("title") or "Untitled Meeting"
    date_raw = meeting.get("start_time") or meeting.get("created_at") or ""
    date_str = _parse_dt(date_raw) if date_raw else "Unknown date"

    lines = [
        title,
        "Date: {}".format(date_str),
        "Meeting ID: {}".format(meeting.get("id", "")),
        "",
        "---",
        "",
        "Transcript",
        "",
    ]

    transcript = meeting.get("transcript") or {}
    turns = (
        transcript.get("turns")
        or transcript.get("segments")
        or transcript.get("utterances")
        or []
    )

    if not turns:
        lines.append("(No transcript content available)")
    else:
        for turn in turns:
            speaker = (
                turn.get("speaker_name")
                or turn.get("speaker")
                or turn.get("name")
                or "Unknown"
            )
            text = turn.get("content") or turn.get("text") or ""
            ts = _ts_label(turn.get("start_time") or turn.get("offset"))
            lines.append("{}{}: {}".format(ts, speaker, text))

    return "\n".join(lines)


def doc_title(meeting):
    title = meeting.get("title") or "Untitled"
    date_raw = meeting.get("start_time") or meeting.get("created_at") or ""
    prefix = ""
    if date_raw:
        try:
            dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
            prefix = dt.strftime("%Y-%m-%d ")
        except ValueError:
            pass
    return "{}{}".format(prefix, title)


def meeting_folder_name(meeting):
    """Return the Read AI folder name for this meeting, or UNFILED_FOLDER."""
    folders = meeting.get("folders") or []
    return folders[0] if folders else UNFILED_FOLDER


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------
def sync(dry_run=False, full_scan=False):
    config = load_config()
    root_folder_id = config["gdrive_root_folder_id"]

    # Migrate old config key if needed
    if "gdrive_folder_id" in config and "gdrive_root_folder_id" not in config:
        config["gdrive_root_folder_id"] = config.pop("gdrive_folder_id")

    folder_map = config.setdefault("folder_map", {})
    meeting_map = config.setdefault("meeting_map", {})

    service = get_drive_service()
    log.info("Connected to Google Drive.")

    uploaded = moved = skipped = errors = 0
    cursor = None  # Always scan all meetings to catch folder moves

    while True:
        access_token, config = get_valid_access_token(config)

        try:
            data = fetch_meetings_page(access_token, cursor)
        except requests.HTTPError as e:
            sys.exit("Read AI API error: {}\nResponse: {}".format(e, e.response.text))

        meetings = data.get("data") or []
        if not meetings:
            break

        for meeting in meetings:
            meeting_id = meeting.get("id")
            if not meeting_id:
                continue

            if not meeting.get("transcript"):
                skipped += 1
                continue

            folder_name = meeting_folder_name(meeting)
            title = doc_title(meeting)

            # Get or create the GDrive subfolder for this Read AI folder
            target_folder_id = get_or_create_gdrive_folder(
                service, root_folder_id, folder_name, folder_map, dry_run=dry_run
            )

            existing = meeting_map.get(meeting_id)

            if existing:
                gdrive_file_id = existing["gdrive_file_id"]
                prev_folder_name = existing.get("folder_name", "")

                if folder_name != prev_folder_name:
                    # Meeting moved to a different folder in Read AI — move the GDrive file
                    prev_folder_id = folder_map.get(prev_folder_name)
                    if dry_run:
                        log.info(
                            "  [DRY RUN] Would move '%s' from '%s' to '%s'",
                            title, prev_folder_name, folder_name,
                        )
                    else:
                        try:
                            if prev_folder_id and target_folder_id:
                                move_gdrive_file(service, gdrive_file_id, prev_folder_id, target_folder_id)
                                meeting_map[meeting_id]["folder_name"] = folder_name
                                log.info(
                                    "  Moved '%s': '%s' -> '%s'",
                                    title, prev_folder_name, folder_name,
                                )
                                moved += 1
                        except Exception as exc:
                            log.error("  Failed to move '%s': %s", title, exc)
                            errors += 1
                else:
                    skipped += 1  # Already in the right place
            else:
                # New meeting — upload it
                content = format_transcript(meeting)
                if dry_run:
                    log.info(
                        "  [DRY RUN] Would upload '%s' -> folder '%s'",
                        title, folder_name,
                    )
                    uploaded += 1
                else:
                    try:
                        if target_folder_id is None:
                            raise ValueError("No target folder (dry_run bug?)")
                        gdrive_file_id = upload_gdoc(service, target_folder_id, title, content)
                        meeting_map[meeting_id] = {
                            "gdrive_file_id": gdrive_file_id,
                            "folder_name": folder_name,
                            "title": title,
                        }
                        log.info("  Uploaded '%s' -> '%s'", title, folder_name)
                        uploaded += 1
                    except Exception as exc:
                        log.error("  Failed to upload '%s': %s", title, exc)
                        errors += 1

        next_cursor = data.get("next_cursor")
        has_more = data.get("has_more", False)
        if not has_more and not next_cursor:
            break
        cursor = next_cursor or (meetings[-1]["id"] if meetings else None)

    if not dry_run:
        config["folder_map"] = folder_map
        config["meeting_map"] = meeting_map
        save_config(config)

    log.info(
        "Sync complete — uploaded: %d  |  moved: %d  |  skipped: %d  |  errors: %d",
        uploaded, moved, skipped, errors,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Read AI transcripts to Google Drive")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--folder-id", help="Google Drive root folder ID (required for --setup)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    args = parser.parse_args()

    if args.setup:
        setup(folder_id=args.folder_id)
    else:
        sync(dry_run=args.dry_run)
