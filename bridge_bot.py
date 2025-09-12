import os
from flask import Flask, request, jsonify
import requests
from openai import OpenAI

app = Flask(__name__)

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("TELEGRAM_TOKEN")     # Token de tu bot en Telegram (BridgeBot)
ORBIS_API = os.getenv("ORBIS_API")             # URL de Orbis como API: https://orbis-xxx.onrender.com/procesar
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")   # Tu API key de OpenAI

BRIDGE_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendMessage"

# Inicializar cliente de OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)


# === Función: consultar a MesaGPT ===
def consultar_mesa_gpt(texto: str) -> str:
    """Envía el mensaje a OpenAI (MesaGPT) y devuelve la respuesta"""
    try:
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Eres MesaGPT, un asistente legal y de agenda. Si es tema de agenda, responde con un comando que Orbis entienda (/agenda, /registrar HH:MM tarea, /borrar HH:MM). Si no es agenda, responde con texto normal."},
                {"role": "user", "content": texto}
            ]
        )
        return respuesta.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Error consultando a MesaGPT:", str(e), flush=True)
        return "⚠️ No pude comunicarme con MesaGPT."


# === ENDPOINT DE MESA (para usarlo interno) ===
@app.route("/mesa", methods=["POST"])
def mesa():
    data = request.get_json(force=True)
    chat_id = data.get("chat_id")
    orden = data.get("orden", "")

    if not chat_id or not orden:
        return jsonify({"error": "Falta chat_id u orden"}), 400

    try:
        # Paso 1: consultar a MesaGPT
        respuesta_mesa = consultar_mesa_gpt(orden)
        print(f"🤖 MesaGPT interpretó: {orden}  →  {respuesta_mesa}", flush=True)

        # Paso 2: si es un comando de agenda (/...), lo pasamos a Orbis
        if respuesta_mesa.startswith("/"):
            r = requests.post(ORBIS_API, json={"texto": respuesta_mesa})
            try:
                respuesta_orbis = r.json().get("respuesta", "❌ Orbis no devolvió respuesta")
            except Exception:
                respuesta_orbis = "⚠️ Error: Orbis devolvió algo inesperado"
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": respuesta_orbis})
        else:
            # Si no es comando, es respuesta normal de MesaGPT
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": f"🤖 MesaGPT: {respuesta_mesa}"})
    except Exception as e:
        print("❌ Error en /mesa:", str(e), flush=True)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


# === TELEGRAM WEBHOOK ===
@app.route("/", methods=["POST"])
def webhook():
    """Telegram envía mensajes aquí → BridgeBot los manda a MesaGPT"""
    data = request.get_json(force=True)

    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    print(f"📩 Telegram → BridgeBot: {text}", flush=True)

    # Redirigir a /mesa internamente
    mesa_data = {"chat_id": chat_id, "orden": text}
    with app.test_request_context("/mesa", method="POST", json=mesa_data):
        return mesa()


# === RUTA HOME ===
@app.route("/ping", methods=["GET"])
def home():
    return "✅ Bridge Bot activo en Render"
