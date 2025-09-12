import os
from flask import Flask, request
import requests

app = Flask(__name__)

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("TELEGRAM_TOKEN")   # Token del nuevo bot (BridgeBot)
ORBIS_URL = os.getenv("ORBIS_URL")           # URL del servicio Orbis en Render

BRIDGE_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendMessage"


# === RUTA WEBHOOK ===


@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    print("📩 Llego update:", data, flush=True)

    if "message" not in data:
        print("❌ No hay campo 'message' en el update", flush=True)
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")
    print(f"➡️ Mensaje recibido: {text}", flush=True)

    try:
        if not text:  # 👈 si está vacío
            print("⚠️ Mensaje sin texto, BridgeBot responde por fallback", flush=True)
            requests.post(BRIDGE_API, json={
                "chat_id": chat_id,
                "text": "🤖 MesaGPT: recibí tu mensaje (sin texto)"
            })
        elif text.startswith("/") or "agenda" in text.lower() or "cita" in text.lower():
            print("🔗 Reenviando update completo a Orbis...", flush=True)
            r = requests.post(ORBIS_URL, json=data)
            print("Respuesta Orbis:", r.text, flush=True)
        else:
            print("🤖 Respondiendo desde BridgeBot", flush=True)
            r = requests.post(BRIDGE_API, json={
                "chat_id": chat_id,
                "text": f"🤖 MesaGPT: te escuché → {text}"
            })
            print("Respuesta BridgeBot:", r.text, flush=True)
    except Exception as e:
        print("❌ Error procesando mensaje:", str(e), flush=True)

    return {"ok": True}



# === RUTA HOME ===
@app.route("/ping", methods=["GET"])
def home():
    return "✅ Bridge Bot activo en Render"
