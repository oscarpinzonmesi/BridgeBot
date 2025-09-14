import os
import json
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
LAST_CHAT_ID = None          # último chat que habló (para alarmas)
ULTIMA_AGENDA = {}           # cache de la última lista de agenda por chat
PENDIENTE = {}               # confirmaciones pendientes

# =========================
# CONFIG
# =========================
BRIDGE_TOKEN   = os.getenv("TELEGRAM_TOKEN")
ORBIS_API      = os.getenv("ORBIS_API")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

TELEGRAM_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}"
BRIDGE_API   = f"{TELEGRAM_API}/sendMessage"

client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# TIEMPO (Bogotá)
# =========================
def ahora_bogota():
    return datetime.now(timezone.utc) - timedelta(hours=5)

def fecha_bogota(delta_dias=0) -> str:
    return (ahora_bogota() + timedelta(days=delta_dias)).strftime("%Y-%m-%d")

# =========================
# UTILIDADES TEXTO
# =========================
def es_si(texto: str) -> bool:
    t = (texto or "").lower()
    return bool(re.search(r"\b(s[ií]|claro|ok|dale|h[áa]gale|de una|correcto|afirmativo|por favor|sí)\b", t))

def es_no(texto: str) -> bool:
    t = (texto or "").lower()
    return bool(re.search(r"\bno\b", t))

def normalizar_manjana(texto: str) -> str:
    t = texto or ""
    t = re.sub(r"\bmanana\b", "mañana", t, flags=re.IGNORECASE)
    t = re.sub(r"\bmañan\b", "mañana", t, flags=re.IGNORECASE)
    t = re.sub(r"\bmañna\b", "mañana", t, flags=re.IGNORECASE)
    return t

MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12
}

# =========================
# ORBIS (HTTP)
# =========================
def _llamar_orbis(texto, chat_id, modo="json", timeout_s=12, reintentos=1):
    for intento in range(reintentos + 1):
        try:
            r = requests.post(
                ORBIS_API,
                json={"texto": texto, "chat_id": chat_id, "modo": modo},
                timeout=timeout_s
            )
            return r.json()
        except requests.exceptions.Timeout:
            if intento < reintentos:
                time.sleep(1.5)
                continue
            return {"ok": False, "error": "timeout_orbis"}
        except Exception:
            if intento < reintentos:
                time.sleep(1.0)
                continue
            return {"ok": False, "error": "respuesta_no_json"}

