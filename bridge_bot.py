import os
import json  # ⬅️ NECESARIO
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
# MEMORIA
# =========================
# Recordar el último chat que habló con el bot (para alarmas)
LAST_CHAT_ID = None
# Memoria de la última agenda listada por chat (para "borra esa")
ULTIMA_AGENDA = {}
PENDIENTE = {}  
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

# =========================
# Hora local de Bogotá (contexto para MesaGPT)
# =========================
def ahora_bogota():
    return datetime.now(timezone.utc) - timedelta(hours=5)
def fecha_bogota(delta_dias=0) -> str:
    return (ahora_bogota() + timedelta(days=delta_dias)).strftime("%Y-%m-%d")

def es_si(texto: str) -> bool:
    t = texto.strip().lower()
    return t in {"si","sí","claro","ok","dale","hazlo","hágale","de una","correcto","afirmativo","por favor"}

def es_no(texto: str) -> bool:
    t = texto.strip().lower()
    return t in {"no","nel","negativo","mejor no","nop"}

def normalizar_manjana(texto: str) -> str:
    # Corrige variantes comunes: 'manana', 'mañan', 'mañna', etc.
    t = texto
    t = re.sub(r"\bmanana\b", "mañana", t, flags=re.IGNORECASE)
    t = re.sub(r"\bmañan\b", "mañana", t, flags=re.IGNORECASE)
    t = re.sub(r"\bmañna\b", "mañana", t, flags=re.IGNORECASE)
    return t


