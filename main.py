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
    return "API Test Sunucusu"

def run_test():
    url = "https://app.coinmarketman.com/api/hyperliquid/large-positions"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        status = response.status_code
        
        if status == 200:
            data = response.json()
            positions = data.get("positions", [])
            count = len(positions)
            
            if count > 0:
                sample = positions[0]
                msg = (
                    f"✅ *API Bağlantısı Başarılı! (HTTP 200)*\n\n"
                    f"📊 *Toplam Çekilen Pozisyon Sayısı:* `{count}`\n"
                    f"🔍 *Örnek Pozisyon:* `{sample.get('symbol')}` - `${float(sample.get('size_usd', 0)):,.2f}`"
                )
            else:
                msg = f"⚠️ *API Bağlandı (HTTP 200) ama gelen pozisyon listesi boş!*"
        else:
            msg = f"❌ *API Bağlantı Hatası!* HTTP Kod: `{status}`"
            
    except Exception as e:
        msg = f"💥 *Bağlantı İstek Hatası:* `{str(e)}`"
        
    send_telegram(msg)

if __name__ == "__main__":
    run_test()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