# =========================
# FECHAS RELATIVAS
# =========================
def _fechas_proxima_semana_bogota():
    """Lista de 7 YYYY-MM-DD (lun-dom) para la próxima semana en Bogotá."""
    base = ahora_bogota().date()
    wd = base.weekday()  # lunes=0
    dias_hasta_prox_lunes = (7 - wd) % 7
    if dias_hasta_prox_lunes == 0:
        dias_hasta_prox_lunes = 7
    lunes = base + timedelta(days=dias_hasta_prox_lunes)
    return [(lunes + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

def _inferir_fecha_dia(dia: int, chat_id: int | str) -> str | None:
    """Intenta inferir YYYY-MM a partir de ULTIMA_AGENDA o mes actual (Bogotá)."""
    items = ULTIMA_AGENDA.get(chat_id) or []
    for it in items:
        try:
            yyyy, mm, dd = it["fecha"].split("-")
            if int(dd) == dia:
                return f"{yyyy}-{mm}-{str(dia).zfill(2)}"
        except Exception:
            continue
    base = ahora_bogota()
    return f"{base.year}-{str(base.month).zfill(2)}-{str(dia).zfill(2)}"

# =========================
# ATAJOS DETECCIÓN
# =========================
def detectar_atajo_comando(texto: str) -> str | None:
    t = (texto or "").lower()
    if re.search(r"\b(borra(r)?|elimina(r)?|quita(r)?|suprime(r)?|limpia(r)?)\b", t):
        return None
    if re.search(r"\b(agenda|citas?)\b", t) and re.search(r"\b(tod[oa]s?|completa|general)\b", t):
        return "/agenda"
    if (re.search(r"\b(qué|que|cuál|cual|dime|mu[eé]strame|muestrame|lista|ens[eé]ñame|enseñame)\b", t)
        and re.search(r"\b(agenda|citas?)\b", t)):
        if re.search(r"\b(hoy|mañana|semana|mes|pr[oó]xim[ao]s?)\b", t):
            return None
        return "/agenda"
    if re.fullmatch(r"\s*(toda|todas|todo|agenda(?:\s+(completa|general))?)\s*", t):
        return "/agenda"
    return None

def detectar_borrar_todo(texto: str) -> bool:
    t = (texto or "").lower()
    if re.search(r"\b(borra(r)?|elimina(r)?|limpia(r)?)\b", t) and re.search(r"\b(tod[oa]s?|agenda|citas?)\b", t):
        return True
    if re.search(r"\b(borra|elimina)\s+tod[oa]s?\s+(la\s+)?(agenda|citas?)\b", t):
        return True
    return False

# =========================
# PARSERS ES
# =========================
def _parsear_fecha_es(texto: str) -> str | None:
    t = normalizar_manjana((texto or "").lower().strip())

    # Hoy / Mañana
    if re.search(r"\bhoy\b", t):
        return fecha_bogota(0)
    if re.search(r"\bmañana\b", t):
        return fecha_bogota(1)

    # "15 de este mes"
    m = re.search(r"\b(\d{1,2})\s*(?:de\s+)?este\s+mes\b", t)
    if m:
        d = int(m.group(1))
        base = ahora_bogota()
        return f"{base.year}-{base.month:02d}-{d:02d}"

    # ISO
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # dd/mm o dd-mm
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", t)
    if m:
        d = int(m.group(1)); mth = int(m.group(2))
        yyyy = ahora_bogota().year
        return f"{yyyy}-{str(mth).zfill(2)}-{str(d).zfill(2)}"

    # "10 de septiembre"
    m = re.search(r"\b(\d{1,2})\s*(?:de\s+)?([a-záéíóúüñ]+)\b", t)
    if m:
        d = int(m.group(1))
        mes_nombre = {"setiembre": "septiembre"}.get(m.group(2), m.group(2))
        if mes_nombre in MESES_ES:
            yyyy = ahora_bogota().year
            mm = MESES_ES[mes_nombre]
            return f"{yyyy}-{str(mm).zfill(2)}-{str(d).zfill(2)}"

    return None

def _parsear_hora_es(texto: str) -> str | None:
    t = (texto or "").lower()

    if re.search(r"\bmediod[ií]a\b", t):
        return "12:00"
    if re.search(r"\bmedianoche\b", t):
        return "00:00"

    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", t)
    if m:
        h = int(m.group(1)); mnt = int(m.group(2)); suf = (m.group(3) or "").lower()
        if suf == "am":
            if h == 12: h = 0
        elif suf == "pm":
            if h != 12: h += 12
        return f"{h:02d}:{mnt:02d}"

    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)); suf = m.group(2).lower()
        if suf == "am":
            if h == 12: h = 0
        elif suf == "pm":
            if h != 12: h += 12
        return f"{h:02d}:00"

    m = re.search(r"(?:a\s+las\s+)?(\d{1,2})(?:\s*y\s*(\d{1,2}))?\s+de\s+la\s+(mañana|tarde|noche)\b", t)
    if m:
        h = int(m.group(1)); mnt = int(m.group(2) or 0); tramo = m.group(3)
        if tramo == "mañana":
            if h == 12: h = 0
        elif tramo in ("tarde", "noche"):
            if h != 12: h += 12
        return f"{h:02d}:{mnt:02d}"

    return None

