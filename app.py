import os
import re
import logging
import json
import hmac
import hashlib
import httpx
from flask import Flask, request, abort
import anthropic
import redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS       = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")  # unused legacy variable
SMS_GATEWAY         = os.environ["SMS_GATEWAY"]
MAILGUN_SANDBOX     = os.environ["MAILGUN_SANDBOX"]
MAILGUN_API_KEY     = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN      = os.environ.get("MAILGUN_DOMAIN", "")
ALLOWED_GATEWAYS    = set(os.environ.get("ALLOWED_GATEWAYS", SMS_GATEWAY).split(","))
MAILGUN_WEBHOOK_KEY = os.environ.get("MAILGUN_WEBHOOK_KEY", "")
MAX_HISTORY         = int(os.environ.get("MAX_HISTORY", "20"))
MODEL               = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful SMS assistant. Always follow these rules:\n"
    "1. Never use markdown. No asterisks, bold, italics, headers, bullet points, or backticks. Plain text only.\n"
    "2. Be direct. Skip filler phrases like 'Certainly!' or 'Great question!'.\n"
    "3. If listing things, use plain numbered lines.\n"
    "4. Give complete answers. Long replies are automatically split into multiple messages."
)
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

HELP_TEXT = (
    "Commands:\n"
    "help - show this list\n"
    "ping - check if bot is online\n"
    "model - show active AI model\n"
    "about - about this bot\n"
    "reset - clear conversation history"
)

ABOUT_TEXT = "AI assistant powered by Claude. Replies via SMS. Text 'help' for commands."

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

REDIS_URL = os.environ.get("REDIS_URL", "")
_redis: redis.Redis | None = None
if REDIS_URL:
    _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    logger.info("Using Redis for conversation persistence")
else:
    logger.info("No REDIS_URL set — using in-memory conversation storage")

_local_conversations: dict[str, list[dict]] = {}


def get_history(sender: str) -> list[dict]:
    if _redis:
        raw = _redis.get(f"conv:{sender}")
        return json.loads(raw) if raw else []
    return _local_conversations.setdefault(sender, [])


def add_to_history(sender: str, role: str, content: str) -> None:
    history = get_history(sender)
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    if _redis:
        _redis.set(f"conv:{sender}", json.dumps(history))
    else:
        _local_conversations[sender] = history


def clear_history(sender: str) -> None:
    if _redis:
        _redis.delete(f"conv:{sender}")
    else:
        _local_conversations.pop(sender, None)


SMS_CHAR_LIMIT = 1600

def strip_markdown(text: str) -> str:
    """Remove markdown formatting that renders as literal symbols in SMS."""
    # Remove code blocks
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove bold/italic (**, *, __, _)
    text = re.sub(r'\*{1,3}([^*\n]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_\n]+)_{1,3}', r'\1', text)
    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove inline code
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    # Normalize markdown bullets to dashes
    text = re.sub(r'^\s*[*•]\s+', '- ', text, flags=re.MULTILINE)
    # Collapse excess blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def sanitize_for_sms(text: str) -> str:
    """Replace non-GSM-7 characters to keep encoding at 160 chars/segment."""
    replacements = {
        '\u2018': "'", '\u2019': "'",   # curly single quotes
        '\u201c': '"', '\u201d': '"',   # curly double quotes
        '\u2013': '-', '\u2014': '-',   # en/em dash
        '\u2026': '...',                # ellipsis
        '\u00a0': ' ',                  # non-breaking space
    }
    for orig, replacement in replacements.items():
        text = text.replace(orig, replacement)
    # Drop any remaining non-ASCII that would force UCS-2 encoding
    text = text.encode('ascii', errors='ignore').decode('ascii')
    return text


def split_for_sms(text: str) -> list[str]:
    """Split text into SMS_CHAR_LIMIT-sized chunks at word boundaries."""
    if len(text) <= SMS_CHAR_LIMIT:
        return [text]
    parts = []
    remaining = text
    while len(remaining) > SMS_CHAR_LIMIT:
        chunk = remaining[:SMS_CHAR_LIMIT].rsplit(' ', 1)[0]
        if not chunk:
            chunk = remaining[:SMS_CHAR_LIMIT]
        parts.append(chunk)
        remaining = remaining[len(chunk):].lstrip()
    if remaining:
        parts.append(remaining)
    logger.info("Split response into %d SMS parts", len(parts))
    return parts


