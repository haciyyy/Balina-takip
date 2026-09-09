import os
import json
import time
import math
import sqlite3
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone

import requests
import websocket
from flask import Flask

# ============================================================
# CONFIG (GÜÇLENDİRİLMİŞ VE DÜZELTİLMİŞ AYARLAR)
# ============================================================

API_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

TOP_COINS = 100
OI_UPDATE_SECONDS = 15

# KALİTE VE FİLTRE AYARLARI
SIGNAL_COOLDOWN_SECONDS = 1800  # Aynı coin için 30 dakika cooldown
MIN_SCORE = 9                   # Minimum geçerli skor
MIN_STRENGTH = 65               # Yüksek doğruluk için min %65 sinyal gücü
MIN_VOLUME_RATIO = 1.3          # En az normalin 1.3 katı hacim

REQUIRE_5M_CONFIRMATION = True
WHALE_USD = 100_000             # Balina işlem eşiği ($100k)
MAX_CANDLES = 300
MAX_TRADES = 1000

DB_FILE = "hyperliquid_signals.db"

# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "Hyperliquid Direction Bot (Mathematically Corrected & Optimized) Running", 200

# ============================================================
# GLOBAL STATE
# ============================================================

coins = []

candles = {
    "1m": defaultdict(lambda: deque(maxlen=MAX_CANDLES)),
    "5m": defaultdict(lambda: deque(maxlen=MAX_CANDLES)),
    "15m": defaultdict(lambda: deque(maxlen=MAX_CANDLES)),
}

trades = defaultdict(lambda: deque(maxlen=MAX_TRADES))
market = defaultdict(dict)
previous_oi = {}
last_signal = {}

lock = threading.Lock()

# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER,
            coin TEXT,
            direction TEXT,
            entry_price REAL,
            score_long REAL,
            score_short REAL,
            max_possible_score REAL,
            strength REAL,
            price_1m REAL,
            price_5m REAL,
            price_15m REAL,
            ema9_1m REAL,
            ema21_1m REAL,
            ema9_5m REAL,
            ema21_5m REAL,
            rsi_1m REAL,
            rsi_5m REAL,
            oi_change REAL,
            oi_change_pct REAL,
            buy_flow REAL,
            sell_flow REAL,
            volume_ratio_1m REAL,
            volume_ratio_5m REAL,
            result_1m REAL,
            result_5m REAL,
            result_15m REAL,
            evaluated_1m INTEGER DEFAULT 0,
            evaluated_5m INTEGER DEFAULT 0,
            evaluated_15m INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_signal(data):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT INTO signals (
            timestamp, coin, direction, entry_price, score_long, score_short,
            max_possible_score, strength, price_1m, price_5m, price_15m, 
            ema9_1m, ema21_1m, ema9_5m, ema21_5m, rsi_1m, rsi_5m, oi_change, 
            oi_change_pct, buy_flow, sell_flow, volume_ratio_1m, volume_ratio_5m
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["timestamp"], data["coin"], data["direction"], data["entry_price"],
        data["score_long"], data["score_short"], data["max_possible_score"], data["strength"],
        data["price_1m"], data["price_5m"], data["price_15m"],
        data["ema9_1m"], data["ema21_1m"], data["ema9_5m"], data["ema21_5m"],
        data["rsi_1m"], data["rsi_5m"], data["oi_change"], data["oi_change_pct"],
        data["buy_flow"], data["sell_flow"], data["volume_ratio_1m"], data["volume_ratio_5m"]
    ))
    conn.commit()
    conn.close()

# ============================================================
# TELEGRAM INTEGRATION
# ============================================================

def send_telegram(message):
    if not TELEGRAM_TOKEN:
        print("Telegram token ayarlanmamış.", flush=True)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        res = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": message},
            timeout=10
        )
        if res.status_code != 200:
            print(f"Telegram API Hatası ({res.status_code}): {res.text}", flush=True)
    except Exception as e:
        print("Telegram hatası:", e, flush=True)

