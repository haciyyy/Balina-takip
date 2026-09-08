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
# CONFIG
# ============================================================

API_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"

# Buraya TELEGRAM BOT TOKEN'ını koy.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "7806644281:AAHcT0l8PrH-V9M2H5X3C83YKxM0M5wj34Y")

# Chat ID'yi kodda bırakabilirsin.
CHAT_ID = os.environ.get("CHAT_ID", "8833182824")

TOP_COINS = 100

# OI REST güncelleme sıklığı
OI_UPDATE_SECONDS = 15

# Sinyal tekrar engelleme
SIGNAL_COOLDOWN_SECONDS = 300

# Minimum skor
MIN_SCORE = 9

# 5M ana trend filtresi
REQUIRE_5M_CONFIRMATION = True

# Büyük işlem eşiği
WHALE_USD = 100_000

# Kaç candle saklanacak
MAX_CANDLES = 300

# Trade geçmişi
MAX_TRADES = 1000

DB_FILE = "hyperliquid_signals.db"

# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "Hyperliquid Direction Bot Running"


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
            timestamp,
            coin,
            direction,
            entry_price,
            score_long,
            score_short,
            strength,
            price_1m,
            price_5m,
            price_15m,
            ema9_1m,
            ema21_1m,
            ema9_5m,
            ema21_5m,
            rsi_1m,
            rsi_5m,
            oi_change,
            oi_change_pct,
            buy_flow,
            sell_flow,
            volume_ratio_1m,
            volume_ratio_5m
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["timestamp"],
        data["coin"],
        data["direction"],
        data["entry_price"],
        data["score_long"],
        data["score_short"],
        data["strength"],
        data["price_1m"],
        data["price_5m"],
        data["price_15m"],
        data["ema9_1m"],
        data["ema21_1m"],
        data["ema9_5m"],
        data["ema21_5m"],
        data["rsi_1m"],
        data["rsi_5m"],
        data["oi_change"],
        data["oi_change_pct"],
        data["buy_flow"],
        data["sell_flow"],
        data["volume_ratio_1m"],
        data["volume_ratio_5m"],
    ))

    conn.commit()
    conn.close()


# ============================================================
# TELEGRAM
# ============================================================


def send_telegram(message):
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.startswith("BURAYA"):
        print("Telegram token ayarlanmamış.")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message,
            },
            timeout=10
        )
    except Exception as e:
        print("Telegram error:", e)


# ============================================================
# HYPERLIQUID REST
# ============================================================


def hl_info(payload):
    try:
        response = requests.post(
            API_URL,
            json=payload,
            timeout=15
        )

        response.raise_for_status()

        return response.json()

    except Exception as e:
        print("REST error:", e)
        return None


# ============================================================
# LOAD TOP COINS
# ============================================================


def load_top_coins():
    global coins

    data = hl_info({
        "type": "metaAndAssetCtxs"
    })

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

        rows.sort(
            key=lambda x: x[1],
            reverse=True
        )

        coins = [
            x[0]
            for x in rows[:TOP_COINS]
        ]

        print(f"{len(coins)} coin yüklendi.")

        print(coins[:20])

    except Exception as e:
        print("Coin loading error:", e)


# ============================================================
# INITIAL CANDLE HISTORY
# ============================================================


def load_initial_candles():
    now = int(time.time() * 1000)

    intervals = {
        "1m": 600,
        "5m": 500,
        "15m": 300,
    }

    for coin in coins:

        for interval, count in intervals.items():

            minutes = {
                "1m": 1,
                "5m": 5,
                "15m": 15
            }[interval]

            start = now - (
                count * minutes * 60 * 1000
            )

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
                print(
                    "Candle load error",
                    coin,
                    interval,
                    e
                )

        print("History:", coin)


# ============================================================
# INDICATORS
# ============================================================


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for price in values[period:]:
        result = (
            price - result
        ) * multiplier + result

    return result


