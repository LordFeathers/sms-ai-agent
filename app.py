import os
import logging
import smtplib
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, request, abort
import anthropic
 
# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
 
# ── App setup ──────────────────────────────────────────────────────────────
app = Flask(__name__)
 
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS       = os.environ["GMAIL_ADDRESS"]         # your Gmail e.g. yourbot@gmail.com
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]    # Gmail App Password (not your real password)
SMS_GATEWAY         = os.environ["SMS_GATEWAY"]           # e.g. 9292453610@tmomail.net
MAILGUN_SANDBOX     = os.environ["MAILGUN_SANDBOX"]       # e.g. bot@sandbox51c4356966ad4dfdbb93.mailgun.org
ALLOWED_GATEWAYS    = set(os.environ.get("ALLOWED_GATEWAYS", SMS_GATEWAY).split(","))
MAILGUN_WEBHOOK_KEY = os.environ.get("MAILGUN_WEBHOOK_KEY", "")  # optional signing key
SYSTEM_PROMPT      = os.environ.get("SYSTEM_PROMPT", "You are a helpful assistant. Keep replies concise (under 300 characters when possible) since responses are delivered via SMS.")
MAX_HISTORY        = int(os.environ.get("MAX_HISTORY", "20"))
MODEL              = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
 
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
 
# ── In-memory conversation store ───────────────────────────────────────────
# Keyed by the sender's email/gateway address
conversations: dict[str, list[dict]] = {}
 
 
def get_history(sender: str) -> list[dict]:
    return conversations.setdefault(sender, [])
 
 
def add_to_history(sender: str, role: str, content: str) -> None:
    history = get_history(sender)
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY:
        conversations[sender] = history[-MAX_HISTORY:]
 
 
def send_sms(to: str, body: str) -> None:
    """Send a message via Gmail SMTP → carrier email gateway."""
    msg = MIMEMultipart()
    msg["From"]     = GMAIL_ADDRESS
    msg["To"]       = to
    msg["Subject"]  = ""   # carriers ignore subject; keep blank
    msg["Reply-To"] = os.environ.get("MAILGUN_SANDBOX", GMAIL_ADDRESS)  # replies route back through Mailgun
    msg.attach(MIMEText(body, "plain"))
 
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, to, msg.as_string())
    logger.info("Sent SMS to %s: %s", to, body[:80])
 
 
def extract_body(form) -> str:
    """
    Pull the plain-text message body from a Mailgun inbound webhook.
    Mailgun posts fields: stripped-text, body-plain, body-html, etc.
    We prefer stripped-text (reply only, no quoted history).
    """
    text = (
        form.get("stripped-text")
        or form.get("body-plain")
        or ""
    ).strip()
    return text
 
 
def extract_sender(form) -> str:
    """Return the From address from the Mailgun webhook payload."""
    return form.get("sender") or form.get("From") or ""
 
 
# ── Routes ─────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return {"status": "ok"}, 200
 
 
@app.route("/incoming", methods=["POST"])
def incoming_email():
    # ── Optional: verify Mailgun webhook signature ─────────────────────────
    if MAILGUN_WEBHOOK_KEY:
        import hmac
        import hashlib
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
 
    sender      = extract_sender(request.form)
    user_text   = extract_body(request.form)
 
    logger.info("Incoming from %s: %s", sender, user_text)
 
    # ── Allowlist check ────────────────────────────────────────────────────
    # T-Mobile sends replies FROM an address like 9292453610@tmomail.net
    # Accept any address that contains one of our allowed gateway numbers
    sender_allowed = any(
        allowed.split("@")[0] in sender
        for allowed in ALLOWED_GATEWAYS
    )
    if not sender_allowed:
        logger.warning("Blocked sender: %s", sender)
        return "OK", 200   # return 200 so Mailgun doesn't retry
 
    if not user_text:
        return "OK", 200
 
    # ── Special commands ───────────────────────────────────────────────────
    if user_text.lower() in ("reset", "clear", "forget"):
        conversations.pop(sender, None)
        send_sms(SMS_GATEWAY, "Conversation cleared! Starting fresh.")
        return "OK", 200
 
    # ── Build history and call Claude ──────────────────────────────────────
    add_to_history(sender, "user", user_text)
    history = get_history(sender)
 
    try:
        response = claude.messages.create(
            model=MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=history,
        )
        ai_reply = response.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        ai_reply = "Sorry, I ran into an issue. Please try again."
 
    add_to_history(sender, "assistant", ai_reply)
 
    # ── Send reply back via SMS gateway ───────────────────────────────────
    try:
        send_sms(SMS_GATEWAY, ai_reply)
    except Exception as e:
        logger.error("Failed to send SMS: %s", e)
 
    return "OK", 200
 
 
# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
 
