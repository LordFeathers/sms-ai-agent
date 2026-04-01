# SMS AI Agent

A lightweight SMS chatbot powered by Claude AI. Send a text to your phone number and get an AI response back — no app required.

The system works by routing SMS messages through an email-to-SMS gateway (your carrier's free email bridge) via Mailgun. Incoming texts arrive as emails, get processed by Claude, and responses are sent back as emails that your carrier converts back to SMS.

---

## How It Works

```
You send a text
    → Carrier converts it to an email
    → Mailgun receives it and forwards to this app
    → Claude generates a reply
    → Mailgun sends a reply email back to your carrier gateway
    → Carrier delivers it to your phone as a text
```

---

## Services You'll Need

### 1. Mailgun (recommended — free tier works)
Used to send and receive emails that become SMS messages.

- Sign up at [mailgun.com](https://mailgun.com)
- You start on a **sandbox domain** (free) — this works, but you can only send to verified recipient addresses
- Add your carrier gateway address (e.g. `5551234567@txt.att.net`) as a verified recipient in your sandbox settings
- Set up **inbound email routing**: under Receiving → Create Route → match all → forward to your app's `/incoming` URL
- Grab your **API key** (Settings → API Keys) and your **sandbox domain name**

### 2. Anthropic (Claude API)
Used to generate responses.

- Sign up at [console.anthropic.com](https://console.anthropic.com)
- Create an API key
- The app uses `claude-haiku-4-5` by default — fast and cheap, well-suited for SMS

### 3. Render (recommended for hosting — free tier works)
Used to run the Flask app.

- Sign up at [render.com](https://render.com)
- Connect your GitHub repo
- Create a new **Web Service**, point it at this repo
- Set environment variables in the Render dashboard (see below)
- Render auto-deploys on every push to `main`

---

## Carrier Email Gateways

Every major US carrier has a free email-to-SMS gateway. Send an email to the address below and it arrives as a text:

| Carrier | Gateway address |
|---|---|
| AT&T | `number@txt.att.net` |
| T-Mobile | `number@tmomail.net` |
| Verizon | `number@vtext.com` |
| Sprint | `number@messaging.sprintpcs.com` |
| US Cellular | `number@email.uscc.net` |
| Metro PCS | `number@mymetropcs.com` |
| Boost Mobile | `number@sms.myboostmobile.com` |

Replace `number` with the 10-digit phone number (no dashes or spaces). Example: `5551234567@txt.att.net`

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-username/sms-ai-agent.git
cd sms-ai-agent
```

### 2. Install dependencies

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file (never commit this) or set these in your hosting dashboard:

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...          # Your Anthropic API key
GMAIL_ADDRESS=you@yourdomain.com      # The "from" address Mailgun sends as
SMS_GATEWAY=5551234567@txt.att.net    # Your phone's carrier gateway address
MAILGUN_SANDBOX=postmaster@sandbox... # Your Mailgun sandbox address (shown in dashboard)
MAILGUN_API_KEY=key-...               # Your Mailgun API key
MAILGUN_DOMAIN=sandbox....mailgun.org # Your Mailgun domain

# Optional
MAILGUN_WEBHOOK_KEY=                  # Mailgun webhook signing key — set this to verify requests are really from Mailgun
ALLOWED_GATEWAYS=                     # Comma-separated list of allowed sender addresses. Defaults to SMS_GATEWAY. Set to "all" to accept from any sender.
SYSTEM_PROMPT=                        # Custom instructions for the AI. Defaults to a helpful SMS-friendly prompt.
MAX_HISTORY=20                        # How many messages to keep in conversation memory (default: 20)
CLAUDE_MODEL=claude-haiku-4-5-20251001 # Claude model to use (default: claude-haiku-4-5)
```

### 4. Run locally

```bash
flask run
```

Or with gunicorn:

```bash
gunicorn app:app --bind 0.0.0.0:5000 --workers 2 --timeout 120
```

### 5. Expose your local server for testing (optional)

Use [ngrok](https://ngrok.com) to get a public URL while developing:

```bash
ngrok http 5000
```

Set the resulting URL + `/incoming` as your Mailgun inbound route.

---

## Deploying to Render

1. Push your code to GitHub
2. Go to [dashboard.render.com](https://dashboard.render.com) → New → Web Service
3. Connect your repo
4. Set **Start Command** to: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120` (Render reads this from the `Procfile` automatically)
5. Add all environment variables under **Environment**
6. Deploy — Render gives you a public URL like `https://your-app.onrender.com`
7. Set that URL + `/incoming` as your Mailgun inbound route

> **Note:** Render free tier spins down after 15 minutes of inactivity and takes ~30 seconds to wake up. Your first text after a period of inactivity may time out. Upgrade to a paid instance type ($7/mo) if this is a problem.

---

## Special Commands

Send these as a text to control the bot:

| Text | Effect |
|---|---|
| `reset` | Clears conversation history |
| `clear` | Same as reset |
| `forget` | Same as reset |

---

## Environment Variable Reference

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key from console.anthropic.com |
| `GMAIL_ADDRESS` | Yes | The "from" email address used when sending via Mailgun |
| `GMAIL_APP_PASSWORD` | No | Legacy — unused, can be omitted |
| `SMS_GATEWAY` | Yes | Your phone's carrier email gateway (e.g. `5551234567@txt.att.net`) |
| `MAILGUN_SANDBOX` | Yes | Your Mailgun postmaster address (used as Reply-To) |
| `MAILGUN_API_KEY` | Yes | Mailgun API key |
| `MAILGUN_DOMAIN` | Yes | Mailgun domain (e.g. `sandbox123.mailgun.org`) |
| `MAILGUN_WEBHOOK_KEY` | No | Mailgun webhook signing key — strongly recommended for security |
| `ALLOWED_GATEWAYS` | No | Comma-separated sender allowlist. Defaults to `SMS_GATEWAY`. Use `all` to allow any sender. |
| `SYSTEM_PROMPT` | No | Custom system prompt for Claude |
| `MAX_HISTORY` | No | Number of messages to retain per conversation (default: `20`) |
| `CLAUDE_MODEL` | No | Claude model ID (default: `claude-haiku-4-5-20251001`) |
| `PORT` | No | Port to listen on (default: `5000`, set automatically by Render) |

---

## Troubleshooting

**Texts go out but no reply comes back**
- Check your Render logs for `ERROR: Failed to send SMS` — this means Mailgun rejected the send
- On Mailgun sandbox, you must add your carrier gateway as a verified recipient
- Confirm `MAILGUN_API_KEY` and `MAILGUN_DOMAIN` are set correctly

**App receives texts but shows "Empty body — skipping"**
- Your carrier formats the email differently than expected
- Check logs for `Available form fields:` — this will show what Mailgun actually received
- Some carriers put the message text in the `Subject` field (handled automatically)

**Texts are being blocked with "Blocked sender"**
- The `From` address in the email doesn't match `ALLOWED_GATEWAYS`
- Set `ALLOWED_GATEWAYS=all` temporarily to confirm everything else works, then narrow it down

**App isn't receiving texts at all**
- Confirm your Mailgun inbound route points to `https://your-app.onrender.com/incoming`
- Hit `https://your-app.onrender.com/health` — should return `{"status": "ok"}`
- Check that Render hasn't spun down the free instance

---

## Architecture

- **Flask** — HTTP server, single `/incoming` endpoint
- **Mailgun** — Email routing layer (inbound + outbound)
- **Anthropic Claude** — AI response generation
- **Gunicorn** — Production WSGI server
- Conversation history is stored in memory — it resets if the app restarts
