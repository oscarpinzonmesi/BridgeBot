import os
from flask import Flask, request
import requests

app = Flask(__name__)

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("TELEGRAM_TOKEN")   # Token del nuevo bot (BridgeBot)
ORBIS_URL = os.getenv("ORBIS_URL")           # URL de tu servicio Orbis en Render

BRIDGE_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendMessage"


# === RUTA WEBHOOK ===
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json()

    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")

        # Si detecta palabras clave de agenda → enviar a Orbis
        if "agenda" in text.lower() or "cita" in text.lower():
            requests.post(ORBIS_URL, json={
                "chat_id": chat_id,
                "text": f"📌 Orden enviada a Orbis: {text}"
            })
        else:
            # Respuesta normal del bot puente
            requests.post(BRIDGE_API, json={
                "chat_id": chat_id,
                "text": f"🤖 MesaGPT: te escuché → {text}"
            })

    return {"ok": True}


# === RUTA HOME ===
@app.route("/ping", methods=["GET"])
def home():
    return "✅ Bridge Bot activo en Render"
