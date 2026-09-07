import os
import requests
from flask import Flask

app = Flask(__name__)

TELEGRAM_TOKEN = "8991720102:AAHTZGU65iIRD6Pi5gd9dlh_0z8gYhsqJlM"
CHAT_ID = "8833182824"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

@app.route('/')
def home():
    return "Hyperliquid Resmi API Testi"

def run_test():
    # Hyperliquid Resmi Node API Adresi
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    
    # Tüm piyasa verilerini ve son işlemleri çekme isteği
    payload = {"type": "metaAndAssetCtxs"}
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        status = response.status_code
        
        if status == 200:
            data = response.json()
            universe = data[0].get("universe", [])
            asset_ctxs = data[1]
            
            # Örnek ilk coin verisini çekme (BTC)
            btc_data = asset_ctxs[0] if asset_ctxs else {}
            oracle_px = btc_data.get("oraclePx", "N/A")
            
            msg = (
                f"✅ *Hyperliquid Resmi API Bağlantısı Başarılı! (HTTP 200)*\n\n"
                f"📊 *Listelenen Varlık Sayısı:* `{len(universe)}`\n"
                f"🪙 *BTC Oracle Fiyatı:* `${float(oracle_px):,.2f}`\n\n"
                f"🚀 *Sonuç:* Engelleme yok, doğrudan borsa verisi çekilebiliyor!"
            )
        else:
            msg = f"❌ *Hyperliquid API Hatası!* HTTP Kod: `{status}`"
            
    except Exception as e:
        msg = f"💥 *Bağlantı Hatası:* `{str(e)}`"
        
    send_telegram(msg)

if __name__ == "__main__":
    run_test()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
