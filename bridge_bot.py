import os
from flask import Flask, request
import requests

app = Flask(__name__)

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("TELEGRAM_TOKEN")   # Token del bot BridgeBot
ORBIS_API = os.getenv("ORBIS_API")           # URL de Orbis: https://orbis-xxx.onrender.com/procesar

BRIDGE_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendMessage"


@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    print("📩 Llego update:", data, flush=True)

    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")
    print(f"➡️ Mensaje recibido: {text}", flush=True)

    try:
        if text.startswith("/") or "agenda" in text.lower() or "cita" in text.lower():
            print("🔗 Procesando en Orbis...", flush=True)
            r = requests.post(ORBIS_API, json={"texto": text})
            respuesta_orbis = r.json().get("respuesta", "❌ Error en Orbis")
            requests.post(BRIDGE_API, json={
                "chat_id": chat_id,
                "text": respuesta_orbis
            })
        else:
            print("🤖 Respondiendo desde BridgeBot", flush=True)
            requests.post(BRIDGE_API, json={
                "chat_id": chat_id,
                "text": f"🤖 MesaGPT: te escuché → {text}"
            })
    except Exception as e:
        print("❌ Error procesando mensaje:", str(e), flush=True)

    return {"ok": True}


@app.route("/ping", methods=["GET"])
def home():
    return "✅ Bridge Bot activo en Render"
