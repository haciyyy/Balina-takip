import os
import time
import threading
import requests
from flask import Flask

# --- Render Port / Web Sunucusu (Uyku Engelleyici) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Balina Botu Aktif ve Çalışıyor!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- Bot Konfigürasyon ve Mantığı ---
TELEGRAM_TOKEN = "8991720102:AAHTZGU65iIRD6Pi5gd9dlh_0z8gYhsqJlM"
CHAT_ID = "8833182824"
MIN_POSITION_SIZE = 5000

notified_positions = set()

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        return response.json()
    except Exception as e:
        print(f"Telegram Gönderim Hatası: {e}")

def check_hyperliquid_positions():
    url = "https://app.coinmarketman.com/api/hyperliquid/large-positions"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            data = res.json()
            positions = data.get("positions", [])
            
            for pos in positions:
                size = pos.get("size_usd", 0)
                pos_id = f"{pos.get('wallet')}_{pos.get('symbol')}"
                
                if size >= MIN_POSITION_SIZE and pos_id not in notified_positions:
                    msg = (
                        f"🚨 *BÜYÜK POZİSYON TESPİT EDİLDİ!*\n\n"
                        f"📌 *Coin:* `{pos.get('symbol')}`\n"
                        f"📊 *Yön:* `{pos.get('side')}`\n"
                        f"💰 *Büyüklük:* `${size:,.2f}`\n"
                        f"🎯 *Giriş:* `${pos.get('entry_price')}`\n"
                        f"🔥 *Likidasyon:* `${pos.get('liquidation_price')}`\n"
                    )
                    send_telegram_alert(msg)
                    notified_positions.add(pos_id)
    except Exception as e:
        print(f"Veri çekme hatası: {e}")

def tracker_loop():
    send_telegram_alert("🚀 *Balina Botu Web Service Olarak Başlatıldı!* 7/24 Kesintisiz Tarama Yapılıyor...")
    while True:
        check_hyperliquid_positions()
        time.sleep(30)

# Flask'ı ve Botu Aynı Anda Başlat
if __name__ == "__main__":
    t = threading.Thread(target=tracker_loop)
    t.start()
    run_flask()