def rsi(values, period=14):

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(
        gains[:period]
    ) / period

    avg_loss = sum(
        losses[:period]
    ) / period

    for i in range(period, len(gains)):

        avg_gain = (
            (avg_gain * (period - 1))
            + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


def percentage_change(values, periods=1):

    if len(values) <= periods:
        return 0

    old = values[-periods - 1]
    new = values[-1]

    if old == 0:
        return 0

    return (
        (new - old) / old
    ) * 100


def volume_ratio(candle_list, period=20):

    if len(candle_list) < period + 1:
        return 1

    volumes = [
        x["v"]
        for x in candle_list
    ]

    current = volumes[-1]

    avg = sum(
        volumes[-period - 1:-1]
    ) / period

    if avg <= 0:
        return 1

    return current / avg


# ============================================================
# CANDLE HELPERS
# ============================================================


def get_closes(interval, coin):

    return [
        x["c"]
        for x in candles[interval][coin]
    ]


def calculate_indicators(interval, coin):

    data = candles[interval][coin]

    closes = get_closes(
        interval,
        coin
    )

    if len(closes) < 30:
        return None

    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)

    rsi_value = rsi(
        closes,
        14
    )

    momentum = percentage_change(
        closes,
        1
    )

    if interval == "1m":
        momentum_5 = percentage_change(
            closes,
            5
        )
    elif interval == "5m":
        momentum_5 = percentage_change(
            closes,
            1
        )
    else:
        momentum_5 = percentage_change(
            closes,
            1
        )

    vol_ratio = volume_ratio(
        data,
        20
    )

    return {
        "price": closes[-1],
        "ema9": ema9,
        "ema21": ema21,
        "rsi": rsi_value,
        "momentum": momentum,
        "momentum_long": momentum_5,
        "volume_ratio": vol_ratio,
    }


# ============================================================
# TRADE FLOW
# ============================================================


def calculate_trade_flow(coin, seconds=300):

    now = int(time.time() * 1000)

    buy = 0
    sell = 0

    whale_buy = 0
    whale_sell = 0

    with lock:

        for trade in trades[coin]:

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
# OI
# ============================================================


def update_oi():

    while True:

        data = hl_info({
            "type": "metaAndAssetCtxs"
        })

        if data:

            try:

                meta = data[0]
                contexts = data[1]

                for i, item in enumerate(
                    meta["universe"]
                ):

                    if i >= len(contexts):
                        continue

                    coin = item["name"]
                    ctx = contexts[i]

                    oi = float(
                        ctx.get(
                            "openInterest",
                            0
                        ) or 0
                    )

                    oracle = float(
                        ctx.get(
                            "oraclePx",
                            0
                        ) or 0
                    )

                    mark = float(
                        ctx.get(
                            "markPx",
                            0
                        ) or 0
                    )

                    funding = float(
                        ctx.get(
                            "funding",
                            0
                        ) or 0
                    )

                    oi_usd = oi * oracle

                    with lock:

                        old = previous_oi.get(
                            coin,
                            oi_usd
                        )

                        delta = (
                            oi_usd - old
                        )

                        market[coin].update({
                            "oi": oi_usd,
                            "oi_delta": delta,
                            "oracle": oracle,
                            "mark": mark,
                            "funding": funding,
                            "last_oi_update": time.time()
                        })

                        previous_oi[coin] = oi_usd

            except Exception as e:
                print(
                    "OI parse error:",
                    e
                )

        time.sleep(
            OI_UPDATE_SECONDS
        )


# ============================================================
# WEBSOCKET
# ============================================================


def ws_subscribe(ws, coin):

    subscriptions = [
        {
            "type": "trades",
            "coin": coin
        },
        {
            "type": "candle",
            "coin": coin,
            "interval": "1m"
        },
        {
            "type": "candle",
            "coin": coin,
            "interval": "5m"
        },
        {
            "type": "candle",
            "coin": coin,
            "interval": "15m"
        }
    ]

    for sub in subscriptions:

        ws.send(
            json.dumps({
                "method": "subscribe",
                "subscription": sub
            })
        )


def process_trade(data):

    if not isinstance(data, list):
        return

    for trade in data:

        try:

            coin = trade["coin"]

            item = {
                "time": int(
                    trade["time"]
                ),
                "px": float(
                    trade["px"]
                ),
                "sz": float(
                    trade["sz"]
                ),
                "side": trade["side"]
            }

            with lock:
                trades[coin].append(item)

        except Exception:
            pass


def process_candle(data):

    if isinstance(data, list):
        candle_list = data
    elif isinstance(data, dict):
        candle_list = [data]
    else:
        return

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

                arr = candles[
                    interval
                ][coin]

                # Aynı candle'ı güncelle
                if arr and arr[-1]["t"] == item["t"]:
                    arr[-1] = item
                else:
                    arr.append(item)

        except Exception as e:
            print(
                "Candle parse error:",
                e
            )


