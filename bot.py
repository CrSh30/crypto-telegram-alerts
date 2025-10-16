import os
import json
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ====== CONFIG ======
COINS = {
    # simbolo : (okx_instId, kucoin_symbol, bybit_symbol, bitget_symbol)
    "BTC": ("BTC-USDT", "BTC-USDT", "BTCUSDT", None),
    "ETH": ("ETH-USDT", "ETH-USDT", "ETHUSDT", None),
    "BNB": ("BNB-USDT", "BNB-USDT", "BNBUSDT", None),
    "SOL": ("SOL-USDT", "SOL-USDT", "SOLUSDT", None),
    "BGB": (None,       None,       None,       "BGBUSDT"),  # Bitget spot
}

# Dati richiesti
CANDLES_1H = 240       # per segnali 1H
CANDLES_1D = 400       # per trend 1D

# Soglie BUY (pro-holder)
RSI_LOW  = 30
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# Cooldown (ore)
COOLDOWN_HOURS = 6

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

# Stato persistente (cache GH Actions)
STATE_DIR  = Path(".state")
STATE_FILE = STATE_DIR / "last_signals.json"


# ====== UTILITY ======
def notna_all(*vals) -> bool:
    """True se TUTTI i valori non sono NaN/None."""
    for v in vals:
        if v is None:
            return False
        if pd.isna(v):
            return False
    return True


# ====== TELEGRAM ======
def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram env vars mancanti")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, data=payload, timeout=25)
        if r.status_code != 200:
            print("Telegram error:", r.text)
    except Exception as e:
        print("Telegram exception:", e)


# ====== FETCHERS SENZA CHIAVI ======
def fetch_okx(instId: str, limit=200, bar="1H") -> pd.DataFrame:
    url = "https://www.okx.com/api/v5/market/candles"
    params = {"instId": instId, "bar": bar, "limit": str(limit)}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        raise RuntimeError("OKX: no data")
    rows = []
    for row in data:
        ts = int(row[0])
        o, h, l, c, v = float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])
        rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True),
                     "open": o, "high": h, "low": l, "close": c, "volume": v})
    return pd.DataFrame(rows).sort_values("close_time")


def fetch_kucoin(symbol: str, limit=200, typ="1hour") -> pd.DataFrame:
    url = "https://api.kucoin.com/api/v1/market/candles"
    params = {"type": typ, "symbol": symbol}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        raise RuntimeError("KuCoin: no data")
    rows = []
    for row in data:
        ts = int(row[0]) * 1000
        o, c, h, l, v = map(float, row[1:6])
        rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True),
                     "open": o, "high": h, "low": l, "close": c, "volume": v})
    return pd.DataFrame(rows).sort_values("close_time")


def fetch_bybit(symbol: str, limit=200, interval="60") -> pd.DataFrame:
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
        rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True),
                     "open": o, "high": h, "low": l, "close": c, "volume": v})
    return pd.DataFrame(rows).sort_values("close_time")


def fetch_bitget(symbol: str, limit=200) -> pd.DataFrame:
    """
    Bitget spot: prova diverse rotte compatibili (nessuna chiave).
    """
    # 1) v2 spot path, granularity '1h'
    try:
        url = "https://api.bitget.com/api/v2/spot/market/candles"
        params = {"symbol": symbol, "granularity": "1h", "limit": str(limit)}
        r = requests.get(url, params=params, timeout=25)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            rows = []
            for row in data:
                ts = int(row[0])
                o, h, l, c, v = map(float, row[1:6])
                rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True),
                             "open": o, "high": h, "low": l, "close": c, "volume": v})
            return pd.DataFrame(rows).sort_values("close_time")
    except Exception as e:
        print("Bitget v2 spot (1h) fail:", e)

    # 2) v2 generic path con productType=spbl (spot), granularity '1h'
    try:
        url = "https://api.bitget.com/api/v2/market/candles"
        params = {"symbol": symbol, "productType": "spbl", "granularity": "1h", "limit": str(limit)}
        r = requests.get(url, params=params, timeout=25)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            rows = []
            for row in data:
                ts = int(row[0])
                o, h, l, c, v = map(float, row[1:6])
                rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True),
                             "open": o, "high": h, "low": l, "close": c, "volume": v})
            return pd.DataFrame(rows).sort_values("close_time")
    except Exception as e:
        print("Bitget v2 generic (spbl,1h) fail:", e)

    # 3) v1 legacy spot, period '1H'
    try:
        url = "https://api.bitget.com/api/spot/v1/market/candles"
        params = {"symbol": symbol, "period": "1H", "limit": str(limit)}
        r = requests.get(url, params=params, timeout=25)
        r.raise_for_status()
        data = r.json().get("data", [])
        if data:
            rows = []
            for row in data:
                ts = int(row[0])
                o, h, l, c, v = map(float, row[1:6])
                rows.append({"close_time": pd.to_datetime(ts, unit="ms", utc=True),
                             "open": o, "high": h, "low": l, "close": c, "volume": v})
            return pd.DataFrame(rows).sort_values("close_time")
    except Exception as e:
        print("Bitget v1 (1H) fail:", e)

    raise RuntimeError("Bitget: nessun dato ottenuto")


