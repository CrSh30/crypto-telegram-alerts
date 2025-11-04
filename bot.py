# bot.py
# Crypto RSI/MACD bot with provider rotation (OKX→Bybit→Binance), robust 4H→1H fallback,
# diagnostics, CryptoPanic news (intraday/daily), safe error handling.

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

# Lookbacks
LOOKBACK_1H = int(os.getenv("LOOKBACK_1H", "900"))   # ~37 days of 1h
LOOKBACK_1D = int(os.getenv("LOOKBACK_1D", "500"))   # ~1.3 years of 1D

UTC_TZ = timezone.utc
LOCAL_TZNAME = os.getenv("LOCAL_TZ", "Europe/Rome")

# Fallback 1H flag
ALLOW_1H_FALLBACK = os.getenv("ALLOW_1H_FALLBACK", "true").lower() == "true"

# Trend filter flag: off / buy_only_up / all_up
TREND_FILTER = os.getenv("TREND_FILTER", "off").lower()

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
# Provider helpers (OKX / Bybit / Binance)
# -----------------------
OKX_IDS = {"BTC": "BTC-USDT", "ETH": "ETH-USDT", "BNB": "BNB-USDT", "SOL": "SOL-USDT"}
BYBIT_SYM = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT"}

def df_from_klines(rows, schema="binance"):
    # rows: list, newest-last or first depending on api. Standardize to newest-last with UTC index.
    # Binance schema known; OKX/Bybit convert below.
    if schema == "binance":
        cols = ["time","o","h","l","c","v","ct","qv","n","tb","qtb","ig"]
        df = pd.DataFrame(rows, columns=cols)
        df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        df["open"] = df["o"].astype(float)
        df["high"] = df["h"].astype(float)
        df["low"] = df["l"].astype(float)
        df["close"] = df["c"].astype(float)
        df["volume"] = df["v"].astype(float)
        return df[["time","open","high","low","close","volume"]].set_index("time")

def fetch_okx(inst_id: str, bar: str, limit: int) -> pd.DataFrame:
    # bar: "1H"/"1D"
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": inst_id, "bar": bar, "limit": min(limit, 300)}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    data = j.get("data", [])
    if not data:
        return pd.DataFrame()
    # OKX returns newest first: [ts, o,h,l,c, vol, volCcy, volCcyQuote, ...]
    rows = []
    for x in data[::-1]:
        ts, o, h, l, c, vol = int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])
        rows.append([pd.to_datetime(ts, unit="ms", utc=True), o, h, l, c, vol])
    df = pd.DataFrame(rows, columns=["time","open","high","low","close","volume"]).set_index("time")
    return df

def fetch_bybit(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    # category spot
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "spot", "symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    data = j.get("result", {}).get("list", [])
    if not data:
        return pd.DataFrame()
    # Bybit newest first: [ts, o,h,l,c, vol, ...]
    rows = []
    for x in data[::-1]:
        ts, o, h, l, c, v = int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])
        rows.append([pd.to_datetime(ts, unit="ms", utc=True), o, h, l, c, v])
    df = pd.DataFrame(rows, columns=["time","open","high","low","close","volume"]).set_index("time")
    return df