# =========================
# SELECCIÓN DESDE CONTEXTO
# =========================
def _extraer_indice(texto: str) -> int | None:
    t = (texto or "").lower()
    m = re.search(r"\b(\d{1,2})\s*[\)\.]", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    for k, v in {"primera":1,"segunda":2,"tercera":3,"cuarta":4,"quinta":5}.items():
        if k in t:
            return v
    return None

def _seleccionar_item_desde_contexto(chat_id: int | str, texto: str):
    items = ULTIMA_AGENDA.get(chat_id) or []
    if not items:
        return None

    q = re.search(r"[\"“”'‘’](.+?)[\"“”'‘’]", texto)
    if q:
        frag = q.group(1).strip().lower()
        for it in items:
            if frag and frag in (it.get("texto") or "").lower():
                return it

    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", texto)
    if m:
        hhmm = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
        for it in items:
            if (it.get("hora") or "") == hhmm:
                return it

    idx = _extraer_indice(texto)
    if idx and 1 <= idx <= len(items):
        return items[idx - 1]

    return items[0]

# =========================
# COMANDOS EN TEXTO
# =========================
def _sanitizar_comando_capturado(raw: str) -> str:
    if not raw:
        return ""
    m = re.search(
        r"/\s*(agenda|registrar|borrar(?:_fecha|_todo)?|buscar(?:_fecha)?|cuando|reprogramar|modificar)\b[^\n`]*",
        raw,
        flags=re.IGNORECASE
    )
    cmd = m.group(0) if m else raw.strip()
    cmd = cmd.replace("```", " ").replace("`", " ").strip()
    cmd = re.sub(r"\s+", " ", cmd)
    cmd = cmd.replace("/.", "/").strip()
    cmd = re.sub(r"^/\s+", "/", cmd)
    cmd = re.sub(r"(?<=/)\s+(?=\w)", "", cmd)
    return cmd

def _parsear_lineas_a_items(texto: str):
    items = []
    if not isinstance(texto, str):
        return items
    for linea in texto.splitlines():
        m = re.match(r"\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*→\s*(.+)\s*$", linea)
        if m:
            items.append({"fecha": m.group(1), "hora": m.group(2), "texto": m.group(3)})
    return items

# =========================
# GPT INTÉRPRETE
# =========================
def consultar_mesa_gpt(texto: str) -> str:
    try:
        hoy = ahora_bogota().strftime("%Y-%m-%d")
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Eres MesaGPT, el asistente personal de Doctor Mesa.\n"
                        f"Hoy es {hoy} (America/Bogota).\n\n"
                        "OBJETIVO:\n"
                        "- Eres el CEREBRO. Orbis es el CUADERNO/agenda.\n"
                        "- Si el mensaje es de AGENDA, responde SOLO con un comando válido para Orbis.\n"
                        "- Si NO es de agenda, conversa normal.\n"
                        "- Nunca digas 'no tengo acceso a tu agenda'. Si te piden ver citas/agenda, devuelve el comando adecuado.\n\n"
                        "COMANDOS VÁLIDOS:\n"
                        "/agenda\n"
                        "/registrar YYYY-MM-DD HH:MM Tarea\n"
                        "/borrar YYYY-MM-DD HH:MM\n"
                        "/borrar_fecha YYYY-MM-DD\n"
                        "/borrar_todo\n"
                        "/buscar Nombre\n"
                        "/buscar_fecha YYYY-MM-DD\n"
                        "/cuando Nombre\n"
                        "/reprogramar YYYY-MM-DD HH:MM NUEVA_FECHA NUEVA_HORA\n"
                        "/modificar YYYY-MM-DD HH:MM Nuevo texto\n"
                        "- Si el usuario escribe '/', repite EXACTAMENTE ese comando.\n"
                        "- 'mañana' = hoy+1; 'hoy' = hoy.\n"
                        "- 'No estoy seguro a qué cita te refieres' SOLO si quiere borrar/modificar sin contexto.\n\n"
                        "ATAJOS → COMANDO:\n"
                        "- 'todas las citas', 'agenda general', 'agenda completa' → /agenda\n"
                        "- 'qué tengo mañana' → /buscar_fecha YYYY-MM-DD (mañana)\n"
                        "- 'qué tengo hoy' → /buscar_fecha YYYY-MM-DD (hoy)\n"
                    )
                },
                {"role": "user", "content": texto}
            ]
        )
        return respuesta.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Error consultando a MesaGPT:", str(e), flush=True)
        return "Lo siento, tuve un problema interpretando el mensaje."