def websocket_worker():

    while True:

        try:

            print(
                "WebSocket bağlanıyor..."
            )

            ws = websocket.create_connection(
                WS_URL,
                timeout=30
            )

            print(
                "WebSocket bağlandı."
            )

            for coin in coins:
                ws_subscribe(
                    ws,
                    coin
                )

            last_ping = time.time()

            while True:

                if time.time() - last_ping > 25:

                    try:
                        ws.send(
                            json.dumps({
                                "method": "ping"
                            })
                        )

                        last_ping = time.time()

                    except Exception:
                        break

                try:

                    raw = ws.recv()

                    if not raw:
                        break

                    message = json.loads(raw)

                    channel = message.get(
                        "channel"
                    )

                    data = message.get(
                        "data"
                    )

                    if channel == "trades":

                        process_trade(data)

                    elif channel == "candle":

                        process_candle(data)

                except Exception as e:

                    print(
                        "WS receive error:",
                        e
                    )

                    break

        except Exception as e:

            print(
                "WebSocket error:",
                e
            )

        time.sleep(5)


# ============================================================
# SIGNAL ENGINE
# ============================================================


def generate_signal(coin):

    i1 = calculate_indicators(
        "1m",
        coin
    )

    i5 = calculate_indicators(
        "5m",
        coin
    )

    i15 = calculate_indicators(
        "15m",
        coin
    )

    if not i1 or not i5 or not i15:
        return None

    with lock:

        m = dict(
            market.get(
                coin,
                {}
            )
        )

    if not m:
        return None

    oi_delta = m.get(
        "oi_delta",
        0
    )

    oi = m.get(
        "oi",
        0
    )

    if oi <= 0:
        oi_pct = 0
    else:
        oi_pct = (
            oi_delta / oi
        ) * 100

    flow = calculate_trade_flow(
        coin,
        300
    )

    buy = flow["buy"]
    sell = flow["sell"]

    whale_buy = flow["whale_buy"]
    whale_sell = flow["whale_sell"]

    long_score = 0
    short_score = 0

    reasons_long = []
    reasons_short = []

    # ========================================================
    # 5M — EN ÖNEMLİ
    # ========================================================

    if i5["ema9"] > i5["ema21"]:

        long_score += 3
        reasons_long.append(
            "5M EMA bullish"
        )

    elif i5["ema9"] < i5["ema21"]:

        short_score += 3
        reasons_short.append(
            "5M EMA bearish"
        )

    if i5["momentum"] > 0.25:

        long_score += 2
        reasons_long.append(
            "5M momentum pozitif"
        )

    elif i5["momentum"] < -0.25:

        short_score += 2
        reasons_short.append(
            "5M momentum negatif"
        )

    # ========================================================
    # 1M — ENTRY TIMING
    # ========================================================

    if i1["ema9"] > i1["ema21"]:

        long_score += 2
        reasons_long.append(
            "1M EMA bullish"
        )

    elif i1["ema9"] < i1["ema21"]:

        short_score += 2
        reasons_short.append(
            "1M EMA bearish"
        )

    if i1["momentum"] > 0.15:

        long_score += 2
        reasons_long.append(
            "1M momentum pozitif"
        )

    elif i1["momentum"] < -0.15:

        short_score += 2
        reasons_short.append(
            "1M momentum negatif"
        )

    # ========================================================
    # 15M TREND FILTER
    # ========================================================

    if i15["ema9"] > i15["ema21"]:

        long_score += 1

    elif i15["ema9"] < i15["ema21"]:

        short_score += 1

    # ========================================================
    # RSI
    # ========================================================

    if i5["rsi"] is not None:

        if 52 <= i5["rsi"] <= 68:

            long_score += 1

        elif 32 <= i5["rsi"] <= 48:

            short_score += 1

    # ========================================================
    # VOLUME
    # ========================================================

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

    # ========================================================
    # OI + PRICE
    # ========================================================

    price_5m = i5["momentum"]

    # Fiyat ↑ + OI ↑ = yeni long pozisyon olasılığı
    if oi_pct > 0.30 and price_5m > 0.30:

        long_score += 2
        reasons_long.append(
            "OI↑ + Price↑"
        )

    # Fiyat ↓ + OI ↑ = yeni short pozisyon olasılığı
    elif oi_pct > 0.30 and price_5m < -0.30:

        short_score += 2
        reasons_short.append(
            "OI↑ + Price↓"
        )

    # Fiyat ↑ + OI ↓ = short covering
    elif oi_pct < -0.30 and price_5m > 0.30:

        long_score += 1
        reasons_long.append(
            "Short covering"
        )

    # Fiyat ↓ + OI ↓ = long liquidation
    elif oi_pct < -0.30 and price_5m < -0.30:

        short_score += 1
        reasons_short.append(
            "Long liquidation"
        )

    # ========================================================
    # TRADE FLOW
    # ========================================================

    total_flow = buy + sell

    if total_flow > 0:

        buy_ratio = buy / total_flow

        if buy_ratio >= 0.60:

            long_score += 2

            reasons_long.append(
                "Aggressive buy flow"
            )

        elif buy_ratio <= 0.40:

            short_score += 2

            reasons_short.append(
                "Aggressive sell flow"
            )

    # ========================================================
    # WHALE FLOW
    # ========================================================

    whale_total = (
        whale_buy +
        whale_sell
    )

    if whale_total > WHALE_USD:

        whale_ratio = (
            whale_buy /
            whale_total
        )

        if whale_ratio >= 0.60:

            long_score += 2

            reasons_long.append(
                "Whale buy"
            )

        elif whale_ratio <= 0.40:

            short_score += 2

            reasons_short.append(
                "Whale sell"
            )

    # ========================================================
    # 5M CONFIRMATION
    # ========================================================

    five_min_bullish = (
        i5["ema9"] > i5["ema21"]
        and i5["momentum"] > 0
    )

    five_min_bearish = (
        i5["ema9"] < i5["ema21"]
        and i5["momentum"] < 0
    )

    if REQUIRE_5M_CONFIRMATION:

        if long_score > short_score:
            if not five_min_bullish:
                return None

        elif short_score > long_score:
            if not five_min_bearish:
                return None

    # ========================================================
    # DECISION
    # ========================================================

    difference = abs(
        long_score -
        short_score
    )

    if (
        long_score >= MIN_SCORE
        and long_score > short_score
        and difference >= 3
    ):

        direction = "LONG"

    elif (
        short_score >= MIN_SCORE
        and short_score > long_score
        and difference >= 3
    ):

        direction = "SHORT"

    else:

        return None

    # ========================================================
    # SIGNAL STRENGTH
    # ========================================================

    max_possible = 18

    winning_score = max(
        long_score,
        short_score
    )

    strength = min(
        100,
        int(
            (
                winning_score /
                max_possible
            ) * 100
        )
    )

    return {
        "coin": coin,
        "direction": direction,
        "price": i1["price"],

        "long_score": long_score,
        "short_score": short_score,
        "strength": strength,

        "i1": i1,
        "i5": i5,
        "i15": i15,

        "oi": oi,
        "oi_delta": oi_delta,
        "oi_pct": oi_pct,

        "buy": buy,
        "sell": sell,

        "whale_buy": whale_buy,
        "whale_sell": whale_sell,

        "reasons_long": reasons_long,
        "reasons_short": reasons_short
    }


