#!/usr/bin/env python3
"""
Every Monday at 9am GMT: fetch last week's Read AI meeting transcripts,
use Claude to summarize key learnings by category, post to Slack #general.
"""

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_FILE = Path.home() / ".read_ai_sync" / "config.json"
READ_AI_BASE = "https://api.read.ai/v1"
READ_AI_TOKEN_URL = "https://authn.read.ai/oauth2/token"

CATEGORIES = ["Product", "Marketing", "Strategy", "Finance", "CX"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_config():
    return json.loads(CONFIG_FILE.read_text())


def get_last_week_range():
    """Return (start, end) ms timestamps for Mon–Sun of the current week."""
    today = datetime.now(timezone.utc)
    this_monday = today - timedelta(days=today.weekday())
    this_monday = this_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    this_sunday = this_monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return this_monday, this_sunday


def refresh_access_token(config):
    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST", READ_AI_TOKEN_URL,
            "-u", "{}:{}".format(config["client_id"], config["client_secret"]),
            "-d", "grant_type=refresh_token",
            "-d", "refresh_token={}".format(config["refresh_token"]),
        ],
        capture_output=True, text=True, timeout=30,
    )
    data = json.loads(result.stdout)
    if "error" in data:
        sys.exit("Token refresh failed: {}".format(data))
    return data["access_token"]


def fetch_meetings_for_week(access_token, start_ms, end_ms):
    """Fetch all meetings from last week that have transcripts."""
    meetings = []
    cursor = None

    while True:
        params = {"limit": 10, "expand[]": "transcript"}
        if cursor:
            params["cursor"] = cursor

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
        data = resp.json()
        page = data.get("data") or []

        if not page:
            break

        for m in page:
            meeting_ms = m.get("start_time_ms", 0)
            if start_ms <= meeting_ms <= end_ms:
                if m.get("transcript"):
                    meetings.append(m)
            elif meeting_ms < start_ms:
                # API returns newest first — once we're before the window, stop
                return meetings

        if not data.get("has_more") and not data.get("next_cursor"):
            break
        cursor = data.get("next_cursor") or (page[-1]["id"] if page else None)

    return meetings


def meeting_to_text(meeting):
    """Convert a meeting + transcript to plain text for Claude."""
    title = meeting.get("title") or "Untitled"
    folder = (meeting.get("folders") or ["Unfiled"])[0]

    transcript = meeting.get("transcript") or {}
    turns = (
        transcript.get("turns")
        or transcript.get("segments")
        or transcript.get("utterances")
        or []
    )
    if not turns:
        return None

    lines = ["Meeting: {}".format(title), "Folder: {}".format(folder), ""]
    for turn in turns:
        speaker = (
            turn.get("speaker_name") or turn.get("speaker") or turn.get("name") or "Unknown"
        )
        text = turn.get("content") or turn.get("text") or ""
        if text.strip():
            lines.append("{}: {}".format(speaker, text))

    return "\n".join(lines)