# ============================================================
# HYPERLIQUID REST API
# ============================================================

def hl_info(payload, retries=3):
    for attempt in range(retries):
        try:
            response = requests.post(API_URL, json=payload, timeout=15)
            if response.status_code == 429:
                time.sleep(2)
                continue
            response.raise_for_status()
            return response.json()
        except Exception as e:
            if attempt == retries - 1:
                print("REST error:", e, flush=True)
            time.sleep(1)
    return None

# ============================================================
# LOAD TOP COINS
# ============================================================

def load_top_coins():
    global coins
    data = hl_info({"type": "metaAndAssetCtxs"})
    if not data:
        return

    try:
        meta = data[0]
        contexts = data[1]
        rows = []

        for i, item in enumerate(meta["universe"]):
            if i >= len(contexts):
                continue
            ctx = contexts[i]
            coin = item["name"]
            volume = float(ctx.get("dayNtlVlm", 0) or 0)
            rows.append((coin, volume))

        rows.sort(key=lambda x: x[1], reverse=True)
        coins = [x[0] for x in rows[:TOP_COINS]]

        print(f"{len(coins)} adet yüksek hacimli coin yüklendi.", flush=True)
    except Exception as e:
        print("Coin yükleme hatası:", e, flush=True)

# ============================================================
# INITIAL CANDLE HISTORY
# ============================================================

def load_initial_candles():
    now = int(time.time() * 1000)
    intervals = {"1m": 600, "5m": 500, "15m": 300}

    for coin in coins:
        for interval, count in intervals.items():
            minutes = {"1m": 1, "5m": 5, "15m": 15}[interval]
            start = now - (count * minutes * 60 * 1000)

            data = hl_info({
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": start,
                    "endTime": now
                }
            })

            if not data:
                continue

            try:
                with lock:
                    for candle in data:
                        candles[interval][coin].append({
                            "t": int(candle["t"]),
                            "T": int(candle["T"]),
                            "o": float(candle["o"]),
                            "h": float(candle["h"]),
                            "l": float(candle["l"]),
                            "c": float(candle["c"]),
                            "v": float(candle["v"]),
                            "n": int(candle["n"])
                        })
            except Exception as e:
                print("Candle load error", coin, interval, e, flush=True)

            time.sleep(0.1)

        print("Geçmiş veri yüklendi:", coin, flush=True)

# ============================================================
# MATHEMATICALLY CORRECT INDICATORS
# ============================================================

def ema(values, period):
    """
    Doğru Üstel Hareketli Ortalama (EMA) Hesaplaması
    """
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    # İlk değer için Basit Hareketli Ortalama (SMA)
    result = sum(values[:period]) / period
    
    # Takip eden değerler için EMA Formülü: (Fiyat * K) + (Önceki EMA * (1 - K))
    for price in values[period:]:
        result = (price * multiplier) + (result * (1 - multiplier))
    return result

def rsi(values, period=14):
    """
    Doğru Wilder's Smoothing Yöntemli RSI Hesaplaması
    """
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    if len(gains) < period:
        return None

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def percentage_change(values, periods=1):
    """
    Tam Periyot Değişim Yüzdesi Hesaplaması
    """
    if len(values) <= periods:
        return 0.0
    old = values[-periods - 1]
    new = values[-1]
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100.0

def volume_ratio(candle_list, period=20):
    if len(candle_list) < period + 1:
        return 1.0
    volumes = [x["v"] for x in candle_list]
    current = volumes[-1]
    avg = sum(volumes[-period - 1:-1]) / period
    if avg <= 0:
        return 1.0
    return current / avg

# ============================================================
# INDICATOR HELPER
# ============================================================

def get_closes(interval, coin):
    with lock:
        return [x["c"] for x in candles[interval][coin]]