def send_sms(to: str, body: str) -> None:
    """Send via Mailgun API to carrier email gateway."""
    last_exc = None
    for attempt in range(2):
        try:
            resp = httpx.post(
                f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data={
                    "from":     GMAIL_ADDRESS,
                    "to":       to,
                    "subject":  "",
                    "text":     body,
                    "h:Reply-To": MAILGUN_SANDBOX,
                },
                timeout=30,
            )
            resp.raise_for_status()
            logger.info("Sent SMS to %s: %s", to, body)
            return
        except Exception as e:
            last_exc = e
            logger.warning("send_sms attempt %d failed: %s", attempt + 1, e)
    raise last_exc


def extract_body(form, files) -> str:
    for field in ["stripped-text", "body-plain", "Subject", "subject"]:
        val = form.get(field, "").strip()
        if val:
            return val

    attachment_count = int(form.get("attachment-count", 0))
    for i in range(1, attachment_count + 1):
        file = files.get(f"attachment-{i}")
        if file and file.filename.endswith(".txt"):
            text = file.read().decode("utf-8", errors="ignore").strip()
            if text:
                logger.info("Extracted text from %s: %s", file.filename, text)
                return text

    html = form.get("body-html", "").strip()
    if html:
        text = re.sub(r'<[^>]+>', ' ', html).strip()
        text = re.sub(r'\s+', ' ', text).strip()
        if text:
            return text

    logger.warning("Could not extract body. Available form fields: %s", list(form.keys()))
    return ""


def extract_sender(form) -> str:
    raw = form.get("sender") or form.get("From") or ""
    match = re.search(r'<([^>]+)>', raw)
    return match.group(1).strip() if match else raw.strip()


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.route("/incoming", methods=["POST"])
def incoming_email():
    if MAILGUN_WEBHOOK_KEY:
        token     = request.form.get("token", "")
        timestamp = request.form.get("timestamp", "")
        signature = request.form.get("signature", "")
        expected  = hmac.new(
            MAILGUN_WEBHOOK_KEY.encode(),
            f"{timestamp}{token}".encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("Invalid Mailgun signature — request rejected.")
            abort(403)

    sender    = extract_sender(request.form)
    user_text = extract_body(request.form, request.files)

    logger.info("Incoming from %s: %s", sender, user_text)

    sender_allowed = "all" in ALLOWED_GATEWAYS or any(
        allowed.split("@")[0] in sender
        for allowed in ALLOWED_GATEWAYS
    )
    if not sender_allowed:
        logger.warning("Blocked sender: %s", sender)
        return "OK", 200

    if not user_text:
        logger.warning("Empty body — skipping")
        return "OK", 200

    command = user_text.lower().strip()

    if command in ("reset", "clear", "forget"):
        clear_history(sender)
        send_sms(SMS_GATEWAY, "Conversation cleared. Starting fresh.")
        return "OK", 200

    if command == "help":
        send_sms(SMS_GATEWAY, HELP_TEXT)
        return "OK", 200

    if command == "ping":
        send_sms(SMS_GATEWAY, "Pong! Bot is online.")
        return "OK", 200

    if command == "model":
        send_sms(SMS_GATEWAY, f"Model: {MODEL}")
        return "OK", 200

    if command == "about":
        send_sms(SMS_GATEWAY, ABOUT_TEXT)
        return "OK", 200

    add_to_history(sender, "user", user_text)
    history = get_history(sender)

    try:
        response = claude.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=history,
            timeout=30,
        )
        ai_reply = sanitize_for_sms(strip_markdown(response.content[0].text))
    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        ai_reply = "Sorry, I ran into an issue. Please try again."

    add_to_history(sender, "assistant", ai_reply)

    try:
        for part in split_for_sms(ai_reply):
            send_sms(SMS_GATEWAY, part)
    except Exception as e:
        logger.error("Failed to send SMS: %s", e)

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
