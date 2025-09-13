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
# MEMORIA
# =========================
# Recordar el último chat que habló con el bot (para alarmas)
LAST_CHAT_ID = None
# Memoria de la última agenda listada por chat (para "borra esa")
ULTIMA_AGENDA = {}

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

# =========================
# INTÉRPRETE (GPT)
# =========================
def consultar_mesa_gpt(texto: str) -> str:
    """
    Interpreta el mensaje del usuario. Si es agenda, convierte a comandos para Orbis.
    Si el usuario ya envía un comando (/algo), lo devuelve tal cual.
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
                        f"Hoy es {hoy} en zona horaria America/Bogota.\n\n"
                        "⚠️ REGLAS IMPORTANTES:\n"
                        "- Nunca inventes citas ni agendas. Solo Orbis sabe la verdad.\n"
                        "- Si el usuario escribe directamente un comando (comienza con '/'), "
                        "debes responder exactamente ese mismo comando, sin agregar nada más.\n"
                        "- Si el usuario habla en lenguaje natural, tradúcelo a uno de los comandos válidos:\n"
                        "  • /agenda\n"
                        "  • /registrar YYYY-MM-DD HH:MM Tarea\n"
                        "  • /borrar YYYY-MM-DD HH:MM\n"
                        "  • /borrar_fecha YYYY-MM-DD\n"
                        "  • /borrar_todo\n"
                        "  • /buscar Nombre\n"
                        "  • /buscar_fecha YYYY-MM-DD\n"
                        "  • /cuando Nombre\n"
                        "  • /reprogramar YYYY-MM-DD HH:MM NUEVA_FECHA NUEVA_HORA\n"
                        "  • /modificar YYYY-MM-DD HH:MM Nuevo texto\n"
                        "- Nunca uses '/borrar_todo' salvo que el usuario diga explícitamente 'borra todo' o 'elimina toda la agenda'.\n"
                        "- Si el usuario dice 'borra esa', 'borra esto' o algo ambiguo, responde con '__referencia__'.\n"
                        "- Si no tienes contexto, responde '⚠️ No estoy seguro a qué cita te refieres'.\n\n"
                        "Ejemplos:\n"
                        "Usuario: '¿Tengo cita con Juan?'\n"
                        "Respuesta: '/buscar Juan'\n\n"
                        "Usuario: 'Muéstrame la agenda de mañana'\n"
                        "Respuesta: '/buscar_fecha 2025-09-13'\n\n"
                        "Usuario: '/agenda'\n"
                        "Respuesta: '/agenda'\n"
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
# Texto → Voz (gTTS) y envío
# =========================
def preparar_texto_para_audio(texto: str) -> str:
    """
    Limpia signos y formatea fechas/horas para que suene natural.
    """
    limpio = re.sub(r"[→←↑↓➜➡️⬅️➤➔•·\-\*_,\.]", " ", texto)

    def convertir_fecha(m):
        dia, mes, anio = int(m.group(1)), int(m.group(2)), int(m.group(3))
        meses = [
            "enero","febrero","marzo","abril","mayo","junio",
            "julio","agosto","septiembre","octubre","noviembre","diciembre"
        ]
        return f"{dia} de {meses[mes-1]} de {anio}"
    limpio = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", convertir_fecha, limpio)

    limpio = re.sub(
        r"\b(\d{1,2})/(\d{1,2})\b",
        lambda m: f"{int(m.group(1))} de "
                  f"{['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'][int(m.group(2))-1]}",
        limpio
    )

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
    limpio = re.sub(r"\s+", " ", limpio)
    return limpio.strip()

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

        # 1) Interpretar con GPT (cerebro)
        respuesta_mesa = consultar_mesa_gpt(orden)
        print(f"🤖 MesaGPT interpretó: {orden} → {respuesta_mesa}", flush=True)

        # 2) Resolver comandos / referencias
        comando = None
        if respuesta_mesa.startswith("/"):
            comando = respuesta_mesa.strip()
        elif respuesta_mesa == "__referencia__":
            citas = ULTIMA_AGENDA.get(chat_id, [])
            if citas:
                primera = citas[0]  # Heurística inicial: primera de la última agenda listada
                comando = f"/borrar {primera['fecha']} {primera['hora']}"
            else:
                msg = "⚠️ No tengo registrada una agenda reciente para saber qué borrar. Pídeme primero 'muéstrame la agenda'."
                if prefer_audio:
                    enviar_audio(chat_id, msg)
                else:
                    requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
                return jsonify({"ok": True})
        else:
            m = re.search(r"(/[\w_]+.*)", respuesta_mesa)
            if m:
                comando = m.group(1).strip()

        # 3) Confirmación explícita para /borrar_todo
        if comando and comando.startswith("/borrar_todo") and "confirmar" not in comando:
            msg = "⚠️ ¿Seguro que deseas borrar TODA la agenda? Responde con '/borrar_todo confirmar'."
            if prefer_audio:
                enviar_audio(chat_id, msg)
            else:
                requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
            return jsonify({"ok": True})

        # 4) Si hay comando → consultar Orbis en modo JSON y GPT compone la respuesta
        if comando:
            # Sanitizar comillas/espacios raros al inicio/fin (evita /buscar_fecha 2025-09-13')
            comando = re.sub(r"^[\s'\"`]+|[\s'\"`]+$", "", comando).strip()

            r = requests.post(ORBIS_API, json={"texto": comando, "chat_id": chat_id, "modo": "json"})
            try:
                data_orbis = r.json()
            except Exception:
                data_orbis = {"ok": False, "error": "respuesta_no_json"}

            print(f"📦 Datos de Orbis: {data_orbis}", flush=True)

            texto_final = ""
            if not data_orbis.get("ok"):
                # GPT traduce el error sin inventar
                op = data_orbis.get("op")
                err = data_orbis.get("error", "error_desconocido")
                if op == "borrar" and err == "no_encontrado":
                    texto_final = "No encontré una cita con esa fecha y hora para borrar."
                elif op == "reprogramar" and err == "no_encontrado":
                    texto_final = "No pude reprogramar porque no hallé la cita original."
                elif op == "modificar" and err == "no_encontrado":
                    texto_final = "No pude modificar: no existe una cita en esa fecha y hora."
                else:
                    texto_final = "Ocurrió un problema con la agenda. Intenta de nuevo."
            else:
                op = data_orbis.get("op")

                # Listados: agenda / buscar_fecha / buscar
                if op in ("agenda", "buscar_fecha", "buscar"):
                    items = data_orbis.get("items", [])
                    # Guardar última agenda (para “borra esa”)
                    if items:
                        ULTIMA_AGENDA[chat_id] = items

                    if not items:
                        if op == "agenda":
                            texto_final = "No tienes citas en tu agenda."
                        elif op == "buscar_fecha":
                            fecha = data_orbis.get("fecha")
                            texto_final = f"No tienes citas el {fecha}."
                        else:
                            q = data_orbis.get("q", "")
                            texto_final = f"No encontré citas que contengan “{q}”."
                    else:
                        if op == "buscar_fecha":
                            fecha = data_orbis.get("fecha")
                            encabezado = f"Estas son tus citas del {fecha}:"
                        elif op == "buscar":
                            q = data_orbis.get("q", "")
                            encabezado = f"Encontré estas citas relacionadas con “{q}”:"
                        else:
                            encabezado = "Esta es tu agenda:"
                        filas = [f"- {it['hora']} → {it['texto']}" for it in items]
                        texto_final = f"{encabezado}\n" + "\n".join(filas)

                elif op == "registrar":
                    it = data_orbis.get("item", {})
                    texto_final = f"Anotado: {it.get('texto','(sin texto)')} el {it.get('fecha')} a las {it.get('hora')}."

                elif op == "borrar":
                    d = data_orbis.get("deleted", {})
                    texto_final = f"Eliminé la cita de las {d.get('hora')} del {d.get('fecha')}: {d.get('texto')}."

                elif op == "borrar_fecha":
                    cnt = data_orbis.get("count", 0)
                    texto_final = "No había citas para esa fecha." if cnt == 0 else f"Listo: eliminé {cnt} cita(s) de ese día."

                elif op == "borrar_todo":
                    cnt = data_orbis.get("count", 0)
                    texto_final = "Tu agenda ya estaba vacía." if cnt == 0 else f"Se borró toda la agenda ({cnt} cita(s))."

                elif op == "reprogramar":
                    viejo = data_orbis.get("from", "")
                    nuevo = data_orbis.get("to", "")
                    texto = data_orbis.get("texto", "")
                    texto_final = f"Reprogramé “{texto}” de {viejo} a {nuevo}."

                elif op == "modificar":
                    it = data_orbis.get("item", {})
                    texto_final = f"Actualicé la cita del {it.get('fecha')} {it.get('hora')}: {it.get('texto')}."

                elif op == "cuando":
                    fechas = data_orbis.get("fechas", [])
                    q = data_orbis.get("q", "")
                    if fechas:
                        texto_final = f"Tienes con {q} en: " + ", ".join(fechas)
                    else:
                        texto_final = f"No tienes cita con {q}."

                elif op == "proximos":
                    eventos = data_orbis.get("eventos", [])
                    if not eventos:
                        texto_final = "No hay eventos inmediatos en los próximos minutos."
                    else:
                        filas = [f"- {ev['hora']} → {ev['texto']}" for ev in eventos]
                        texto_final = "Próximos eventos:\n" + "\n".join(filas)

                else:
                    texto_final = "He procesado la solicitud."

            # 5) Entregar respuesta SIEMPRE desde GPT (texto o audio)
            if prefer_audio:
                enviar_audio(chat_id, texto_final)
            else:
                requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})

        # 6) No era agenda → responder como chat normal
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

    # 🔴 Recordar el último chat_id para alarmas del scheduler
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