# =========================
# DESCARGA & TRANSCRIPCIÓN
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
# TTS (gTTS)
# =========================
def preparar_texto_para_audio(texto: str) -> str:
    limpio = re.sub(r"[^A-Za-zÁÉÍÓÚÜáéíóúüÑñ0-9\s/.:]", " ", texto or "")
    limpio = re.sub(r"[()→←↑↓➜➡️⬅️➤➔•·_\-\*=\[\]{}<>|#%~\"']", " ", limpio)

    def _mes(n):
        return ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"][n-1]

    limpio = re.sub(
        r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
        lambda m: f"{int(m.group(1))} de {_mes(int(m.group(2)))} de {m.group(3)}",
        limpio
    )
    limpio = re.sub(
        r"\b(\d{1,2})/(\d{1,2})\b",
        lambda m: f"{int(m.group(1))} de {_mes(int(m.group(2)))}",
        limpio
    )

    def conv_hora(m):
        h = int(m.group(1)); mnt = int(m.group(2))
        if h == 0:
            h12, suf = 12, "de la noche"
        elif h < 12:
            h12, suf = h, "de la mañana"
        elif h == 12:
            h12, suf = 12, "del mediodía"
        elif h < 19:
            h12, suf = h - 12, "de la tarde"
        else:
            h12, suf = h - 12, "de la noche"
        if mnt == 0:
            return f"{h12} en punto {suf}"
        elif mnt < 10:
            return f"{h12} y {mnt} {suf}"
        else:
            return f"{h12} {mnt} {suf}"

    limpio = re.sub(r"\b(\d{1,2})[:.](\d{2})\b", conv_hora, limpio)
    limpio = re.sub(r"[,:;.\-]{2,}", " ", limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()
    return limpio

def enviar_audio(chat_id: int | str, texto: str):
    try:
        texto_para_leer = preparar_texto_para_audio(texto)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            mp3_path = Path(tmp.name)
        gTTS(text=texto_para_leer, lang="es").save(str(mp3_path))
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
    finally:
        try:
            if mp3_path and mp3_path.exists():
                mp3_path.unlink()
        except Exception:
            pass

# =========================
# ALARMAS (desde Orbis)
# =========================
def enviar_alarma(chat_id: int | str, mensaje: str, prefer_audio: bool = False):
    try:
        if prefer_audio:
            enviar_audio(chat_id, f"⏰ Recordatorio: {mensaje}")
        else:
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": f"⏰ Recordatorio: {mensaje}"})
        print(f"✅ Alarma enviada a {chat_id}: {mensaje}", flush=True)
    except Exception as e:
        print("❌ Error enviando alarma:", str(e), flush=True)

# =========================
# ENDPOINT PRINCIPAL
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
        orden = normalizar_manjana(orden)
        txt_low = orden.lower()

        # Preferencias de salida
        if any(k in txt_low for k in [" en audio", "nota de voz", "mensaje de voz"]):
            prefer_audio = True
        if " en texto" in txt_low:
            prefer_audio = False

        # 1) Confirmaciones pendientes
        if chat_id in PENDIENTE:
            pend = PENDIENTE[chat_id]
            if es_si(txt_low):
                if pend.get("tipo") == "buscar_fecha" and pend.get("fecha") == "manana":
                    comando = f"/buscar_fecha {fecha_bogota(1)}"
                elif pend.get("tipo") == "buscar_fecha" and pend.get("fecha") == "hoy":
                    comando = f"/buscar_fecha {fecha_bogota(0)}"
                elif pend.get("tipo") == "borrar_todo":
                    comando = "/borrar_todo confirmar"
                else:
                    comando = pend.get("comando")
                PENDIENTE.pop(chat_id, None)

                datos_orbis = _llamar_orbis(comando, chat_id, "json", timeout_s=12, reintentos=1)
                print(f"📦 Datos de Orbis (confirmado): {datos_orbis}", flush=True)

                if isinstance(datos_orbis, dict) and datos_orbis.get("ok"):
                    if datos_orbis.get("items"):
                        ULTIMA_AGENDA[chat_id] = datos_orbis["items"]
                    if datos_orbis.get("op") in {"borrar_todo", "borrar_fecha", "borrar"}:
                        ULTIMA_AGENDA[chat_id] = []
                elif isinstance(datos_orbis, dict) and isinstance(datos_orbis.get("respuesta"), str):
                    parsed = _parsear_lineas_a_items(datos_orbis["respuesta"])
                    if parsed: ULTIMA_AGENDA[chat_id] = parsed

                contenido_json = json.dumps(datos_orbis, ensure_ascii=False) if isinstance(datos_orbis, dict) else str(datos_orbis)
                respuesta_natural = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": ("Eres el asistente de Doctor Mesa. Redacta en lenguaje natural, claro y breve, usando SOLO los datos de Orbis. No inventes. Si no hay citas, dilo.")},
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
            # si no es sí/no, continuamos sin borrar PENDIENTE

        # 2-a) Borrar TODO (atajo con confirmación)
        if detectar_borrar_todo(txt_low):
            PENDIENTE[chat_id] = {"tipo": "borrar_todo"}
            msg = "⚠️ ¿Seguro que deseas borrar TODA la agenda? Responde con 'sí' para confirmar."
            if prefer_audio: enviar_audio(chat_id, msg)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
            return jsonify({"ok": True})

        # 2-b) Borrados específicos / por fecha / por contexto
        if re.search(r"\b(borra(?:r)?|[ae]limina(?:r)?|quita(?:r)?|suprime(?:r)?|borre)\b", txt_low):
            comando = None
            fecha_det = _parsear_fecha_es(txt_low)
            hora_det  = _parsear_hora_es(txt_low)

            if fecha_det and hora_det:
                comando = f"/borrar {fecha_det} {hora_det}"
            elif fecha_det:
                comando = f"/borrar_fecha {fecha_det}"
            else:
                m = re.search(r"\btodas?\s+las\s+del\s+(\d{1,2})\b", txt_low)
                if m:
                    dia = int(m.group(1))
                    fecha_inf = _inferir_fecha_dia(dia, chat_id)
                    PENDIENTE[chat_id] = {"tipo": "borrar_fecha", "fecha_propuesta": fecha_inf, "comando": f"/borrar_fecha {fecha_inf}"}
                    msg = f"¿Borro todas las citas del {fecha_inf} en Orbis?"
                    if prefer_audio: enviar_audio(chat_id, msg)
                    else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
                    return jsonify({"ok": True})
                target = _seleccionar_item_desde_contexto(chat_id, orden)
                if target:
                    comando = f"/borrar {target['fecha']} {target['hora']}"

            if comando:
                datos_orbis = _llamar_orbis(comando, chat_id, "json", timeout_s=12, reintentos=1)
                print(f"📦 Datos de Orbis (atajo borrar): {datos_orbis}", flush=True)

                if isinstance(datos_orbis, dict) and datos_orbis.get("ok"):
                    if datos_orbis.get("items"):
                        ULTIMA_AGENDA[chat_id] = datos_orbis["items"]
                    if datos_orbis.get("op") in {"borrar_todo", "borrar_fecha", "borrar"}:
                        ULTIMA_AGENDA[chat_id] = []
                elif isinstance(datos_orbis, dict) and isinstance(datos_orbis.get("respuesta"), str):
                    parsed = _parsear_lineas_a_items(datos_orbis["respuesta"])
                    if parsed: ULTIMA_AGENDA[chat_id] = parsed

                contenido_json = json.dumps(datos_orbis, ensure_ascii=False) if isinstance(datos_orbis, dict) else str(datos_orbis)
                respuesta_natural = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": ("Eres el asistente de Doctor Mesa. Redacta claro y breve usando SOLO los datos de Orbis. No inventes.")},
                        {"role": "user", "content": f"Mensaje del usuario: {orden}"},
                        {"role": "user", "content": f"Datos de Orbis (JSON o texto): {contenido_json}"}
                    ]
                )
                texto_final = respuesta_natural.choices[0].message.content.strip()
                if prefer_audio: enviar_audio(chat_id, texto_final)
                else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})
                return jsonify({"ok": True})

        # 2-c) Verificaciones rápidas: "¿ya borraste todo?" / "qué agenda hay?"
        if (re.search(r"\bya\s+borra(?:ste|ron)\b.*\b(todo|agenda|citas?)\b", txt_low)
            or re.search(r"\bqued[óo]\s+borrad[ao]\b.*\b(agenda|citas?)\b", txt_low)
            or re.search(r"\b(qué|que|cu[aá]l|cual)\s+agenda\s+(hay|queda|qued[óo])\b", txt_low)):
            datos_orbis = _llamar_orbis("/agenda", chat_id, "json", timeout_s=12, reintentos=1)
            if isinstance(datos_orbis, dict) and datos_orbis.get("ok"):
                items = datos_orbis.get("items") or []
                ULTIMA_AGENDA[chat_id] = items
                if not items:
                    msg = "La agenda está vacía ahora mismo."
                else:
                    primeras = "\n".join(f"- {it['fecha']} {it['hora']}: {it['texto']}" for it in items[:5])
                    extra = f"\n(y {len(items)-5} más...)" if len(items) > 5 else ""
                    msg = f"Tienes {len(items)} citas en la agenda:\n{primeras}{extra}"
            else:
                msg = "No pude verificar la agenda en Orbis ahora mismo. ¿Intento de nuevo?"
            if prefer_audio: enviar_audio(chat_id, msg)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
            return jsonify({"ok": True})

        # 2-d) "mañana ..." → consultar directo sin pedir confirmación
        if re.search(r"\bmañana\b", txt_low) and re.search(r"\b(agenda|citas?|tengo|hay|qu[eé])\b", txt_low):
            fecha = fecha_bogota(1)
            datos_orbis = _llamar_orbis(f"/buscar_fecha {fecha}", chat_id, "json", timeout_s=12, reintentos=1)
            print(f"📦 Datos de Orbis (mañana directo): {datos_orbis}", flush=True)
            if isinstance(datos_orbis, dict) and datos_orbis.get("ok"):
                items = datos_orbis.get("items") or []
                ULTIMA_AGENDA[chat_id] = items
                if not items:
                    msg = f"Mañana ({fecha}) no tienes citas en Orbis."
                else:
                    lista = "\n".join(f"- {it['hora']}: {it['texto']}" for it in items)
                    msg = f"Para mañana {fecha} tienes:\n{lista}"
            else:
                msg = "No pude consultar Orbis ahora mismo. ¿Intento de nuevo?"
            if prefer_audio: enviar_audio(chat_id, msg)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
            return jsonify({"ok": True})

        # 2-e) Próxima/otra semana → agregación de 7 días
        if re.search(r"\b(pr[óo]xima|otra)\s+semana\b", txt_low):
            fechas = _fechas_proxima_semana_bogota()
            agregados = []
            for f in fechas:
                datos = _llamar_orbis(f"/buscar_fecha {f}", chat_id, "json", timeout_s=12, reintentos=1)
                if isinstance(datos, dict) and datos.get("ok"):
                    agregados.extend(datos.get("items") or [])
            ULTIMA_AGENDA[chat_id] = agregados
            if not agregados:
                msg = f"La próxima semana ({fechas[0]} a {fechas[-1]}) no tienes citas en Orbis."
            else:
                por_dia = {}
                for it in agregados:
                    por_dia.setdefault(it["fecha"], []).append(it)
                partes = []
                for f in fechas:
                    lst = por_dia.get(f, [])
                    if lst:
                        cuerpo = "\n".join(f"  - {x['hora']}: {x['texto']}" for x in lst)
                        partes.append(f"{f}:\n{cuerpo}")
                msg = "Agenda de la próxima semana:\n" + "\n".join(partes)
            if prefer_audio: enviar_audio(chat_id, msg)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
            return jsonify({"ok": True})

        # 2-f) Programar "en N minutos/horas ..."
        m = re.search(r"\ben\s+(\d{1,3})\s*(minutos|min)\b", txt_low)
        h = re.search(r"\ben\s+(\d{1,2})\s*horas?\b", txt_low)
        if m or h:
            add_minutes = int(m.group(1)) if m else int(h.group(1)) * 60
            dt = ahora_bogota() + timedelta(minutes=add_minutes)
            fecha_str = dt.strftime("%Y-%m-%d")
            hora_str  = dt.strftime("%H:%M")
            # Intentar extraer motivo después de "para ..."
            desc_match = re.search(r"\bpara\s+(.+)", orden, flags=re.IGNORECASE)
            descripcion = desc_match.group(1).strip() if desc_match else "Recordatorio"
            comando = f"/registrar {fecha_str} {hora_str} {descripcion}"
            datos_orbis = _llamar_orbis(comando, chat_id, "json", timeout_s=12, reintentos=1)
            print(f"📦 Datos de Orbis (en N minutos/horas): {datos_orbis}", flush=True)
            if isinstance(datos_orbis, dict) and datos_orbis.get("ok"):
                msg = f"Listo. Programé '{descripcion}' para {fecha_str} a las {hora_str}."
            else:
                msg = "No pude programarlo en Orbis ahora mismo."
            if prefer_audio: enviar_audio(chat_id, msg)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
            return jsonify({"ok": True})

        # 2-g) Atajo /agenda
        cmd_atajo = detectar_atajo_comando(txt_low)
        if cmd_atajo:
            datos_orbis = _llamar_orbis(cmd_atajo, chat_id, "json", timeout_s=12, reintentos=1)
            print(f"📦 Datos de Orbis (atajo /agenda): {datos_orbis}", flush=True)
            if isinstance(datos_orbis, dict) and datos_orbis.get("ok"):
                if datos_orbis.get("items"): ULTIMA_AGENDA[chat_id] = datos_orbis["items"]
                elif isinstance(datos_orbis.get("respuesta"), str):
                    parsed = _parsear_lineas_a_items(datos_orbis["respuesta"])
                    if parsed: ULTIMA_AGENDA[chat_id] = parsed
            contenido_json = json.dumps(datos_orbis, ensure_ascii=False) if isinstance(datos_orbis, dict) else str(datos_orbis)
            respuesta_natural = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": ("Eres el asistente de Doctor Mesa. Redacta claro y breve usando SOLO los datos de Orbis. No inventes.")},
                    {"role": "user", "content": f"Mensaje del usuario: {orden}"},
                    {"role": "user", "content": f"Datos de Orbis (JSON o texto): {contenido_json}"}
                ]
            )
            texto_final = respuesta_natural.choices[0].message.content.strip()
            if prefer_audio: enviar_audio(chat_id, texto_final)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})
            return jsonify({"ok": True})

        # 2-h) Reprogramar / modificar usando contexto + nueva fecha/hora
        if re.search(r"\b(modifica|midifica|modificar|cambia|cambiar|reprograma|reprogramar|mueve|mover|cámbiala|cambiala)\b", txt_low):
            target = _seleccionar_item_desde_contexto(chat_id, orden)
            nueva_fecha = _parsear_fecha_es(txt_low)
            nueva_hora  = _parsear_hora_es(txt_low)
            if target and (nueva_fecha or nueva_hora):
                old_fecha = target["fecha"]; old_hora = target["hora"]
                nueva_fecha = nueva_fecha or old_fecha
                nueva_hora  = nueva_hora  or old_hora
                comando = f"/reprogramar {old_fecha} {old_hora} {nueva_fecha} {nueva_hora}"
                datos_orbis = _llamar_orbis(comando, chat_id, "json", timeout_s=12, reintentos=1)
                print(f"📦 Datos de Orbis (atajo reprogramar): {datos_orbis}", flush=True)
                if isinstance(datos_orbis, dict) and datos_orbis.get("ok"):
                    if datos_orbis.get("items"): ULTIMA_AGENDA[chat_id] = datos_orbis["items"]
                    elif isinstance(datos_orbis.get("respuesta"), str):
                        parsed = _parsear_lineas_a_items(datos_orbis["respuesta"])
                        if parsed: ULTIMA_AGENDA[chat_id] = parsed
                contenido_json = json.dumps(datos_orbis, ensure_ascii=False) if isinstance(datos_orbis, dict) else str(datos_orbis)
                respuesta_natural = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": ("Eres el asistente de Doctor Mesa. Redacta claro y breve usando SOLO los datos de Orbis. No inventes.")},
                        {"role": "user", "content": f"Mensaje del usuario: {orden}"},
                        {"role": "user", "content": f"Datos de Orbis (JSON o texto): {contenido_json}"}
                    ]
                )
                texto_final = respuesta_natural.choices[0].message.content.strip()
                if prefer_audio: enviar_audio(chat_id, texto_final)
                else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})
                return jsonify({"ok": True})

            msg = (f"¿Para qué fecha y hora quieres mover la cita de {target['fecha']} a las {target['hora']}?"
                   if target else
                   "Indícame qué cita (de la última lista) y la nueva fecha u hora.")
            if prefer_audio: enviar_audio(chat_id, msg)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
            return jsonify({"ok": True})

        # 3) Interpretación GPT (fallback)
        interpretacion = consultar_mesa_gpt(orden)
        print(f"🤖 MesaGPT interpretó: {orden} → {interpretacion}", flush=True)

        # Nunca respondas "no tengo acceso a tu agenda"
        if re.search(r"\bno\s+tengo\s+acceso\s+a\s+tu\s+agenda\b", interpretacion, flags=re.IGNORECASE):
            interpretacion = "Puedo revisarlo por ti. Ya mismo consulto en Orbis si lo deseas."

        # Suavizar ambigüedad para saludos
        if interpretacion.startswith("⚠️ No estoy seguro") and not re.search(r"\b(borra|borrar|modificar|reprogramar|cambiar)\b", txt_low):
            interpretacion = "¡Aquí estoy! Te escucho. ¿En qué te ayudo?"

        # 4) ¿Es comando?
        comando = None
        if interpretacion.startswith("/"):
            comando = _sanitizar_comando_capturado(interpretacion)
        else:
            m = re.search(r"(/[\w_]+(?:\s+.+)?)", interpretacion)
            if m:
                comando = _sanitizar_comando_capturado(m.group(1))

        # Corrección: si devolvió /agenda pero el usuario dijo “mañana”
        if comando and comando.startswith("/agenda") and "mañana" in txt_low:
            comando = f"/buscar_fecha {fecha_bogota(1)}"

        if comando:
            comando = comando.strip()
            comando = re.sub(r"^[\s'\"`]+|[\s'\"`]+$", "", comando)
            comando = comando.replace("/.", "/").strip()

            if comando.startswith("/borrar_todo") and "confirmar" not in comando:
                msg = "⚠️ ¿Seguro que deseas borrar TODA la agenda? Responde con '/borrar_todo confirmar'."
                if prefer_audio: enviar_audio(chat_id, msg)
                else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
                return jsonify({"ok": True})

            datos_orbis = _llamar_orbis(comando, chat_id, "json", timeout_s=12, reintentos=1)
            print(f"📦 Datos de Orbis: {datos_orbis}", flush=True)

            if isinstance(datos_orbis, dict) and datos_orbis.get("ok"):
                if datos_orbis.get("items"):
                    ULTIMA_AGENDA[chat_id] = datos_orbis["items"]
                elif isinstance(datos_orbis.get("respuesta"), str):
                    parsed = _parsear_lineas_a_items(datos_orbis["respuesta"])
                    if parsed: ULTIMA_AGENDA[chat_id] = parsed
                if datos_orbis.get("op") in {"borrar_todo", "borrar_fecha", "borrar"}:
                    ULTIMA_AGENDA[chat_id] = []

            contenido_json = json.dumps(datos_orbis, ensure_ascii=False) if isinstance(datos_orbis, dict) else str(datos_orbis)
            respuesta_natural = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": ("Eres el asistente de Doctor Mesa. Redacta en lenguaje natural, claro y breve, usando SOLO los datos de Orbis. No inventes.")},
                    {"role": "user", "content": f"Mensaje del usuario: {orden}"},
                    {"role": "user", "content": f"Datos de Orbis (JSON o texto): {contenido_json}"}
                ]
            )
            texto_final = respuesta_natural.choices[0].message.content.strip()
            if prefer_audio: enviar_audio(chat_id, texto_final)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})
        else:
            if prefer_audio: enviar_audio(chat_id, interpretacion)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": interpretacion})

    except Exception as e:
        print("❌ Error en /mesa:", str(e), flush=True)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})

