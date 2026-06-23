import os
from datetime import date, timedelta

try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(_env_path, override=True)
except ImportError:
    pass

from dataclasses import dataclass

BASE_URL          = os.getenv("REDMINE_URL", os.getenv("BASE_URL", "http://localhost:3000"))
API_KEY           = os.getenv("REDMINE_API_KEY", os.getenv("API_KEY", ""))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FERNET_KEY        = os.getenv("FERNET_KEY", "")
ADMIN_PASSWORD    = os.getenv("ADMIN_PASSWORD", "")
OWNER_IPS         = [ip.strip() for ip in os.getenv("OWNER_IPS", "127.0.0.1").split(",") if ip.strip()]
REDMINE_PUBLIC_URL = os.getenv("REDMINE_PUBLIC_URL", "")
DEMO_URL = os.getenv("DEMO_URL", "")
DEMO_KEY = os.getenv("DEMO_KEY", "")

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

DEFAULT_PROJECT_ID    = os.getenv("DEFAULT_PROJECT_ID", "")
DEFAULT_UPDATED_AFTER = os.getenv("DEFAULT_UPDATED_AFTER",
    (date.today() - timedelta(days=90)).strftime("%Y-%m-%d"))
AI_MODEL = os.getenv("AI_MODEL", "claude-haiku-4-5-20251001")

RESEND_API_KEY  = os.getenv("RESEND_API_KEY", "")
RESEND_FROM     = os.getenv("RESEND_FROM", "onboarding@resend.dev")
SUPPORT_EMAIL   = os.getenv("SUPPORT_EMAIL", "support@vantix.app")

PORTONE_STORE_ID            = os.getenv("PORTONE_STORE_ID", "")
PORTONE_CHANNEL_KEY         = os.getenv("PORTONE_CHANNEL_KEY", "")
PORTONE_CHANNEL_KEY_INICIS  = os.getenv("PORTONE_CHANNEL_KEY_INICIS", "")
PORTONE_CHANNEL_KEY_TOSSPAY = os.getenv("PORTONE_CHANNEL_KEY_TOSSPAY", "")
PORTONE_API_SECRET          = os.getenv("PORTONE_API_SECRET", "")

PLAN_PRICES = {
    "pro":      19000,
    "business": 49000,
}