def calculate_indicators(interval, coin):
    closes = get_closes(interval, coin)
    if len(closes) < 30:
        return None

    with lock:
        data_copy = list(candles[interval][coin])

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    rsi_value = rsi(closes, 14)
    momentum = percentage_change(closes, 1)
    
    # 5 mumluk momentum hesaplama
    momentum_long = percentage_change(closes, 5 if len(closes) >= 6 else 1)
    vol_ratio = volume_ratio(data_copy, 20)

    return {
        "price": closes[-1],
        "ema9": ema9,
        "ema21": ema21,
        "rsi": rsi_value,
        "momentum": momentum,
        "momentum_long": momentum_long,
        "volume_ratio": vol_ratio,
    }

# ============================================================
# TRADE & WHALE FLOW ANALYSIS
# ============================================================

def calculate_trade_flow(coin, seconds=300):
    now = int(time.time() * 1000)
    buy = sell = whale_buy = whale_sell = 0.0

    with lock:
        trade_list = list(trades[coin])

    for trade in trade_list:
        if now - trade["time"] > seconds * 1000:
            continue
        usd = trade["px"] * trade["sz"]
        if trade["side"] == "B":
            buy += usd
            if usd >= WHALE_USD:
                whale_buy += usd
        else:
            sell += usd
            if usd >= WHALE_USD:
                whale_sell += usd

    return {
        "buy": buy,
        "sell": sell,
        "whale_buy": whale_buy,
        "whale_sell": whale_sell
    }

# ============================================================
# OI MONITORING
# ============================================================

def update_oi():
    print("OI Güncelleme servisi aktif...", flush=True)
    while True:
        data = hl_info({"type": "metaAndAssetCtxs"})
        if data:
            try:
                meta = data[0]
                contexts = data[1]
                for i, item in enumerate(meta["universe"]):
                    if i >= len(contexts):
                        continue
                    coin = item["name"]
                    ctx = contexts[i]

                    oi = float(ctx.get("openInterest", 0) or 0)
                    oracle = float(ctx.get("oraclePx", 0) or 0)
                    mark = float(ctx.get("markPx", 0) or 0)
                    funding = float(ctx.get("funding", 0) or 0)
                    oi_usd = oi * oracle

                    with lock:
                        old = previous_oi.get(coin, oi_usd)
                        delta = oi_usd - old

                        # DÜZELTME #2: Değişim yüzdesi doğru biçimde ESKİ OI değerine oranlanıyor
                        oi_pct = (delta / old) * 100.0 if old > 0 else 0.0

                        market[coin].update({
                            "oi": oi_usd,
                            "oi_delta": delta,
                            "oi_pct": oi_pct,
                            "oracle": oracle,
                            "mark": mark,
                            "funding": funding,
                            "last_oi_update": time.time()
                        })
                        previous_oi[coin] = oi_usd
            except Exception as e:
                print("OI Parse Hatası:", e, flush=True)

        time.sleep(OI_UPDATE_SECONDS)

# ============================================================
# WEBSOCKET WORKER
# ============================================================

def ws_subscribe(ws, coin):
    subscriptions = [
        {"type": "trades", "coin": coin},
        {"type": "candle", "coin": coin, "interval": "1m"},
        {"type": "candle", "coin": coin, "interval": "5m"},
        {"type": "candle", "coin": coin, "interval": "15m"}
    ]
    for sub in subscriptions:
        ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

def process_trade(data):
    if not isinstance(data, list):
        return
    for trade in data:
        try:
            coin = trade["coin"]
            item = {
                "time": int(trade["time"]),
                "px": float(trade["px"]),
                "sz": float(trade["sz"]),
                "side": trade["side"]
            }
            with lock:
                trades[coin].append(item)
        except Exception:
            pass

def process_candle(data):
    candle_list = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    for candle in candle_list:
        try:
            interval = candle["i"]
            coin = candle["s"]
            item = {
                "t": int(candle["t"]),
                "T": int(candle["T"]),
                "o": float(candle["o"]),
                "h": float(candle["h"]),
                "l": float(candle["l"]),
                "c": float(candle["c"]),
                "v": float(candle["v"]),
                "n": int(candle["n"])
            }
            with lock:
                arr = candles[interval][coin]
                if arr and arr[-1]["t"] == item["t"]:
                    arr[-1] = item
                else:
                    arr.append(item)
        except Exception as e:
            print("Candle parse hatası:", e, flush=True)

