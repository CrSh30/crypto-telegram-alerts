# bot.py — main (tabella daily + news intraday + robust fetch)

import os
import json
import requests
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ========= CONFIG =========

# Symbols mapping: (OKX instId, Bybit symbol, Bitget spot symbol)
COINS = {
    "BTC": ("BTC-USDT", "BTCUSDT", None),
    "ETH": ("ETH-USDT", "ETHUSDT", None),
    "BNB": ("BNB-USDT", "BNBUSDT", None),
    "SOL": ("SOL-USDT", "SOLUSDT", None),
    "BGB": (None, None, "BGBUSDT"),  # BGB via Bitget spot
}

CANDLES_1H = 240
CANDLES_1D = 400

# BUY (conservativo)
RSI_LOW = 30
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "6"))

# OPPORTUNITY (wide) — controllabili via ENV
ENABLE_OPPORTUNITY = os.getenv("ENABLE_OPPORTUNITY", "1") == "1"
RSI_WIDE = int(os.getenv("RSI_WIDE", "40"))
OPPORTUNITY_COOLDOWN_HOURS = int(os.getenv("OPPORTUNITY_COOLDOWN_HOURS", "3"))

# Trend 4H opzionale
ENABLE_4H_TREND_ALERTS = True
TREND4H_COOLDOWN_HOURS = 6

# Telegram
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

# Stato su filesystem per Actions Cache
STATE_DIR  = Path(".state")
STATE_FILE = STATE_DIR / "last_signals.json"

# === NEWS / HEADLINES ===
ENABLE_NEWS = os.getenv("ENABLE_NEWS", "1") == "1"           # 0 per disattivare
NEWS_INTRADAY = os.getenv("NEWS_INTRADAY", "1") == "1"       # 1 = anche durante la giornata
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN")           # secret
NEWS_MOVE_PCT = float(os.getenv("NEWS_MOVE_PCT", "3.0"))     # soglia PriceΔ% 1D
NEWS_COOLDOWN_HOURS = int(os.getenv("NEWS_COOLDOWN_HOURS", "6"))

# ========= UTILS =========

def notna_all(*vals) -> bool:
    for v in vals:
        if v is None or pd.isna(v):
            return False
    return True

def safe_get(row, key, default=None):
    if row is None:
        return default
    try:
        if key in row:  # pandas Series
            return row[key]
    except Exception:
        pass
    try:
        return row.get(key, default)  # dict-like
    except Exception:
        return default

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

def pct(a, b):
    try:
        a = float(a); b = float(b)
        return (a - b) / b * 100.0 if b != 0 else 0.0
    except Exception:
        return 0.0

def macd_delta_pct(macd_val, sig_val):
    try:
        macd_val = float(macd_val); sig_val = float(sig_val)
        if sig_val == 0:
            return 0.0
        return (macd_val - sig_val) / abs(sig_val) * 100.0
    except Exception:
        return 0.0

# ========= FETCHERS =========

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

def fetch_bitget_spot(sym, limit, granularity="1h"):
    g = (granularity or "").lower().strip()
    if g in ("1d", "day", "d"):
        g = "1day"
    if g == "1":
        g = "1h"

    if limit is None or limit <= 0:
        limit = 100
    else:
        limit = min(int(limit), 100)

    urls = [
        ("https://api.bitget.com/api/v2/spot/market/candles",
         {"symbol": sym, "granularity": g, "limit": limit}),
        ("https://api.bitget.com/api/v2/spot/market/history-candles",
         {"symbol": sym, "granularity": g, "limit": limit,
          "endTime": int(datetime.now(timezone.utc).timestamp() * 1000)}),
    ]
    for url, p in urls:
        try:
            r = requests.get(url, params=p, timeout=25)
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                continue
            rows = [{
                "close_time": pd.to_datetime(int(x[0]), unit="ms", utc=True),
                "open": float(x[1]), "high": float(x[2]),
                "low": float(x[3]), "close": float(x[4]),
                "volume": float(x[5]),
            } for x in data]
            df = pd.DataFrame(rows).sort_values("close_time")
            if not df.empty:
                return df
        except Exception as e:
            print(f"Bitget spot fetch fail ({url}):", e)

    raise RuntimeError("Bitget spot: no data")

