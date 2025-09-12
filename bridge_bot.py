# bridge_bot.py
import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from pathlib import Path
import tempfile
from gtts import gTTS
from datetime import datetime, timezone, timedelta
import re
import schedule
import threading
import time
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

# Hora local de Bogotá (contexto para MesaGPT)
def ahora_bogota():
    return datetime.now(timezone.utc) - timedelta(hours=5)


def consultar_mesa_gpt(texto: str) -> str:
    """
    Interpreta el mensaje del usuario. Si es agenda, sugiere comandos para Orbis.
    El envío en audio o texto lo decide este archivo (no lo menciones en la respuesta).
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
                        "  • /borrar_fecha YYYY-MM-DD   ← (nuevo, para borrar todas las citas de un día específico)\n"
                        "  • /reprogramar YYYY-MM-DD HH:MM NUEVA_FECHA NUEVA_HORA\n"
                        "- Si el usuario dice: 'borra lo de mañana', 'elimínalo todo para el 15 de septiembre', etc., "
                        "usa /borrar_fecha con la fecha correspondiente en formato YYYY-MM-DD.\n"
                        "- Tú eres el cerebro: Orbis solo ejecuta, nunca responde directo al usuario.\n"
                        "- Responde claro y natural como un secretario humano.\n"
                        "- No prometas nada sobre audio: este sistema decidirá el canal de salida.\n\n"
                        "Ejemplos:\n"
                        "Usuario: \"¿Tengo cita con Juan?\"\n"
                        "Tú: \"Sí, tienes cita con Juan el 15 de septiembre a las 10 de la mañana.\"\n\n"
                        "Usuario: \"Muéstrame la agenda de mañana\"\n"
                        "Tú: \"Mañana tienes: a las 10 de la mañana reunión con Joaquín, a la 1 de la tarde almuerzo con Ana.\"\n\n"
                        "Usuario: \"Borra todo lo de mañana\"\n"
                        "Tú: \"/borrar_fecha YYYY-MM-DD\" (con la fecha exacta de mañana)."
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



def preparar_texto_para_audio(texto: str) -> str:
    """
    Prepara el texto para que se escuche natural en voz.
    - Elimina flechas, guiones, comas y puntos innecesarios.
    - Convierte fechas 15/09/2025 → "15 de septiembre de 2025".
    - Convierte horas 24h (10:00, 13:00, 20:30) a 12h con 'de la mañana/tarde/noche'.
    """
    # 1. Eliminar símbolos raros
    limpio = re.sub(r"[→←↑↓➜➡️⬅️➤➔•·\-\*_,\.]", " ", texto)

    # 2. Fechas DD/MM/YYYY
    def convertir_fecha(m):
        dia, mes, anio = int(m.group(1)), int(m.group(2)), int(m.group(3))
        meses = [
            "enero","febrero","marzo","abril","mayo","junio",
            "julio","agosto","septiembre","octubre","noviembre","diciembre"
        ]
        return f"{dia} de {meses[mes-1]} de {anio}"
    limpio = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", convertir_fecha, limpio)

    # 3. Fechas DD/MM
    limpio = re.sub(
        r"\b(\d{1,2})/(\d{1,2})\b",
        lambda m: f"{int(m.group(1))} de "
                  f"{['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'][int(m.group(2))-1]}",
        limpio
    )

    # 4. Horas HH:MM o HH.MM
    def convertir_hora(m):
        h = int(m.group(1))
        mnt = int(m.group(2))

        if h == 0:
            h12, suf = 12, "de la noche"
        elif 1 <= h < 12:
            h12, suf = h, "de la mañana"
        elif h == 12:
            h12, suf = 12, "del mediodía"
        elif 13 <= h < 19:
            h12, suf = h - 12, "de la tarde"
        else:
            h12, suf = h - 12, "de la noche"

        if mnt == 0:
            return f"{h12} {suf}"
        else:
            return f"{h12} y {mnt} {suf}"

    limpio = re.sub(r"\b(\d{1,2})[:.](\d{2})\b", convertir_hora, limpio)

    # 5. Quitar espacios múltiples
    limpio = re.sub(r"\s+", " ", limpio)

    return limpio.strip()




# =========================
# TTS (texto → voz) con gTTS (MP3) y envío
# =========================
def enviar_audio(chat_id: int | str, texto: str):
    """
    Genera MP3 con gTTS y lo envía como audio (sendAudio).
    Si algo falla, hace fallback a texto.
    """
    try:
        texto_para_leer = preparar_texto_para_audio(texto)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            mp3_path = Path(tmp.name)
        tts = gTTS(text=texto_para_leer, lang="es")
        tts.save(str(mp3_path))

        with open(mp3_path, "rb") as f:
            requests.post(
                f"{TELEGRAM_API}/sendAudio",
                data={"chat_id": chat_id, "title": "Respuesta"},
                files={"audio": f}
            )
        print(f"🎧 Audio MP3 enviado a chat {chat_id}", flush=True)
    except Exception as e:
        print("❌ Error enviando audio:", str(e), flush=True)
        requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto})

# =========================
# Alarmas (Orbis → Telegram)
# =========================
def enviar_alarma(chat_id: int | str, mensaje: str, prefer_audio: bool = False):
    """
    Envía un recordatorio/alarma al usuario.
    Si prefer_audio=True, lo envía como nota de voz (gTTS).
    Si no, lo envía como texto normal.
    """
    try:
        if prefer_audio:
            enviar_audio(chat_id, f"⏰ Recordatorio: {mensaje}")
        else:
            requests.post(
                BRIDGE_API,
                json={"chat_id": chat_id, "text": f"⏰ Recordatorio: {mensaje}"}
            )
        print(f"✅ Alarma enviada a {chat_id}: {mensaje}", flush=True)
    except Exception as e:
        print("❌ Error enviando alarma:", str(e), flush=True)


@app.route("/mesa", methods=["POST"])
def mesa():
    data = request.get_json(force=True)
    chat_id       = data.get("chat_id")
    orden         = data.get("orden", "")
    prefer_audio  = bool(data.get("prefer_audio", False))  # espejo de la entrada

    if not chat_id or not orden:
        return jsonify({"error": "Falta chat_id u orden"}), 400

    try:
        # Overrides por palabras (opcionales)
        txt_low = orden.lower()
        if any(k in txt_low for k in [" en audio", "nota de voz", "mensaje de voz"]):
            prefer_audio = True
        if " en texto" in txt_low:
            prefer_audio = False

        respuesta_mesa = consultar_mesa_gpt(orden)
        print(f"🤖 MesaGPT interpretó: {orden} → {respuesta_mesa}", flush=True)

        # ============================
        # Detectar comandos (/agenda, /borrar_todo, etc.)
        # ============================
        comando = None
        if respuesta_mesa.startswith("/"):
            comando = respuesta_mesa.strip()
        else:
            match = re.search(r"(/[\w_]+.*)", respuesta_mesa)
            if match:
                comando = match.group(1).strip()

        if comando:
            # Pasar chat_id a Orbis por si programa recordatorios
            r = requests.post(ORBIS_API, json={"texto": comando, "chat_id": chat_id})
            try:
                respuesta_orbis = r.json().get("respuesta", "❌ No obtuve respuesta de la agenda.")
            except Exception:
                respuesta_orbis = "⚠️ Error: la agenda devolvió un formato inesperado."

            texto_final = f"{respuesta_orbis}"
            if prefer_audio:
                enviar_audio(chat_id, texto_final)
            else:
                requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})

        # ============================
        # Respuesta normal de MesaGPT
        # ============================
        else:
            if prefer_audio:
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

    msg     = data["message"]
    chat_id = msg["chat"]["id"]

    # Texto → respondo en texto
    if "text" in msg:
        text = msg["text"]
        print(f"📩 Telegram → Doctor (texto): {text}", flush=True)
        payload = {"chat_id": chat_id, "orden": text, "prefer_audio": False}

    # Voz (mensaje de voz) → respondo en audio
    elif "voice" in msg:
        file_id = msg["voice"]["file_id"]
        print(f"🎤 Telegram → Doctor (voz): {file_id}", flush=True)
        ogg_path = descargar_archivo(file_id, "voz.ogg")
        transcripcion = transcribir_audio(ogg_path) if ogg_path else ""
        print(f"📝 Transcripción: {transcripcion}", flush=True)
        payload = {"chat_id": chat_id, "orden": transcripcion or "(audio vacío)", "prefer_audio": True}

    # Video note → también respondo en audio
    elif "video_note" in msg:
        file_id = msg["video_note"]["file_id"]
        print(f"🎥 Telegram → Doctor (video_note): {file_id}", flush=True)
        mp4_path = descargar_archivo(file_id, "nota_video.mp4")
        transcripcion = transcribir_audio(mp4_path) if mp4_path else ""
        print(f"📝 Transcripción (video_note): {transcripcion}", flush=True)
        payload = {"chat_id": chat_id, "orden": transcripcion or "(audio vacío)", "prefer_audio": True}

    else:
        return {"ok": True}

    # Redirigir internamente a /mesa
    with app.test_request_context("/mesa", method="POST", json=payload):
        return mesa()


# Healthcheck
@app.route("/ping", methods=["GET"])
def ping():
    return "✅ BridgeBot activo en Render"
# =========================
# Scheduler de alertas
# =========================


def revisar_agenda_y_enviar_alertas():
    """
    Consulta a Orbis si hay eventos próximos y manda recordatorios.
    Orbis debe implementar el comando /proximos y devolver un JSON:
    {
        "eventos": [
            {"chat_id": 5155863903, "mensaje": "Reunión con Joaquín a las 10:00"},
            {"chat_id": 5155863903, "mensaje": "Almuerzo con Ana a las 13:00"}
        ]
    }
    """
    try:
        r = requests.post(ORBIS_API, json={"texto": "/proximos"})
        if r.status_code != 200:
            print("⚠️ Orbis no respondió correctamente", flush=True)
            return

        eventos = r.json().get("eventos", [])
        for ev in eventos:
            chat_id = ev.get("chat_id")
            mensaje = ev.get("mensaje")
            if chat_id and mensaje:
                enviar_alarma(chat_id, mensaje, prefer_audio=True)

    except Exception as e:
        print("❌ Error revisando agenda:", str(e), flush=True)


def iniciar_scheduler():
    # Revisar la agenda cada minuto
    schedule.every(1).minutes.do(revisar_agenda_y_enviar_alertas)

    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(1)

    threading.Thread(target=run_scheduler, daemon=True).start()


# Iniciar scheduler automáticamente al levantar el bot
iniciar_scheduler()
