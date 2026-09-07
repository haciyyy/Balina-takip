import os
import time
import threading
import requests
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Balina Takip Botu Aktif!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

TELEGRAM_TOKEN = "8991720102:AAHTZGU65iIRD6Pi5gd9dlh_0z8gYhsqJlM"
CHAT_ID = "8833182824"
MIN_POSITION_SIZE = 500000  # $500k Filtresi

notified_positions = set()

def send_telegram_alert(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Hatasi: {e}")

def check_positions():
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    
    try:
        # Piyasa genelindeki varlık verilerini çek
        res = requests.post(url, json={"type": "metaAndAssetCtxs"}, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            universe = data[0].get("universe", [])
            asset_ctxs = data[1]

            for idx, asset in enumerate(universe):
                symbol = asset.get("name")
                ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
                
                oracle_price = float(ctx.get("oraclePx", 0))
                open_interest = float(ctx.get("openInterest", 0))
                position_value = open_interest * oracle_price

                # Örnek hacim değişiminden yön tespiti (Pozitif artış LONG, düşüş SHORT)
                day_ntl_vlm = float(ctx.get("dayNtlVlm", 0))
                premium = float(ctx.get("premium", 0))
                
                # Yön Mantığı: Premium ve fonlama pozitifse LONG ağırlıklı, negatifse SHORT ağırlıklı
                side = "🟢 LONG" if premium >= 0 else "🔴 SHORT"
                
                pos_id = f"{symbol}_{int(position_value / 100000)}"

                if position_value >= MIN_POSITION_SIZE and pos_id not in notified_positions:
                    msg = (
                        f"🚨 *BÜYÜK POZİSYON HAREKETİ TESPİT EDİLDİ!*\n\n"
                        f"📌 *Coin:* `{symbol}`\n"
                        f"📊 *Baskın Yön:* `{side}`\n"
                        f"💰 *Toplam Açık Pozisyon:* `${position_value:,.2f}`\n"
                        f"🎯 *Anlık Fiyat:* `${oracle_price:,.4f}`\n"
                    )
                    send_telegram_alert(msg)
                    notified_positions.add(pos_id)

    except Exception as e:
        print(f"Tarama hatasi: {e}")

def tracker_loop():
    send_telegram_alert("🚀 *Yön (LONG/SHORT) Destekli Bot Başlatıldı!*\n\n$500k+ işlemler taranıyor...")
    while True:
        check_positions()
        time.sleep(30)

if __name__ == "__main__":
    t = threading.Thread(target=tracker_loop)
    t.start()
    run_flask()
