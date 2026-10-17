"""What we pull from GitLab and how politely we ask.

Source: the public GitLab API, project gitlab-org/gitlab (id 278964), one of
the largest openly developed codebases. Each merged merge request (MR) is a
completed task; the `group::<team>` label says which team owns it.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "team_performance.db"
SQL_DIR = ROOT / "sql"

API_BASE = "https://gitlab.com/api/v4"
PROJECT_ID = 278964           # gitlab-org/gitlab
PROJECT_NAME = "gitlab-org/gitlab"

# How far back the first run goes. Each day is ~300 MRs = ~3-4 requests.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 30))
# Days are re-fetched on every run while they may still change: today is
# incomplete, and an MR merged late yesterday can still be reverted.
REFRESH_RECENT_DAYS = 2

PER_PAGE = 100                # GitLab's maximum
MAX_PAGES_PER_DAY = 20        # safety stop; a normal day needs ~4

# HTTP behaviour
TIMEOUT_S = 30
MAX_RETRIES = 5
BACKOFF_BASE_S = 2.0
MIN_INTERVAL_S = 0.5          # polite pacing between requests
MAX_RATE_LIMIT_WAIT_S = 300   # wait out a 429 up to this long, else stop the run
USER_AGENT = "team-performance-pipeline/1.0 (take-home exercise)"
# Optional: a personal access token raises rate limits. Never required.
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN")

# Anonymisation. Associates are shown as stable pseudonyms derived from their
# GitLab user id plus this salt, so the dashboard never shows real names.
ANON_SALT = os.environ.get("ANON_SALT", "change-me-for-a-real-deployment")

# Accounts that are automation, not people. Excluded from associate metrics.
BOT_USERNAME_PATTERNS = ("bot", "renovate", "dependabot", "project_278964_")

# Labels that GitLab uses to classify work
TEAM_LABEL_PREFIX = "group::"
TYPE_LABEL_PREFIX = "type::"
