import os
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("TELEGRAM_TOKEN")   # Token del bot BridgeBot
ORBIS_API = os.getenv("ORBIS_API")           # URL de Orbis: https://orbis-xxx.onrender.com/procesar

BRIDGE_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendMessage"


# === INTERPRETADOR DE ÓRDENES ===
def interpretar_orden(texto: str) -> str:
    """Convierte frases en lenguaje natural a comandos que entiende Orbis"""
    t = texto.lower()

    # Ejemplo: detectar "mañana a las 3 pm"
    if "carlos" in t and ("3 pm" in t or "tres" in t):
        return "/registrar 15:00 Cita con Carlos en el parque"

    # Podrías añadir más reglas aquí para otros casos
    # Por defecto devuelve el mismo texto
    return texto


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
        # Interpretamos primero
        orden_traducida = interpretar_orden(orden)
        print(f"🔎 Interpretado: {orden}  →  {orden_traducida}", flush=True)

        if orden_traducida.startswith("/") or "agenda" in orden_traducida.lower() or "cita" in orden_traducida.lower():
            print("🔗 MesaGPT dio orden para Orbis:", orden_traducida, flush=True)
            r = requests.post(ORBIS_API, json={"texto": orden_traducida})
            try:
                respuesta_orbis = r.json().get("respuesta", "❌ Orbis no devolvió respuesta")
            except Exception:
                respuesta_orbis = "⚠️ Error: Orbis devolvió algo inesperado"
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": respuesta_orbis})
        else:
            print("🤖 MesaGPT respondió directo:", orden_traducida, flush=True)
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": f"🤖 MesaGPT: {orden_traducida}"})
    except Exception as e:
        print("❌ Error en /mesa:", str(e), flush=True)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


# === TELEGRAM WEBHOOK ===
@app.route("/", methods=["POST"])
def webhook():
    """Telegram envía los mensajes aquí → BridgeBot los manda a MesaGPT"""
    data = request.get_json(force=True)

    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    print(f"📩 Telegram → BridgeBot: {text}", flush=True)

    # Simulamos el paso por MesaGPT
    mesa_data = {"chat_id": chat_id, "orden": text}
    with app.test_request_context("/mesa", method="POST", json=mesa_data):
        return mesa()

    return {"ok": True}


# === RUTA HOME ===
@app.route("/ping", methods=["GET"])
def home():
    return "✅ Bridge Bot activo en Render"
