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


# === RUTA PRINCIPAL ===
@app.route("/", methods=["GET"])
def home():
    return "✅ Bridge Bot activo en Heroku"


# === RUTA DEL WEBHOOK ===
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")

        if "agenda" in text.lower() or "cita" in text.lower():
            # Si es algo para la agenda → mandar a Orbis
            requests.post(ORBIS_URL, json={
                "chat_id": chat_id,
                "text": f"📌 Orden enviada a Orbis: {text}"
            })
        else:
            # Respuesta normal del bot puente
            requests.post(BRIDGE_URL, json={
                "chat_id": chat_id,
                "text": f"🤖 MesaGPT: te escuché → {text}"
            })

    return {"ok": True}


if __name__ == "__main__":
    # Usar el puerto dinámico que asigna Heroku
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
