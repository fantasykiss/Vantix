import os
from datetime import date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:
    pass

from dataclasses import dataclass

BASE_URL          = os.getenv("REDMINE_URL", os.getenv("BASE_URL", "http://localhost:3000"))
API_KEY           = os.getenv("REDMINE_API_KEY", os.getenv("API_KEY", ""))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
REDMINE_PUBLIC_URL = os.getenv("REDMINE_PUBLIC_URL", "")

@dataclass
class EmailConfig:
    host:       str
    port:       int
    user:       str
    password:   str
    sender:     str
    recipients: list
    enabled:    bool

def _load_email():
    host   = os.getenv("SMTP_HOST", "")
    port   = int(os.getenv("SMTP_PORT", "587"))
    user   = os.getenv("SMTP_USER", "")
    pw     = os.getenv("SMTP_PASS", "")
    sender = os.getenv("SMTP_FROM", f"Vantix <{user}>")
    recip  = [r.strip() for r in os.getenv("REPORT_RECIPIENTS", "").split(",") if r.strip()]
    return EmailConfig(host, port, user, pw, sender, recip, bool(host and user and pw and recip))

EMAIL_CFG = _load_email()

REPORT_DAY    = os.getenv("REPORT_DAY",    "mon")
REPORT_HOUR   = int(os.getenv("REPORT_HOUR",   "9"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))

DEFAULT_PROJECT_ID    = os.getenv("DEFAULT_PROJECT_ID",    "ds_project")
DEFAULT_UPDATED_AFTER = os.getenv("DEFAULT_UPDATED_AFTER",
    (date.today() - timedelta(days=30)).strftime("%Y-%m-%d"))