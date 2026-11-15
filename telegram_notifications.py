"""Narrow Telegram sender for the Joao trade alerts (stdlib only).

Credentials come from the process environment first, then from ONE shared file,
~/.env.shared (the same file the other projects use). This module never reads
this project's own .env and never writes or logs a token: every error text is
scrubbed of the bot token before it leaves this module.

Keys used:
  TELEGRAM_BOT_TOKEN        shared bot token
  JOAO_TELEGRAM_CHAT_ID     the channel id (-100...)
  JOAO_TRADE_ALERTS_ENABLED / JOAO_ALERT_PLANS_ENABLED   feature switches (default off)
"""
import json
import os
import time
import urllib.error
import urllib.request

SHARED_ENV = os.path.join(os.path.expanduser("~"), ".env.shared")
TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
CHAT_KEY = "JOAO_TELEGRAM_CHAT_ID"
ENABLED_KEY = "JOAO_TRADE_ALERTS_ENABLED"
PLANS_KEY = "JOAO_ALERT_PLANS_ENABLED"

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4000                 # Telegram hard limit is 4096
MAX_RETRY_AFTER_S = 60          # longer 429 waits are left for the next daily run


class ConfigError(Exception):
    pass


class SendError(Exception):
    """transient=True: the same message may be retried on a later run."""

    def __init__(self, message, transient=True):
        super().__init__(message)
        self.transient = transient


def read_shared_env(path=None):
    """Parse KEY=VALUE lines (optional `export `, quotes, # comments)."""
    out = {}
    try:
        with open(path or SHARED_ENV, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def lookup(key, path=None, environ=None):
    environ = os.environ if environ is None else environ
    value = environ.get(key)
    if value not in (None, ""):
        return value
    return read_shared_env(path).get(key) or None


def flag(key, path=None, environ=None):
    return (lookup(key, path, environ) or "").strip().lower() in ("1", "true", "yes", "on")


def get_config(path=None, environ=None):
    token, chat = lookup(TOKEN_KEY, path, environ), lookup(CHAT_KEY, path, environ)
    if not token or not chat:
        raise ConfigError(f"{TOKEN_KEY} or {CHAT_KEY} is not set (environment or ~/.env.shared)")
    return token, chat


def _scrub(text, token):
    return str(text).replace(token, "<token>") if token else str(text)


def send_message(token, chat_id, text, *, timeout=20, attempts=3,
                 opener=urllib.request.urlopen, sleep=time.sleep):
    """Send plain text; return the Telegram message_id. Plain text on purpose:
    no parse_mode, so quoted post text can never break the markup."""
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT - 1] + "…"
    body = json.dumps({"chat_id": chat_id, "text": text,
                       "disable_web_page_preview": True}).encode()
    last = "unknown error"
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            API.format(token=token, method="sendMessage"), data=body,
            headers={"Content-Type": "application/json"})
        wait = 2 * attempt
        try:
            with opener(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            mid = ((data or {}).get("result") or {}).get("message_id")
            if data.get("ok") is True and isinstance(mid, int):
                return mid
            raise SendError("Telegram answered without ok/message_id", transient=True)
        except urllib.error.HTTPError as exc:
            try:
                info = json.loads(exc.read() or b"{}")
            except ValueError:
                info = {}
            desc = _scrub(info.get("description", ""), token)
            last = f"HTTP {exc.code} {desc}".strip()
            if exc.code == 429:
                wait = (info.get("parameters") or {}).get("retry_after", 5)
                if not isinstance(wait, (int, float)) or wait > MAX_RETRY_AFTER_S:
                    raise SendError(f"rate limited: {last}", transient=True) from None
            elif exc.code < 500:
                raise SendError(last, transient=False) from None      # bad chat/token/text
        except SendError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = _scrub(f"{type(exc).__name__}", token)
        if attempt < attempts:
            sleep(wait)
    raise SendError(f"failed after {attempts} attempts: {last}", transient=True)
