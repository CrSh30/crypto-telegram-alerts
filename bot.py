import os
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone

# ====== CONFIG ======
COINS = {
    # simbolo logico : (okx_instId, kucoin_symbol, bybit_symbol, bitget_symbol)
    "BTC": ("BTC-USDT", "BTC-USDT", "BTCUSDT", None),
    "ETH": ("ETH-USDT", "ETH-USDT", "ETHUSDT", None),
    "BNB": ("BNB-USDT", "BNB-USDT", "BNBUSDT", None),
    "SOL": ("SOL-USDT", "SOL-USDT", "SOLUSDT", None),
    "BGB": (None,       None,       None,       "BGBUSDT"),  # Bitget only
}
TF = "1h"          # timeframe logico
CANDLES = 200      # numero di barre da scaricare

# Soglie RSI
RSI_LOW  = 35
RSI_HIGH = 70

# MACD standard
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

# ====== TELEGRAM ======
def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram env vars mancanti")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.text)
    except Exception as e:
        print("Telegram exception:", e)

# ====== FETCHERS SENZA CHIAVI ======
def fetch_okx(instId: str, limit=200, bar="1H") -> pd.DataFrame:
    # OKX: ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": instId, "bar": bar, "limit": str(limit)}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        raise RuntimeError("OKX: no data")
    rows = []
    for row in data:
        ts, o, h, l, c = int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])
        rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True), "open": o, "high": h, "low": l, "close": c, "volume": float(row[5])})
    df = pd.DataFrame(rows)
    df.sort_values("close_time", inplace=True)
    return df

def fetch_kucoin(symbol: str, limit=200, typ="1hour") -> pd.DataFrame:
    # KuCoin: [time, open, close, high, low, volume, turnover]
    url = "https://api.kucoin.com/api/v1/market/candles"
    params = {"type": typ, "symbol": symbol}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        raise RuntimeError("KuCoin: no data")
    rows = []
    for row in data:
        # Kucoin time è in secondi string
        ts = int(row[0]) * 1000
        o, c, h, l, v = map(float, row[1:6])
        rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True), "open": o, "high": h, "low": l, "close": c, "volume": v})
    df = pd.DataFrame(rows)
    df.sort_values("close_time", inplace=True)
    return df

def fetch_bybit(symbol: str, limit=200, interval="60") -> pd.DataFrame:
    # Bybit v5: list of [start, open, high, low, close, volume, turnover]
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "spot", "symbol": symbol, "interval": interval, "limit": str(limit)}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json().get("result", {}).get("list", [])
    if not data:
        raise RuntimeError("Bybit: no data")
    rows = []
    for row in data:
        ts = int(row[0])
        o, h, l, c, v = float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
        rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True), "open": o, "high": h, "low": l, "close": c, "volume": v})
    df = pd.DataFrame(rows)
    df.sort_values("close_time", inplace=True)
    return df

def fetch_bitget(symbol: str, limit=200, granularity="60"):
    # Bitget v2 market/candles (spot): [ts, o, h, l, c, vol, quoteVol]
    url = "https://api.bitget.com/api/v2/market/candles"
    params = {"symbol": symbol, "granularity": granularity, "limit": str(limit)}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        raise RuntimeError("Bitget: no data")
    rows = []
    for row in data:
        ts = int(row[0])
        o, h, l, c, v = float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
        rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True), "open": o, "high": h, "low": l, "close": c, "volume": v})
    df = pd.DataFrame(rows)
    df.sort_values("close_time", inplace=True)
    return df

def fetch_ohlc(symbol_key: str) -> pd.DataFrame:
    okx_id, ku_id, by_id, bg_id = COINS[symbol_key]
    # 1) OKX
    if okx_id:
        try:
            return fetch_okx(okx_id, limit=CANDLES, bar="1H")
        except Exception as e:
            print(symbol_key, "OKX fail:", e)
    # 2) KuCoin
    if ku_id:
        try:
            return fetch_kucoin(ku_id, limit=CANDLES, typ="1hour")
        except Exception as e:
            print(symbol_key, "KuCoin fail:", e)
    # 3) Bybit
    if by_id:
        try:
            return fetch_bybit(by_id, limit=CANDLES, interval="60")
        except Exception as e:
            print(symbol_key, "Bybit fail:", e)
    # 4) Bitget (solo per BGB, ma lasciamo fallback)
    if bg_id:
        try:
            return fetch_bitget(bg_id, limit=CANDLES, granularity="60")
        except Exception as e:
            print(symbol_key, "Bitget fail:", e)
    raise RuntimeError("Nessuna fonte disponibile")

# ====== INDICATORS ======
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["rsi"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    return df

def last_closed_rows(df: pd.DataFrame):
    if len(df) < 3:
        return None, None
    return df.iloc[-2], df.iloc[-3]  # penultima (chiusa) e precedente

def check_rsi(row, prev):
    msgs = []
    if pd.notna(row["rsi"]) and pd.notna(prev["rsi"]):
        if prev["rsi"] >= RSI_LOW and row["rsi"] < RSI_LOW:
            msgs.append(("rsi_low", f"RSI < {RSI_LOW} (Buy zone) — RSI {row['rsi']:.2f}"))
        if prev["rsi"] <= RSI_HIGH and row["rsi"] > RSI_HIGH:
            msgs.append(("rsi_high", f"RSI > {RSI_HIGH} (Take profit) — RSI {row['rsi']:.2f}"))
    return msgs

def check_macd(row, prev):
    msgs = []
    if all(pd.notna([row["macd"], row["macd_signal"], prev["macd"], prev["macd_signal"]])):
        if prev["macd"] <= prev["macd_signal"] and row["macd"] > row["macd_signal"]:
            msgs.append(("macd_up",  f"MACD bullish cross — {row['macd']:.4f} > {row['macd_signal']:.4f}"))
        if prev["macd"] >= prev["macd_signal"] and row["macd"] < row["macd_signal"]:
            msgs.append(("macd_dn",  f"MACD bearish cross — {row['macd']:.4f} < {row['macd_signal']:.4f}"))
    return msgs

def run_once():
    all_msgs = []
    for sym in COINS.keys():
        try:
            df = fetch_ohlc(sym)
            df = compute_indicators(df)
            row, prev = last_closed_rows(df)
            if row is None:
                continue
            ts = row["close_time"].strftime("%Y-%m-%d %H:%M UTC")
            price = row["close"]
            sigs = check_rsi(row, prev) + check_macd(row, prev)
            for _, text in sigs:
                all_msgs.append(f"• <b>{sym}</b> — {text}\nPrice: {price:.6f} USDT\nTime: {ts}")
        except Exception as e:
            all_msgs.append(f"⚠️ <b>{sym}</b> errore dati: {e}")

    if all_msgs:
        send_telegram("📣 <b>Crypto Alerts (1h)</b>\n" + "\n\n".join(all_msgs))
    else:
        print("Nessun segnale questa run.")

if __name__ == "__main__":
    run_once()