def websocket_worker():
    while True:
        try:
            ws = websocket.create_connection(WS_URL, timeout=30)
            for coin in coins:
                ws_subscribe(ws, coin)

            last_ping = time.time()
            while True:
                if time.time() - last_ping > 25:
                    try:
                        ws.send(json.dumps({"method": "ping"}))
                        last_ping = time.time()
                    except Exception:
                        break

                try:
                    raw = ws.recv()
                    if not raw:
                        break
                    message = json.loads(raw)
                    channel = message.get("channel")
                    data = message.get("data")

                    if channel == "trades":
                        process_trade(data)
                    elif channel == "candle":
                        process_candle(data)
                except Exception:
                    break
        except Exception:
            pass

        time.sleep(5)

# ============================================================
# SIGNAL ENGINE (GÜÇLENDİRİLMİŞ MANTIK VE DİNAMİK SKORLAMA)
# ============================================================

def generate_signal(coin):
    i1 = calculate_indicators("1m", coin)
    i5 = calculate_indicators("5m", coin)
    i15 = calculate_indicators("15m", coin)

    if not i1 or not i5 or not i15:
        return None

    # Hacim Filtresi (En az bir periyotta minimum hacim şartı)
    if i1["volume_ratio"] < MIN_VOLUME_RATIO and i5["volume_ratio"] < MIN_VOLUME_RATIO:
        return None

    with lock:
        m = dict(market.get(coin, {}))

    if not m:
        return None

    oi = m.get("oi", 0)
    oi_delta = m.get("oi_delta", 0)
    oi_pct = m.get("oi_pct", 0)

    flow = calculate_trade_flow(coin, 300)
    buy, sell = flow["buy"], flow["sell"]
    whale_buy, whale_sell = flow["whale_buy"], flow["whale_sell"]

    long_score = 0
    short_score = 0
    max_possible_score = 0  # Dinamik olarak hesaplanacak

    reasons_long, reasons_short = [], []

    # 1. 5M Trend (Max +3)
    max_possible_score += 3
    if i5["ema9"] > i5["ema21"]:
        long_score += 3
        reasons_long.append("5M EMA bullish")
    elif i5["ema9"] < i5["ema21"]:
        short_score += 3
        reasons_short.append("5M EMA bearish")

    # 2. 5M Momentum (Max +2)
    max_possible_score += 2
    if i5["momentum"] > 0.25:
        long_score += 2
        reasons_long.append("5M momentum pozitif")
    elif i5["momentum"] < -0.25:
        short_score += 2
        reasons_short.append("5M momentum negatif")

    # 3. 1M Entry Trend (Max +2)
    max_possible_score += 2
    if i1["ema9"] > i1["ema21"]:
        long_score += 2
        reasons_long.append("1M EMA bullish")
    elif i1["ema9"] < i1["ema21"]:
        short_score += 2
        reasons_short.append("1M EMA bearish")

    # 4. 1M Momentum (Max +2)
    max_possible_score += 2
    if i1["momentum"] > 0.15:
        long_score += 2
        reasons_long.append("1M momentum pozitif")
    elif i1["momentum"] < -0.15:
        short_score += 2
        reasons_short.append("1M momentum negatif")

    # 5. 15M Macro Trend (Max +1)
    max_possible_score += 1
    if i15["ema9"] > i15["ema21"]:
        long_score += 1
    elif i15["ema9"] < i15["ema21"]:
        short_score += 1

    # 6. RSI Seviyesi (Max +1)
    max_possible_score += 1
    if i5["rsi"] is not None:
        if 52 <= i5["rsi"] <= 68:
            long_score += 1
        elif 32 <= i5["rsi"] <= 48:
            short_score += 1

    # 7. Hacim İvmesi (Max +2)
    max_possible_score += 2
    if i1["volume_ratio"] >= 1.5:
        if i1["momentum"] > 0:
            long_score += 1
        elif i1["momentum"] < 0:
            short_score += 1

    if i5["volume_ratio"] >= 1.5:
        if i5["momentum"] > 0:
            long_score += 1
        elif i5["momentum"] < 0:
            short_score += 1

    # 8. Open Interest + Price Action (Max +2)
    max_possible_score += 2
    price_5m = i5["momentum"]
    if oi_pct > 0.30 and price_5m > 0.30:
        long_score += 2
        reasons_long.append("OI↑ + Price↑ (Güçlü Alım)")
    elif oi_pct > 0.30 and price_5m < -0.30:
        short_score += 2
        reasons_short.append("OI↑ + Price↓ (Güçlü Satım)")

    # 9. Trade Flow (Max +2)
    total_flow = buy + sell
    if total_flow > 0:
        max_possible_score += 2
        buy_ratio = buy / total_flow
        if buy_ratio >= 0.55:
            long_score += 2
            reasons_long.append("Aggressive buy flow")
        elif buy_ratio <= 0.45:
            short_score += 2
            reasons_short.append("Aggressive sell flow")

    # 10. Whale Flow - Akıllı Para Akışı (Max +2)
    whale_total = whale_buy + whale_sell
    if whale_total >= WHALE_USD:
        max_possible_score += 2
        whale_ratio = whale_buy / whale_total
        if whale_ratio >= 0.60:
            long_score += 2
            reasons_long.append("Whale buy")
        elif whale_ratio <= 0.40:
            short_score += 2
            reasons_short.append("Whale sell")

    # Trend Teyidi (5M Onayı Şartı)
    if REQUIRE_5M_CONFIRMATION:
        if long_score > short_score and not (i5["ema9"] > i5["ema21"]):
            return None
        elif short_score > long_score and not (i5["ema9"] < i5["ema21"]):
            return None

    difference = abs(long_score - short_score)
    winning_score = max(long_score, short_score)

    # Minimum skor ve yön farkı eşiği
    if winning_score < MIN_SCORE or difference < 3:
        return None

    if long_score > short_score:
        direction = "LONG"
    elif short_score > long_score:
        direction = "SHORT"
    else:
        return None

    # DÜZELTME #1: Güç hesabı dinamik max_possible_score üzerinden yapılıyor
    strength = min(100, int((winning_score / max_possible_score) * 100))

    if strength < MIN_STRENGTH:
        return None

    return {
        "coin": coin,
        "direction": direction,
        "price": i1["price"],
        "long_score": long_score,
        "short_score": short_score,
        "max_possible_score": max_possible_score,
        "strength": strength,
        "i1": i1, "i5": i5, "i15": i15,
        "oi": oi, "oi_delta": oi_delta, "oi_pct": oi_pct,
        "buy": buy, "sell": sell,
        "whale_buy": whale_buy, "whale_sell": whale_sell,
        "reasons_long": reasons_long, "reasons_short": reasons_short
    }