def fetch_ohlc_1h(sym):
    okx, by, bg = COINS[sym]
    if okx:
        try:
            return fetch_okx(okx, CANDLES_1H, "1H")
        except Exception as e:
            print(sym, "OKX 1H fail:", e)
    if by:
        try:
            return fetch_bybit(by, CANDLES_1H, "60")
        except Exception as e:
            print(sym, "Bybit 1H fail:", e)
    if bg:
        try:
            return fetch_bitget_spot(bg, CANDLES_1H, "1h")
        except Exception as e:
            print(sym, "Bitget 1H fail:", e)
    raise RuntimeError("no 1H data for " + sym)

def fetch_ohlc_1d(sym):
    okx, by, bg = COINS[sym]
    if okx:
        try:
            return fetch_okx(okx, CANDLES_1D, "1D")
        except Exception as e:
            print(sym, "OKX 1D fail:", e)
    if by:
        try:
            return fetch_bybit(by, CANDLES_1D, "D")
        except Exception as e:
            print(sym, "Bybit 1D fail:", e)
    if bg:
        try:
            return fetch_bitget_spot(bg, min(CANDLES_1D, 100), "1day")
        except Exception as e:
            print(sym, "Bitget 1D fail:", e)
    print("DEBUG 1D miss:", sym)
    raise RuntimeError("no 1D data for " + sym)

def resample_to_4h(df_1h: pd.DataFrame) -> pd.DataFrame:
    df = df_1h.copy()
    df = df.set_index(pd.to_datetime(df["close_time"], utc=True)).sort_index()
    ohlc = df["close"].resample("4h", label="right", closed="right").ohlc()
    vol  = df["volume"].resample("4h", label="right", closed="right").sum()
    out = pd.concat([ohlc, vol], axis=1).dropna().reset_index()
    out = out.rename(columns={"index":"close_time"})
    return out

# ========= TECHNICALS =========