# =========================
# WEBHOOK TELEGRAM
# =========================
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if "message" not in data:
        return {"ok": True}

    msg     = data["message"]
    chat_id = msg["chat"]["id"]

    global LAST_CHAT_ID
    LAST_CHAT_ID = chat_id

    if "text" in msg:
        text = msg["text"]
        print(f"📩 Telegram → Doctor (texto): {text}", flush=True)
        payload = {"chat_id": chat_id, "orden": text, "prefer_audio": False}
    elif "voice" in msg:
        file_id = msg["voice"]["file_id"]
        print(f"🎤 Telegram → Doctor (voz): {file_id}", flush=True)
        ogg_path = descargar_archivo(file_id, "voz.ogg")
        transcripcion = transcribir_audio(ogg_path) if ogg_path else ""
        print(f"📝 Transcripción: {transcripcion}", flush=True)
        payload = {"chat_id": chat_id, "orden": transcripcion or "(audio vacío)", "prefer_audio": True}
    elif "video_note" in msg:
        file_id = msg["video_note"]["file_id"]
        print(f"🎥 Telegram → Doctor (video_note): {file_id}", flush=True)
        mp4_path = descargar_archivo(file_id, "nota_video.mp4")
        transcripcion = transcribir_audio(mp4_path) if mp4_path else ""
        print(f"📝 Transcripción (video_note): {transcripcion}", flush=True)
        payload = {"chat_id": chat_id, "orden": transcripcion or "(audio vacío)", "prefer_audio": True}
    else:
        return {"ok": True}

    with app.test_request_context("/mesa", method="POST", json=payload):
        return mesa()

# =========================
# HEALTHCHECK
# =========================
@app.route("/ping", methods=["GET"])
def ping():
    return "✅ BridgeBot activo en Render"

# =========================
# SCHEDULER (recordatorios desde Orbis)
# =========================
def revisar_agenda_y_enviar_alertas():
    try:
        if LAST_CHAT_ID is None:
            return
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
    if os.getenv("ENABLE_SCHEDULER", "1") != "1":
        print("⏭️ Scheduler desactivado por ENABLE_SCHEDULER", flush=True)
        return
    schedule.every(1).minutes.do(revisar_agenda_y_enviar_alertas)
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(1)
    threading.Thread(target=run_scheduler, daemon=True).start()

# Lanzar scheduler al importar (modo gunicorn)
try:
    iniciar_scheduler()
except Exception as _e:
    print("⚠️ No se pudo iniciar el scheduler:", _e, flush=True)
