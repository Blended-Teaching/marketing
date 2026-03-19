# Read AI -> Google Drive Transcript Sync

Automatically downloads transcripts from Read AI and uploads them as Markdown files to a Google Drive folder.

Read AI uses **OAuth 2.1** — there are no static API keys. The setup script handles client registration and the browser-based consent flow automatically. Access tokens expire every 10 minutes; the script auto-refreshes them using a refresh token.

---

## 1. Set up Google Drive API credentials

You need a **Google Cloud OAuth client** (free, one-time):

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Enable the **Google Drive API** (search for it in the API Library and click Enable)
4. Create OAuth credentials:
   - Go to **APIs & Services > Credentials**
   - Click **Create Credentials > OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON file
5. Rename it to `gdrive_credentials.json` and place it at:
   ```
   ~/.read_ai_sync/gdrive_credentials.json
   ```
6. On the **OAuth consent screen**, add your Google account as a test user

---

## 2. Find your Google Drive folder ID

Open the target folder in Google Drive. The URL looks like:

```
https://drive.google.com/drive/folders/1A2B3C4D5E6F7G8H9I0J
```

The folder ID is the last segment — e.g. `1A2B3C4D5E6F7G8H9I0J`.

---

## 3. Install dependencies

```bash
cd ~/read_ai_gdrive
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Run setup

```bash
python sync_transcripts.py --setup
```

This will:
1. Prompt for your Google Drive folder ID
2. Register an OAuth client with Read AI (automatic — no manual step needed)
3. Open your browser to the Read AI consent page — sign in and approve
4. Exchange the authorization code for tokens and save everything to `~/.read_ai_sync/config.json`

---

## 5. Authorize Google Drive (first run)

```bash
python sync_transcripts.py --dry-run
```

On first run, a browser window opens for Google Drive authorization. After that, a token is cached at `~/.read_ai_sync/gdrive_token.json`. The `--dry-run` flag shows what would be uploaded without actually uploading anything.

---

## 6. Sync

```bash
python sync_transcripts.py
```

Transcripts are uploaded as `YYYY-MM-DD_Meeting_Title.md` files. The script tracks the last synced cursor so each run only fetches new meetings.

---

## 7. Automate with cron

Run daily at 8 AM:

```bash
crontab -e
```

Add this line (adjust paths as needed):

```
0 8 * * * /Users/philiplaney/read_ai_gdrive/.venv/bin/python /Users/philiplaney/read_ai_gdrive/sync_transcripts.py >> /Users/philiplaney/read_ai_gdrive/sync.log 2>&1
```

---

## Useful flags

| Flag | Description |
|------|-------------|
| `--setup` | Run the interactive setup wizard |
| `--dry-run` | Show what would be uploaded without actually uploading |
| `--all` | Ignore the saved cursor and re-sync all meetings |

---

## Config files (all in `~/.read_ai_sync/`)

| File | Contents |
|------|----------|
| `config.json` | Read AI OAuth tokens, Drive folder ID, sync cursor |
| `gdrive_credentials.json` | Google Cloud OAuth client secret (you provide this) |
| `gdrive_token.json` | Cached Google Drive access token (auto-created) |
