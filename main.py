import os
import time
import threading
import requests
from flask import Flask
from datetime import datetime

# ==========================================
# ⚙️ BOT AYARLARI
# ==========================================
TOP_COINS_LIMIT = 100          # En yüksek hacimli ilk 100 coin
MIN_POSITION_SIZE = 300000     # Altcoin'ler için min işlem filtresi ($300k)
POSITION_CHANGE_THRESHOLD = 500000  # Altcoin'ler için OI değişim eşiği ($500k)
SCAN_INTERVAL = 15             # Tarama sıklığı (15 saniye)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8991720102:AAHTZGU65iIRD6Pi5gd9dlh_0z8gYhsqJlM")
CHAT_ID = os.environ.get("CHAT_ID", "8833182824")

# ==========================================
# 🚀 UYGULAMA VE SUNUCU KURULUMU
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return f"Gelis mis Balina Botu Aktif! Top {TOP_COINS_LIMIT} Coins"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

previous_positions = {}
notified_trades = {}

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram API Hatasi: {response.text}")
    except Exception as e:
        print(f"Telegram Hatasi: {e}")

def get_market_data():
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    try:
        payload = {"type": "metaAndAssetCtxs"}
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        print(f"Piyasa verisi hatasi: {e}")
        return None

def get_recent_trades_for_coin(coin):
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    try:
        payload = {"type": "recentTrades", "coin": coin}
        response = requests.post(url, json=payload, headers=headers, timeout=5)
        if response.status_code == 200:
            return response.json()
        return []
    except Exception:
        return []

def analyze_large_trades(universe):
    current_time = datetime.now()
    target_coins = [asset.get("name") for asset in universe[:TOP_COINS_LIMIT]]
    
    for coin in target_coins:
        trades = get_recent_trades_for_coin(coin)
        time.sleep(0.03)  # Rate limit emniyeti
        
        for trade in trades:
            try:
                side = trade.get("side", "")
                size = float(trade.get("sz", 0))
                price = float(trade.get("px", 0))
                trade_time = trade.get("time", 0)
                
                trade_value = size * price
                
                # BTC, ETH, SOL için esnek işlem eşiği ($150k), diğerleri ($300k)
                min_threshold = 150000 if coin in ["BTC", "ETH", "SOL"] else MIN_POSITION_SIZE
                
                if trade_value < min_threshold:
                    continue
                
                trade_id = f"{coin}_{trade_time}_{int(trade_value)}"
                if trade_id in notified_trades:
                    continue
                
                direction = "🟢 LONG (ALIM)" if side == "B" else "🔴 SHORT (SATIM)"
                emoji = "🐋" if trade_value >= 2000000 else "🦈"
                
                msg = (
                    f"{emoji} *BÜYÜK İŞLEM TESPİT EDİLDİ!*\n\n"
                    f"📌 *Coin:* `{coin}`\n"
                    f"📊 *Yön:* {direction}\n"
                    f"💰 *Değer:* `${trade_value:,.2f}`\n"
                    f"📈 *Fiyat:* `${price:,.4f}`\n"
                    f"🔢 *Miktar:* `{size:,.4f}`\n"
                    f"🕐 *Zaman:* `{datetime.fromtimestamp(trade_time/1000).strftime('%H:%M:%S')}`\n"
                )
                
                send_telegram_alert(msg)
                notified_trades[trade_id] = current_time
                
            except Exception as e:
                print(f"Trade hatasi: {e}")

def monitor_position_changes(data):
    if not data:
        return
    
    try:
        universe = data[0].get("universe", [])
        asset_ctxs = data[1]
        current_time = datetime.now()
        
        for idx, asset in enumerate(universe):
            symbol = asset.get("name")
            if idx >= len(asset_ctxs):
                continue
            
            ctx = asset_ctxs[idx]
            oracle_price = float(ctx.get("oraclePx", 0))
            open_interest = float(ctx.get("openInterest", 0))
            current_position_value = open_interest * oracle_price
            
            if symbol in previous_positions:
                prev_val = previous_positions[symbol]['val']
                prev_price = previous_positions[symbol]['px']
                
                change = current_position_value - prev_val
                price_change = oracle_price - prev_price
                
                # BTC, ETH, SOL için esnek OI eşiği ($300k), diğerleri ($500k)
                oi_threshold = 300000 if symbol in ["BTC", "ETH", "SOL"] else POSITION_CHANGE_THRESHOLD
                
                if abs(change) >= oi_threshold:
                    # FİYAT + OI ÇAPRAZ PİYASA TEŞHİSİ
                    if change > 0 and price_change >= 0:
                        action = "🟢 BÜYÜK LONG GİRİŞİ (Yeni Alıcılar)"
                        emoji = "🚀"
                    elif change > 0 and price_change < 0:
                        action = "🔴 BÜYÜK SHORT GİRİŞİ (Baskı Var)"
                        emoji = "📉"
                    elif change < 0 and price_change >= 0:
                        action = "⚡ SHORT SQUEEZE (Short'lar Kapanıyor/Patlıyor!)"
                        emoji = "🔥"
                    else:
                        action = "💥 LONG LİKİDASYON / Kar Satışı"
                        emoji = "⚠️"

                    direction = "ARTIŞ 📈" if change > 0 else "AZALIŞ 📉"
                    
                    pos_key = f"{symbol}_{int(change/100000)}"
                    if pos_key not in notified_trades:
                        msg = (
                            f"{emoji} *AÇIK POZİSYON DEĞİŞİMİ!*\n\n"
                            f"📌 *Coin:* `{symbol}`\n"
                            f"📊 *OI Değişimi:* {direction}\n"
                            f"💡 *Piyasa Teşhisi:* `{action}`\n"
                            f"💰 *Değişim Miktarı:* `${abs(change):,.2f}`\n"
                            f"📈 *Toplam Açık Pozisyon:* `${current_position_value:,.2f}`\n"
                            f"🎯 *Güncel Fiyat:* `${oracle_price:,.4f}`\n"
                            f"🕐 *Zaman:* `{current_time.strftime('%H:%M:%S')}`\n"
                        )
                        send_telegram_alert(msg)
                        notified_trades[pos_key] = current_time
            
            # Fiyat ve OI değerini hafızaya alma
            previous_positions[symbol] = {
                'val': current_position_value,
                'px': oracle_price
            }
            
    except Exception as e:
        print(f"Pozisyon hatasi: {e}")

def tracker_loop():
    send_telegram_alert("🎯 *Gelişmiş Balina Botu Aktif!*\n\n"
                       f"Kapsam: Top {TOP_COINS_LIMIT} Coin\n"
                       "Gelişmiş Piyasa Teşhisi (Short Squeeze & Likidasyon) entegre edildi.\n"
                       "Canlı tarama başladı...")
    
    while True:
        try:
            market_data = get_market_data()
            if market_data:
                universe = market_data[0].get("universe", [])
                analyze_large_trades(universe)
                monitor_position_changes(market_data)
            
            if len(notified_trades) > 1000:
                notified_trades.clear()

        except Exception as e:
            print(f"Döngü hatasi: {e}")
        
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=tracker_loop)
    t.daemon = True
    t.start()
    run_flask()