# ============================================================
# TELEGRAM SİNYAL MESAJI
# ============================================================

def send_signal(signal):
    coin = signal["coin"]
    direction = signal["direction"]
    i1, i5, i15 = signal["i1"], signal["i5"], signal["i15"]

    title = "🚀 GÜÇLÜ LONG SİNYALİ" if direction == "LONG" else "🔻 GÜÇLÜ SHORT SİNYALİ"
    emoji = "🟢" if direction == "LONG" else "🔴"

    whale_total = signal["whale_buy"] + signal["whale_sell"]
    if whale_total > 0:
        whale_bias = "ALIM ağırlıklı" if signal["whale_buy"] >= signal["whale_sell"] else "SATIM ağırlıklı"
    else:
        whale_bias = "Belirgin whale akışı yok"

    message = f"""
{title}

📌 Coin: {coin}
🎯 YÖN: {direction}
💰 Fiyat: ${signal["price"]:.6f}

━━━━━━━━━━━━━━━━━━

🔥 5M ANA TREND
Fiyat Değişimi: {i5["momentum"]:+.2f}%
EMA9: {i5["ema9"]:.6f}
EMA21: {i5["ema21"]:.6f}
RSI: {i5["rsi"]:.1f}

⚡ 1M GİRİŞ
Fiyat Değişimi: {i1["momentum"]:+.2f}%
EMA9: {i1["ema9"]:.6f}
EMA21: {i1["ema21"]:.6f}
RSI: {i1["rsi"]:.1f}

📈 15M TREND
Değişim: {i15["momentum"]:+.2f}%

━━━━━━━━━━━━━━━━━━

📊 OPEN INTEREST (OI)
Değişim: ${signal["oi_delta"]:,.0f}
OI %: {signal["oi_pct"]:+.2f}%
Toplam OI: ${signal["oi"]:,.0f}

━━━━━━━━━━━━━━━━━━

🐋 WHALE FLOW ({whale_bias})
BUY: ${signal["whale_buy"]:,.0f}
SELL: ${signal["whale_sell"]:,.0f}

📊 TRADE FLOW
BUY: ${signal["buy"]:,.0f}
SELL: ${signal["sell"]:,.0f}

━━━━━━━━━━━━━━━━━━

📊 HACİM İVMESİ
1M Hacim Katı: x{i1["volume_ratio"]:.2f}
5M Hacim Katı: x{i5["volume_ratio"]:.2f}

━━━━━━━━━━━━━━━━━━

🟢 LONG SCORE: {signal["long_score"]} / {signal["max_possible_score"]}
🔴 SHORT SCORE: {signal["short_score"]} / {signal["max_possible_score"]}

{emoji} SIGNAL STRENGTH: %{signal["strength"]}

━━━━━━━━━━━━━━━━━━

⏱️ {datetime.now().strftime("%H:%M:%S")}
"""
    send_telegram(message)

