import os
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("TELEGRAM_TOKEN")   # Token del bot BridgeBot
ORBIS_API = os.getenv("ORBIS_API")           # URL de Orbis: https://orbis-xxx.onrender.com/procesar
BRIDGEBOT_URL = os.getenv("BRIDGEBOT_URL", "https://bridgebot-shtq.onrender.com")

BRIDGE_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendMessage"


# === ENDPOINT DE MESA ===
@app.route("/mesa", methods=["POST"])
def mesa():
    """Endpoint donde MesaGPT envía la orden procesada"""
    data = request.get_json(force=True)
    chat_id = data.get("chat_id")
    orden = data.get("orden", "")

    if not chat_id or not orden:
        return jsonify({"error": "Falta chat_id u orden"}), 400

    try:
        if orden.startswith("/") or "agenda" in orden.lower() or "cita" in orden.lower():
            print("🔗 MesaGPT dio orden para Orbis:", orden, flush=True)
            r = requests.post(ORBIS_API, json={"texto": orden})
            respuesta_orbis = r.json().get("respuesta", "❌ Error en Orbis")
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": respuesta_orbis})
        else:
            print("🤖 MesaGPT respondió directo:", orden, flush=True)
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": f"🤖 MesaGPT: {orden}"})
    except Exception as e:
        print("❌ Error en /mesa:", str(e), flush=True)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


# === TELEGRAM WEBHOOK ===
@app.route("/", methods=["POST"])
def webhook():
    """Telegram envía los mensajes aquí → BridgeBot los reenvía a MesaGPT"""
    data = request.get_json(force=True)

    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    print(f"📩 Telegram → BridgeBot: {text}", flush=True)

    # Simulación de paso por MesaGPT (ahora se llama al endpoint /mesa con la URL pública)
    orden_simulada = text
    requests.post(f"{BRIDGEBOT_URL}/mesa", json={"chat_id": chat_id, "orden": orden_simulada})

    return {"ok": True}


# === RUTA HOME ===
@app.route("/ping", methods=["GET"])
def home():
    return "✅ Bridge Bot activo en Render"