# ============================================================
# TELEGRAM SIGNAL
# ============================================================


def send_signal(signal):

    coin = signal["coin"]
    direction = signal["direction"]

    i1 = signal["i1"]
    i5 = signal["i5"]
    i15 = signal["i15"]

    if direction == "LONG":

        title = "🚀 GÜÇLÜ LONG SİNYALİ"

        emoji = "🟢"

    else:

        title = "🔻 GÜÇLÜ SHORT SİNYALİ"

        emoji = "🔴"

    buy = signal["buy"]
    sell = signal["sell"]

    message = f"""
{title}

📌 Coin: {coin}

🎯 YÖN: {direction}

💰 Fiyat: ${signal["price"]:.6f}

━━━━━━━━━━━━━━━━━━

🔥 5M ANA TREND
Fiyat: {i5["momentum"]:+.2f}%
EMA9: {i5["ema9"]:.6f}
EMA21: {i5["ema21"]:.6f}
RSI: {i5["rsi"]:.1f}

⚡ 1M GİRİŞ
Fiyat değişimi: {i1["momentum"]:+.2f}%
EMA9: {i1["ema9"]:.6f}
EMA21: {i1["ema21"]:.6f}
RSI: {i1["rsi"]:.1f}

📈 15M TREND
Değişim: {i15["momentum"]:+.2f}%

━━━━━━━━━━━━━━━━━━

📊 OI

Değişim: ${signal["oi_delta"]:,.0f}
OI %: {signal["oi_pct"]:+.2f}%
Toplam OI: ${signal["oi"]:,.0f}

━━━━━━━━━━━━━━━━━━

🐋 WHALE FLOW

BUY: ${signal["whale_buy"]:,.0f}
SELL: ${signal["whale_sell"]:,.0f}

📊 TRADE FLOW

BUY: ${buy:,.0f}
SELL: ${sell:,.0f}

━━━━━━━━━━━━━━━━━━

📊 HACİM

1M: x{i1["volume_ratio"]:.2f}
5M: x{i5["volume_ratio"]:.2f}

━━━━━━━━━━━━━━━━━━

🟢 LONG SCORE: {signal["long_score"]}
🔴 SHORT SCORE: {signal["short_score"]}

{emoji} SIGNAL STRENGTH:
{signal["strength"]}/100

━━━━━━━━━━━━━━━━━━

⏱️ {datetime.now().strftime("%H:%M:%S")}
"""

    send_telegram(
        message
    )


