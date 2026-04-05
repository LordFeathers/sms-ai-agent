import os
import re
import logging
import json
import hmac
import hashlib
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx
from flask import Flask, request, abort
from google import genai
from google.genai import types
import redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

GEMINI_API_KEY      = os.environ["GEMINI_API_KEY"]
GMAIL_ADDRESS       = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")  # unused legacy variable
SMS_GATEWAY         = os.environ["SMS_GATEWAY"]
MAILGUN_SANDBOX     = os.environ["MAILGUN_SANDBOX"]
MAILGUN_API_KEY     = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN      = os.environ.get("MAILGUN_DOMAIN", "")
ALLOWED_GATEWAYS    = set(os.environ.get("ALLOWED_GATEWAYS", SMS_GATEWAY).split(","))
MAILGUN_WEBHOOK_KEY = os.environ.get("MAILGUN_WEBHOOK_KEY", "")
MAX_HISTORY         = int(os.environ.get("MAX_HISTORY", "20"))
MODEL               = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

TIMEZONE             = os.environ.get("TIMEZONE", "America/New_York")
GOOGLE_MAPS_API_KEY  = os.environ.get("GOOGLE_MAPS_API_KEY", "") or GEMINI_API_KEY

DEFAULT_SYSTEM_PROMPT = (
    "You are a personal AI assistant replying via SMS. Always follow these rules:\n"
    "1. Plain text only. No markdown, asterisks, bold, italics, headers, bullets, or backticks.\n"
    "2. Be direct and conversational. No filler phrases like 'Certainly!' or 'Great question!'.\n"
    "3. Use numbered lines for lists.\n"
    "4. Give complete answers. Long replies are split into multiple messages automatically.\n"
    "5. Use Google Search for anything real-time: weather, news, prices, hours, transit, scores, etc. Always search before saying you don't know something current.\n"
    "6. For directions, use the get_directions tool for precise step-by-step routes. Default to public transit unless the user says otherwise.\n"
    "7. If the user shares a URL, use fetch_url to read it and summarize or answer questions about it.\n"
    "8. Use what you know about the user (provided below) to personalize answers. Use their name if known, use their location for local questions."
)
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

HELP_TEXT = (
    "Commands:\n"
    "help - show this list\n"
    "ping - check if bot is online\n"
    "model - show active AI model\n"
    "about - about this bot\n"
    "profile - show remembered facts\n"
    "reset - clear conversation history\n"
    "resetprofile - forget all saved facts"
)

ABOUT_TEXT = (
    "Made by Yaakov Sassoon. Powered by Gemini AI with real-time Google Search and Google Maps. "
    "Runs on Railway, sends SMS via Mailgun. "
    "Remembers your conversation and learns facts about you over time. "
    "Text 'help' for commands."
)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

REDIS_URL = os.environ.get("REDIS_URL", "")
_redis: redis.Redis | None = None
if REDIS_URL:
    _redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    logger.info("Using Redis for conversation persistence")
else:
    logger.info("No REDIS_URL set — using in-memory conversation storage")

_local_conversations: dict[str, list[dict]] = {}
_local_profiles: dict[str, dict] = {}


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


def get_profile(sender: str) -> dict:
    if _redis:
        raw = _redis.get(f"profile:{sender}")
        return json.loads(raw) if raw else {}
    return _local_profiles.get(sender, {})


def save_profile(sender: str, profile: dict) -> None:
    if _redis:
        _redis.set(f"profile:{sender}", json.dumps(profile))
    else:
        _local_profiles[sender] = profile


def history_to_gemini(history: list[dict]) -> list[types.Content]:
    """Convert stored history to Gemini Content format."""
    contents = []
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))
    return contents


def fetch_url(url: str) -> str:
    """Fetch and return the text content of a web page.

    Args:
        url: The URL to fetch and read.

    Returns:
        The readable text content of the page, up to 5000 characters.
    """
    try:
        resp = httpx.get(
            url, timeout=10, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', resp.text, flags=re.IGNORECASE)
        text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:5000] if len(text) > 5000 else text
    except Exception as e:
        return f"Could not fetch URL: {e}"


