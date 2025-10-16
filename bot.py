import os, json, requests, pandas as pd, pandas_ta as ta
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ===== CONFIG =====
COINS = {
    "BTC": ("BTC-USDT", "BTC-USDT", "BTCUSDT", None),
    "ETH": ("ETH-USDT", "ETH-USDT", "ETHUSDT", None),
    "BNB": ("BNB-USDT", "BNB-USDT", "BNBUSDT", None),
    "SOL": ("SOL-USDT", "SOL-USDT", "SOLUSDT", None),
    "BGB": (None, None, None, "BGBUSDT"),
}
CANDLES_1H, CANDLES_1D = 240, 400
RSI_LOW, RSI_HIGH = 30, 75
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
COOLDOWN_HOURS = 6

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")
STATE_DIR, STATE_FILE = Path(".state"), Path(".state/last_signals.json")

# ===== TELEGRAM =====
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML","disable_web_page_preview":True},timeout=25)
        if r.status_code != 200: print("Telegram error:",r.text)
    except Exception as e: print("Telegram exc:",e)

# ===== FETCH =====
def fetch_okx(inst,limit,bar):
    r=requests.get("https://www.okx.com/api/v5/market/candles",
                   params={"instId":inst,"bar":bar,"limit":limit},timeout=25)
    r.raise_for_status(); data=r.json().get("data",[])
    rows=[{"close_time":pd.to_datetime(int(x[0]),unit="ms",utc=True),
           "open":float(x[1]),"high":float(x[2]),"low":float(x[3]),
           "close":float(x[4]),"volume":float(x[5])} for x in data]
    return pd.DataFrame(rows).sort_values("close_time")

def fetch_kucoin(sym,limit,typ):
    r=requests.get("https://api.kucoin.com/api/v1/market/candles",
                   params={"type":typ,"symbol":sym},timeout=25)
    r.raise_for_status(); data=r.json().get("data",[])
    rows=[{"close_time":pd.to_datetime(int(x[0])*1000,unit="ms",utc=True),
           "open":float(x[1]),"close":float(x[2]),"high":float(x[3]),
           "low":float(x[4]),"volume":float(x[5])} for x in data]
    return pd.DataFrame(rows).sort_values("close_time")

def fetch_bybit(sym,limit,interval):
    r=requests.get("https://api.bybit.com/v5/market/kline",
                   params={"category":"spot","symbol":sym,"interval":interval,"limit":limit},timeout=25)
    r.raise_for_status(); data=r.json().get("result",{}).get("list",[])
    rows=[{"close_time":pd.to_datetime(int(x[0]),unit="ms",utc=True),
           "open":float(x[1]),"high":float(x[2]),"low":float(x[3]),
           "close":float(x[4]),"volume":float(x[5])} for x in data]
    return pd.DataFrame(rows).sort_values("close_time")

def fetch_bitget(sym,limit):
    for u in [
        ("https://api.bitget.com/api/v2/spot/market/candles",{"symbol":sym,"granularity":"1h","limit":limit}),
        ("https://api.bitget.com/api/v2/market/candles",{"symbol":sym,"productType":"spbl","granularity":"1h","limit":limit}),
        ("https://api.bitget.com/api/spot/v1/market/candles",{"symbol":sym,"period":"1H","limit":limit})
    ]:
        try:
            r=requests.get(u[0],params=u[1],timeout=25); r.raise_for_status()
            data=r.json().get("data",[])
            rows=[{"close_time":pd.to_datetime(int(x[0]),unit="ms",utc=True),
                   "open":float(x[1]),"high":float(x[2]),"low":float(x[3]),
                   "close":float(x[4]),"volume":float(x[5])} for x in data]
            return pd.DataFrame(rows).sort_values("close_time")
        except Exception as e: print("Bitget fail:",e)
    raise RuntimeError("Bitget no data")