# ============================================================
# SIGNAL MONITOR
# ============================================================

def signal_monitor():
    print("Sinyal motoru başlatıldı.", flush=True)
    while True:
        try:
            for coin in list(coins):
                try:
                    signal = generate_signal(coin)
                    if not signal:
                        continue

                    now = time.time()
                    previous = last_signal.get(coin)

                    if previous:
                        if (previous["direction"] == signal["direction"] and
                                now - previous["time"] < SIGNAL_COOLDOWN_SECONDS):
                            continue

                    last_signal[coin] = {"direction": signal["direction"], "time": now}

                    save_signal({
                        "timestamp": int(now),
                        "coin": coin,
                        "direction": signal["direction"],
                        "entry_price": signal["price"],
                        "score_long": signal["long_score"],
                        "score_short": signal["short_score"],
                        "max_possible_score": signal["max_possible_score"],
                        "strength": signal["strength"],
                        "price_1m": signal["i1"]["momentum"],
                        "price_5m": signal["i5"]["momentum"],
                        "price_15m": signal["i15"]["momentum"],
                        "ema9_1m": signal["i1"]["ema9"],
                        "ema21_1m": signal["i1"]["ema21"],
                        "ema9_5m": signal["i5"]["ema9"],
                        "ema21_5m": signal["i5"]["ema21"],
                        "rsi_1m": signal["i1"]["rsi"],
                        "rsi_5m": signal["i5"]["rsi"],
                        "oi_change": signal["oi_delta"],
                        "oi_change_pct": signal["oi_pct"],
                        "buy_flow": signal["buy"],
                        "sell_flow": signal["sell"],
                        "volume_ratio_1m": signal["i1"]["volume_ratio"],
                        "volume_ratio_5m": signal["i5"]["volume_ratio"],
                    })

                    send_signal(signal)

                except Exception as e:
                    print("Signal hatası", coin, e, flush=True)

        except Exception as e:
            print("Monitor hatası:", e, flush=True)

        time.sleep(10)

