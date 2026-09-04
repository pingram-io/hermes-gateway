DEFAULT_NOTIFICATION_TYPE = "hermes_agent"
DEFAULT_FROM_NAME = "Hermes"
DEDUP_TTL_SECONDS = 3600
DOWNLOAD_TIMEOUT = 30

PINGRAM_IMPORT = "pingram"
PINGRAM_PACKAGE = "pingram-python"
AIOHTTP_IMPORT = "aiohttp"
AIOHTTP_PACKAGE = "aiohttp"
INSTALL_TIMEOUT = 300

DEFAULT_POLL_INTERVAL = 15
MIN_POLL_INTERVAL = 3
DEFAULT_POLL_LIMIT = 50
MAX_POLL_PAGES = 10
MIN_SMS_DIGITS = 10

LOG_EVENT_SMS_INBOUND = "sms_inbound"
LOG_EVENT_EMAIL_INBOUND = "inbound"

PLATFORM_SMS = "pingram-sms"
PLATFORM_EMAIL = "pingram-email"
PLATFORM_VOICE = "pingram-voice"

DISPLAY_OVERRIDES = {
    "interim_assistant_messages": False,
    "tool_progress": "off",
    "streaming": False,
    "busy_ack_detail": False,
    "long_running_notifications": True,
}

CT_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
}