def add_indicators(df):
    if df is None or len(df) < 30:
        print("⚠️ add_indicators: dataframe vuoto o troppo corto (len =", len(df) if df is not None else "None", ")")
        return df

    df = df.copy()
    if "close_time" in df:
        df = df.sort_values("close_time").drop_duplicates(subset="close_time")

    try:
        df["rsi"] = ta.rsi(df["close"], length=14)
    except Exception as e:
        print("⚠️ add_indicators RSI error:", e)
        df["rsi"] = None

    try:
        macd = ta.macd(df["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        if macd is not None and "MACD_12_26_9" in macd:
            df["macd"] = macd["MACD_12_26_9"]
            df["macd_signal"] = macd["MACDs_12_26_9"]
        else:
            df["macd"], df["macd_signal"] = None, None
            print("⚠️ add_indicators: MACD returned None or missing columns")
    except Exception as e:
        print("⚠️ add_indicators MACD error:", e)
        df["macd"], df["macd_signal"] = None, None

    try:
        if "macd" in df and "macd_signal" in df:
            df["macd_hist"] = df["macd"] - df["macd_signal"]
        else:
            df["macd_hist"] = None
    except Exception as e:
        print("⚠️ add_indicators hist error:", e)
        df["macd_hist"] = None

    return df

def last_closed_rows(df):
    if df is None or len(df) < 3:
        return None, None
    return df.iloc[-2], df.iloc[-3]

def trend_state_from_row(row):
    if row is None:
        return "NEUTRAL"
    macd_val = safe_get(row, "macd")
    sig_val  = safe_get(row, "macd_signal")
    if notna_all(macd_val, sig_val):
        if abs(macd_val - sig_val) < 1e-12:
            return "NEUTRAL"
        return "UP" if macd_val > sig_val else "DOWN"
    return "NEUTRAL"

# ========= STATE =========

def load_state():
    try:
        if STATE_FILE.exists():
            return json.load(open(STATE_FILE))
    except:
        pass
    return {}

def save_state(s):
    STATE_DIR.mkdir(exist_ok=True)
    json.dump(s, open(STATE_FILE, "w"))

def cooldown_ok(s, c, k, h):
    now = datetime.now(timezone.utc)
    last = s.get(c, {}).get(k)
    if not last:
        return True
    try:
        last = datetime.fromisoformat(last)
    except:
        return True
    return now - last >= timedelta(hours=h)

def mark_sent(s, c, k):
    s.setdefault(c, {})[k] = datetime.now(timezone.utc).isoformat()

# ========= DAILY REPORT TABELLARE =========

def build_daily_trend_report():
    rows = []
    for sym in COINS.keys():
        try:
            dfD = add_indicators(fetch_ohlc_1d(sym))
            if dfD is None or len(dfD) < 3:
                rows.append((sym, "?", 0.0, 0.0))
                continue

            last = dfD.iloc[-2]
            prev = dfD.iloc[-3]

            macd = safe_get(last, "macd"); sig = safe_get(last, "macd_signal")
            trend_up = notna_all(macd, sig) and macd > sig
            arrow = "↑" if trend_up else "↓"

            mdp = macd_delta_pct(macd, sig)
            price_pct = pct(float(safe_get(last, "close")), float(safe_get(prev, "close")))

            rows.append((sym, arrow, mdp, price_pct))
        except Exception as e:
            print(f"[DAILY] {sym} build err:", e)
            rows.append((sym, "?", 0.0, 0.0))

    header = ["COIN", "TREND", "MACDΔ%", "PriceΔ%"]
    data = []
    for sym, arrow, mdp, price_pct in rows:
        data.append([sym, arrow, f"{mdp:+.2f}%", f"{price_pct:+.2f}%"])

    cols = list(zip(*([header] + data)))
    widths = [max(len(str(x)) for x in col) for col in cols]

    def fmt(row):
        return "  ".join(str(val).rjust(w) for val, w in zip(row, widths))

    lines = [fmt(header), fmt(["-"*w for w in widths])]
    for r in data:
        lines.append(fmt(r))

    table = "\n".join(lines)
    return f"<pre>{table}</pre>"

# ========= NEWS =========

def cryptopanic_news(symbol, limit=2):
    if not CRYPTOPANIC_TOKEN:
        return []
    cur = symbol.upper()
    try:
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={
                "auth_token": CRYPTOPANIC_TOKEN,
                "currencies": cur,
                "kind": "news",
                "filter": "hot",
                "public": "true",
                "regions": "en",
            },
            timeout=20
        )
        r.raise_for_status()
        data = r.json().get("results", [])
        out = []
        for item in data:
            title = item.get("title")
            url = item.get("url") or (item.get("source") or {}).get("domain")
            if title and url:
                out.append((title, url))
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        print(f"[NEWS] CryptoPanic error for {symbol}:", e)
        return []

def maybe_send_news_for_moves(state):
    if not ENABLE_NEWS:
        return
    for sym in COINS.keys():
        try:
            dfD = add_indicators(fetch_ohlc_1d(sym))
            if dfD is None or len(dfD) < 3:
                continue
            last = dfD.iloc[-2]; prev = dfD.iloc[-3]
            price_pct = pct(float(safe_get(last, "close")), float(safe_get(prev, "close")))
            if abs(price_pct) < NEWS_MOVE_PCT:
                continue
            if not cooldown_ok(state, sym, "news_alert", NEWS_COOLDOWN_HOURS):
                continue
            headlines = cryptopanic_news(sym, limit=2)
            if headlines:
                lines = [f"📰 <b>{sym}</b> news (PriceΔ {price_pct:+.2f}% 1D):"]
                for t, u in headlines:
                    lines.append(f"• {t}\n  {u}")
                send_telegram("\n".join(lines))
                mark_sent(state, sym, "news_alert")
            else:
                print(f"[NEWS] No headlines for {sym} (PriceΔ {price_pct:+.2f}%).")
        except Exception as e:
            print(f"[NEWS] {sym} err:", e)

# ========= DAILY / HEARTBEAT =========

def now_rome():
    return datetime.now(ZoneInfo("Europe/Rome"))

def should_send_daily_report(state):
    n = now_rome()
    if str(n.date()) == str(state.get("_daily_report_date")):
        return False
    return n.hour >= 8

def mark_daily_report_sent(state):
    state["_daily_report_date"] = str(now_rome().date())

def should_send_heartbeat(state):
    n = now_rome()
    if str(n.date()) == str(state.get("_heartbeat_date")):
        return False
    return n.hour >= 8

def mark_heartbeat_sent(state):
    state["_heartbeat_date"] = str(now_rome().date())

# ========= MAIN =========

def run_once():
    state = load_state()

    try:
        utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        rome_now = now_rome().strftime("%Y-%m-%d %H:%M:%S Europe/Rome")
        last_daily = state.get("_daily_report_date")
        last_hb = state.get("_heartbeat_date")
        print(f"[SYNC] Start: {utc_now} | Local: {rome_now} | last_daily={last_daily} | last_heartbeat={last_hb}")
    except Exception as e:
        print("[SYNC] log error:", e)

    msgs = []

    for sym in COINS.keys():
        try:
            df1_raw = None
            try:
                df1_raw = fetch_ohlc_1h(sym)
            except Exception as fe:
                print(sym, "1H fetch fail:", fe)
                df1_raw = None

            if df1_raw is None or len(df1_raw) < 3:
                print(sym, "skip: no 1H data (raw is None or too short)")
                continue

            df1 = add_indicators(df1_raw)
            row1, prev1 = last_closed_rows(df1)
            if row1 is None:
                print(sym, "skip: no 1H last closed after indicators")
                continue

            dfD_raw = None
            try:
                dfD_raw = fetch_ohlc_1d(sym)
            except Exception as feD:
                print(sym, "1D fetch fail:", feD)
                dfD_raw = None

            rowD = prevD = None
            trend_up = False
            macdD = sigD = None

            if dfD_raw is None or len(dfD_raw) < 3:
                print(sym, "skip: no 1D data (raw is None or too short)")
            else:
                dfD = add_indicators(dfD_raw)
                rowD, prevD = last_closed_rows(dfD)
                if rowD is None:
                    print(sym, "skip: no 1D last closed after indicators")
                else:
                    macdD = safe_get(rowD, "macd")
                    sigD  = safe_get(rowD, "macd_signal")
                    trend_up = notna_all(macdD, sigD) and (macdD > sigD)

            if trend_up and notna_all(safe_get(prev1, "rsi"), safe_get(row1, "rsi"),
                                      safe_get(prev1, "macd"), safe_get(prev1, "macd_signal"),
                                      safe_get(row1, "macd"), safe_get(row1, "macd_signal")):
                rsi_cross  = (safe_get(prev1, "rsi") >= RSI_LOW) and (safe_get(row1, "rsi") < RSI_LOW)
                macd_cross = (safe_get(prev1, "macd") <= safe_get(prev1, "macd_signal")) and (safe_get(row1, "macd") > safe_get(row1, "macd_signal"))
                if rsi_cross and macd_cross and cooldown_ok(state, sym, "buy_combo", COOLDOWN_HOURS):
                    msgs.append(
                        f"🟢 <b>{sym}</b> BUY (RSI < {RSI_LOW} + MACD ↑)\n"
                        f"Price: {safe_get(row1,'close'):.6f} USDT\n"
                        f"Time: {safe_get(row1,'close_time').strftime('%Y-%m-%d %H:%M UTC')} | Trend 1D: MACD ↑"
                    )
                    mark_sent(state, sym, "buy_combo")

            if ENABLE_OPPORTUNITY and trend_up and notna_all(safe_get(row1,"rsi"), safe_get(row1,"macd"),
                                                             safe_get(row1,"macd_signal"), safe_get(row1,"macd_hist")):
                rsi_ok  = (safe_get(row1, "rsi") < RSI_WIDE)
                macd_ok = (safe_get(row1, "macd") > safe_get(row1, "macd_signal"))

                hist_ok = False
                if prev1 is not None and len(df1) >= 4:
                    h_1 = safe_get(df1.iloc[-2], "macd_hist")
                    h_2 = safe_get(df1.iloc[-3], "macd_hist")
                    h_3 = safe_get(df1.iloc[-4], "macd_hist")
                    if notna_all(h_1, h_2, h_3):
                        hist_ok = (h_1 > h_2) and (h_2 > h_3)

                if rsi_ok and (macd_ok or hist_ok) and cooldown_ok(state, sym, "opp_alert", OPPORTUNITY_COOLDOWN_HOURS):
                    send_telegram(
                        "🟡 <b>{}</b> OPPORTUNITY (wider)\n"
                        "Price: {:.6f} USDT | RSI: {:.2f}\n"
                        "MACD {} Signal | 1D Trend: UP".format(
                            sym, safe_get(row1,"close"), safe_get(row1,"rsi"),
                            ">" if safe_get(row1,"macd") > safe_get(row1,"macd_signal") else "≈"
                        )
                    )
                    mark_sent(state, sym, "opp_alert")

            if rowD is not None and notna_all(macdD, sigD):
                curr_state = "UP" if macdD > sigD else ("NEUTRAL" if abs(macdD - sigD) < 1e-12 else "DOWN")
                prev_state = state.get("_trend1d_state", {}).get(sym)
                if curr_state != prev_state:
                    send_telegram(f"📈 <b>{sym}</b> Trend 1D cambiato: {prev_state or 'UNKNOWN'} → <b>{curr_state}</b>")
                    state.setdefault("_trend1d_state", {})[sym] = curr_state
                    if curr_state == "UP":
                        send_telegram(f"🧭 {sym}: Trend 1D <b>BULLISH</b>. Holder: attendi pullback 1H (RSI<30 + MACD ↑) o valuta 🟡 OPPORTUNITY.")

            if ENABLE_4H_TREND_ALERTS:
                df4 = add_indicators(resample_to_4h(df1))
                row4, prev4 = last_closed_rows(df4)
                if row4 is not None:
                    curr4 = trend_state_from_row(row4)
                    last4 = state.get("_trend4h_state", {}).get(sym)
                    if curr4 != last4 and cooldown_ok(state, sym, "trend4h_alert", TREND4H_COOLDOWN_HOURS):
                        send_telegram(f"⏱️ <b>{sym}</b> Trend 4H → <b>{curr4}</b>")
                        state.setdefault("_trend4h_state", {})[sym] = curr4
                        mark_sent(state, sym, "trend4h_alert")

        except Exception as e:
            import traceback
            print(f"\n--- DEBUG TRACE for {sym} ---")
            traceback.print_exc()
            print("--- END TRACE ---\n")
            print(sym, "errore:", e)

    if msgs:
        send_telegram("📣 <b>Crypto BUY Alerts (Holder)</b>\n" + "\n\n".join(msgs))
    else:
        print("Nessun BUY valido (filtrato da trend 1D / cooldown).")

    try:
        will_daily = should_send_daily_report(state)
        will_hb = should_send_heartbeat(state)
        print(f"[DAILY] should_send_daily_report={will_daily} | [HEARTBEAT] should_send_heartbeat={will_hb}")

        if will_daily:
            summary = build_daily_trend_report()
            send_telegram("🗞️ <b>Daily Trend 1D</b>  <i>(MACDΔ% & PriceΔ%)</i>\n" + summary)
            mark_daily_report_sent(state)

        # News intraday se flag attivo; altrimenti solo con il Daily
        if ENABLE_NEWS:
            if NEWS_INTRADAY:
                maybe_send_news_for_moves(state)
            else:
                if will_daily:
                    maybe_send_news_for_moves(state)

        if will_hb:
            send_telegram("✅ Heartbeat: bot attivo e sincronizzato")
            mark_heartbeat_sent(state)
    except Exception as e:
        print("Daily/Heartbeat error:", e)

    save_state(state)

if __name__ == "__main__":
    run_once()