def fetch_binance(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data:
        return pd.DataFrame()
    return df_from_klines(data, "binance")

# Bitget (BGB only) – v2 requires granularity "1h" or "1day"
def fetch_bitget_bgb(interval: str, limit: int) -> pd.DataFrame:
    """
    Bitget v2 (spot) K-line:
    - granularity must be one of: 1min,3min,5min,15min,30min,1h,4h,6h,12h,1day,1week,1M,6Hutc,12Hutc,1Dutc,3Dutc,1Wutc,1Mutc
    - data rows typically: [ts, open, high, low, close, baseVol, quoteVol, ...]  (>=6 fields)
      We map the first 6: ts,o,h,l,c,volume (use baseVol if present, else 0)
    - Some endpoints return newest-first: we reverse to oldest→newest.
    """
    gran_map = {"1h": "1h", "1d": "1day"}
    gran = gran_map[interval]
    lim = min(max(int(limit), 1), 200)  # v2 spesso max 200

    attempts = [
        # 1) candles (senza endTime)
        ("https://api.bitget.com/api/v2/spot/market/candles",
         {"symbol": "BGBUSDT", "granularity": gran, "limit": str(lim)}),

        # 2) history-candles (con endTime)
        ("https://api.bitget.com/api/v2/spot/market/history-candles",
         {"symbol": "BGBUSDT", "granularity": gran, "limit": str(lim),
          "endTime": str(int(dt.datetime.now(timezone.utc).timestamp() * 1000))}),
    ]

    last_err = None
    for url, params in attempts:
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            j = r.json()

            if j.get("code") != "00000" or "data" not in j:
                last_err = f"[BGB] Bitget unexpected payload: {j}"
                print(last_err)
                continue

            data = j["data"]
            if not data:
                last_err = "[BGB] Bitget: empty data"
                print(last_err)
                continue

            # v2: newest-first → invertiamo
            rows = data[::-1]
            recs = []
            for row in rows:
                # row può avere 6,7,8... campi: prendiamo i primi 6 in ordine noto
                ts = int(row[0])
                o = float(row[1]); h = float(row[2]); l = float(row[3]); c = float(row[4])
                v = float(row[5]) if len(row) > 5 else 0.0
                t = pd.to_datetime(ts, unit="ms", utc=True)
                recs.append([t, o, h, l, c, v])

            df = pd.DataFrame(recs, columns=["time","open","high","low","close","volume"]).set_index("time")
            if not df.empty:
                return df

            last_err = "[BGB] Bitget: parsed empty dataframe"
            print(last_err)

        except requests.HTTPError as he:
            print(f"[BGB] Bitget fetch fail ({url}): {he.response.status_code} {he.response.text[:200]}")
            last_err = str(he)
        except Exception as e:
            print(f"[BGB] Bitget fetch error ({url}): {e}")
            last_err = str(e)

    print(last_err or "[BGB] Bitget: no data")
    return pd.DataFrame()

# ---- Unified fetchers with rotation ----
def fetch_ohlc_1h(symbol: str) -> pd.DataFrame:
    if symbol == "BGB":
        try:
            return fetch_bitget_bgb("1h", LOOKBACK_1H)
        except Exception as e:
            print("BGB 1h fetch error:", e)
            return pd.DataFrame()
    # rotation: OKX → Bybit → Binance
    try:
        return fetch_okx(OKX_IDS[symbol], "1H", LOOKBACK_1H)
    except Exception as e:
        print(symbol, "OKX 1H fail:", e)
    try:
        return fetch_bybit(BYBIT_SYM[symbol], "60", LOOKBACK_1H)
    except Exception as e:
        print(symbol, "Bybit 1H fail:", e)
    try:
        return fetch_binance(symbol + BASE, "1h", LOOKBACK_1H)
    except Exception as e:
        print(symbol, "Binance 1H fail:", e)
        return pd.DataFrame()

def fetch_ohlc_1d(symbol: str) -> pd.DataFrame:
    if symbol == "BGB":
        try:
            return fetch_bitget_bgb("1d", LOOKBACK_1D)
        except Exception as e:
            print("BGB 1d fetch error:", e)
            return pd.DataFrame()
    # rotation: OKX → Bybit → Binance
    try:
        return fetch_okx(OKX_IDS[symbol], "1D", LOOKBACK_1D)
    except Exception as e:
        print(symbol, "OKX 1D fail:", e)
    try:
        return fetch_bybit(BYBIT_SYM[symbol], "D", LOOKBACK_1D)
    except Exception as e:
        print(symbol, "Bybit 1D fail:", e)
    try:
        return fetch_binance(symbol + BASE, "1d", LOOKBACK_1D)
    except Exception as e:
        print(symbol, "Binance 1D fail:", e)
        return pd.DataFrame()

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

# 4H from 1H with optional fallback to 1H
def resample_to_4h(df1h: pd.DataFrame):
    if df1h is None or df1h.empty:
        return None, "no-1h"
    ohlc = df1h["close"].resample("4h", label="right", closed="right").ohlc()
    vol = df1h["volume"].resample("4h", label="right", closed="right").sum()
    df4 = pd.concat([ohlc, vol], axis=1)
    df4.columns = ["open","high","low","close","volume"]
    df4 = df4.dropna()
    if len(df4) < 60:
        if ALLOW_1H_FALLBACK:
            print(f"[FALLBACK] 4H insufficiente (len={len(df4)}). Uso 1H per segnali intraday.")
            return df1h, "1h"
        else:
            print(f"[SKIP] 4H insufficiente (len={len(df4)}). Nessun fallback: coin ignorata.")
            return None, "none"
    return df4, "4h"

# -----------------------
# Signals + reasons
# -----------------------
def evaluate_signals(symbol: str, state, nowu: dt.datetime) -> dict:
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
        if last["macd"] > last["macd_signal"] and last["macd_hist"] > prev["macd_hist"]:
            trend = "UP"
        elif last["macd"] < last["macd_signal"] and last["macd_hist"] < prev["macd_hist"]:
            trend = "DOWN"
        else:
            trend = "FLAT"

    # 4H (or fallback 1H) for intraday signals
    dfX, used = resample_to_4h(df1h)
    if dfX is None or used == "none":
        return {"ok": False, "reason": f"insufficient-4h-no-fallback"}

    x = add_indicators(dfX)
    if x is None or x.empty:
        return {"ok": False, "reason": f"no-{used}-indicators"}

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

    # Trend filter modes
    if TREND_FILTER == "buy_only_up":
        if trend != "UP":
            condBUY = False  # blocca solo i BUY
    elif TREND_FILTER == "all_up":
        if trend != "UP":
            condBUY = False
            condOPP = False

    # Default soft filter (if off): blocca solo BUY in 1D chiaramente DOWN, lascia OPP se hist migliora
    trendOK = True
    if TREND_FILTER == "off":
        trendOK = (trend != "DOWN") or condOPP

    if not trendOK:
        return {"ok": False, "reason": f"blocked-by-1D-trend({trend})", "price": price, "trend1d": trend, "frameUsed": used}

    # Cooldown logic for OPP
    coinKey = f"{symbol}_OPP"
    cd_until = state["cooldowns"].get(coinKey, 0)
    cooldown_active = nowu.timestamp() < cd_until

    buy = condBUY
    opp = False
    if ENABLE_OPP:
        opp = (condOPP and not buy and not cooldown_active)

    if buy:
        return {"ok": True, "reason": "BUY", "price": price, "buy": True, "opp": False, "trend1d": trend, "frameUsed": used}
    if opp:
        state["cooldowns"][coinKey] = (nowu + timedelta(hours=OPP_COOLDOWN_H)).timestamp()
        return {"ok": True, "reason": "OPPORTUNITY", "price": price, "buy": False, "opp": True, "trend1d": trend, "frameUsed": used}

    details = []
    if last["rsi"] > rsiOpp:
        details.append(f"RSI>{rsiOpp:.0f}")
    if not crossUp and not buy:
        details.append("no MACD cross↑")
    if not histImproving and not crossUp:
        details.append("hist not improving")

    return {"ok": False, "reason": "no-signal(" + ", ".join(details) + ")", "price": price, "trend1d": trend, "frameUsed": used}

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
        try:
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
        except Exception as e:
            print(f"[DAILY] {c} build err:", e)
            lines.append(f"{c:5}    n/a     n/a    n/a")
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
    state.setdefault("newsCooldowns", {})[key] = (nowu + timedelta(hours=NEWS_COOLDOWN_H)).timestamp()

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
    had_buy = False
    had_opp = False
    for c in COINS:
        res = evaluate_signals(c, state, nowu)
        if not res.get("ok", False):
            reason = res.get("reason","")
            print(f"{c} no-alert: {reason}")
            continue
        price = res["price"]
        trend = res.get("trend1d","UNKNOWN")
        frame = res.get("frameUsed","?")
        if res.get("buy", False):
            had_buy = True
            msg = f"🟢 <b>BUY</b> {c}/{BASE} ({frame}, 1D {trend})\nPrezzo: {price:.4f}"
            send_telegram(msg)
        elif res.get("opp", False):
            had_opp = True
            msg = f"🟡 <b>OPPORTUNITY</b> {c}/{BASE} ({frame}, 1D {trend})\nPrezzo: {price:.4f}"
            send_telegram(msg)

    if not had_buy and not had_opp:
        print("Nessun BUY/OPP valido (filtrato da trend 1D / cooldown / condizioni tecniche).")

    # Daily & heartbeat (safe)
    try:
        if should_send_daily_report(state):
            report = build_daily_table()
            send_telegram("🗞️ <b>Daily Trend 1D</b>\n" + report)
            state["last_daily"] = nowu.date().isoformat()
        if should_send_heartbeat(state):
            send_telegram("✅ Heartbeat: bot attivo e sincronizzato")
            state["last_heartbeat"] = nowu.date().isoformat()
    except Exception as e:
        print(f"[DAILY/HB] error: {e}")

    # News intraday – safe per provider errors
    for c in COINS:
        try:
            d1 = fetch_ohlc_1d(c)
            if d1 is None or len(d1) < 2:
                continue
            last = d1.iloc[-1]["close"]
            prev = d1.iloc[-2]["close"]
            move = pct(last, prev)
            try_send_news(c, move, state, nowu)
        except Exception as e:
            print(f"[NEWS LOOP] {c} fetch err: {e}")

    save_state(state)

if __name__ == "__main__":
    run_once()