def fetch_ohlc_1h(sym: str) -> pd.DataFrame:
    okx_id, ku_id, by_id, bg_id = COINS[sym]
    # 1) OKX 1H
    if okx_id:
        try:
            return fetch_okx(okx_id, limit=CANDLES_1H, bar="1H")
        except Exception as e:
            print(sym, "1H OKX fail:", e)
    # 2) KuCoin 1H
    if ku_id:
        try:
            return fetch_kucoin(ku_id, limit=CANDLES_1H, typ="1hour")
        except Exception as e:
            print(sym, "1H KuCoin fail:", e)
    # 3) Bybit 1H
    if by_id:
        try:
            return fetch_bybit(by_id, limit=CANDLES_1H, interval="60")
        except Exception as e:
            print(sym, "1H Bybit fail:", e)
    # 4) Bitget (BGB)
    if bg_id:
        try:
            return fetch_bitget(bg_id, limit=CANDLES_1H)
        except Exception as e:
            print(sym, "1H Bitget fail:", e)
    raise RuntimeError("no 1H data")


def fetch_ohlc_1d(sym: str) -> pd.DataFrame:
    okx_id, ku_id, by_id, bg_id = COINS[sym]
    # 1) OKX 1D
    if okx_id:
        try:
            return fetch_okx(okx_id, limit=CANDLES_1D, bar="1D")
        except Exception as e:
            print(sym, "1D OKX fail:", e)
    # 2) KuCoin 1D
    if ku_id:
        try:
            return fetch_kucoin(ku_id, limit=CANDLES_1D, typ="1day")
        except Exception as e:
            print(sym, "1D KuCoin fail:", e)
    # 3) Bybit 1D (interval D)
    if by_id:
        try:
            return fetch_bybit(by_id, limit=CANDLES_1D, interval="D")
        except Exception as e:
            print(sym, "1D Bybit fail:", e)
    # 4) Bitget (fallback)
    if bg_id:
        try:
            return fetch_bitget(bg_id, limit=CANDLES_1D)
        except Exception as e:
            print(sym, "1D Bitget fail:", e)
    raise RuntimeError("no 1D data")


