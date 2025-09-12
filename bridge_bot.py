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
TELEGRAM_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}"  # 👈 nuevo

# Inicializar cliente de OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)


# === Función: consultar a MesaGPT ===
def consultar_mesa_gpt(texto: str) -> str:
    try:
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres MesaGPT, el cerebro de Doctor Mesa. "
                        "Tu tarea es interpretar instrucciones en lenguaje natural y devolver SIEMPRE "
                        "un comando válido para Orbis cuando la instrucción sea sobre la agenda. "
                        "Comandos disponibles:\n\n"
                        "👉 /agenda → mostrar toda la agenda\n"
                        "👉 /registrar HH:MM tarea → registrar cita/tarea\n"
                        "👉 /borrar HH:MM → borrar cita en hora exacta\n"
                        "👉 /borrar_todo → borrar toda la agenda\n"
                        "👉 /reprogramar HH:MM → mover todas las citas a una nueva hora\n"
                        "👉 /buscar Nombre → mostrar citas con esa persona\n"
                        "👉 /buscar_fecha YYYY-MM-DD → mostrar citas de un día\n"
                        "👉 /cuando Nombre → decir a qué hora tiene citas con esa persona\n\n"
                        "Reglas:\n"
                        "- Si el usuario dice algo de agenda, traduce a un comando exacto de arriba.\n"
                        "- Si la orden no tiene hora o fecha, infórmalo en texto claro.\n"
                        "- Si no es tema de agenda, responde como asistente normal (legal o conversación).\n"
                    )
                },
                {"role": "user", "content": texto}
            ]
        )

        return respuesta.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Error consultando a MesaGPT:", str(e), flush=True)
        return "⚠️ No pude comunicarme con MesaGPT."


# === Funciones nuevas para VOZ ===
def descargar_voz(file_id: str) -> str:
    """Descarga el archivo de voz de Telegram y lo guarda como voice.ogg"""
    try:
        r = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
        file_path = r["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BRIDGE_TOKEN}/{file_path}"
        voice_file = requests.get(file_url)
        with open("voice.ogg", "wb") as f:
            f.write(voice_file.content)
        return "voice.ogg"
    except Exception as e:
        print("❌ Error descargando voz:", str(e), flush=True)
        return None


def transcribir_voz(file_path: str) -> str:
    """Envía el audio a Whisper y devuelve el texto transcrito"""
    try:
        with open(file_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(  # 👈 usa Whisper
                model="whisper-1",
                file=audio_file
            )
        return transcript.text
    except Exception as e:
        print("❌ Error transcribiendo voz:", str(e), flush=True)
        return ""


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

        # 🧹 Normalizar respuesta (quitar comillas, espacios extra, etc.)
        respuesta_mesa = respuesta_mesa.strip().strip("'").strip('"')

        # Paso 2: si es un comando de agenda (/...), lo pasamos a Orbis
        if respuesta_mesa.startswith("/"):
            try:
                r = requests.post(ORBIS_API, json={"texto": respuesta_mesa})
                respuesta_orbis = r.json().get("respuesta", "❌ Orbis no devolvió respuesta")
            except Exception as e:
                print("❌ Error consultando Orbis:", str(e), flush=True)
                respuesta_orbis = "⚠️ Error: Orbis devolvió algo inesperado"

            # Mandar la respuesta de Orbis al chat de Telegram
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
    data = request.get_json(force=True)

    # 👀 Debug: imprimir todo lo que manda Telegram
    print("📦 Datos crudos de Telegram:", data, flush=True)

    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]

    # Caso 1: mensaje de texto
    if "text" in data["message"]:
        text = data["message"]["text"]
        print(f"📩 Telegram → BridgeBot (texto): {text}", flush=True)
        mesa_data = {"chat_id": chat_id, "orden": text}

    # Caso 2: mensaje de voz (nota de voz en formato ogg)
    elif "voice" in data["message"]:
        file_id = data["message"]["voice"]["file_id"]
        print(f"🎤 Telegram → BridgeBot (voice): {file_id}", flush=True)
        ogg_file = descargar_voz(file_id)
        if ogg_file:
            transcripcion = transcribir_voz(ogg_file)
            print(f"📝 Transcripción (voice): {transcripcion}", flush=True)
            mesa_data = {"chat_id": chat_id, "orden": transcripcion}
        else:
            return jsonify({"error": "No se pudo descargar el audio (voice)"}), 500

    # Caso 3: mensaje de audio (archivo de música / mp3 / ogg)
    elif "audio" in data["message"]:
        file_id = data["message"]["audio"]["file_id"]
        print(f"🎶 Telegram → BridgeBot (audio): {file_id}", flush=True)
        ogg_file = descargar_voz(file_id)
        if ogg_file:
            transcripcion = transcribir_voz(ogg_file)
            print(f"📝 Transcripción (audio): {transcripcion}", flush=True)
            mesa_data = {"chat_id": chat_id, "orden": transcripcion}
        else:
            return jsonify({"error": "No se pudo descargar el audio (audio)"}), 500

    # Caso 4: mensaje de video_note (nota de voz redonda en Telegram)
    elif "video_note" in data["message"]:
        file_id = data["message"]["video_note"]["file_id"]
        print(f"🎥 Telegram → BridgeBot (video_note): {file_id}", flush=True)
        ogg_file = descargar_voz(file_id)
        if ogg_file:
            transcripcion = transcribir_voz(ogg_file)
            print(f"📝 Transcripción (video_note): {transcripcion}", flush=True)
            mesa_data = {"chat_id": chat_id, "orden": transcripcion}
        else:
            return jsonify({"error": "No se pudo descargar el video_note"}), 500

    else:
        return {"ok": True}

    # Redirigir a /mesa internamente
    with app.test_request_context("/mesa", method="POST", json=mesa_data):
        return mesa()




# === RUTA HOME ===
@app.route("/ping", methods=["GET"])
def home():
    return "✅ Bridge Bot activo en Render"
