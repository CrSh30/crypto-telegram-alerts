import os
import json
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ===== CONFIG =====
COINS = {
    "BTC": ("BTC-USDT", "BTC-USDT", "BTCUSDT", None),
    "ETH": ("ETH-USDT", "ETH-USDT", "ETHUSDT", None),
    "BNB": ("BNB-USDT", "BNB-USDT", "BNBUSDT", None),
    "SOL": ("SOL-USDT", "SOL-USDT", "SOLUSDT", None),
    "BGB": (None, None, None, "BGBUSDT"),
}

CANDLES_1H = 240
CANDLES_1D = 400

# BUY (conservativo)
RSI_LOW = 30
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
COOLDOWN_HOURS = 6

# OPPORTUNITY (più largo)
ENABLE_OPPORTUNITY = True
RSI_WIDE = 40
OPPORTUNITY_COOLDOWN_HOURS = 3

# Trend-change 4H opzionale (resta com'era)
ENABLE_4H_TREND_ALERTS = True
TREND4H_COOLDOWN_HOURS = 6

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

STATE_DIR  = Path(".state")
STATE_FILE = STATE_DIR / "last_signals.json"

# ===== UTILITIES =====
def notna_all(*vals) -> bool:
    for v in vals:
        if v is None or pd.isna(v):
            return False
    return True

def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=25,
        )
        if r.status_code != 200:
            print("Telegram error:", r.text)
    except Exception as e:
        print("Telegram exc:", e)

# ===== FETCHERS =====
def fetch_okx(inst, limit, bar):
    r = requests.get("https://www.okx.com/api/v5/market/candles",
                     params={"instId": inst, "bar": bar, "limit": limit}, timeout=25)
    r.raise_for_status()
    data = r.json().get("data", [])
    rows = [{"close_time": pd.to_datetime(int(x[0]), unit="ms", utc=True),
             "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
             "close": float(x[4]), "volume": float(x[5])} for x in data]
    return pd.DataFrame(rows).sort_values("close_time")

def fetch_bybit(sym, limit, interval):
    r = requests.get("https://api.bybit.com/v5/market/kline",
                     params={"category": "spot", "symbol": sym, "interval": interval, "limit": limit}, timeout=25)
    r.raise_for_status()
    data = r.json().get("result", {}).get("list", [])
    rows = [{"close_time": pd.to_datetime(int(x[0]), unit="ms", utc=True),
             "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
             "close": float(x[4]), "volume": float(x[5])} for x in data]
    return pd.DataFrame(rows).sort_values("close_time")

def fetch_bitget(sym, limit):
    urls = [
        ("https://api.bitget.com/api/v2/spot/market/candles", {"symbol": sym, "granularity": "1h", "limit": limit}),
        ("https://api.bitget.com/api/v2/market/candles", {"symbol": sym, "productType": "spbl", "granularity": "1h", "limit": limit}),
        ("https://api.bitget.com/api/spot/v1/market/candles", {"symbol": sym, "period": "1H", "limit": limit}),
    ]
    for url, p in urls:
        try:
            r = requests.get(url, params=p, timeout=25)
            r.raise_for_status()
            data = r.json().get("data", [])
            rows = [{"close_time": pd.to_datetime(int(x[0]), unit="ms", utc=True),
                     "open": float(x[1]), "high": float(x[2]), "low": float(x[3]),
                     "close": float(x[4]), "volume": float(x[5])} for x in data]
            if len(rows):
                return pd.DataFrame(rows).sort_values("close_time")
        except Exception:
            continue
    raise RuntimeError("Bitget no data")

def fetch_ohlc_1h(sym):
    okx, _, by, bg = COINS[sym]
    for f, args in [(fetch_okx, (okx, CANDLES_1H, "1H")),
                    (fetch_bybit, (by, CANDLES_1H, "60")),
                    (fetch_bitget, (bg, CANDLES_1H))]:
        if args[0]:
            try: return f(*args)
            except Exception as e: print(sym, "1H fail:", e)
    raise RuntimeError("no 1H data")

def fetch_ohlc_1d(sym):
    okx, _, by, bg = COINS[sym]
    for f, args in [(fetch_okx, (okx, CANDLES_1D, "1D")),
                    (fetch_bybit, (by, CANDLES_1D, "D")),
                    (fetch_bitget, (bg, CANDLES_1D))]:
        if args[0]:
            try: return f(*args)
            except Exception as e: print(sym, "1D fail:", e)
    raise RuntimeError("no 1D data")

