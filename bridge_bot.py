# archivo: bridge_bot.py
import os
from flask import Flask, request
import requests

app = Flask(__name__)

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN")   # Token del nuevo bot puente (Telegram)
ORBIS_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Token del bot Orbis (ya configurado)

BRIDGE_URL = f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendMessage"
ORBIS_URL = f"https://api.telegram.org/bot{ORBIS_TOKEN}/sendMessage"

# === RUTA WEBHOOK ===
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json()

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")

        # Yo (GPT) respondo desde aquí
        if "agenda" in text.lower() or "cita" in text.lower():
            # Si es algo para la agenda → mandar a Orbis
            requests.post(ORBIS_URL, json={
                "chat_id": chat_id,
                "text": f"📌 Orden enviada a Orbis: {text}"
            })
        else:
            # Respuesta normal mía
            requests.post(BRIDGE_URL, json={
                "chat_id": chat_id,
                "text": f"🤖 MesaGPT: te escuché → {text}"
            })

    return {"ok": True}

# === RUTA HOME (GET) ===
@app.route("/", methods=["GET"])
def home():
    return "✅ Bridge Bot activo"

# === MAIN PARA HEROKU ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # Heroku asigna un puerto dinámico
    app.run(host="0.0.0.0", port=port)
