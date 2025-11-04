# bot.py
# Crypto RSI/MACD bot with robust 4H→1H fallback + diagnostics + news + daily table
# Works on GitHub Actions (requests + pandas only)

import os, json, time, math, datetime as dt
from datetime import timezone, timedelta
import requests
import pandas as pd

# -----------------------
# Config (from env)
# -----------------------
COINS = os.getenv("COINS", "BTC,ETH,BNB,SOL,BGB").split(",")
BASE = "USDT"

RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_BUY = float(os.getenv("RSI_BUY", "30"))         # BUY threshold
RSI_OPP = float(os.getenv("RSI_OPP", "40"))         # OPPORTUNITY threshold
RSI_WIDE = os.getenv("RSI_WIDE", "false").lower() == "true"

FAST = int(os.getenv("MACD_FAST", "12"))
SLOW = int(os.getenv("MACD_SLOW", "26"))
SIGN = int(os.getenv("MACD_SIGNAL", "9"))

ENABLE_OPP = os.getenv("ENABLE_OPPORTUNITY", "true").lower() == "true"
OPP_COOLDOWN_H = int(os.getenv("OPPORTUNITY_COOLDOWN_HOURS", "6"))

NEWS_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "").strip()
NEWS_MOVE_PCT = float(os.getenv("NEWS_MOVE_PCT", "3.0"))
NEWS_COOLDOWN_H = int(os.getenv("NEWS_COOLDOWN_HOURS", "6"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_DIR = ".state"
STATE_FILE = f"{STATE_DIR}/state.json"

# Lookbacks (increase to avoid short DF on Bitget/BGB)
LOOKBACK_1H = int(os.getenv("LOOKBACK_1H", "900"))   # ~37 days of 1h
LOOKBACK_1D = int(os.getenv("LOOKBACK_1D", "500"))   # ~1.3 years of 1D

UTC_TZ = timezone.utc
LOCAL_TZNAME = os.getenv("LOCAL_TZ", "Europe/Rome")

# -----------------------
# Utilities
# -----------------------
def now_utc():
    return dt.datetime.now(UTC_TZ)

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("[TELEGRAM] Missing token/chat. Message:")
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[TELEGRAM] send failed: {e}")

def ensure_state():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(STATE_FILE):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_daily": "", "last_heartbeat": "", "cooldowns": {}, "newsCooldowns": {}}, f)

def load_state():
    ensure_state()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f)

def pct(a, b):
    if b == 0 or b is None or a is None:
        return 0.0
    return (a/b - 1.0) * 100.0

# -----------------------
# Data providers
# -----------------------
def fetch_binance(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    # symbol e.g. BTCUSDT, interval '1h' or '1d'
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data:
        return pd.DataFrame()
    cols = ["time","o","h","l","c","v","ct","qv","n","tb","qtb","ig"]
    df = pd.DataFrame(data, columns=cols)
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df["open"] = df["o"].astype(float)
    df["high"] = df["h"].astype(float)
    df["low"] = df["l"].astype(float)
    df["close"] = df["c"].astype(float)
    df["volume"] = df["v"].astype(float)
    return df[["time","open","high","low","close","volume"]].set_index("time")

def fetch_bitget_bgb(interval: str, limit: int) -> pd.DataFrame:
    # Bitget v2 history candles, BGB/USDT
    gran = {"1h":"1h", "1d":"1d"}[interval]
    url = "https://api.bitget.com/api/v2/spot/market/history-candles"
    params = {"symbol":"BGBUSDT", "granularity": gran, "limit": str(min(limit, 400))}
    r = requests.get(url, params=params, timeout=20)
    if r.status_code != 200:
        print(f"[BGB] Bitget fetch fail ({url}): {r.status_code} {r.text[:200]}")
        return pd.DataFrame()
    j = r.json()
    if j.get("code") != "00000" or "data" not in j:
        print(f"[BGB] Bitget unexpected payload: {j}")
        return pd.DataFrame()
    # Bitget returns newest first, [ts, o, h, l, c, v]
    rows = j["data"][::-1]
    if not rows:
        return pd.DataFrame()
    recs = []
    for row in rows:
        ts, o, h, l, c, v = row
        t = pd.to_datetime(int(ts), unit="ms", utc=True)
        recs.append([t, float(o), float(h), float(l), float(c), float(v)])
    df = pd.DataFrame(recs, columns=["time","open","high","low","close","volume"]).set_index("time")
    return df

def fetch_ohlc_1h(symbol: str) -> pd.DataFrame:
    if symbol == "BGB":
        return fetch_bitget_bgb("1h", LOOKBACK_1H)
    else:
        return fetch_binance(symbol + BASE, "1h", LOOKBACK_1H)

def fetch_ohlc_1d(symbol: str) -> pd.DataFrame:
    if symbol == "BGB":
        return fetch_bitget_bgb("1d", LOOKBACK_1D)
    else:
        return fetch_binance(symbol + BASE, "1d", LOOKBACK_1D)

# -----------------------
# Indicators (pure pandas)
# -----------------------
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    roll_up = ema(up, n)
    roll_down = ema(down, n)
    rs = roll_up / roll_down.replace(0, 1e-10)
    return 100.0 - (100.0 / (1.0 + rs))

def macd(series: pd.Series, fast=12, slow=26, signal=9):
    m = ema(series, fast) - ema(series, slow)
    s = ema(m, signal)
    h = m - s
    return m, s, h

def add_indicators(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty or len(df) < max(SLOW+SIGN+5, 60):
        print(f"⚠️ add_indicators: dataframe vuoto o troppo corto (len = {0 if df is None else len(df)} )")
        return None
    out = df.copy()
    out["rsi"] = rsi(out["close"], RSI_LEN)
    m, s, h = macd(out["close"], FAST, SLOW, SIGN)
    out["macd"] = m
    out["macd_signal"] = s
    out["macd_hist"] = h
    return out.dropna().copy()

# 4H from 1H with fallback to 1H if needed
def resample_to_4h(df1h: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    if df1h is None or df1h.empty:
        return None, "no-1h"
    # 4h resample (use 'h' to avoid pandas future warning)
    ohlc = df1h["close"].resample("4h", label="right", closed="right").ohlc()
    vol = df1h["volume"].resample("4h", label="right", closed="right").sum()
    df4 = pd.concat([ohlc, vol], axis=1)
    df4.columns = ["open","high","low","close","volume"]
    df4 = df4.dropna()
    if len(df4) < 60:
        # Fallback to 1h if 4h too short
        print(f"[FALLBACK] 4H insufficiente (len={len(df4)}). Uso 1H per segnali intraday.")
        return df1h, "1h"
    return df4, "4h"

# -----------------------
# Signals + reasons
# -----------------------
def evaluate_signals(symbol: str, state, nowu: dt.datetime) -> dict:
    """
    Returns dict:
      {
        'ok': bool,
        'reason': 'text',
        'price': last_close,
        'buy': True/False,
        'opp': True/False,
        'trend1d': 'UP/DOWN/FLAT/UNKNOWN',
        'frameUsed': '4h or 1h'
      }
    """
    try:
        df1h = fetch_ohlc_1h(symbol)
        df1d = fetch_ohlc_1d(symbol)
    except Exception as e:
        return {"ok": False, "reason": f"fetch-error: {e}"}

    if df1h is None or df1h.empty:
        return {"ok": False, "reason": "no-1h-data"}
    if df1d is None or df1d.empty:
        return {"ok": False, "reason": "no-1d-data"}

    # Trend 1D
    d1 = add_indicators(df1d)
    if d1 is None or d1.empty:
        trend = "UNKNOWN"
    else:
        last = d1.iloc[-1]
        prev = d1.iloc[-2] if len(d1) > 1 else last
        # simple trend: macd above signal → UP; below → DOWN; else FLAT
        if last["macd"] > last["macd_signal"] and last["macd_hist"] > prev["macd_hist"]:
            trend = "UP"
        elif last["macd"] < last["macd_signal"] and last["macd_hist"] < prev["macd_hist"]:
            trend = "DOWN"
        else:
            trend = "FLAT"

    # 4H (or fallback 1H) for intraday signals
    frameUsed = "4h"
    dfX, used = resample_to_4h(df1h)
    if used == "1h":
        frameUsed = "1h"

    x = add_indicators(dfX)
    if x is None or x.empty:
        return {"ok": False, "reason": f"no-{frameUsed}-indicators"}

    last = x.iloc[-1]
    prev = x.iloc[-2] if len(x) > 1 else last
    price = last["close"]

    # MACD cross up?
    crossUp = last["macd"] >= last["macd_signal"] and prev["macd"] < prev["macd_signal"]
    histImproving = (last["macd_hist"] > prev["macd_hist"])

    # RSI thresholds (optionally widen 5%)
    rsiBuy = RSI_BUY - (5 if RSI_WIDE else 0)
    rsiOpp = RSI_OPP - (5 if RSI_WIDE else 0)

    condBUY = (last["rsi"] <= rsiBuy) and crossUp
    condOPPcore = (last["rsi"] <= rsiOpp) and (last["macd"] > last["macd_signal"])
    condOPP = condOPPcore or histImproving

    # Trend filter: require 1D not strongly DOWN for BUY; allow OPP even in DOWN if hist improving
    trendOK = (trend != "DOWN") or condOPP

    if not trendOK:
        return {"ok": False, "reason": f"blocked-by-1D-trend({trend})", "price": price, "trend1d": trend, "frameUsed": frameUsed}

    # Cooldown logic for OPP
    coinKey = f"{symbol}_OPP"
    cd_until = state["cooldowns"].get(coinKey, 0)
    cooldown_active = nowu.timestamp() < cd_until

    buy = condBUY
    opp = False
    if ENABLE_OPP:
        # only send OPP if not in cooldown
        opp = (condOPP and not buy and not cooldown_active)

    if buy:
        return {"ok": True, "reason": "BUY", "price": price, "buy": True, "opp": False, "trend1d": trend, "frameUsed": frameUsed}
    if opp:
        # set cooldown
        state["cooldowns"][coinKey] = (nowu + timedelta(hours=OPP_COOLDOWN_H)).timestamp()
        return {"ok": True, "reason": "OPPORTUNITY", "price": price, "buy": False, "opp": True, "trend1d": trend, "frameUsed": frameUsed}

    # No signal: provide reason
    details = []
    if last["rsi"] > rsiOpp:
        details.append(f"RSI>{rsiOpp:.0f}")
    if not crossUp and not buy:
        details.append("no MACD cross↑")
    if not histImproving and not crossUp:
        details.append("hist not improving")

    return {"ok": False, "reason": "no-signal(" + ", ".join(details) + ")", "price": price, "trend1d": trend, "frameUsed": frameUsed}

# -----------------------
# Daily + Heartbeat
# -----------------------
def should_send_daily_report(state):
    today = now_utc().date().isoformat()
    return state.get("last_daily", "") != today

def should_send_heartbeat(state):
    today = now_utc().date().isoformat()
    return state.get("last_heartbeat", "") != today

def build_daily_table():
    lines = []
    lines.append("SYMB   1D Δ%   MACDΔ%   TREND")
    lines.append("-----  ------  -------  ------")
    for c in COINS:
        d1 = fetch_ohlc_1d(c)
        if d1 is None or d1.empty or len(d1) < 2:
            lines.append(f"{c:5}    n/a     n/a    n/a")
            continue
        d1i = add_indicators(d1)
        if d1i is None or d1i.empty:
            lines.append(f"{c:5}    n/a     n/a    n/a")
            continue
        last = d1i.iloc[-1]
        prev = d1i.iloc[-2]
        pchg = pct(last["close"], prev["close"])
        macddelta = (last["macd"] - last["macd_signal"]) - (prev["macd"] - prev["macd_signal"])
        t = "UP" if last["macd"] >= last["macd_signal"] else "DOWN"
        lines.append(f"{c:5} {pchg:7.2f}% {macddelta:8.3f}  {t:>5}")
    return "<pre>" + "\n".join(lines) + "</pre>"

# -----------------------
# News (CryptoPanic)
# -----------------------
def news_allowed_for(symbol: str, state, nowu: dt.datetime, move_pct_24h: float) -> bool:
    if not NEWS_TOKEN:
        return False
    if abs(move_pct_24h) < NEWS_MOVE_PCT:
        return False
    key = f"{symbol}_NEWS"
    until = state.get("newsCooldowns", {}).get(key, 0)
    if nowu.timestamp() < until:
        return False
    return True

def mark_news_cooldown(symbol: str, state, nowu: dt.datetime):
    key = f"{symbol}_NEWS"
    state["newsCooldowns"][key] = (nowu + timedelta(hours=NEWS_COOLDOWN_H)).timestamp()

def try_send_news(symbol: str, move_pct_24h: float, state, nowu: dt.datetime):
    if not news_allowed_for(symbol, state, nowu, move_pct_24h):
        print(f"[NEWS] No headlines for {symbol} (PriceΔ {move_pct_24h:+.2f}%).")
        return
    url = "https://cryptopanic.com/api/v1/posts/"
    params = {"auth_token": NEWS_TOKEN, "currencies": symbol.lower(), "kind": "news", "public": "true", "filter": "hot"}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        j = r.json()
        posts = j.get("results", [])[:5]
        if not posts:
            print(f"[NEWS] No headlines for {symbol} (PriceΔ {move_pct_24h:+.2f}%).")
            return
        lines = [f"🗞️ <b>{symbol} news</b> (Δ24h {move_pct_24h:+.2f}%)"]
        for p in posts:
            title = p.get("title", "")[:120]
            link = p.get("url", "")
            votes = p.get("votes", {})
            tag = []
            if votes.get("important"): tag.append("⭐")
            if votes.get("positive"): tag.append("🟢")
            if votes.get("negative"): tag.append("🔴")
            lines.append("• " + "".join(tag) + f" <a href=\"{link}\">{title}</a>")
        send_telegram("\n".join(lines))
        mark_news_cooldown(symbol, state, nowu)
    except Exception as e:
        print(f"[NEWS] fetch error for {symbol}: {e}")

# -----------------------
# Main run
# -----------------------
def run_once():
    state = load_state()
    nowu = now_utc()
    print(f"[SYNC] Start: {nowu.strftime('%Y-%m-%d %H:%M:%S')} UTC | Local: {nowu.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} | last_daily={state.get('last_daily','')} | last_heartbeat={state.get('last_heartbeat','')}")

    # Signals
    summary = []
    had_buy = False
    had_opp = False
    for c in COINS:
        res = evaluate_signals(c, state, nowu)
        if not res.get("ok", False):
            reason = res.get("reason","")
            print(f"{c} no-alert: {reason}")
            continue
        price = res["price"]
        trend = res["trend1d"]
        frame = res["frameUsed"]
        if res.get("buy", False):
            had_buy = True
            msg = f"🟢 <b>BUY</b> {c}/{BASE} ({frame}, 1D {trend})\nPrezzo: {price:.4f}"
            send_telegram(msg)
            summary.append(f"{c}: BUY")
        elif res.get("opp", False):
            had_opp = True
            msg = f"🟡 <b>OPPORTUNITY</b> {c}/{BASE} ({frame}, 1D {trend})\nPrezzo: {price:.4f}"
            send_telegram(msg)
            summary.append(f"{c}: OPP")

    if not had_buy and not had_opp:
        print("Nessun BUY/OPP valido (filtrato da trend 1D / cooldown / condizioni tecniche).")

    # Daily & heartbeat
    try:
        will_daily = should_send_daily_report(state)
        will_heart = should_send_heartbeat(state)
    except Exception as e:
        print(f"[SYNC] log error: {e}")
        will_daily = False
        will_heart = False

    if will_daily:
        report = build_daily_table()
        send_telegram("🗞️ <b>Daily Trend 1D</b>\n" + report)
        state["last_daily"] = nowu.date().isoformat()

    if will_heart:
        send_telegram("✅ Heartbeat: bot attivo e sincronizzato")
        state["last_heartbeat"] = nowu.date().isoformat()

    # News intraday (Δ24h)
    for c in COINS:
        d1 = fetch_ohlc_1d(c)
        if d1 is None or len(d1) < 2:
            continue
        last = d1.iloc[-1]["close"]
        prev = d1.iloc[-2]["close"]
        move = pct(last, prev)
        try_send_news(c, move, state, nowu)

    save_state(state)

if __name__ == "__main__":
    try:
        run_once()
    except Exception as e:
        print(f"[FATAL] {e}")
        # keep non-zero exit to surface on Actions
        raise
