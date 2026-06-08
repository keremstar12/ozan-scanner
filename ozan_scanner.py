#!/usr/bin/env python3
"""
OZAN SCANNER — Binance TR / Global Fırsat Tarayıcısı
Fiyatları TRY cinsinden gösterir.
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta
from binance.client import Client
import pandas as pd
from collections import defaultdict

# ─── AYARLAR ──────────────────────────────────────────────────────────────────
MIN_VOLUME      = 5_000_000    # 5M USDT
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

EXCLUDE = {
    'BTCUSDT','USDTUSDT','USDCUSDT','BUSDUSDT',
    'TUSDUSDT','FDUSDUSDT','USDPUSDT','EURUSDT'
}

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
TUR_TZ           = timezone(timedelta(hours=3))
# ──────────────────────────────────────────────────────────────────────────────

client = Client("", "")


def get_usdtry() -> float:
    """USD/TRY kurunu çek. Önce Binance TR, sonra fixer API."""
    try:
        tr_client = Client("", "", tld='me')
        rate = float(tr_client.get_ticker(symbol='USDTTRY')['lastPrice'])
        if rate > 1:
            return rate
    except:
        pass
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=5)
        return r.json()['rates']['TRY']
    except:
        return 38.0  # Fallback sabit kur


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


def get_all_usdt_tickers():
    return [t for t in client.get_ticker() if t['symbol'].endswith('USDT')]

def get_btc_change():
    return float(client.get_ticker(symbol='BTCUSDT')['priceChangePercent'])

def get_candles(symbol):
    klines = client.get_klines(
        symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=CANDLE_LIMIT
    )
    df = pd.DataFrame(klines, columns=[
        'time','open','high','low','close','volume',
        'ct','qv','tr','tbbv','tbqv','ign'
    ])
    for c in ['open','high','low','close','volume']:
        df[c] = df[c].astype(float)
    return df.iloc[:-1].reset_index(drop=True)


def calc_indicators(df):
    df = df.copy()
    df['ma7']  = df['close'].rolling(7).mean()
    df['ma30'] = df['close'].rolling(30).mean()
    df['ma99'] = df['close'].rolling(99).mean()
    delta = df['close'].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df['rsi'] = 100 - (100 / (1 + gain / loss))
    return df

def get_swing_highs(df):
    d = df.tail(SWING_LOOKBACK).reset_index(drop=True)
    w, highs = SWING_WINDOW, []
    for i in range(w, len(d) - w):
        if d['high'].iloc[i] == d['high'].iloc[i-w: i+w+1].max():
            highs.append(d['high'].iloc[i])
    return sorted(set(highs))

def get_swing_lows(df):
    d = df.tail(SWING_LOOKBACK).reset_index(drop=True)
    w, lows = SWING_WINDOW, []
    for i in range(w, len(d) - w):
        if d['low'].iloc[i] == d['low'].iloc[i-w: i+w+1].min():
            lows.append(d['low'].iloc[i])
    return sorted(set(lows), reverse=True)

def nearest_resistance(df, price):
    above = [h for h in get_swing_highs(df) if h > price * 1.005]
    return min(above) if above else None

def nearest_support_below(price, sups):
    below = [s for s in sups if s < price]
    return below[0] if below else None

def ma_quality(price, ma30):
    d = abs(price - ma30) / ma30 * 100
    if d <= 1.0: return 'A'
    if d <= 2.0: return 'B'
    if d <= 3.0: return 'C'
    return 'X'


def analyze(symbol, vol_usdt, change_24h, elenenler):
    coin = symbol.replace('USDT', '')
    try:
        df   = get_candles(symbol)
        df   = calc_indicators(df)
        last = df.iloc[-1]

        price = last['close']
        ma7   = last['ma7']
        ma30  = last['ma30']
        ma99  = last['ma99']
        rsi   = last['rsi']

        if pd.isna(ma99) or pd.isna(rsi):
            elenenler['Veri yetersiz'].append(coin); return None
        if not (ma7 > ma30 > ma99):
            elenenler['MA7>MA30>MA99 değil'].append(coin); return None
        if rsi > RSI_MAX:
            elenenler[f'RSI yüksek (>{RSI_MAX})'].append(f"{coin}({rsi:.0f})"); return None
        if price <= ma30:
            elenenler['Fiyat MA30 altında'].append(coin); return None

        quality = ma_quality(price, ma30)
        if quality == 'X':
            dist = abs(price - ma30) / ma30 * 100
            elenenler["MA30'a %3'ten uzak"].append(f"{coin}(%{dist:.1f})"); return None
        if last['close'] <= last['open']:
            elenenler['Kırmızı mum'].append(coin); return None

        avg_vol = df['volume'].iloc[-VOL_LOOKBACK-1:-1].mean()
        if last['volume'] <= avg_vol:
            elenenler['Hacim artmamış'].append(coin); return None

        sups    = get_swing_lows(df)
        near_ma = abs(last['low'] - ma30) / ma30 < 0.03
        near_s  = any(abs(last['low'] - s) / s < 0.03 for s in sups[:5])
        if not (near_ma or near_s):
            elenenler['Desteğe yakın değil'].append(coin); return None

        res = nearest_resistance(df, price)
        if res:
            space_pct = (res - price) / price * 100
            if space_pct < MIN_SPACE_PCT:
                elenenler[f'Alan yetersiz (<%{MIN_SPACE_PCT})'].append(
                    f"{coin}(%{space_pct:.1f})"); return None
        else:
            space_pct = None

        support  = nearest_support_below(price, sups)
        stop     = support * (1 - STOP_BUFFER / 100) if support else price * 0.95
        stop_pct = (price - stop) / price * 100

        if res:
            tp_pct = space_pct
        else:
            tp_pct = stop_pct * MIN_RR
            res    = price * (1 + tp_pct / 100)

        rr = tp_pct / stop_pct
        if rr < MIN_RR:
            elenenler['R/R yetersiz (<1:2)'].append(f"{coin}(1:{rr:.1f})"); return None
        if tp_pct < MIN_SPACE_PCT:
            elenenler['Hedef çok küçük'].append(coin); return None

        return dict(
            symbol=symbol, coin=coin, price=price,
            ma7=ma7, ma30=ma30, ma99=ma99,
            rsi=rsi, quality=quality,
            vol_m=vol_usdt / 1_000_000,
            change_24h=change_24h,
            stop=stop, stop_pct=stop_pct,
            target=res, target_pct=tp_pct,
            rr=rr, space_pct=space_pct,
        )
    except Exception:
        elenenler['Hata'].append(coin); return None


def print_elenenler(elenenler, toplam):
    print(f"\n{'═'*54}")
    print(f"  FİLTRE RAPORU  —  {toplam} coin analiz edildi")
    print(f"{'═'*54}")
    for filtre, coinler in elenenler.items():
        if coinler:
            isimler = ', '.join(coinler)
            print(f"\n  ✗ {filtre} ({len(coinler)}):")
            print(f"    {isimler}")
    print(f"\n{'─'*54}")


def fmt(val, kur):
    """USDT değeri TRY'ye çevirip formatlar."""
    try_val = val * kur
    if try_val >= 1:
        return f"{try_val:.4g} TL"
    else:
        return f"{try_val:.6g} TL"


