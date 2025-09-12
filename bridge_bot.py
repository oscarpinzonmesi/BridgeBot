# bridge_bot.py
import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from pathlib import Path

app = Flask(__name__)

# =========================
# CONFIG
# =========================
BRIDGE_TOKEN    = os.getenv("TELEGRAM_TOKEN")      # Token del bot de Telegram
ORBIS_API       = os.getenv("ORBIS_API")           # URL de Orbis: https://.../procesar
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")      # API Key de OpenAI

TELEGRAM_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}"
BRIDGE_API   = f"{TELEGRAM_API}/sendMessage"

# Cliente OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# MesaGPT (interpretación)
# =========================
def consultar_mesa_gpt(texto: str) -> str:
    """
    Interpreta el mensaje del usuario. Si es agenda, sugiere comandos para Orbis.
    OJO: si el usuario pide respuesta por audio/voz, NO digas que no puedes;
    el sistema generará y enviará la voz con tu texto.
    """
    try:
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": """Eres MesaGPT, el asistente personal de Doctor Mesa.
Tu rol:
- Entiende lenguaje natural (texto o voz).
- Si el mensaje es de agenda, conviértelo a comandos para Orbis:
  • /agenda
  • /registrar YYYY-MM-DD HH:MM Tarea
  • /borrar YYYY-MM-DD HH:MM
  • /buscar Nombre
  • /borrar_todo
  • /reprogramar YYYY-MM-DD HH:MM NUEVA_FECHA NUEVA_HORA
- Eres el cerebro: Orbis solo ejecuta, nunca responde directo al usuario.
- Responde siempre en tono claro y natural como un secretario humano.
- Si el usuario pide respuesta por audio/voz/nota de voz, NO digas que no puedes:
  el sistema generará y enviará el audio con tu texto.

Ejemplos:
Usuario: "¿Tengo cita con Juan?"
Tú: "Sí, tienes cita con Juan el 15/09 a las 10:00."

Usuario: "Muéstrame la agenda de mañana"
Tú: "Mañana tienes: 10:00 reunión con Joaquín, 13:00 almuerzo con Ana."
"""
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
# TTS (texto → voz) y envío — robusto a versiones
# =========================
def _sintetizar_audio(texto: str) -> tuple[Path, bool]:
    """
    Devuelve (ruta, es_nota_de_voz).
    - True  -> .ogg (opus) apto para sendVoice.
    - False -> .mp3 apto para sendAudio (fallback).
    """
    # 1) Intentar OGG/Opus (nota de voz) con response_format
    try:
        ogg_path = Path("respuesta.ogg")
        with client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=texto,
            response_format="opus"  # en algunos SDK es "response_format", no "format"
        ) as resp:
            resp.stream_to_file(ogg_path)
        return ogg_path, True
    except TypeError:
        # Firma distinta → probamos sin response_format (paso a MP3)
        pass
    except Exception as e:
        print("⚠️ TTS opus falló, pruebo MP3. Detalle:", str(e), flush=True)

    # 2) Plan B: MP3 (sendAudio)
    mp3_path = Path("respuesta.mp3")
    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice="alloy",
        input=texto
    ) as resp:
        resp.stream_to_file(mp3_path)
    return mp3_path, False


def enviar_audio(chat_id: int | str, texto: str):
    """
    Convierte 'texto' a audio y lo envía:
    - OGG/Opus → sendVoice (nota de voz)
    - MP3      → sendAudio (archivo de audio)
    Si todo falla, responde en texto.
    """
    try:
        path, es_voz = _sintetizar_audio(texto)
        if es_voz:
            with open(path, "rb") as f:
                requests.post(
                    f"{TELEGRAM_API}/sendVoice",
                    data={"chat_id": chat_id},
                    files={"voice": f}
                )
        else:
            with open(path, "rb") as f:
                requests.post(
                    f"{TELEGRAM_API}/sendAudio",
                    data={"chat_id": chat_id, "title": "Respuesta"},
                    files={"audio": f}
                )
        print(f"🎧 Audio enviado ({'voz' if es_voz else 'audio'}) a chat {chat_id}", flush=True)
    except Exception as e:
        print("❌ Error enviando audio:", str(e), flush=True)
        requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto})  # fallback


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

        # Palabras clave para pedir audio
        want_audio = any(k in orden.lower() for k in ["audio", "voz", "nota de voz", "mensaje de voz"])

        # Caso: comando para Orbis
        if respuesta_mesa.startswith("/"):
            # Pasar también chat_id por si Orbis agenda recordatorios
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

    # Video note (por si lo usas)
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