def fetch_ohlc_1h(sym):
    okx,ku,by,bg=COINS[sym]
    for f,args in [(fetch_okx,(okx,CANDLES_1H,"1H")),
                   (fetch_kucoin,(ku,CANDLES_1H,"1hour")),
                   (fetch_bybit,(by,CANDLES_1H,"60")),
                   (fetch_bitget,(bg,CANDLES_1H))]:
        if args[0]:
            try: return f(*args)
            except Exception as e: print(sym,"1H fail:",e)
    raise RuntimeError("no 1H data")

def fetch_ohlc_1d(sym):
    okx,ku,by,bg=COINS[sym]
    for f,args in [(fetch_okx,(okx,CANDLES_1D,"1D")),
                   (fetch_kucoin,(ku,CANDLES_1D,"1day")),
                   (fetch_bybit,(by,CANDLES_1D,"D")),
                   (fetch_bitget,(bg,CANDLES_1D))]:
        if args[0]:
            try: return f(*args)
            except Exception as e: print(sym,"1D fail:",e)
    raise RuntimeError("no 1D data")

# ===== INDICATORS =====
def add_ind(df):
    df["rsi"]=ta.rsi(df["close"],length=14)
    m=ta.macd(df["close"],fast=MACD_FAST,slow=MACD_SLOW,signal=MACD_SIGNAL)
    df["macd"],df["macd_signal"]=m["MACD_12_26_9"],m["MACDs_12_26_9"]
    return df

def last_rows(df):
    if len(df)<3: return None,None
    return df.iloc[-2],df.iloc[-3]

# ===== STATE =====
def load_state():
    try:
        if STATE_FILE.exists(): return json.load(open(STATE_FILE))
    except: pass
    return {}
def save_state(s):
    STATE_DIR.mkdir(exist_ok=True)
    json.dump(s,open(STATE_FILE,"w"))
def cooldown_ok(s,c,k,h):
    now=datetime.now(timezone.utc)
    last=s.get(c,{}).get(k)
    if not last: return True
    try: last=datetime.fromisoformat(last)
    except: return True
    return now-last>=timedelta(hours=h)
def mark_sent(s,c,k): s.setdefault(c,{})[k]=datetime.now(timezone.utc).isoformat()

# ===== MAIN =====
def run_once():
    state, msgs = load_state(), []
    for sym in COINS.keys():
        try:
            df1=add_ind(fetch_ohlc_1h(sym))
            r1,p1=last_rows(df1)
            if r1 is None: continue

            dfD=add_ind(fetch_ohlc_1d(sym))
            rd,pd=last_rows(dfD)
            trend_up=pd.notna(rd["macd"]) and rd["macd"]>rd["macd_signal"]
            if not trend_up: continue  # filtro macro 1D

            # RSI + MACD insieme
            if all(pd.notna([p1["rsi"],r1["rsi"],p1["macd"],p1["macd_signal"],r1["macd"],r1["macd_signal"]])):
                rsi_cross = p1["rsi"]>=RSI_LOW and r1["rsi"]<RSI_LOW
                macd_cross= p1["macd"]<=p1["macd_signal"] and r1["macd"]>r1["macd_signal"]
                if rsi_cross and macd_cross:
                    key="buy_combo"
                    if cooldown_ok(state,sym,key,COOLDOWN_HOURS):
                        msgs.append(f"🟢 <b>{sym}</b> BUY (RSI < {RSI_LOW} + MACD ↑)\n"
                                    f"Price: {r1['close']:.6f} USDT\nTime: {r1['close_time'].strftime('%Y-%m-%d %H:%M UTC')} | Trend 1D ↑")
                        mark_sent(state,sym,key)
        except Exception as e:
            print(sym,"errore:",e)
    if msgs:
        send_telegram("📣 <b>Crypto BUY Alerts (Holder)</b>\n"+"\n\n".join(msgs))
    else:
        print("Nessun BUY valido (filtrato da trend 1D / cooldown).")
    save_state(state)

if __name__=="__main__": run_once()