def summarize_with_claude(meeting_texts, week_start, week_end, api_key):
    client = anthropic.Anthropic(api_key=api_key)

    week_str = "{} – {}".format(
        week_start.strftime("%b %d"), week_end.strftime("%b %d, %Y")
    )

    # Truncate each transcript to ~1500 chars to stay within rate limits
    # (captures opening discussion which has the most substance)
    truncated = []
    for t in meeting_texts:
        if len(t) > 1500:
            t = t[:1500] + "\n[transcript truncated]"
        truncated.append(t)

    combined = "\n\n---\n\n".join(truncated)

    prompt = """You are summarizing last week's meeting transcripts for a startup leadership team.

Week: {week}

Your task:
1. Read all the meeting transcripts below
2. Group insights by these categories: Product, Marketing, Strategy, Finance, CX
3. For each category that had relevant meetings, write 3–5 concise bullet points covering:
   - Key decisions made
   - Important learnings or insights
   - Action items or next steps
4. Only include categories that have genuinely relevant content
5. Skip small talk — focus on substance
6. The whole summary should take 1–2 minutes to read

Format exactly like this (use Slack mrkdwn):
*Product*
• ...
• ...

*Marketing*
• ...

(only include categories with content)

Meeting transcripts:

{transcripts}""".format(week=week_str, transcripts=combined)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def fetch_posthog_stats(config):
    """Fetch latest value for each selected PostHog insight."""
    api_key = config.get("posthog_api_key")
    project_id = config.get("posthog_project_id")
    insights = config.get("posthog_insights", [])
    if not api_key or not insights:
        return []

    stats = []
    for insight in insights:
        try:
            resp = requests.get(
                "https://app.posthog.com/api/projects/{}/insights/{}/".format(
                    project_id, insight["id"]
                ),
                headers={"Authorization": "Bearer {}".format(api_key)},
                params={"refresh": "blocking"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            result = data.get("result", [])

            value = None
            prev_value = None
            if isinstance(result, list) and result:
                if isinstance(result[0], list):
                    # Table format — count the rows
                    value = len(result)
                elif isinstance(result[0], dict):
                    # Time series format
                    series = result[0]
                    values = series.get("data", [])
                    if values:
                        value = values[-1]
                        prev_value = values[-2] if len(values) >= 2 else None
            elif isinstance(result, (int, float)):
                value = result

            if value is not None:
                stats.append({
                    "name": insight["name"],
                    "value": value,
                    "prev_value": prev_value,
                })
        except Exception as e:
            log.warning("Failed to fetch PostHog insight %s: %s", insight["name"], e)

    return stats


def format_stat_line(stat):
    value = stat["value"]
    prev = stat["prev_value"]
    name = stat["name"]

    # Format number
    if isinstance(value, float) and value != int(value):
        val_str = "{:.1f}".format(value)
    else:
        val_str = "{:,}".format(int(value))

    # Week-on-week change
    if prev and prev > 0:
        change = ((value - prev) / prev) * 100
        arrow = ":chart_with_upwards_trend:" if change >= 0 else ":chart_with_downwards_trend:"
        change_str = " ({}{:.1f}% WoW)".format("+" if change >= 0 else "", change)
    else:
        arrow = ":bar_chart:"
        change_str = ""

    return "{} *{}:* {}{}".format(arrow, name, val_str, change_str)


def post_to_slack(slack_token, summary, week_start, week_end, meeting_count, stats):
    client = WebClient(token=slack_token)
    week_str = "{} – {}".format(
        week_start.strftime("%b %d"), week_end.strftime("%b %d, %Y")
    )

    stats_block = ""
    if stats:
        stats_lines = "\n".join(format_stat_line(s) for s in stats)
        stats_block = "*Key Stats*\n{}\n\n".format(stats_lines)

    text = (
        ":calendar: *Weekly Summary — {}*\n"
        "_{} meetings reviewed_\n\n"
        "{}"
        "{}"
    ).format(week_str, meeting_count, stats_block, summary)

    try:
        client.chat_postMessage(channel="#general", text=text, mrkdwn=True)
        log.info("Posted summary to #general")
    except SlackApiError as e:
        sys.exit("Slack error: {}".format(e.response["error"]))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    config = load_config()

    slack_token = config.get("slack_token")
    anthropic_key = config.get("anthropic_api_key")

    if not slack_token:
        sys.exit("No slack_token in config. Run: python weekly_summary.py --setup")
    if not anthropic_key:
        sys.exit("No anthropic_api_key in config. Run: python weekly_summary.py --setup")

    access_token = refresh_access_token(config)
    week_start, week_end = get_last_week_range()
    start_ms = int(week_start.timestamp() * 1000)
    end_ms = int(week_end.timestamp() * 1000)

    log.info("Fetching meetings from %s to %s...", week_start.date(), week_end.date())
    meetings = fetch_meetings_for_week(access_token, start_ms, end_ms)
    log.info("Found %d meetings with transcripts", len(meetings))

    if not meetings:
        log.info("No meetings last week — skipping Slack post")
        return

    meeting_texts = [t for t in (meeting_to_text(m) for m in meetings) if t]

    if not meeting_texts:
        log.info("No transcripts had content — skipping")
        return

    log.info("Generating summary with Claude...")
    summary = summarize_with_claude(meeting_texts, week_start, week_end, anthropic_key)

    log.info("Fetching PostHog stats...")
    stats = fetch_posthog_stats(config)

    log.info("Posting to Slack...")
    post_to_slack(slack_token, summary, week_start, week_end, len(meetings), stats)
    log.info("Done.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true", help="Save Slack token and Anthropic API key")
    parser.add_argument("--slack-token", help="Slack bot token (xoxb-...)")
    parser.add_argument("--anthropic-key", help="Anthropic API key (sk-ant-...)")
    args = parser.parse_args()

    if args.setup:
        cfg = json.loads(CONFIG_FILE.read_text())
        if args.slack_token:
            cfg["slack_token"] = args.slack_token
        if args.anthropic_key:
            cfg["anthropic_api_key"] = args.anthropic_key
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
        print("Saved to {}".format(CONFIG_FILE))
    else:
        main()