# ============================================================
# SIGNAL MONITOR
# ============================================================


def signal_monitor():

    print(
        "Signal engine başladı."
    )

    while True:

        try:

            for coin in list(coins):

                try:

                    signal = generate_signal(
                        coin
                    )

                    if not signal:
                        continue

                    now = time.time()

                    previous = last_signal.get(
                        coin
                    )

                    if previous:

                        # Aynı yön tekrarını cooldown'a al
                        if (
                            previous["direction"]
                            == signal["direction"]
                            and now - previous["time"]
                            < SIGNAL_COOLDOWN_SECONDS
                        ):
                            continue

                    last_signal[coin] = {
                        "direction":
                            signal["direction"],
                        "time":
                            now
                    }

                    # DB
                    save_signal({
                        "timestamp": int(now),
                        "coin": coin,
                        "direction":
                            signal["direction"],
                        "entry_price":
                            signal["price"],

                        "score_long":
                            signal["long_score"],
                        "score_short":
                            signal["short_score"],
                        "strength":
                            signal["strength"],

                        "price_1m":
                            signal["i1"]["momentum"],
                        "price_5m":
                            signal["i5"]["momentum"],
                        "price_15m":
                            signal["i15"]["momentum"],

                        "ema9_1m":
                            signal["i1"]["ema9"],
                        "ema21_1m":
                            signal["i1"]["ema21"],

                        "ema9_5m":
                            signal["i5"]["ema9"],
                        "ema21_5m":
                            signal["i5"]["ema21"],

                        "rsi_1m":
                            signal["i1"]["rsi"],
                        "rsi_5m":
                            signal["i5"]["rsi"],

                        "oi_change":
                            signal["oi_delta"],
                        "oi_change_pct":
                            signal["oi_pct"],

                        "buy_flow":
                            signal["buy"],
                        "sell_flow":
                            signal["sell"],

                        "volume_ratio_1m":
                            signal["i1"]["volume_ratio"],
                        "volume_ratio_5m":
                            signal["i5"]["volume_ratio"],
                    })

                    send_signal(
                        signal
                    )

                except Exception as e:

                    print(
                        "Signal error",
                        coin,
                        e
                    )

        except Exception as e:

            print(
                "Monitor error:",
                e
            )

        time.sleep(10)


# ============================================================
# PERFORMANCE EVALUATOR
# ============================================================


def evaluate_signals():

    while True:

        try:

            conn = sqlite3.connect(
                DB_FILE
            )

            rows = conn.execute("""
                SELECT
                    id,
                    timestamp,
                    coin,
                    direction,
                    entry_price,
                    evaluated_1m,
                    evaluated_5m,
                    evaluated_15m
                FROM signals
                WHERE
                    evaluated_1m = 0
                    OR evaluated_5m = 0
                    OR evaluated_15m = 0
                ORDER BY id ASC
                LIMIT 200
            """).fetchall()

            for row in rows:

                (
                    sid,
                    ts,
                    coin,
                    direction,
                    entry,
                    ev1,
                    ev5,
                    ev15
                ) = row

                now = int(
                    time.time()
                )

                age = now - ts

                # Mevcut fiyat
                with lock:

                    current = market.get(
                        coin,
                        {}
                    ).get(
                        "mark",
                        0
                    )

                if current <= 0:
                    continue

                # ------------------------------------------------
                # 1 MIN
                # ------------------------------------------------

                if (
                    not ev1
                    and age >= 60
                ):

                    result = (
                        (
                            current -
                            entry
                        ) /
                        entry
                    ) * 100

                    if direction == "SHORT":
                        result *= -1

                    conn.execute("""
                        UPDATE signals
                        SET result_1m = ?,
                            evaluated_1m = 1
                        WHERE id = ?
                    """, (
                        result,
                        sid
                    ))

                # ------------------------------------------------
                # 5 MIN
                # ------------------------------------------------

                if (
                    not ev5
                    and age >= 300
                ):

                    result = (
                        (
                            current -
                            entry
                        ) /
                        entry
                    ) * 100

                    if direction == "SHORT":
                        result *= -1

                    conn.execute("""
                        UPDATE signals
                        SET result_5m = ?,
                            evaluated_5m = 1
                        WHERE id = ?
                    """, (
                        result,
                        sid
                    ))

                # ------------------------------------------------
                # 15 MIN
                # ------------------------------------------------

                if (
                    not ev15
                    and age >= 900
                ):

                    result = (
                        (
                            current -
                            entry
                        ) /
                        entry
                    ) * 100

                    if direction == "SHORT":
                        result *= -1

                    conn.execute("""
                        UPDATE signals
                        SET result_15m = ?,
                            evaluated_15m = 1
                        WHERE id = ?
                    """, (
                        result,
                        sid
                    ))

            conn.commit()
            conn.close()

        except Exception as e:

            print(
                "Evaluator error:",
                e
            )

        time.sleep(30)


