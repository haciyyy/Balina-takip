import os
import time
import threading
import requests
from flask import Flask
from datetime import datetime

# ==========================================
# ⚙️ BOT AYARLARI (İstediğin Gibi Değiştir)
# ==========================================
TOP_COINS_LIMIT = 100          # Taranacak en yüksek hacimli coin sayısı (Örn: 50, 100, 150)
MIN_POSITION_SIZE = 500000     # Minimum işlem büyüklüğü ($500k)
POSITION_CHANGE_THRESHOLD = 1000000  # $1M açık pozisyon değişim eşiği
SCAN_INTERVAL = 20             # Tarama sıklığı (saniye cinsinden)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8991720102:AAHTZGU65iIRD6Pi5gd9dlh_0z8gYhsqJlM")
CHAT_ID = os.environ.get("CHAT_ID", "8833182824")

# ==========================================
# 🚀 UYGULAMA VE SUNUCU KURULUMU
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return f"Balina Botu Aktif! Target: Top {TOP_COINS_LIMIT} Coins"

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
            print(f"Telegram API Hatası: {response.text}")
    except Exception as e:
        print(f"Telegram Hatası: {e}")

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
        print(f"Piyasa verisi çekme hatası: {e}")
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
    
    # Dinamik olarak belirlediğin TOP_COINS_LIMIT kadar coin çekilir
    target_coins = [asset.get("name") for asset in universe[:TOP_COINS_LIMIT]]
    
    for coin in target_coins:
        trades = get_recent_trades_for_coin(coin)
        time.sleep(0.04)  # Rate limit emniyet beklemesi
        
        for trade in trades:
            try:
                side = trade.get("side", "")
                size = float(trade.get("sz", 0))
                price = float(trade.get("px", 0))
                trade_time = trade.get("time", 0)
                
                trade_value = size * price
                
                if trade_value < MIN_POSITION_SIZE:
                    continue
                
                trade_id = f"{coin}_{trade_time}_{int(trade_value)}"
                if trade_id in notified_trades:
                    continue
                
                direction = "🟢 LONG (ALIM)" if side == "B" else "🔴 SHORT (SATIM)"
                
                if trade_value >= 5000000:
                    emoji = "🐋"
                elif trade_value >= 1000000:
                    emoji = "🦈"
                else:
                    emoji = "🐟"
                
                msg = (
                    f"{emoji} *BÜYÜK İŞLEM TESPİT EDİLDİ!*\n\n"
                    f"📌 *Coin:* `{coin}`\n"
                    f"📊 *İşlem Yönü:* {direction}\n"
                    f"💰 *İşlem Değeri:* `${trade_value:,.2f}`\n"
                    f"📈 *İşlem Fiyatı:* `${price:,.4f}`\n"
                    f"🔢 *Miktar:* `{size:,.4f}`\n"
                    f"🕐 *Zaman:* `{datetime.fromtimestamp(trade_time/1000).strftime('%H:%M:%S')}`\n"
                )
                
                send_telegram_alert(msg)
                notified_trades[trade_id] = current_time
                
            except Exception as e:
                print(f"Trade analiz hatası: {e}")

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
                prev_value = previous_positions[symbol]
                change = current_position_value - prev_value
                
                if abs(change) >= POSITION_CHANGE_THRESHOLD:
                    direction = "ARTIŞ 📈" if change > 0 else "AZALIŞ 📉"
                    action = "NET LONG HAKİMİYETİ" if change > 0 else "NET SHORT HAKİMİYETİ"
                    
                    pos_key = f"{symbol}_{int(change/100000)}"
                    if pos_key not in notified_trades:
                        msg = (
                            f"🚨 *AÇIK POZİSYON DEĞİŞİMİ!*\n\n"
                            f"📌 *Coin:* `{symbol}`\n"
                            f"📊 *Değişim:* {direction}\n"
                            f"💡 *Piyasa Eğilimi:* `{action}`\n"
                            f"💰 *Değişim Miktarı:* `${abs(change):,.2f}`\n"
                            f"📈 *Toplam Açık Pozisyon:* `${current_position_value:,.2f}`\n"
                            f"🎯 *Güncel Fiyat:* `${oracle_price:,.4f}`\n"
                            f"🕐 *Zaman:* `{current_time.strftime('%H:%M:%S')}`\n"
                        )
                        send_telegram_alert(msg)
                        notified_trades[pos_key] = current_time
            
            previous_positions[symbol] = current_position_value
            
    except Exception as e:
        print(f"Pozisyon izleme hatası: {e}")

def tracker_loop():
    send_telegram_alert("⚙️ *Balina Takip Botu Ayarları Güncellendi!*\n\n"
                       f"🎯 *Tarama Kapsamı:* İlk {TOP_COINS_LIMIT} Coin\n"
                       f"💵 *Min İşlem Eşiği:* ${MIN_POSITION_SIZE:,}\n"
                       f"⏱️ *Tarama Sıklığı:* {SCAN_INTERVAL} saniye\n\n"
                       "Bot kesintisiz takibe devam ediyor...")
    
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
            print(f"Genel tarama hatası: {e}")
        
        # Dinamik bekleme süresi
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    t = threading.Thread(target=tracker_loop)
    t.daemon = True
    t.start()
    run_flask()
