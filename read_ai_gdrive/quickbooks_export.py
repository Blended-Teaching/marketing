#!/usr/bin/env python3
"""
Export the QuickBooks Online Income Statement (Profit & Loss) for the last
calendar month and save it as a CSV file.
"""

import argparse
import csv
import json
import logging
import sys
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from dateutil.relativedelta import relativedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_DIR = Path.home() / ".read_ai_sync"
CONFIG_FILE = CONFIG_DIR / "config.json"

QB_CLIENT_ID = "YOUR_QB_CLIENT_ID"
QB_CLIENT_SECRET = "YOUR_QB_CLIENT_SECRET"
QB_REDIRECT_URI = "http://localhost:8766/callback"
QB_SCOPES = "com.intuit.quickbooks.accounting"
QB_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
QB_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QB_BASE_URL = "https://quickbooks.api.intuit.com"

OAUTH_PORT = 8766

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
        sys.exit("Config not found. Run: python quickbooks_export.py --setup")
    return json.loads(CONFIG_FILE.read_text())


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------
def _catch_callback():
    """Spin up a temporary localhost server to catch the OAuth callback."""
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            result["code"] = params.get("code", [None])[0]
            result["realm_id"] = params.get("realmId", [None])[0]
            body = b"QuickBooks authorization successful! You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", OAUTH_PORT), Handler)
    server.handle_request()
    return result.get("code"), result.get("realm_id")


def authorize():
    """Run the OAuth 2.0 browser flow and return (access_token, refresh_token, realm_id)."""
    import base64, os as _os
    state = base64.urlsafe_b64encode(_os.urandom(16)).rstrip(b"=").decode()

    params = urllib.parse.urlencode({
        "client_id": QB_CLIENT_ID,
        "response_type": "code",
        "scope": QB_SCOPES,
        "redirect_uri": QB_REDIRECT_URI,
        "state": state,
    })
    auth_url = "{}?{}".format(QB_AUTH_URL, params)

    print("\nOpening browser for QuickBooks authorization...")
    print("URL: {}\n".format(auth_url))
    webbrowser.open(auth_url)
    print("Waiting for callback on http://localhost:{}/callback ...".format(OAUTH_PORT))

    code, realm_id = _catch_callback()
    if not code:
        sys.exit("No authorization code received.")

    # Exchange code for tokens
    resp = requests.post(
        QB_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": QB_REDIRECT_URI,
        },
        auth=(QB_CLIENT_ID, QB_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    tokens = resp.json()
    return tokens["access_token"], tokens["refresh_token"], realm_id


def refresh_qb_token(refresh_token):
    resp = requests.post(
        QB_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(QB_CLIENT_ID, QB_CLIENT_SECRET),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    tokens = resp.json()
    return tokens["access_token"], tokens.get("refresh_token", refresh_token)


def get_qb_token(config):
    """Return a valid QB access token, refreshing if needed."""
    try:
        access_token, new_refresh = refresh_qb_token(config["qb_refresh_token"])
        config["qb_refresh_token"] = new_refresh
        save_config(config)
        return access_token
    except Exception as e:
        sys.exit("QB token refresh failed: {}. Re-run --setup to reauthorize.".format(e))


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def setup():
    print("=== QuickBooks Income Statement Export — Setup ===\n")
    access_token, refresh_token, realm_id = authorize()

    config = load_config()
    config["qb_refresh_token"] = refresh_token
    config["qb_realm_id"] = realm_id
    save_config(config)

    print("\nQuickBooks authorized! Realm ID: {}".format(realm_id))
    print("Run:  python quickbooks_export.py")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def last_calendar_month():
    today = datetime.now(timezone.utc)
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - relativedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    return last_month_start.strftime("%Y-%m-%d"), last_month_end.strftime("%Y-%m-%d")


def fetch_profit_and_loss(access_token, realm_id, start_date, end_date):
    url = "{}/v3/company/{}/reports/ProfitAndLoss".format(QB_BASE_URL, realm_id)
    resp = requests.get(
        url,
        headers={
            "Authorization": "Bearer {}".format(access_token),
            "Accept": "application/json",
        },
        params={
            "start_date": start_date,
            "end_date": end_date,
            "accounting_method": "Accrual",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def parse_report_rows(report):
    """Flatten the nested P&L report rows into (section, label, amount) tuples."""
    rows = []

    def walk(nodes, section=""):
        for node in nodes:
            node_type = node.get("type", "")
            if node_type == "Section":
                header = node.get("Header", {})
                section_name = ""
                cols = header.get("ColData", [])
                if cols:
                    section_name = cols[0].get("value", "")
                child_rows = node.get("Rows", {}).get("Row", [])
                summary = node.get("Summary", {})
                walk(child_rows, section_name)
                # Section total
                if summary:
                    s_cols = summary.get("ColData", [])
                    if len(s_cols) >= 2:
                        rows.append({
                            "section": section_name,
                            "label": "Total {}".format(section_name),
                            "amount": s_cols[1].get("value", ""),
                            "is_total": True,
                        })
            elif node_type == "Data":
                cols = node.get("ColData", [])
                if len(cols) >= 2:
                    rows.append({
                        "section": section,
                        "label": cols[0].get("value", ""),
                        "amount": cols[1].get("value", ""),
                        "is_total": False,
                    })

    top_rows = report.get("Rows", {}).get("Row", [])
    walk(top_rows)
    return rows


def save_csv(rows, start_date, end_date, output_path):
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["section", "label", "amount", "is_total"])
        writer.writeheader()
        writer.writerows(rows)
    log.info("Saved: %s", output_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    config = load_config()

    if "qb_refresh_token" not in config:
        sys.exit("QuickBooks not authorized. Run: python quickbooks_export.py --setup")

    access_token = get_qb_token(config)
    realm_id = config["qb_realm_id"]

    start_date, end_date = last_calendar_month()
    log.info("Fetching P&L for %s to %s...", start_date, end_date)

    report = fetch_profit_and_loss(access_token, realm_id, start_date, end_date)
    rows = parse_report_rows(report)

    if not rows:
        log.warning("Report returned no data.")
        return

    output_path = Path.home() / "read_ai_gdrive" / "income_statement_{}_{}.csv".format(
        start_date, end_date
    )
    save_csv(rows, start_date, end_date, output_path)
    log.info("Done. %d line items exported.", len(rows))
    print("\nSaved to: {}".format(output_path))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export QuickBooks Income Statement")
    parser.add_argument("--setup", action="store_true", help="Authorize with QuickBooks")
    args = parser.parse_args()

    if args.setup:
        setup()
    else:
        main()
