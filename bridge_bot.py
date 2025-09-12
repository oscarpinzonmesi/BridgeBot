import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("TELEGRAM_TOKEN")     
ORBIS_API = os.getenv("ORBIS_API")             
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")   

BRIDGE_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendMessage"
TELEGRAM_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}"

# Inicializar cliente de OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# === Función: interpretar con GPT ===
def consultar_mesa_gpt(texto: str) -> str:
    try:
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """Eres MesaGPT, el asistente personal de Doctor Mesa.
Tu tarea:
- Entiendes lenguaje natural (texto o voz).
- Si el mensaje es sobre agenda, conviértelo en comandos para Orbis:
  • /agenda
  • /registrar YYYY-MM-DD HH:MM tarea
  • /borrar YYYY-MM-DD HH:MM
  • /buscar Nombre
  • /borrar_todo
  • /reprogramar ...
- Tú eres el cerebro: Orbis solo ejecuta, pero nunca responde directo al usuario.
- Siempre da respuestas claras y naturales como un secretario humano.
Ejemplos:
Usuario: "¿Tengo cita con Juan?"
Tú: "Sí, tienes cita con Juan el 15/09 a las 10:00."
Usuario: "Muéstrame la agenda de mañana"
Tú: "Mañana tienes: 10:00 reunión con Joaquín, 13:00 almuerzo con Ana.""""}, 
                {"role": "user", "content": texto}
            ]
        )
        return respuesta.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Error consultando a MesaGPT:", str(e), flush=True)
        return "⚠️ No pude comunicarme con MesaGPT."

# === Manejo de voz ===
def descargar_archivo(file_id: str, nombre: str) -> str:
    try:
        r = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
        file_path = r["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BRIDGE_TOKEN}/{file_path}"
        data = requests.get(file_url)
        with open(nombre, "wb") as f:
            f.write(data.content)
        return nombre
    except Exception as e:
        print("❌ Error descargando archivo:", str(e), flush=True)
        return None

def transcribir_audio(file_path: str) -> str:
    try:
        with open(file_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        return transcript.text
    except Exception as e:
        print("❌ Error transcribiendo audio:", str(e), flush=True)
        return ""

# === Procesar mensaje ===
@app.route("/mesa", methods=["POST"])
def mesa():
    data = request.get_json(force=True)
    chat_id = data.get("chat_id")
    orden = data.get("orden", "")

    if not chat_id or not orden:
        return jsonify({"error": "Falta chat_id u orden"}), 400

    try:
        respuesta_mesa = consultar_mesa_gpt(orden)
        print(f"🤖 MesaGPT interpretó: {orden} → {respuesta_mesa}", flush=True)

        # Si es un comando de agenda, hablar con Orbis
        if respuesta_mesa.startswith("/"):
            r = requests.post(ORBIS_API, json={"texto": respuesta_mesa, "chat_id": chat_id})
            try:
                respuesta_orbis = r.json().get("respuesta", "❌ Orbis no devolvió respuesta")
            except:
                respuesta_orbis = "⚠️ Error: Orbis devolvió algo inesperado"

            # MesaGPT traduce la respuesta de Orbis
            final = f"📋 Aquí está lo que encontré: {respuesta_orbis}"
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": final})
        else:
            # Respuesta normal de MesaGPT
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": respuesta_mesa})

    except Exception as e:
        print("❌ Error en /mesa:", str(e), flush=True)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})

# === Webhook de Telegram ===
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]

    # Texto
    if "text" in data["message"]:
        text = data["message"]["text"]
        print(f"📩 Telegram → Doctor: {text}", flush=True)
        mesa_data = {"chat_id": chat_id, "orden": text}

    # Voz
    elif "voice" in data["message"]:
        file_id = data["message"]["voice"]["file_id"]
        print(f"🎤 Telegram → Doctor (voz): {file_id}", flush=True)
        ogg_file = descargar_archivo(file_id, "voz.ogg")
        transcripcion = transcribir_audio(ogg_file)
        print(f"📝 Transcripción: {transcripcion}", flush=True)
        mesa_data = {"chat_id": chat_id, "orden": transcripcion}

    else:
        return {"ok": True}

    with app.test_request_context("/mesa", method="POST", json=mesa_data):
        return mesa()

@app.route("/ping", methods=["GET"])
def ping():
    return "✅ BridgeBot activo"