def print_signal(s, kur):
    icon = {'A': '🟢', 'B': '🟡', 'C': '🟠'}[s['quality']]
    sp   = f"%{s['space_pct']:.1f}" if s['space_pct'] else f"%{s['target_pct']:.1f}"
    print(f"""
{'─'*54}
{icon}  SİNYAL: {s['coin']}   [{s['quality']} KALİTE]

  Fiyat   : {fmt(s['price'], kur)}
  MA7     : {fmt(s['ma7'], kur)}
  MA30    : {fmt(s['ma30'], kur)}
  MA99    : {fmt(s['ma99'], kur)}
  RSI     : {s['rsi']:.1f}   |   Hacim: {s['vol_m']:.0f}M USDT
  24s     : +{s['change_24h']:.1f}%

  ✅ MA7 > MA30 > MA99
  ✅ Fiyat MA30 üstünde
  ✅ Desteğe yakın
  ✅ Yeşil mum kapandı
  ✅ Hacim artmış
  ✅ Önünde {sp} alan var

  Giriş   : ~{fmt(s['price'], kur)}
  Stop    : {fmt(s['stop'], kur)}  (-%{s['stop_pct']:.2f}%)
  Hedef   : {fmt(s['target'], kur)}  (+%{s['target_pct']:.2f}%)
  R/Ödül  : 1 : {s['rr']:.1f}

  Binance TR'de {s['coin']}TRY paritesini inceleyin.
{'─'*54}""")