# ============================================================
# EVALUATOR & PERFORMANCE REPORT
# ============================================================

def evaluate_signals():
    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            rows = conn.execute("""
                SELECT id, timestamp, coin, direction, entry_price, evaluated_1m, evaluated_5m, evaluated_15m
                FROM signals
                WHERE evaluated_1m = 0 OR evaluated_5m = 0 OR evaluated_15m = 0
                ORDER BY id ASC LIMIT 200
            """).fetchall()

            for row in rows:
                sid, ts, coin, direction, entry, ev1, ev5, ev15 = row
                now = int(time.time())
                age = now - ts

                with lock:
                    current = market.get(coin, {}).get("mark", 0)

                if current <= 0:
                    continue

                if not ev1 and age >= 60:
                    result = ((current - entry) / entry) * 100
                    if direction == "SHORT":
                        result *= -1
                    conn.execute("UPDATE signals SET result_1m = ?, evaluated_1m = 1 WHERE id = ?", (result, sid))

                if not ev5 and age >= 300:
                    result = ((current - entry) / entry) * 100
                    if direction == "SHORT":
                        result *= -1
                    conn.execute("UPDATE signals SET result_5m = ?, evaluated_5m = 1 WHERE id = ?", (result, sid))

                if not ev15 and age >= 900:
                    result = ((current - entry) / entry) * 100
                    if direction == "SHORT":
                        result *= -1
                    conn.execute("UPDATE signals SET result_15m = ?, evaluated_15m = 1 WHERE id = ?", (result, sid))

            conn.commit()
            conn.close()
        except Exception as e:
            print("Evaluator hatası:", e, flush=True)

        time.sleep(30)

def performance_report():
    while True:
        time.sleep(3600)
        try:
            conn = sqlite3.connect(DB_FILE)
            for period in ["result_1m", "result_5m", "result_15m"]:
                rows = conn.execute(f"SELECT {period} FROM signals WHERE {period} IS NOT NULL").fetchall()
                values = [float(x[0]) for x in rows]
                if not values:
                    continue

                wins = [x for x in values if x > 0]
                accuracy = (len(wins) / len(values)) * 100
                avg = sum(values) / len(values)

                print(f"\n[PERFORMANS - {period}] Sinyal: {len(values)} | Başarılı: {len(wins)} | Doğruluk Oranı: %{accuracy:.2f} | Ort. Getiri: %{avg:+.4f}\n", flush=True)
            conn.close()
        except Exception as e:
            print("Performans raporu hatası:", e, flush=True)

# ============================================================
# CLEANUP
# ============================================================

def cleanup():
    while True:
        now = int(time.time() * 1000)
        with lock:
            for coin in list(trades):
                while trades[coin] and now - trades[coin][0]["time"] > 15 * 60 * 1000:
                    trades[coin].popleft()
        time.sleep(60)

# ============================================================
# MAIN THREAD MANAGEMENT
# ============================================================

def start_background_tasks():
    print("""
==========================================================
   HYPERLIQUID DIRECTION BOT (MATHEMATICALLY CORRECTED)
==========================================================
""", flush=True)
    init_db()
    load_top_coins()

    if not coins:
        print("Coin listesi alınamadı.", flush=True)
        return

    threading.Thread(target=update_oi, daemon=True).start()
    threading.Thread(target=websocket_worker, daemon=True).start()
    threading.Thread(target=signal_monitor, daemon=True).start()
    threading.Thread(target=evaluate_signals, daemon=True).start()
    threading.Thread(target=performance_report, daemon=True).start()
    threading.Thread(target=cleanup, daemon=True).start()

    print("Geçmiş mum verileri indiriliyor...", flush=True)
    threading.Thread(target=load_initial_candles, daemon=True).start()

threading.Thread(target=start_background_tasks, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
