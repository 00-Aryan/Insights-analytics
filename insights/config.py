"""Paths and settings. Everything secret lives in .env or secrets/ (both git-ignored)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
SECRETS_DIR = ROOT / "secrets"
REPORTS_DIR = ROOT / "reports"
DB_PATH = Path(os.getenv("INSIGHTS_DB", DATA_DIR / "insights.db"))

YT_CLIENT_SECRET = ROOT / os.getenv("YT_CLIENT_SECRET", "secrets/client_secret.json")
YT_TOKEN = SECRETS_DIR / "yt_token.json"
YT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
YT_MAX_VIDEOS = int(os.getenv("YT_MAX_VIDEOS", "50"))

IG_TOKEN_FILE = SECRETS_DIR / "ig_token.json"
IG_API_VERSION = os.getenv("IG_API_VERSION", "v25.0")
IG_MAX_MEDIA = int(os.getenv("IG_MAX_MEDIA", "50"))

MIN_POSTS_FOR_VERDICT = int(os.getenv("MIN_POSTS_FOR_VERDICT", "8"))
MIN_AGE_HOURS = float(os.getenv("MIN_AGE_HOURS", "72"))


class AuthError(RuntimeError):
    """Credentials missing, expired or revoked. Never retried."""


class QuotaError(RuntimeError):
    """Platform rate limit or quota hit. Never retried in the same run."""