def resample_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    df = df_1h.copy()
    df = df.set_index(pd.to_datetime(df["close_time"], utc=True)).sort_index()
    ohlc = df["close"].resample("4H", label="right", closed="right").ohlc()
    vol = df["volume"].resample("4H", label="right", closed="right").sum()
    out = pd.concat([ohlc, vol], axis=1).dropna().reset_index()
    out = out.rename(columns={"index":"close_time"})
    return out

# ===== TECHNICALS =====
def add_indicators(df):
    df["rsi"] = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    df["macd"], df["macd_signal"] = macd["MACD_12_26_9"], macd["MACDs_12_26_9"]
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    return df

def last_closed_rows(df):
    if len(df) < 3: return None, None
    return df.iloc[-2], df.iloc[-3]

def trend_state_from_row(row):
    if notna_all(row["macd"], row["macd_signal"]):
        if abs(row["macd"] - row["macd_signal"]) < 1e-12:
            return "NEUTRAL"
        return "UP" if row["macd"] > row["macd_signal"] else "DOWN"
    return "NEUTRAL"

# ===== STATE =====
def load_state():
    try:
        if STATE_FILE.exists(): return json.load(open(STATE_FILE))
    except: pass
    return {}
def save_state(s):
    STATE_DIR.mkdir(exist_ok=True)
    json.dump(s, open(STATE_FILE,"w"))
def cooldown_ok(s,c,k,h):
    now=datetime.now(timezone.utc)
    last=s.get(c,{}).get(k)
    if not last: return True
    try:last=datetime.fromisoformat(last)
    except:return True
    return now-last>=timedelta(hours=h)
def mark_sent(s,c,k): s.setdefault(c,{})[k]=datetime.now(timezone.utc).isoformat()

# ===== REPORT 1D (percentuale) =====
def build_daily_trend_report():
    lines=[]
    for sym in COINS.keys():
        try:
            dfD=add_indicators(fetch_ohlc_1d(sym))
            rowD,_=last_closed_rows(dfD)
            if rowD is None:
                lines.append(f"{sym} ?"); continue
            arrow="?"
            strength=""
            if notna_all(rowD["macd"], rowD["macd_signal"], rowD["close"]):
                delta=rowD["macd"]-rowD["macd_signal"]
                pct=(delta/max(abs(rowD["close"]),1e-9))*100
                arrow="→" if abs(delta)<1e-12 else ("↑" if delta>0 else "↓")
                strength=f" ({pct:+.2f}%)"
            lines.append(f"{sym} {arrow}{strength}")
        except: lines.append(f"{sym} ?")
    return " ".join(lines)

# ===== DAILY WINDOW CONTROL =====
def now_rome(): return datetime.now(ZoneInfo("Europe/Rome"))
def should_send_daily_report(state):
    n=now_rome()
    if not (n.hour==8 and n.minute<15): return False
    return str(n.date())!=str(state.get("_daily_report_date"))
def mark_daily_report_sent(state): state["_daily_report_date"]=str(now_rome().date())
def should_send_heartbeat(state):
    n=now_rome()
    if not (n.hour==8 and n.minute<15): return False
    return str(n.date())!=str(state.get("_heartbeat_date"))
def mark_heartbeat_sent(state): state["_heartbeat_date"]=str(now_rome().date())