def telegram_signal(s, kur, now):
    icon = {'A': '🟢', 'B': '🟡', 'C': '🟠'}[s['quality']]
    sp   = f"%{s['space_pct']:.1f}" if s['space_pct'] else f"%{s['target_pct']:.1f}"
    msg = (
        f"{icon} <b>{s['coin']}</b>  [{s['quality']} KALİTE]\n\n"
        f"Fiyat : {fmt(s['price'], kur)}\n"
        f"MA30  : {fmt(s['ma30'], kur)}\n"
        f"RSI   : {s['rsi']:.1f}   |   Hacim: {s['vol_m']:.0f}M USDT\n"
        f"24s   : +{s['change_24h']:.1f}%\n\n"
        f"✅ MA7 > MA30 > MA99\n"
        f"✅ Fiyat MA30 üstünde\n"
        f"✅ Desteğe yakın\n"
        f"✅ Yeşil mum kapandı\n"
        f"✅ Hacim artmış\n"
        f"✅ Önünde {sp} alan var\n\n"
        f"Giriş  : ~{fmt(s['price'], kur)}\n"
        f"Stop   : {fmt(s['stop'], kur)}  (-%{s['stop_pct']:.2f}%)\n"
        f"Hedef  : {fmt(s['target'], kur)}  (+%{s['target_pct']:.2f}%)\n"
        f"R/Ödül : 1 : {s['rr']:.1f}\n\n"
        f"Binance TR'de <b>{s['coin']}TRY</b>\n"
        f"⚠️ Yatırım tavsiyesi değildir."
    )
    telegram(msg)


def main():
    now = datetime.now(TUR_TZ).strftime('%d.%m.%Y %H:%M')
    print(f"\n  OZAN SCANNER — {now}\n")

    # USD/TRY kuru
    kur = get_usdtry()
    print(f"  USD/TRY kuru: {kur:.2f}")

    try:
        btc = get_btc_change()
        if btc <= BTC_DROP_LIMIT:
            msg = f"⛔ BTC {btc:+.2f}% düştü — sinyal üretilmiyor."
            print(f"\n  {msg}")
            telegram(msg); return
        print(f"  BTC 24s: {btc:+.2f}% — uygun\n")
    except Exception as e:
        print(f"  BTC hatası: {e}"); return

    try:
        tickers = get_all_usdt_tickers()
    except Exception as e:
        print(f"  Veri hatası: {e}"); return

    candidates = []
    for t in tickers:
        try:
            sym    = t['symbol']
            vol    = float(t['quoteVolume'])
            change = float(t['priceChangePercent'])
            if sym in EXCLUDE: continue
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

    order = {'A': 0, 'B': 1, 'C': 2}
    signals.sort(key=lambda x: (order[x['quality']], -x['rr']))

    if not signals:
        print("\n  Şu an sinyal bulunamadı.\n")
    else:
        print(f"\n  {len(signals)} SİNYAL BULUNDU:")
        telegram(f"📊 <b>OZAN SCANNER</b> — {now}\n{len(signals)} sinyal bulundu\n{'─'*28}")
        time.sleep(0.5)
        for s in signals:
            print_signal(s, kur)
            telegram_signal(s, kur, now)
            time.sleep(0.5)

    print("  ⚠️  Yatırım tavsiyesi değildir.\n")
    input("  Çıkmak için Enter'a basın...")


if __name__ == "__main__":
    main()