# ====== INDICATORS ======
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["rsi"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    return df


def last_closed_rows(df: pd.DataFrame):
    if len(df) < 3:
        return None, None
    return df.iloc[-2], df.iloc[-3]  # penultima chiusa + precedente


# ====== STATE (cooldown) ======
def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        print("load_state error:", e)
    return {}


def save_state(state: dict):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print("save_state error:", e)


def cooldown_ok(state: dict, coin: str, signal_key: str, hours: int) -> bool:
    """True se possiamo inviare il segnale (non in cooldown)."""
    now = datetime.now(timezone.utc)
    last = None
    if coin in state and signal_key in state[coin]:
        try:
            last = datetime.fromisoformat(state[coin][signal_key])
        except Exception:
            last = None
    if last is None:
        return True
    return now - last >= timedelta(hours=hours)


def mark_sent(state: dict, coin: str, signal_key: str):
    now = datetime.now(timezone.utc).isoformat()
    state.setdefault(coin, {})[signal_key] = now


# ====== DAILY REPORT (08:00 Europe/Rome) ======
def today_in_rome():
    return datetime.now(ZoneInfo("Europe/Rome")).date()


def should_send_daily_report(state: dict) -> bool:
    # invia tra le 08:00 e le 08:05 ora italiana, una sola volta al giorno
    now_it = datetime.now(ZoneInfo("Europe/Rome"))
    if not (now_it.hour == 8 and now_it.minute < 5):
        return False
    last = state.get("_daily_report_date")
    return str(today_in_rome()) != str(last)


def mark_daily_report_sent(state: dict):
    state["_daily_report_date"] = str(today_in_rome())

def should_send_heartbeat(state: dict) -> bool:
    """
    Invia l'heartbeat una sola volta al giorno nella finestra 08:00–08:05 Europe/Rome.
    (Il workflow gira all'ora spaccata; usiamo la stessa finestra del daily.)
    """
    now_it = datetime.now(ZoneInfo("Europe/Rome"))
    if not (now_it.hour == 8 and now_it.minute < 5):
        return False
    last = state.get("_heartbeat_date")
    return str(today_in_rome()) != str(last)

def mark_heartbeat_sent(state: dict):
    state["_heartbeat_date"] = str(today_in_rome())


def build_daily_trend_report() -> str:
    lines = []
    for sym in COINS.keys():
        try:
            dfD = add_indicators(fetch_ohlc_1d(sym))
            rowD, prevD = last_closed_rows(dfD)
            if rowD is None:
                lines.append(f"{sym} ?")
                continue

            arrow = "?"
            strength = ""
            if notna_all(rowD["macd"], rowD["macd_signal"]):
                delta = rowD["macd"] - rowD["macd_signal"]   # forza: distanza MACD–Signal
                if abs(delta) < 1e-12:
                    arrow = "→"
                else:
                    arrow = "↑" if delta > 0 else "↓"
                strength = f" ({delta:+.4f})"  # es. +0.0025 o -0.0017

            lines.append(f"{sym} {arrow}{strength}")
        except Exception:
            lines.append(f"{sym} ?")
    return " ".join(lines)


# ====== MAIN LOGIC (BUY only, 1H + trend 1D) ======
def run_once():
    state = load_state()
    messages = []

    for sym in COINS.keys():
        try:
            # 1H (segnale micro)
            df1 = add_indicators(fetch_ohlc_1h(sym))
            row1, prev1 = last_closed_rows(df1)
            if row1 is None:
                continue

            # 1D (trend macro) - filtro: MACD(1D) > Signal(1D)
            dfD = add_indicators(fetch_ohlc_1d(sym))
            rowD, prevD = last_closed_rows(dfD)
            trend_up = notna_all(rowD["macd"], rowD["macd_signal"]) and (rowD["macd"] > rowD["macd_signal"])
            if not trend_up:
                continue

            # BUY solo se RSI<30 e MACD cross up INSIEME (su 1H)
            if notna_all(prev1["rsi"], row1["rsi"], prev1["macd"], prev1["macd_signal"], row1["macd"], row1["macd_signal"]):
                rsi_cross  = (prev1["rsi"] >= RSI_LOW) and (row1["rsi"] < RSI_LOW)
                macd_cross = (prev1["macd"] <= prev1["macd_signal"]) and (row1["macd"] > row1["macd_signal"])
                if rsi_cross and macd_cross:
                    key = "buy_combo"
                    if cooldown_ok(state, sym, key, COOLDOWN_HOURS):
                        price = row1["close"]
                        ts = row1["close_time"].strftime("%Y-%m-%d %H:%M UTC")
                        messages.append(
                            f"🟢 <b>{sym}</b> BUY (RSI < {RSI_LOW} + MACD ↑)\n"
                            f"Price: {price:.6f} USDT\nTime: {ts}  | Trend 1D: MACD ↑"
                        )
                        mark_sent(state, sym, key)

        except Exception as e:
            print(sym, "errore:", e)

    # Invio segnali
    if messages:
        send_telegram("📣 <b>Crypto BUY Alerts (Holder)</b>\n" + "\n\n".join(messages))
    else:
        print("Nessun BUY valido (filtrato da trend 1D / cooldown).")

    # Mini-report giornaliero 08:00 Europe/Rome
    try:
        if should_send_daily_report(state):
            summary = build_daily_trend_report()
            send_telegram(f"🗞️ <b>Daily Trend 1D</b>\n{summary}")
            mark_daily_report_sent(state)
    except Exception as e:
        print("Daily report error:", e)

    # Heartbeat giornaliero nella stessa finestra del daily (08:00–08:05 Europe/Rome)
    try:
        if should_send_heartbeat(state):
            send_telegram("✅ Heartbeat: bot attivo e sincronizzato")
            mark_heartbeat_sent(state)
    except Exception as e:
        print("Heartbeat error:", e)

    # Salva stato
    save_state(state)


if __name__ == "__main__":
    run_once()