# =========================
# INTÉRPRETE (GPT)
# =========================
def consultar_mesa_gpt(texto: str) -> str:
    """
    Interpreta el mensaje del usuario.
    - Si es agenda: devuelve un comando /... para Orbis.
    - Si NO es agenda: responde en lenguaje natural (sin consultar Orbis).
    """
    try:
        hoy_dt = ahora_bogota()
        hoy = hoy_dt.strftime("%Y-%m-%d")
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres MesaGPT, el asistente personal de Doctor Mesa.\n"
                        f"Hoy es {hoy} (America/Bogota).\n\n"
                        "TU OBJETIVO:\n"
                        "- Eres el cerebro. Orbis es solo el cuaderno/agenda.\n"
                        "- Si el mensaje es de AGENDA, responde EXCLUSIVAMENTE con un comando válido para Orbis.\n"
                        "- Si NO es de AGENDA, conversa de forma natural y útil (no uses comandos).\n\n"
                        "REGLAS AGENDA:\n"
                        "- Comandos válidos:\n"
                        "  /agenda\n"
                        "  /registrar YYYY-MM-DD HH:MM Tarea\n"
                        "  /borrar YYYY-MM-DD HH:MM\n"
                        "  /borrar_fecha YYYY-MM-DD\n"
                        "  /borrar_todo\n"
                        "  /buscar Nombre\n"
                        "  /buscar_fecha YYYY-MM-DD\n"
                        "  /cuando Nombre\n"
                        "  /reprogramar YYYY-MM-DD HH:MM NUEVA_FECHA NUEVA_HORA\n"
                        "  /modificar YYYY-MM-DD HH:MM Nuevo texto\n"
                        "- Si el usuario escribe directamente un comando (empieza con '/'), respóndelo tal cual.\n"
                        "- Con 'mañana' usa fecha = (hoy + 1 día); con 'hoy' usa fecha = hoy. (No inventes otras interpretaciones.)\n"
                        "- 'No estoy seguro a qué cita te refieres' SOLO se usa si el usuario pide borrar/modificar con referencias ambiguas como 'borra esa/esto' SIN contexto reciente. Para saludos o temas no agenda, NO uses esa frase.\n\n"
                        "REGLAS CONVERSACIÓN NO AGENDA:\n"
                        "- Responde como humano, claro y breve. Si el usuario pide organizar el día, propón un plan y AL FINAL pregunta si deseas que lo agende en Orbis.\n"
                    )
                },
                {"role": "user", "content": texto}
            ]
        )
        return respuesta.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Error consultando a MesaGPT:", str(e), flush=True)
        return "Lo siento, tuve un problema interpretando el mensaje. ¿Puedes repetirlo?"

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
# Texto → Voz (gTTS) y envío
# =========================
def preparar_texto_para_audio(texto: str) -> str:
    # Eliminar emojis/símbolos extraños
    limpio = re.sub(r"[^A-Za-zÁÉÍÓÚÜáéíóúüÑñ0-9\s:,;()¿?¡!/-]", " ", texto)
    # Normalizar flechas, guiones, bullets
    limpio = re.sub(r"[→←↑↓➜➡️⬅️➤➔•·_\*]", " ", limpio)

    # Fechas dd/mm/yyyy → '15 de septiembre de 2025' y dd/mm → '15 de septiembre'
    def _mes(n):
        return ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"][n-1]
    limpio = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
                    lambda m: f"{int(m.group(1))} de {_mes(int(m.group(2)))} de {m.group(3)}",
                    limpio)
    limpio = re.sub(r"\b(\d{1,2})/(\d{1,2})\b",
                    lambda m: f"{int(m.group(1))} de {_mes(int(m.group(2)))}",
                    limpio)

    # Horas HH:MM o HH.MM a 12h natural
    def conv_hora(m):
        h = int(m.group(1)); mnt = int(m.group(2))
        if h == 0:   h12, suf = 12, "de la noche"
        elif h < 12: h12, suf = h, "de la mañana"
        elif h == 12: h12, suf = 12, "del mediodía"
        elif h < 19: h12, suf = h - 12, "de la tarde"
        else:         h12, suf = h - 12, "de la noche"
        return f"{h12} {('y ' + str(mnt)) if mnt else ''} {suf}".strip()
    limpio = re.sub(r"\b(\d{1,2})[:.](\d{2})\b", conv_hora, limpio)


    # Espacios y signos repetidos
    limpio = re.sub(r"[,:;]{2,}", lambda m: m.group(0)[0], limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    return limpio
def enviar_audio(chat_id: int | str, texto: str):
    """
    Genera MP3 con gTTS y lo envía como audio (sendAudio). Si algo falla, hace fallback a texto.
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

# =========================
# Endpoint principal (control GPT)
# =========================
@app.route("/mesa", methods=["POST"])
def mesa():
    data = request.get_json(force=True)
    chat_id       = data.get("chat_id")
    orden         = data.get("orden", "")
    prefer_audio  = bool(data.get("prefer_audio", False))

    if not chat_id or not orden:
        return jsonify({"error": "Falta chat_id u orden"}), 400

    try:
        # 0) Normalizar texto de usuario (errores comunes)
        orden = normalizar_manjana(orden)
        txt_low = orden.lower()

        # Preferencias de salida por texto
        if any(k in txt_low for k in [" en audio", "nota de voz", "mensaje de voz"]):
            prefer_audio = True
        if " en texto" in txt_low:
            prefer_audio = False

        # 1) ¿Confirma algo pendiente con un “sí”/“no”?
        if chat_id in PENDIENTE:
            pend = PENDIENTE[chat_id]
            if es_si(txt_low):
                # Ejecutar la intención pendiente
                if pend.get("tipo") == "buscar_fecha" and pend.get("fecha") == "manana":
                    comando = f"/buscar_fecha {fecha_bogota(1)}"
                elif pend.get("tipo") == "buscar_fecha" and pend.get("fecha") == "hoy":
                    comando = f"/buscar_fecha {fecha_bogota(0)}"
                else:
                    comando = pend.get("comando")

                PENDIENTE.pop(chat_id, None)

                # Consultar Orbis en modo JSON, redactar natural y responder
                r = requests.post(ORBIS_API, json={"texto": comando, "chat_id": chat_id, "modo": "json"})
                try:
                    datos_orbis = r.json()
                except Exception:
                    datos_orbis = {"ok": False, "error": "respuesta_no_json"}

                print(f"📦 Datos de Orbis (confirmado): {datos_orbis}", flush=True)

                if isinstance(datos_orbis, dict) and datos_orbis.get("ok") and datos_orbis.get("items"):
                    ULTIMA_AGENDA[chat_id] = datos_orbis["items"]

                contenido_json = json.dumps(datos_orbis, ensure_ascii=False) if isinstance(datos_orbis, dict) else str(datos_orbis)
                respuesta_natural = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": ("Eres el asistente de Doctor Mesa. Redacta en lenguaje natural, claro y breve, "
                                                       "usando SOLO los datos de Orbis. No inventes. Si no hay citas, dilo.")},
                        {"role": "user", "content": f"Petición confirmada por el usuario: {orden}"},
                        {"role": "user", "content": f"Datos de Orbis (JSON o texto): {contenido_json}"}
                    ]
                )
                texto_final = respuesta_natural.choices[0].message.content.strip()
                if prefer_audio: enviar_audio(chat_id, texto_final)
                else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})
                return jsonify({"ok": True})

            elif es_no(txt_low):
                PENDIENTE.pop(chat_id, None)
                msg = "Listo, no consulto la agenda. ¿Quieres que te proponga un plan para mañana?"
                if prefer_audio: enviar_audio(chat_id, msg)
                else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
                return jsonify({"ok": True})
            # Si no es un “sí/no”, seguimos flujo normal sin borrar el pendiente.

        # 2) Heurística: si el usuario menciona 'mañana' + (agenda|citas), crear PENDIENTE y pedir confirmación
        if ("mañana" in txt_low) and (("agenda" in txt_low) or ("citas" in txt_low)):
            PENDIENTE[chat_id] = {"tipo": "buscar_fecha", "fecha": "manana"}
            msg = "¿Quieres que consulte en Orbis tus citas de mañana?"
            if prefer_audio: enviar_audio(chat_id, msg)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
            return jsonify({"ok": True})

        # 3) GPT interpreta (cerebro primero)
        interpretacion = consultar_mesa_gpt(orden)
        print(f"🤖 MesaGPT interpretó: {orden} → {interpretacion}", flush=True)

        # Respuesta de ambigüedad para saludos u off-topic → reemplazar por saludo humano
        if interpretacion.startswith("⚠️ No estoy seguro") and not re.search(r"\b(borra|borrar|modificar|reprogramar|cambiar)\b", txt_low):
            interpretacion = "¡Aquí estoy! Te escucho. ¿En qué te ayudo?"

        # 4) ¿Es comando de agenda?
        comando = None
        if interpretacion.startswith("/"):
            comando = interpretacion
        else:
            m = re.search(r"(/[\w_]+.*)", interpretacion)
            if m:
                comando = m.group(1)

        # Corrección: si el LLM devolvió /agenda pero el usuario dijo “mañana”
        if comando and comando.startswith("/agenda") and "mañana" in txt_low:
            comando = f"/buscar_fecha {fecha_bogota(1)}"

        if comando:
            # Sanitizar
            comando = comando.strip()
            comando = re.sub(r"^[\s'\"`]+|[\s'\"`]+$", "", comando)
            comando = comando.replace("/.", "/").strip()

            # Confirmación para /borrar_todo
            if comando.startswith("/borrar_todo") and "confirmar" not in comando:
                msg = "⚠️ ¿Seguro que deseas borrar TODA la agenda? Responde con '/borrar_todo confirmar'."
                if prefer_audio: enviar_audio(chat_id, msg)
                else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
                return jsonify({"ok": True})

            # Consultar Orbis (modo JSON; si Orbis viejo, devolverá 'respuesta' texto)
            r = requests.post(ORBIS_API, json={"texto": comando, "chat_id": chat_id, "modo": "json"})
            try:
                datos_orbis = r.json()
            except Exception:
                datos_orbis = {"ok": False, "error": "respuesta_no_json"}

            print(f"📦 Datos de Orbis: {datos_orbis}", flush=True)

            # Guardar última agenda
            if isinstance(datos_orbis, dict) and datos_orbis.get("ok") and datos_orbis.get("items"):
                ULTIMA_AGENDA[chat_id] = datos_orbis["items"]

            # Redacción natural (segunda pasada GPT)
            contenido_json = json.dumps(datos_orbis, ensure_ascii=False) if isinstance(datos_orbis, dict) else str(datos_orbis)
            respuesta_natural = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": ("Eres el asistente de Doctor Mesa. Redacta en lenguaje natural, claro y breve, "
                                                   "usando SOLO los datos de Orbis. No inventes.")},
                    {"role": "user", "content": f"Mensaje del usuario: {orden}"},
                    {"role": "user", "content": f"Datos de Orbis (JSON o texto): {contenido_json}"}
                ]
            )
            texto_final = respuesta_natural.choices[0].message.content.strip()

            if prefer_audio: enviar_audio(chat_id, texto_final)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})

        else:
            # 5) No es agenda → GPT conversa normal (y puede proponer plan y luego ofrecer agendar)
            if prefer_audio:
                enviar_audio(chat_id, interpretacion)
            else:
                requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": interpretacion})

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

    # ✅ Global ANTES de asignar
    global LAST_CHAT_ID
    LAST_CHAT_ID = chat_id

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

# =========================
# Healthcheck
# =========================
@app.route("/ping", methods=["GET"])
def ping():
    return "✅ BridgeBot activo en Render"

# =========================
# Scheduler de alertas
# =========================
def revisar_agenda_y_enviar_alertas():
    """
    Consulta a Orbis si hay eventos próximos y manda recordatorios por Telegram (audio).
    """
    try:
        # Si aún no tenemos un chat_id de Telegram, no intentamos notificar
        if LAST_CHAT_ID is None:
            return

        # Pedimos próximos eventos a Orbis y le pasamos el chat_id
        r = requests.post(ORBIS_API, json={"texto": "/proximos", "chat_id": LAST_CHAT_ID})
        if r.status_code != 200:
            print("⚠️ Orbis no respondió correctamente", flush=True)
            return

        eventos = r.json().get("eventos", [])
        for ev in eventos:
            chat_id = ev.get("chat_id") or LAST_CHAT_ID
            mensaje = ev.get("mensaje") or ev.get("texto")
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