def get_directions(origin: str, destination: str, mode: str = "transit") -> str:
    """Get step-by-step directions using Google Maps.

    Args:
        origin: Starting address or location.
        destination: Destination address or location.
        mode: Travel mode — transit, walking, driving, or bicycling. Default: transit.

    Returns:
        Step-by-step directions with times and transit details.
    """
    if not GOOGLE_MAPS_API_KEY:
        return "Google Maps API key not configured."
    try:
        resp = httpx.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params={
                "origin": origin,
                "destination": destination,
                "mode": mode,
                "alternatives": "false",
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=10,
        )
        data = resp.json()
        if data["status"] != "OK":
            return f"No directions found ({data['status']})."
        leg = data["routes"][0]["legs"][0]
        lines = [
            f"{leg['start_address']} to {leg['end_address']}",
            f"Total: {leg['distance']['text']}, {leg['duration']['text']}",
        ]
        for step in leg["steps"]:
            text = re.sub(r"<[^>]+>", "", step["html_instructions"])
            transit = step.get("transit_details")
            if transit:
                line_name = (transit.get("line", {}).get("short_name")
                             or transit.get("line", {}).get("name", ""))
                dep = transit.get("departure_stop", {}).get("name", "")
                arr = transit.get("arrival_stop", {}).get("name", "")
                dep_time = transit.get("departure_time", {}).get("text", "")
                num_stops = transit.get("num_stops", "")
                text = f"Take {line_name} from {dep} ({dep_time}) to {arr} — {num_stops} stops"
            lines.append(f"- {text} ({step['distance']['text']})")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Maps API error: %s", e)
        return f"Could not get directions: {e}"


def call_gemini(system: str, history: list[dict]) -> str:
    """Call Gemini with conversation history, date/time context, and all tools."""
    now = datetime.now(ZoneInfo(TIMEZONE))
    datetime_str = now.strftime("%A, %B %d, %Y %I:%M %p %Z")
    system_with_time = f"Current date and time: {datetime_str}\n\n{system}"
    response = gemini_client.models.generate_content(
        model=MODEL,
        contents=history_to_gemini(history),
        config=types.GenerateContentConfig(
            system_instruction=system_with_time,
            tools=[
                types.Tool(google_search=types.GoogleSearch()),
                get_directions,
                fetch_url,
            ],
        ),
    )
    return response.text


def extract_facts_background(sender: str, user_text: str, ai_reply: str) -> None:
    """Run in a background thread: extract user facts and merge into profile."""
    try:
        existing = get_profile(sender)
        existing_str = json.dumps(existing) if existing else "{}"
        response = gemini_client.models.generate_content(
            model=MODEL,
            contents=[types.Content(role="user", parts=[types.Part(text=(
                f"Existing profile: {existing_str}\n\n"
                f"User said: {user_text}\n"
                f"Assistant replied: {ai_reply}\n\n"
                "What new facts about the user can be extracted? Return updated JSON only."
            ))])],
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Extract personal facts about the user from the conversation snippet below. "
                    "Return ONLY a JSON object with any of these keys if mentioned: "
                    "name, location, address, email, phone, occupation, age, preferences, family, other. "
                    "Only include facts the user explicitly stated. "
                    "If nothing new, return {}. No explanation, just JSON."
                ),
                tools=[
                    types.Tool(google_search=types.GoogleSearch()),
                ],
            ),
        )
        raw = response.text.strip()
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            facts = json.loads(match.group())
            if facts:
                existing.update(facts)
                save_profile(sender, existing)
                logger.info("Updated profile for %s: %s", sender, existing)
    except Exception as e:
        logger.warning("Fact extraction failed: %s", e)


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
    # Remove search grounding citations like [1], [2], etc.
    text = re.sub(r'\[\d+\]', '', text)
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

    if command == "resetprofile":
        save_profile(sender, {})
        send_sms(SMS_GATEWAY, "Profile cleared. I no longer remember any facts about you.")
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

    if command == "profile":
        profile = get_profile(sender)
        if profile:
            facts = ", ".join(f"{k}: {v}" for k, v in profile.items())
            send_sms(SMS_GATEWAY, f"What I know about you: {facts}")
        else:
            send_sms(SMS_GATEWAY, "I don't have any facts saved about you yet.")
        return "OK", 200

    add_to_history(sender, "user", user_text)
    history = get_history(sender)

    # Build system prompt, injecting known user facts if available
    profile = get_profile(sender)
    system = SYSTEM_PROMPT
    if profile:
        facts = ", ".join(f"{k}: {v}" for k, v in profile.items())
        system += f"\n\nKnown facts about this user: {facts}"

    try:
        ai_reply = sanitize_for_sms(strip_markdown(call_gemini(system, history)))
    except Exception as e:
        logger.error("Gemini API error: %s", e)
        ai_reply = "Sorry, I ran into an issue. Please try again."

    add_to_history(sender, "assistant", ai_reply)

    # Extract and save user facts in the background (no delay to SMS response)
    threading.Thread(
        target=extract_facts_background,
        args=(sender, user_text, ai_reply),
        daemon=True,
    ).start()

    try:
        for part in split_for_sms(ai_reply):
            send_sms(SMS_GATEWAY, part)
    except Exception as e:
        logger.error("Failed to send SMS: %s", e)

    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
