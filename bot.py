import os
import time
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone

# === CONFIG ===
SYMBOLS = {
    # symbol_name : ("source", "pair_or_id")
    "BTCUSDT": ("binance", "BTCUSDT"),
    "ETHUSDT": ("binance", "ETHUSDT"),
    "BNBUSDT": ("binance", "BNBUSDT"),
    "SOLUSDT": ("binance", "SOLUSDT"),
    "BGBUSD":  ("coingecko", "bitget-token"),  # CoinGecko id
}
INTERVAL = "1h"   # timeframe per Binance
CANDLES  = 200    # numero di candele da scaricare

# Soglie RSI:
RSI_LOW  = 35
RSI_HIGH = 70

# Parametri MACD standard:
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Telegram secrets dalle GitHub Secrets (env)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram env vars mancanti.")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code != 200:
            print("Errore Telegram:", r.text)
    except Exception as e:
        print("Eccezione Telegram:", e)

def fetch_binance_klines(symbol: str, interval="1h", limit=200) -> pd.DataFrame:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "taker_base", "taker_quote", "ignore"
    ]
    df = pd.DataFrame(data, columns=cols)
    # Converti tipi
    df["open_time"]  = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # Usa solo candele CHIUSE: scarta l'ultima se è in formazione (binance fornisce solo chiuse, ma teniamo penultima per sicurezza)
    # N.B.: Binance restituisce candele chiuse, ma per minimizzare falsi segnali useremo la penultima per i "crossing"
    return df

def fetch_coingecko_hourly_closes(coin_id: str, days=2) -> pd.DataFrame:
    # restituisce serie oraria (timestamp, price)
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    params = {"vs_currency": "usd", "days": days, "interval": "hourly"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    prices = data.get("prices", [])
    if not prices:
        raise RuntimeError("CoinGecko nessun dato.")
    # prices: [ [ms, price], ... ]
    df = pd.DataFrame(prices, columns=["time", "close"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df["open"] = df["high"] = df["low"] = df["close"]  # OHLC fittizi dal close per TA
    df["volume"] = 0.0
    df.rename(columns={"time":"close_time"}, inplace=True)
    return df

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # RSI
    df["rsi"] = ta.rsi(df["close"], length=14)
    # MACD
    macd = ta.macd(df["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    return df

def last_closed_rows(df: pd.DataFrame):
    # Consideriamo penultima candela (chiusa) e la precedente per verificare "crossing"
    if len(df) < 3:
        return None, None
    return df.iloc[-2], df.iloc[-3]

def check_rsi_signals(sym: str, row, prev) -> list:
    signals = []
    # Crossing below RSI_LOW
    if pd.notna(row["rsi"]) and pd.notna(prev["rsi"]):
        if prev["rsi"] >= RSI_LOW and row["rsi"] < RSI_LOW:
            signals.append(f"📉 <b>{sym}</b> RSI < {RSI_LOW} (Buy zone)\nRSI now: {row['rsi']:.2f}")
        if prev["rsi"] <= RSI_HIGH and row["rsi"] > RSI_HIGH:
            signals.append(f"📈 <b>{sym}</b> RSI > {RSI_HIGH} (Take profit)\nRSI now: {row['rsi']:.2f}")
    return signals

def check_macd_signals(sym: str, row, prev) -> list:
    signals = []
    if all(pd.notna([row["macd"], row["macd_signal"], prev["macd"], prev["macd_signal"]])):
        # Bullish cross: MACD crosses above signal
        if prev["macd"] <= prev["macd_signal"] and row["macd"] > row["macd_signal"]:
            signals.append(f"🟢 <b>{sym}</b> MACD bullish cross\nMACD {row['macd']:.4f} > Signal {row['macd_signal']:.4f}")
        # Bearish cross
        if prev["macd"] >= prev["macd_signal"] and row["macd"] < row["macd_signal"]:
            signals.append(f"🔴 <b>{sym}</b> MACD bearish cross\nMACD {row['macd']:.4f} < Signal {row['macd_signal']:.4f}")
    return signals

def run_once():
    all_msgs = []

    for sym, (source, ident) in SYMBOLS.items():
        try:
            if source == "binance":
                df = fetch_binance_klines(ident, interval=INTERVAL, limit=CANDLES)
                df["close_time"] = pd.to_datetime(df["close_time"], utc=True)
            elif source == "coingecko":
                df = fetch_coingecko_hourly_closes(ident, days=3)
            else:
                continue

            df = compute_indicators(df)
            row, prev = last_closed_rows(df)
            if row is None:
                continue

            ts = row["close_time"].strftime("%Y-%m-%d %H:%M UTC")
            price = row["close"]

            rsi_msgs  = check_rsi_signals(sym, row, prev)
            macd_msgs = check_macd_signals(sym, row, prev)

            for m in (rsi_msgs + macd_msgs):
                all_msgs.append(f"{m}\nPrice: {price:.6f}\nTime: {ts}")

        except Exception as e:
            all_msgs.append(f"⚠️ <b>{sym}</b> errore dati: {e}")

    if not all_msgs:
        print("Nessun segnale questa run.")
        return

    text = "📣 <b>Crypto Alerts (1h)</b>\n" + "\n\n".join(all_msgs)
    print(text)
    send_telegram(text)

if __name__ == "__main__":
    run_once()