# ===== MAIN =====
def run_once():
    state=load_state()
    msgs=[]
    for sym in COINS.keys():
        try:
            df1=add_indicators(fetch_ohlc_1h(sym))
            row1,prev1=last_closed_rows(df1)
            if row1 is None: continue

            dfD=add_indicators(fetch_ohlc_1d(sym))
            rowD,prevD=last_closed_rows(dfD)
            trend_up=notna_all(rowD["macd"],rowD["macd_signal"]) and rowD["macd"]>rowD["macd_signal"]

            # --- BUY SIGNAL (conservativo) ---
            if trend_up and notna_all(prev1["rsi"],row1["rsi"],prev1["macd"],prev1["macd_signal"],row1["macd"],row1["macd_signal"]):
                rsi_cross=prev1["rsi"]>=RSI_LOW and row1["rsi"]<RSI_LOW
                macd_cross=prev1["macd"]<=prev1["macd_signal"] and row1["macd"]>row1["macd_signal"]
                if rsi_cross and macd_cross and cooldown_ok(state,sym,"buy_combo",COOLDOWN_HOURS):
                    msgs.append(
                        f"🟢 <b>{sym}</b> BUY (RSI < {RSI_LOW} + MACD ↑)\n"
                        f"Price: {row1['close']:.6f} USDT\n"
                        f"Time: {row1['close_time'].strftime('%Y-%m-%d %H:%M UTC')} | Trend 1D: MACD ↑"
                    )
                    mark_sent(state,sym,"buy_combo")

            # --- OPPORTUNITY (più largo) ---
            if ENABLE_OPPORTUNITY and trend_up and notna_all(row1["rsi"], row1["macd"], row1["macd_signal"], row1["macd_hist"]):
                rsi_ok = row1["rsi"] < RSI_WIDE
                macd_ok = (row1["macd"] > row1["macd_signal"])
                hist_ok = False
                if prev1 is not None and len(df1) >= 4:
                    h_1 = df1["macd_hist"].iloc[-2]
                    h_2 = df1["macd_hist"].iloc[-3]
                    h_3 = df1["macd_hist"].iloc[-4]
                    if notna_all(h_1, h_2, h_3):
                        hist_ok = (h_1 > h_2) and (h_2 > h_3)
                if rsi_ok and (macd_ok or hist_ok) and cooldown_ok(state, sym, "opp_alert", OPPORTUNITY_COOLDOWN_HOURS):
                    send_telegram(
                        "🟡 <b>{}</b> OPPORTUNITY (wider)\n"
                        "Price: {:.6f} USDT | RSI: {:.2f}\n"
                        "MACD {} Signal | 1D Trend: UP".format(
                            sym, row1["close"], row1["rsi"],
                            ">" if row1["macd"] > row1["macd_signal"] else "≈"
                        )
                    )
                    mark_sent(state, sym, "opp_alert")

            # --- TREND CHANGE ALERT (1D) ---
            curr_state=trend_state_from_row(rowD)
            prev_state=state.get("_trend1d_state",{}).get(sym)
            if curr_state!=prev_state:
                send_telegram(f"📈 <b>{sym}</b> Trend 1D cambiato: {prev_state or 'UNKNOWN'} → <b>{curr_state}</b>")
                state.setdefault("_trend1d_state",{})[sym]=curr_state
                if curr_state=="UP":
                    send_telegram(f"🧭 {sym}: Trend 1D <b>BULLISH</b>. Strategia holder: attendi un pullback 1H (RSI<30 + MACD ↑) o valuta 🟡 OPPORTUNITY.")

            # --- TREND 4H (intraday) opzionale ---
            if ENABLE_4H_TREND_ALERTS:
                df4=add_indicators(resample_to_4h(df1))
                row4,prev4=last_closed_rows(df4)
                if row4 is not None:
                    curr4=trend_state_from_row(row4)
                    key4=f"trend4h_{curr4}"
                    last_key=state.get("_trend4h_state",{}).get(sym)
                    if curr4!=last_key and cooldown_ok(state,sym,"trend4h_alert",TREND4H_COOLDOWN_HOURS):
                        send_telegram(f"⏱️ <b>{sym}</b> Trend 4H → <b>{curr4}</b>")
                        state.setdefault("_trend4h_state",{})[sym]=curr4
                        mark_sent(state,sym,"trend4h_alert")

        except Exception as e:
            print(sym,"errore:",e)

    if msgs:
        send_telegram("📣 <b>Crypto BUY Alerts (Holder)</b>\n"+"\n\n".join(msgs))
    else:
        print("Nessun BUY valido (filtrato da trend 1D / cooldown).")

    # --- Daily report + heartbeat (08:00-08:15 Europe/Rome) ---
    try:
        if should_send_daily_report(state):
            summary=build_daily_trend_report()
            send_telegram(f"🗞️ <b>Daily Trend 1D</b>\n{summary}")
            mark_daily_report_sent(state)
        if should_send_heartbeat(state):
            send_telegram("✅ Heartbeat: bot attivo e sincronizzato")
            mark_heartbeat_sent(state)
    except Exception as e:
        print("Daily/Heartbeat error:", e)

    save_state(state)

if __name__=="__main__":
    run_once()