# ============================================================
# PERFORMANCE REPORT
# ============================================================


def performance_report():

    while True:

        time.sleep(3600)

        try:

            conn = sqlite3.connect(
                DB_FILE
            )

            for period in [
                "result_1m",
                "result_5m",
                "result_15m"
            ]:

                rows = conn.execute(
                    f"""
                    SELECT {period}
                    FROM signals
                    WHERE {period} IS NOT NULL
                    """
                ).fetchall()

                values = [
                    float(x[0])
                    for x in rows
                ]

                if not values:
                    continue

                wins = [
                    x for x in values
                    if x > 0
                ]

                losses = [
                    x for x in values
                    if x <= 0
                ]

                accuracy = (
                    len(wins) /
                    len(values)
                ) * 100

                avg = (
                    sum(values) /
                    len(values)
                )

                print(
                    f"""
==============================
PERFORMANCE {period}
Signals : {len(values)}
Wins    : {len(wins)}
Losses  : {len(losses)}
Accuracy: {accuracy:.2f}%
Avg move: {avg:+.4f}%
==============================
"""
                )

            conn.close()

        except Exception as e:

            print(
                "Performance error:",
                e
            )


# ============================================================
# CLEANUP
# ============================================================


def cleanup():

    while True:

        now = int(
            time.time() * 1000
        )

        with lock:

            for coin in list(trades):

                while (
                    trades[coin]
                    and
                    now - trades[coin][0]["time"]
                    > 15 * 60 * 1000
                ):

                    trades[coin].popleft()

        time.sleep(60)


# ============================================================
# MAIN
# ============================================================


def main():

    print("""
==========================================================
      HYPERLIQUID LONG / SHORT DIRECTION BOT
==========================================================

MAIN TIMEFRAMES:

    1M  -> ENTRY
    5M  -> MAIN TREND
    15M -> TREND FILTER

FEATURES:

    ✓ 1M EMA
    ✓ 5M EMA
    ✓ 15M EMA
    ✓ 1M RSI
    ✓ 5M RSI
    ✓ 1M momentum
    ✓ 5M momentum
    ✓ 15M momentum
    ✓ 1M volume
    ✓ 5M volume
    ✓ Open Interest
    ✓ OI + Price relationship
    ✓ Aggressive Buy/Sell flow
    ✓ Whale flow
    ✓ Signal cooldown
    ✓ SQLite performance tracking

==========================================================
""")

    init_db()

    load_top_coins()

    if not coins:

        print(
            "Coin listesi alınamadı."
        )

        return

    # Tarihsel candle verilerini doldur
    load_initial_candles()

    # OI
    threading.Thread(
        target=update_oi,
        daemon=True
    ).start()

    # WebSocket
    threading.Thread(
        target=websocket_worker,
        daemon=True
    ).start()

    # Signal engine
    threading.Thread(
        target=signal_monitor,
        daemon=True
    ).start()

    # Signal evaluator
    threading.Thread(
        target=evaluate_signals,
        daemon=True
    ).start()

    # Performance
    threading.Thread(
        target=performance_report,
        daemon=True
    ).start()

    # Cleanup
    threading.Thread(
        target=cleanup,
        daemon=True
    ).start()

    # Flask
    app.run(
        host="0.0.0.0",
        port=8080,
        threaded=True
    )


if __name__ == "__main__":
    main()
