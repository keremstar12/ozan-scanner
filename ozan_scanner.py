#!/usr/bin/env python3
"""
OZAN SCANNER — MEXC API (GitHub Actions uyumlu)
Sinyal bulunca Telegram'a bildirim atar.
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import pandas as pd

# ─── AYARLAR ──────────────────────────────────────────────────────────────────
MIN_VOLUME      = 5_000_000
MIN_GAIN_PCT    = 3.0
MAX_GAIN_PCT    = 20.0
BTC_DROP_LIMIT  = -3.0
RSI_MAX         = 70
MIN_SPACE_PCT   = 4.0
MIN_RR          = 2.0
STOP_BUFFER     = 0.7
VOL_LOOKBACK    = 5
CANDLE_LIMIT    = 120
SWING_WINDOW    = 3
SWING_LOOKBACK  = 50

EXCLUDE = {'BTCUSDT','USDTUSDT','USDCUSDT','BUSDUSDT',
           'TUSDUSDT','FDUSDUSDT','USDPUSDT','EURUSDT'}

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
TUR_TZ           = timezone(timedelta(hours=3))
MEXC             = "https://api.mexc.com"
# ──────────────────────────────────────────────────────────────────────────────

def telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"Telegram hatası: {e}")


# ─── VERİ ÇEKME ───────────────────────────────────────────────────────────────

def get_tickers() -> list:
    r = requests.get(f"{MEXC}/api/v3/ticker/24hr", timeout=15)
    return r.json()

def get_btc_change() -> float:
    r = requests.get(f"{MEXC}/api/v3/ticker/24hr",
                     params={"symbol": "BTCUSDT"}, timeout=15)
    data = r.json()
    print(f"  BTC raw data: {data}")
    return float(data["priceChangePercent"])

def get_candles(symbol: str, interval: str = "1h", limit: int = CANDLE_LIMIT) -> pd.DataFrame:
    r = requests.get(f"{MEXC}/api/v3/klines",
                     params={"symbol": symbol, "interval": interval, "limit": limit},
                     timeout=15)
    data = r.json()
    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "ct","qv","tr","tbbv","tbqv","ign"
    ])
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    return df.iloc[:-1].reset_index(drop=True)

def get_daily_ma30(symbol: str) -> bool:
    try:
        df = get_candles(symbol, interval="1d", limit=35)
        df["ma30"] = df["close"].rolling(30).mean()
        last = df.iloc[-1]
        if pd.isna(last["ma30"]): return True
        return float(last["close"]) > float(last["ma30"])
    except:
        return True

def get_usdtry() -> float:
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
        return r.json()["rates"]["TRY"]
    except:
        return 38.0


# ─── TEKNİK ANALİZ ────────────────────────────────────────────────────────────

def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ma7"]  = df["close"].rolling(7).mean()
    df["ma30"] = df["close"].rolling(30).mean()
    df["ma99"] = df["close"].rolling(99).mean()
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss))
    return df

def swing_highs(df: pd.DataFrame) -> list:
    d = df.tail(SWING_LOOKBACK).reset_index(drop=True)
    w, highs = SWING_WINDOW, []
    for i in range(w, len(d) - w):
        if d["high"].iloc[i] == d["high"].iloc[i-w: i+w+1].max():
            highs.append(d["high"].iloc[i])
    return sorted(set(highs))

def swing_lows(df: pd.DataFrame) -> list:
    d = df.tail(SWING_LOOKBACK).reset_index(drop=True)
    w, lows = SWING_WINDOW, []
    for i in range(w, len(d) - w):
        if d["low"].iloc[i] == d["low"].iloc[i-w: i+w+1].min():
            lows.append(d["low"].iloc[i])
    return sorted(set(lows), reverse=True)

def nearest_res(df: pd.DataFrame, price: float):
    above = [h for h in swing_highs(df) if h > price * 1.005]
    return min(above) if above else None

def nearest_sup(price: float, sups: list):
    below = [s for s in sups if s < price]
    return below[0] if below else None

def ma_quality(price: float, ma30: float) -> str:
    d = abs(price - ma30) / ma30 * 100
    if d <= 1.0: return "A"
    if d <= 2.0: return "B"
    if d <= 3.0: return "C"
    return "X"


# ─── ANALİZ ───────────────────────────────────────────────────────────────────

def analyze(symbol: str, vol_usdt: float, change_24h: float, elenenler: dict):
    coin = symbol.replace("USDT", "")
    try:
        df   = get_candles(symbol)
        df   = calc_indicators(df)
        last = df.iloc[-1]

        price = last["close"]
        ma7   = last["ma7"]
        ma30  = last["ma30"]
        ma99  = last["ma99"]
        rsi   = last["rsi"]

        if pd.isna(ma99) or pd.isna(rsi):
            elenenler["Veri yetersiz"].append(coin); return None
        if not (ma7 > ma30 > ma99):
            elenenler["MA7>MA30>MA99 değil"].append(coin); return None
        if rsi > RSI_MAX:
            elenenler[f"RSI yüksek (>{RSI_MAX})"].append(f"{coin}({rsi:.0f})"); return None
        if price <= ma30:
            elenenler["Fiyat MA30 altında"].append(coin); return None

        quality = ma_quality(price, ma30)
        if quality == "X":
            dist = abs(price - ma30) / ma30 * 100
            elenenler["MA30'a %3'ten uzak"].append(f"{coin}(%{dist:.1f})"); return None
        if last["close"] <= last["open"]:
            elenenler["Kırmızı mum"].append(coin); return None

        avg_vol = df["volume"].iloc[-VOL_LOOKBACK-1:-1].mean()
        if last["volume"] <= avg_vol:
            elenenler["Hacim artmamış"].append(coin); return None

        sups    = swing_lows(df)
        near_ma = abs(last["low"] - ma30) / ma30 < 0.03
        near_s  = any(abs(last["low"] - s) / s < 0.03 for s in sups[:5])
        if not (near_ma or near_s):
            elenenler["Desteğe yakın değil"].append(coin); return None

        res = nearest_res(df, price)
        if res:
            space_pct = (res - price) / price * 100
            if space_pct < MIN_SPACE_PCT:
                elenenler[f"Alan yetersiz (<%{MIN_SPACE_PCT})"].append(
                    f"{coin}(%{space_pct:.1f})"); return None
        else:
            space_pct = None

        sup      = nearest_sup(price, sups)
        stop     = sup * (1 - STOP_BUFFER / 100) if sup else price * 0.95
        stop_pct = (price - stop) / price * 100

        tp_pct = space_pct if res else stop_pct * MIN_RR
        if not res:
            res = price * (1 + tp_pct / 100)

        rr = tp_pct / stop_pct
        if rr < MIN_RR:
            elenenler["R/R yetersiz (<1:2)"].append(f"{coin}(1:{rr:.1f})"); return None
        if tp_pct < MIN_SPACE_PCT:
            elenenler["Hedef çok küçük"].append(coin); return None

        return dict(
            coin=coin, price=price,
            ma7=ma7, ma30=ma30, ma99=ma99,
            rsi=rsi, quality=quality,
            vol_m=vol_usdt / 1_000_000,
            change_24h=change_24h,
            stop=stop, stop_pct=stop_pct,
            target=res, target_pct=tp_pct, rr=rr,
            space_pct=space_pct,
        )
    except Exception:
        elenenler["Hata"].append(coin); return None


# ─── ÇIKTI ────────────────────────────────────────────────────────────────────

def tl(val: float, kur: float) -> str:
    v = val * kur
    return f"{v:.4g} TL" if v >= 1 else f"{v:.6g} TL"

def print_elenenler(elenenler: dict, toplam: int):
    print(f"\n{'═'*54}")
    print(f"  FİLTRE RAPORU  —  {toplam} coin analiz edildi")
    print(f"{'═'*54}")
    for filtre, coinler in elenenler.items():
        if coinler:
            print(f"\n  ✗ {filtre} ({len(coinler)}):")
            print(f"    {', '.join(coinler)}")
    print(f"\n{'─'*54}")

def print_signal(s: dict, kur: float):
    icon = {"A": "🟢", "B": "🟡", "C": "🟠"}[s["quality"]]
    sp   = f"%{s['space_pct']:.1f}" if s["space_pct"] else f"%{s['target_pct']:.1f}"
    print(f"""
{'─'*54}
{icon}  SİNYAL: {s['coin']}   [{s['quality']} KALİTE]

  Fiyat   : {tl(s['price'], kur)}
  MA7     : {tl(s['ma7'], kur)}
  MA30    : {tl(s['ma30'], kur)}
  MA99    : {tl(s['ma99'], kur)}
  RSI     : {s['rsi']:.1f}   |   Hacim: {s['vol_m']:.0f}M USDT
  24s     : +{s['change_24h']:.1f}%

  ✅ MA7 > MA30 > MA99
  ✅ Fiyat MA30 üstünde
  ✅ Desteğe yakın
  ✅ Yeşil mum kapandı
  ✅ Hacim artmış
  ✅ Önünde {sp} alan var

  Giriş   : ~{tl(s['price'], kur)}
  Stop    : {tl(s['stop'], kur)}  (-%{s['stop_pct']:.2f}%)
  Hedef   : {tl(s['target'], kur)}  (+%{s['target_pct']:.2f}%)
  R/Ödül  : 1 : {s['rr']:.1f}

  Binance TR'de {s['coin']}TRY paritesini inceleyin.
{'─'*54}""")

def telegram_signal(s: dict, kur: float):
    icon = {"A": "🟢", "B": "🟡", "C": "🟠"}[s["quality"]]
    sp   = f"%{s['space_pct']:.1f}" if s["space_pct"] else f"%{s['target_pct']:.1f}"
    telegram(
        f"{icon} <b>{s['coin']}</b>  [{s['quality']} KALİTE]\n\n"
        f"Fiyat : {tl(s['price'], kur)}\n"
        f"MA30  : {tl(s['ma30'], kur)}\n"
        f"RSI   : {s['rsi']:.1f}   |   Hacim: {s['vol_m']:.0f}M USDT\n"
        f"24s   : +{s['change_24h']:.1f}%\n\n"
        f"✅ MA7 > MA30 > MA99\n"
        f"✅ Fiyat MA30 üstünde\n"
        f"✅ Desteğe yakın\n"
        f"✅ Yeşil mum kapandı\n"
        f"✅ Hacim artmış\n"
        f"✅ Önünde {sp} alan var\n\n"
        f"Giriş  : ~{tl(s['price'], kur)}\n"
        f"Stop   : {tl(s['stop'], kur)}  (-%{s['stop_pct']:.2f}%)\n"
        f"Hedef  : {tl(s['target'], kur)}  (+%{s['target_pct']:.2f}%)\n"
        f"R/Ödül : 1 : {s['rr']:.1f}\n\n"
        f"Binance TR'de <b>{s['coin']}TRY</b>\n"
        f"⚠️ Yatırım tavsiyesi değildir."
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(TUR_TZ).strftime("%d.%m.%Y %H:%M")
    print(f"\n  OZAN SCANNER — {now}  (MEXC)\n")

    kur = get_usdtry()
    print(f"  USD/TRY: {kur:.2f}")

    try:
        btc = get_btc_change()
        if btc <= BTC_DROP_LIMIT:
            msg = f"⛔ BTC {btc:+.2f}% düştü — sinyal üretilmiyor."
            print(f"\n  {msg}"); telegram(msg); return
        print(f"  BTC 24s: {btc:+.2f}% — uygun\n")
    except Exception as e:
        print(f"  BTC hatası: {e}"); return

    try:
        tickers = get_tickers()
    except Exception as e:
        print(f"  Veri hatası: {e}"); return

    # Debug: ilk 2 ticker'ı göster
    if tickers:
        print(f"  Ornek ticker: {tickers[0]}")
    candidates = []
    for t in tickers:
        try:
            sym    = t["symbol"]
            vol    = float(t["quoteVolume"])
            change = float(t["priceChangePercent"])
            if sym in EXCLUDE: continue
            if not sym.endswith("USDT"): continue
            if vol >= MIN_VOLUME and MIN_GAIN_PCT <= change <= MAX_GAIN_PCT:
                candidates.append((sym, vol, change))
        except: continue

    print(f"  {len(candidates)} coin ön filtreyi geçti, analiz ediliyor...")

    elenenler = defaultdict(list)
    signals   = []

    for sym, vol, change in candidates:
        r = analyze(sym, vol, change, elenenler)
        if r: signals.append(r)
        time.sleep(0.1)

    print_elenenler(elenenler, len(candidates))

    order = {"A": 0, "B": 1, "C": 2}
    signals.sort(key=lambda x: (order[x["quality"]], -x["rr"]))

    if not signals:
        print("\n  Şu an sinyal bulunamadı.\n")
    else:
        print(f"\n  {len(signals)} SİNYAL BULUNDU:")
        telegram(f"📊 <b>OZAN SCANNER</b> — {now}\n{len(signals)} sinyal\n{'─'*28}")
        time.sleep(0.5)
        for s in signals:
            print_signal(s, kur)
            telegram_signal(s, kur)
            time.sleep(0.5)

    print("  ⚠️  Yatırım tavsiyesi değildir.\n")
    if not os.environ.get("TELEGRAM_TOKEN"):
        input("  Çıkmak için Enter'a basın...")


if __name__ == "__main__":
    main()
