# 🤖 Crypto RSI/MACD Telegram Bot

![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/CrSh30/crypto-telegram-alerts/crypto-bot.yml?label=Build%20Status&style=flat-square)
![License](https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.12+-yellow?style=flat-square)
![Platform](https://img.shields.io/badge/platform-GitHub%20Actions-lightgrey?style=flat-square)

---

### 📊 Automated crypto trend & buy signal notifier  
Runs on **GitHub Actions**, sends alerts via **Telegram** based on **RSI + MACD** indicators.  
Zero hosting required — everything runs serverlessly.

---

## 🚀 Features

- 🟢 **Buy Signals** — RSI < 30 + MACD cross ↑ on 1H timeframe + confirmed 1D uptrend  
- 📈 **Daily 1D Trend Report** — every morning (08:00–08:15 Europe/Rome)  
- 🧭 **Trend Change Alerts** — 1D & optional 4H with cooldown  
- 💬 **Heartbeat** — daily “bot alive” message  
- 🕒 **Serverless** — powered by GitHub Actions + persistent cache state  

---

## ⚙️ Prerequisites

### 1️⃣ Create a Telegram Bot
1. Open [@BotFather](https://t.me/BotFather) on Telegram  
2. Send `/newbot` → choose name and username  
3. Copy the API Token (example: `123456789:ABCdefGhIJKlmNoPQRstuVWXyz`)  
4. Send a message to your bot  
5. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`  
   → find `"chat":{"id":YOUR_CHAT_ID}` → that’s your **Chat ID**

---

### 2️⃣ Fork this Repository
1. Fork the repo to your account  
2. Go to **Settings → Secrets and variables → Actions → New repository secret**  
   Add:

   | Secret | Description |
   |---------|-------------|
   | `TELEGRAM_BOT_TOKEN` | Token from BotFather |
   | `TELEGRAM_CHAT_ID` | Your numeric chat ID |
   | *(optional)* `CMC_API_KEY` | API key from [CoinMarketCap Pro](https://pro.coinmarketcap.com/signup) |

---

### 3️⃣ Enable GitHub Actions
- Check that Actions are enabled  
- Workflow file: `.github/workflows/crypto-bot.yml`  
- Runs every hour (`cron: "0 * * * *"`)  
- Manual trigger:  
  **Actions → Crypto RSI/MACD Bot → Run workflow**

---

## 🧩 Files Overview

| File | Description |
|------|--------------|
| `bot.py` | Core logic (fetch candles, compute RSI/MACD, send alerts) |
| `.github/workflows/crypto-bot.yml` | Automation workflow |
| `.state/last_signals.json` | Cached cooldown & trend state |

---

## 🔑 Environment Variables

| Variable | Description |
|-----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Telegram Bot token |
| `TELEGRAM_CHAT_ID` | Numeric chat ID |
| `CMC_API_KEY` | *(optional)* CoinMarketCap API key |

---

## 🧮 How It Works

1. Every hour GitHub Actions executes `bot.py`  
2. The bot fetches OHLC data from OKX / Bybit / Bitget APIs  
3. Computes **RSI & MACD** via `pandas_ta`  
4. Detects signals and trend changes  
5. Sends Telegram messages for valid events  
6. Persists state file to avoid duplicate alerts  

---

## 💬 Example Outputs

**Buy Signal**

🟢 BTC BUY (RSI < 30 + MACD ↑)
Price: 58670.23 USDT
Time: 2025-10-17 11:00 UTC | Trend 1D: MACD ↑

**Trend Change + Strategy**
📈 BTC Trend 1D changed: DOWN → UP
🧭 BTC: Trend 1D BULLISH. Holder strategy: wait for RSI<30 + MACD ↑ pullback.

**Daily Report**
🗞️ Daily Trend 1D
BTC ↑ (+0.42%) ETH → (+0.00%) BNB ↓ (−0.18%) SOL ↑ (+0.24%) BGB ↓ (−0.03%)
✅ Heartbeat: bot active and synchronized


---

## 🧰 Configuration (inside `bot.py`)

| Variable | Function | Default |
|-----------|-----------|----------|
| `COINS` | Monitored coins | BTC, ETH, BNB, SOL, BGB |
| `RSI_LOW` | RSI threshold for buy | 30 |
| `COOLDOWN_HOURS` | Min hours between signals | 6 |
| `ENABLE_4H_TREND_ALERTS` | Intraday trend change alerts | True |
| `TREND4H_COOLDOWN_HOURS` | Cooldown for 4H alerts | 6 |

Modify and commit → the next run applies your changes automatically.

---

## 🛠 Troubleshooting

| Issue | Cause | Fix |
|-------|--------|-----|
| No Telegram messages | Wrong token or chat ID | Verify secrets |
| “Cache save failed” | Duplicate job key | Dynamic cache key already solves this |
| “KeyError: macd” | API data empty | Retry later |
| No daily report | GitHub cron delay > 5 min | Window extended to 15 min |

---

## 🧩 Roadmap

- [ ] Add CoinMarketCap Price Watch module  
- [ ] Historical signal stats  
- [ ] Multi-chat support  
- [ ] Optional web dashboard  

---

## 🧑‍💻 Author

**CrSh30**  
Designed for long-term holders seeking high-quality, noise-free signals.  
Combines **technical indicators + GitHub automation + Telegram alerts** for effortless crypto monitoring.

---

## 📜 License
Released under the **MIT License** — free for personal & educational use.

