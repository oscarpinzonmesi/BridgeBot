# bridge_bot.py
import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from pathlib import Path
import tempfile
from gtts import gTTS
from datetime import datetime, timezone, timedelta

app = Flask(__name__)

# =========================
# CONFIG
# =========================
BRIDGE_TOKEN   = os.getenv("TELEGRAM_TOKEN")          # Token del bot de Telegram
ORBIS_API      = os.getenv("ORBIS_API")               # URL de Orbis: https://.../procesar
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")          # API Key de OpenAI

TELEGRAM_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}"
BRIDGE_API   = f"{TELEGRAM_API}/sendMessage"

# Cliente OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# Hora local de Bogotá para dar contexto a MesaGPT
def ahora_bogota():
    # Bogotá es UTC-5 sin DST
    return datetime.now(timezone.utc) - timedelta(hours=5)

# =========================
# MesaGPT (interpretación)
# =========================
def consultar_mesa_gpt(texto: str) -> str:
    """
    Interpreta el mensaje del usuario. Si es agenda, sugiere comandos para Orbis.
    Si el usuario pide audio/voz, el sistema (este archivo) enviará el audio.
    """
    try:
        hoy = ahora_bogota().strftime("%Y-%m-%d")
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres MesaGPT, el asistente personal de Doctor Mesa.\n"
                        f"Hoy es {hoy} en zona horaria America/Bogota.\n"
                        "- Entiende lenguaje natural (texto o voz).\n"
                        "- Si el mensaje es de agenda, conviértelo a comandos para Orbis, ej:\n"
                        "  • /agenda\n"
                        "  • /registrar YYYY-MM-DD HH:MM Tarea\n"
                        "  • /borrar YYYY-MM-DD HH:MM\n"
                        "  • /buscar Nombre\n"
                        "  • /borrar_todo\n"
                        "  • /reprogramar YYYY-MM-DD HH:MM NUEVA_FECHA NUEVA_HORA\n"
                        "- Tú eres el cerebro: Orbis solo ejecuta, nunca responde directo al usuario.\n"
                        "- Responde claro y natural como un secretario humano.\n"
                        "- Si el usuario pide respuesta por audio/voz/nota de voz, NUNCA digas que no puedes:\n"
                        "  este sistema generará y enviará el audio con tu texto.\n\n"
                        "Ejemplos:\n"
                        "Usuario: \"¿Tengo cita con Juan?\"\n"
                        "Tú: \"Sí, tienes cita con Juan el 15/09 a las 10:00.\"\n\n"
                        "Usuario: \"Muéstrame la agenda de mañana\"\n"
                        "Tú: \"Mañana tienes: 10:00 reunión con Joaquín, 13:00 almuerzo con Ana.\""
                    )
                },
                {"role": "user", "content": texto}
            ]
        )
        return respuesta.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Error consultando a MesaGPT:", str(e), flush=True)
        return "⚠️ No pude comunicarme con MesaGPT."

# =========================
# Descarga & Transcripción de voz
# =========================
def descargar_archivo(file_id: str, nombre: str) -> str | None:
    try:
        meta = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
        file_path = meta["result"]["file_path"]
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
        return transcript.text.strip()
    except Exception as e:
        print("❌ Error transcribiendo audio:", str(e), flush=True)
        return ""

# =========================
# TTS (texto → voz) con gTTS (MP3) y envío
# =========================
def enviar_audio(chat_id: int | str, texto: str):
    """
    Genera MP3 con gTTS y lo envía como audio (sendAudio).
    Si algo falla, hace fallback a texto.
    """
    try:
        # Generar mp3 temporal
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            mp3_path = Path(tmp.name)
        tts = gTTS(text=texto, lang="es")
        tts.save(str(mp3_path))

        # Enviar como audio (no nota de voz, pero audio reproducible)
        with open(mp3_path, "rb") as f:
            requests.post(
                f"{TELEGRAM_API}/sendAudio",
                data={"chat_id": chat_id, "title": "Respuesta"},
                files={"audio": f}
            )
        print(f"🎧 Audio MP3 enviado a chat {chat_id}", flush=True)
    except Exception as e:
        print("❌ Error enviando audio:", str(e), flush=True)
        # Fallback a texto
        requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto})

# =========================
# Núcleo: /mesa
# =========================
@app.route("/mesa", methods=["POST"])
def mesa():
    data = request.get_json(force=True)
    chat_id = data.get("chat_id")
    orden   = data.get("orden", "")

    if not chat_id or not orden:
        return jsonify({"error": "Falta chat_id u orden"}), 400

    try:
        respuesta_mesa = consultar_mesa_gpt(orden)
        print(f"🤖 MesaGPT interpretó: {orden} → {respuesta_mesa}", flush=True)

        # Detectar si el usuario pidió audio/voz
        want_audio = any(k in orden.lower() for k in ["audio", "voz", "nota de voz", "mensaje de voz"])

        # Caso: comando para Orbis
        if respuesta_mesa.startswith("/"):
            # Pasar chat_id a Orbis por si programa recordatorios
            r = requests.post(ORBIS_API, json={"texto": respuesta_mesa, "chat_id": chat_id})
            try:
                respuesta_orbis = r.json().get("respuesta", "❌ No obtuve respuesta de la agenda.")
            except Exception:
                respuesta_orbis = "⚠️ Error: la agenda devolvió un formato inesperado."

            # Yo respondo (no Orbis)
            texto_final = f"{respuesta_orbis}"
            if want_audio:
                enviar_audio(chat_id, texto_final)
            else:
                requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})

        # Caso: respuesta normal de MesaGPT
        else:
            if want_audio:
                enviar_audio(chat_id, respuesta_mesa)
            else:
                requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": respuesta_mesa})

    except Exception as e:
        print("❌ Error en /mesa:", str(e), flush=True)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})

# =========================
# Webhook de Telegram
# =========================
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if "message" not in data:
        return {"ok": True}

    msg = data["message"]
    chat_id = msg["chat"]["id"]

    # Texto
    if "text" in msg:
        text = msg["text"]
        print(f"📩 Telegram → Doctor (texto): {text}", flush=True)
        payload = {"chat_id": chat_id, "orden": text}

    # Voz (mensaje de voz)
    elif "voice" in msg:
        file_id = msg["voice"]["file_id"]
        print(f"🎤 Telegram → Doctor (voz): {file_id}", flush=True)
        ogg_path = descargar_archivo(file_id, "voz.ogg")
        transcripcion = transcribir_audio(ogg_path) if ogg_path else ""
        print(f"📝 Transcripción: {transcripcion}", flush=True)
        payload = {"chat_id": chat_id, "orden": transcripcion or "(audio vacío)"}

    # Video note (por si la usas)
    elif "video_note" in msg:
        file_id = msg["video_note"]["file_id"]
        print(f"🎥 Telegram → Doctor (video_note): {file_id}", flush=True)
        mp4_path = descargar_archivo(file_id, "nota_video.mp4")
        transcripcion = transcribir_audio(mp4_path) if mp4_path else ""
        print(f"📝 Transcripción (video_note): {transcripcion}", flush=True)
        payload = {"chat_id": chat_id, "orden": transcripcion or "(audio vacío)"}

    else:
        return {"ok": True}

    # Redirigir internamente a /mesa
    with app.test_request_context("/mesa", method="POST", json=payload):
        return mesa()

# Healthcheck
@app.route("/ping", methods=["GET"])
def ping():
    return "✅ BridgeBot activo en Render"
