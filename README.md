# KingdomEdge Algo — Self-Hosted Bot Template

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/kingdomedge-bot-template)

**The deployable scaffold for KingdomEdge Algo Ultimate-tier subscribers.** Fork this repo, deploy to your own Railway account (~$5/mo), fill in your env vars, and the bot runs in your name with your broker keys. **KingdomEdge never sees your keys.**

---

## What this is

A single-tenant Flask service that:

1. Receives webhook alerts from the SETS Trade KingdomEdge Algo indicator on TradingView
2. Runs pre-trade risk checks (kill switch, daily loss limit, position size, cooldown, trading hours, etc.)
3. Submits a bracket order (entry + stop loss + up to 3 take profits) to your Alpaca paper or live account

That's it. No multi-tenancy, no subscriber database, no admin endpoints, no Stripe integration. Just your indicator signal → your broker.

If you'd rather KingdomEdge run the bot for you, look at the **Concierge Mentor Track** tier ($499/mo, by application, max 10 active members) at [kingdomedgealgo.com/#pricing](https://kingdomedgealgo.com/#pricing).

---

## 60-second setup

> **Time:** ~30-45 minutes the first time · **Cost:** ~$5/month Railway hosting (after 30-day trial)

### 1. Get your Alpaca keys

If you don't have an Alpaca account yet, [sign up](https://app.alpaca.markets/signup) (free). After signup:

- Go to your [paper trading dashboard](https://app.alpaca.markets/paper/dashboard/overview)
- Click **Generate New Keys** in the API Keys panel
- Copy the **API Key** AND the **API Secret** somewhere safe (you only see the secret once)

> 💡 **Start with paper trading** for at least the first 30 days. The bot defaults to paper. Switch to live only after you've watched it work and trust the behavior.

### 2. Generate a webhook secret

Open a terminal and run one of these to generate a random 32-character secret:

```bash
# macOS / Linux
openssl rand -hex 32

# Windows PowerShell
-join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | % {[char]$_})
```

Save it in a password manager. You'll paste it into both Railway AND your TradingView alert JSON.

### 3. Deploy to Railway

Click the **Deploy on Railway** button at the top of this README. (If this is your first Railway project, you'll sign up first — free trial then ~$5/month.)

When Railway asks for env vars, paste in:

| Variable | Value |
|---|---|
| `WEBHOOK_SECRET` | The 32-char string from step 2 |
| `ALPACA_API_KEY` | Your Alpaca paper API key from step 1 |
| `ALPACA_API_SECRET` | Your Alpaca paper API secret from step 1 |
| `ALPACA_PAPER` | `true` (keep this until you're confident) |
| `QUANTITY_OVERRIDE` | `1` (recommended for the first 2 weeks) |

Click **Deploy**. ~2-3 minutes later, Railway gives you a public URL like `https://your-bot.up.railway.app`.

### 4. Verify the bot is up

Open `https://your-bot.up.railway.app/ping` in a browser. You should see:

```json
{"status": "ok", "service": "kingdomedge-bot-template"}
```

If you do, the bot is running and ready to accept signals.

### 5. Configure the TradingView alert

In your TradingView SETS Trade chart:
- Click **Alerts** (bell icon) → **Create Alert**
- Condition: **"Any alert() function call"** (NOT "Price Crossing")
- Webhook URL: `https://your-bot.up.railway.app/webhook`
- Enable **"Webhook URL"** toggle
- Enable **"Webhook JSON Format"** toggle (this is critical — see [Library Step 6](https://kingdomedgealgo.com/library/#step6))
- In the message field, the SETS Trade indicator generates the JSON automatically — just make sure your alert is set to use the indicator's alert() output

### 6. Watch your first paper trade

When the next SETS signal fires, your bot will:
1. Receive the webhook from TradingView
2. Run risk checks
3. Place a bracket order in your Alpaca paper account

You'll see it in your [Alpaca paper dashboard](https://app.alpaca.markets/paper/dashboard/overview) → Orders / Positions. **The KingdomEdge team will never see this trade** — it's in your account only.

---

## Configuration reference

See `.env.example` for the full list of env vars with descriptions. Required vs optional:

**Required:**
- `WEBHOOK_SECRET`
- `ALPACA_API_KEY`, `ALPACA_API_SECRET`
- `ALPACA_PAPER` (defaults to true)

**Optional but commonly set:**
- `QUANTITY_OVERRIDE` — fixed contract count per trade (overrides signal qty)
- `RISK_*` variables — override the built-in risk defaults
- `SENTRY_DSN` — error tracking

---

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/ping` | GET | Liveness check (returns `{"status": "ok"}`) |
| `/status` | GET | Detailed status including risk-control state + kill switch |
| `/webhook` | POST | TradingView alerts land here (auth: `secret` in JSON body) |
| `/kill` | POST | Engage/disengage emergency kill switch (auth: `secret` in JSON body) |

There are NO admin endpoints. This is a single-tenant bot — you don't manage subscribers.

---

## Going live (after paper-trading shakedown)

Once you've watched the bot work in paper trading for at least 30 days and you trust the behavior:

1. [Generate a live API key on Alpaca](https://app.alpaca.markets/live/dashboard/overview)
2. In Railway, update env vars:
   - `ALPACA_API_KEY` = your live key
   - `ALPACA_API_SECRET` = your live secret
   - `ALPACA_PAPER` = `false`
3. Redeploy (Railway does this automatically when you edit env vars)

**Strongly recommended before going live:**
- Set `RISK_MAX_DAILY_LOSS` to a value you can stomach
- Set `QUANTITY_OVERRIDE` to a small number (1 share/contract) for the first week of live trading
- Confirm Sentry is configured so you get pinged on any errors

---

## Support

- **Subscriber Library:** [kingdomedgealgo.com/library/](https://kingdomedgealgo.com/library/) — full onboarding walkthrough including this bot
- **AI Chatbot:** bottom-right of every page on [kingdomedgealgo.com](https://kingdomedgealgo.com) — answers most "how do I" questions instantly
- **Discord:** Join the community at [discord.gg/en738ANNnj](https://discord.gg/en738ANNnj) — `#ultimate-help` channel for self-hosted bot questions
- **Email:** [support@kingdomedgealgo.com](mailto:support@kingdomedgealgo.com) for billing or anything the chatbot can't answer

**What KingdomEdge supports vs. what's yours:**

| If X is broken | Who looks at it |
|---|---|
| TradingView alert not firing | You + library Step 6 |
| Webhook returns 401 | You (check WEBHOOK_SECRET) |
| Webhook returns 500 | You (check Railway logs) + Discord `#ultimate-help` |
| Alpaca order rejected | You (Alpaca dashboard shows reason) |
| Indicator showing wrong values | KingdomEdge support |
| Bot template has a bug | KingdomEdge support (open a GitHub issue) |

See the [What You Own](https://kingdomedgealgo.com/library/#what-you-own) reference doc for the full ownership map.

---

## Updating

KingdomEdge will push updates to this template (bug fixes, new broker support, new features). To pull them into your fork:

```bash
git remote add upstream https://github.com/markjramos2009/kingdomedge-bot-template
git fetch upstream
git merge upstream/main
git push origin main
```

Railway auto-redeploys when you push to your fork's `main`.

---

## License

MIT — see [LICENSE](./LICENSE). The trading risk disclaimer at the bottom of LICENSE applies to your use of this bot.

---

*KingdomEdge Algo · Self-Hosted Bot Template · v1.0 (2026-06-04)*
