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
    t = re.sub(r"[^\wáéíóúüñ\s]", " ", texto or "").strip().lower()
    candidatos = {"si","sí","claro","ok","dale","hagale","hágale","de una","correcto","afirmativo","por favor","okay","vale"}
    return any(tok in candidatos for tok in t.split())

def es_no(texto: str) -> bool:
    t = re.sub(r"[^\wáéíóúüñ\s]", " ", texto or "").strip().lower()
    candidatos = {"no","nel","negativo","mejor no","nop","nopes"}
    return any(tok in candidatos for tok in t.split())

def normalizar_manjana(texto: str) -> str:
    # Corrige variantes comunes: 'manana', 'mañan', 'mañna', etc.
    t = texto
    t = re.sub(r"\bmanana\b", "mañana", t, flags=re.IGNORECASE)
    t = re.sub(r"\bmañan\b", "mañana", t, flags=re.IGNORECASE)
    t = re.sub(r"\bmañna\b", "mañana", t, flags=re.IGNORECASE)
    return t

MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12
}

def _inferir_fecha_dia(dia: int, chat_id: int | str) -> str | None:
    """Intenta inferir YYYY-MM a partir del contexto (ULTIMA_AGENDA) o del mes actual de Bogotá."""
    # 1) Si la última agenda listada tiene fechas, intenta encontrar alguna con ese día
    items = ULTIMA_AGENDA.get(chat_id) or []
    for it in items:
        try:
            # it: {"fecha":"YYYY-MM-DD","hora":"HH:MM","texto":"..."}
            yyyy, mm, dd = it["fecha"].split("-")
            if int(dd) == dia:
                return f"{yyyy}-{mm}-{str(dia).zfill(2)}"
        except Exception:
            continue
    # 2) Si no hay contexto, asume mes/año actuales (Bogotá)
    base = ahora_bogota()
    yyyy = base.year
    mm = base.month
    return f"{yyyy}-{str(mm).zfill(2)}-{str(dia).zfill(2)}"
def detectar_atajo_comando(texto: str) -> str | None:
    t = (texto or "").lower()
    # agenda completa / general / todas las citas
    if (re.search(r"\b(todas?|completa|general)\b.*\b(citas|agenda)\b", t)
        or re.search(r"\b(agenda|citas)\b.*\b(todas?|completa|general)\b", t)
        or re.search(r"\b(qué|que|cual|cuál|dime|muestrame|muéstrame|lista)\b.*\b(agenda|citas)\b", t)):
        return "/agenda"
    # También si el usuario insiste con “todas” a secas
    if re.fullmatch(r"\s*(todas?|agenda|agenda completa|agenda general)\s*", t):
        return "/agenda"
    return None

def _parsear_fecha_es(texto: str) -> str | None:
    """
    Extrae una fecha 'YYYY-MM-DD' desde texto en español:
    - '10 de septiembre', '10 septiembre'
    - '10/09', '10-09'
    - '2025-09-10'
    - 'hoy', 'mañana'
    Devuelve None si no detecta nada.
    """
    
    t = (texto or "").lower().strip()
    t = normalizar_manjana(t)

    # Hoy / Mañana
    if re.search(r"\bhoy\b", t):
        return fecha_bogota(0)
    if re.search(r"\bmañana\b", t):
        return fecha_bogota(1)
    
    # dentro de _parsear_fecha_es, tras hoy/mañana:
    m = re.search(r"\b(\d{1,2})\s*(?:de\s+)?este\s+mes\b", (texto or "").lower())
    if m:
        d = int(m.group(1))
        base = ahora_bogota()
        return f"{base.year}-{base.month:02d}-{d:02d}"


    # ISO directo
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # dd/mm o dd-mm (sin año)
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", t)
    if m:
        d = int(m.group(1)); mth = int(m.group(2))
        yyyy = ahora_bogota().year
        return f"{yyyy}-{str(mth).zfill(2)}-{str(d).zfill(2)}"

    # '10 de septiembre' o '10 septiembre' (sin año)
    m = re.search(r"\b(\d{1,2})\s*(?:de\s+)?([a-záéíóúüñ]+)\b", t)
    if m:
        d = int(m.group(1))
        mes_nombre = m.group(2)
        mes_nombre = {"setiembre": "septiembre"}.get(mes_nombre, mes_nombre)  # alias común
        if mes_nombre in MESES_ES:
            yyyy = ahora_bogota().year
            mm = MESES_ES[mes_nombre]
            return f"{yyyy}-{str(mm).zfill(2)}-{str(d).zfill(2)}"

    return None

def _parsear_hora_es(texto: str) -> str | None:
    """
    Devuelve HH:MM en 24h a partir de expresiones como:
    - 16:30, 4:30 pm, 4 pm
    - 4 de la tarde / 9 de la mañana / 10 de la noche
    - mediodía / medianoche
    """
    t = (texto or "").lower()

    # mediodía / medianoche
    if re.search(r"\bmediod[ií]a\b", t):
        return "12:00"
    if re.search(r"\bmedianoche\b", t):
        return "00:00"

    # HH:MM (opcional am/pm)
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", t)
    if m:
        h = int(m.group(1)); mnt = int(m.group(2)); suf = m.group(3)
        if suf == "am":
            if h == 12: h = 0
        elif suf == "pm":
            if h != 12: h += 12
        return f"{str(h).zfill(2)}:{str(mnt).zfill(2)}"

    # H am/pm
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)); suf = m.group(2)
        if suf == "am":
            if h == 12: h = 0
        elif suf == "pm":
            if h != 12: h += 12
        return f"{str(h).zfill(2)}:00"

    # "a las 4 (y 15) de la tarde/mañana/noche"
    m = re.search(r"(?:a\s+las\s+)?(\d{1,2})(?:\s*y\s*(\d{1,2}))?\s+de\s+la\s+(mañana|tarde|noche)\b", t)
    if m:
        h = int(m.group(1)); mnt = int(m.group(2) or 0); tramo = m.group(3)
        if tramo == "mañana":
            if h == 12: h = 0
        elif tramo == "tarde":
            if h != 12: h += 12
        elif tramo == "noche":
            if h != 12: h += 12
        return f"{str(h).zfill(2)}:{str(mnt).zfill(2)}"

    return None


def _extraer_indice(texto: str) -> int | None:
    """Soporta '1.', '1)', 'primera', 'segunda', ..."""
    t = (texto or "").lower()
    m = re.search(r"\b(\d{1,2})\s*[\)\.]", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    ordinales = {"primera":1, "segunda":2, "tercera":3, "cuarta":4, "quinta":5}
    for k, v in ordinales.items():
        if k in t:
            return v
    return None


def _seleccionar_item_desde_contexto(chat_id: int | str, texto: str):
    """
    Elige una cita de ULTIMA_AGENDA según:
    1) Texto entre comillas que aparezca dentro de 'texto'
    2) Hora explícita (HH:MM) que coincida
    3) Índice enumerado (1., 2., ...)
    4) Fallback: primera
    """
    items = ULTIMA_AGENDA.get(chat_id) or []
    if not items:
        return None

    # 1) Frase entre comillas
    q = re.search(r"[\"“”'‘’](.+?)[\"“”'‘’]", texto)
    if q:
        frag = q.group(1).strip().lower()
        for it in items:
            if frag and frag in (it.get("texto") or "").lower():
                return it

    # 2) Hora explícita
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", texto)
    if m:
        hhmm = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"
        for it in items:
            if (it.get("hora") or "") == hhmm:
                return it

    # 3) Índice
    idx = _extraer_indice(texto)
    if idx and 1 <= idx <= len(items):
        return items[idx - 1]

    # 4) Fallback: primera
    return items[0]
   

def _sanitizar_comando_capturado(raw: str) -> str:
    """
    De un texto que contiene un comando (incluso en bloque de código),
    devuelve solo el comando y sus argumentos en una sola línea.
    """
    if not raw:
        return ""
    # tomar la primera coincidencia /palabra...
    m = re.search(r"/(agenda|registrar|borrar(?:_fecha|_todo)?|buscar(?:_fecha)?|cuando|reprogramar|modificar)\b[^\n`]*", raw, flags=re.IGNORECASE)
    if not m:
        return raw.strip()
    cmd = m.group(0)
    # limpiar backticks y espacios
    cmd = cmd.replace("```", " ").replace("`", " ").strip()
    # colapsar espacios múltiples
    cmd = re.sub(r"\s+", " ", cmd)
    # normalizar '/.' -> '/'
    cmd = cmd.replace("/.", "/").strip()
    return cmd


def _parsear_lineas_a_items(texto: str):
    """
    Convierte líneas 'YYYY-MM-DD HH:MM → Texto' en [{'fecha','hora','texto'}].
    Ignora líneas que no coincidan.
    """
    items = []
    if not isinstance(texto, str):
        return items
    for linea in texto.splitlines():
        m = re.match(r"\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*→\s*(.+)\s*$", linea)
        if m:
            items.append({"fecha": m.group(1), "hora": m.group(2), "texto": m.group(3)})
    return items

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
                        "OBJETIVO Y PAPEL:\n"
                        "- Eres el CEREBRO. Orbis es el CUADERNO/agenda.\n"
                        "- Si el mensaje es de AGENDA, responde EXCLUSIVAMENTE con un comando válido para Orbis.\n"
                        "- Si NO es de agenda, conversa de forma natural y útil (no uses comandos).\n"
                        "- NUNCA digas frases como 'no tengo acceso a tu agenda'. Si te piden ver citas/agenda, responde con el comando adecuado.\n\n"
                        "REGLAS AGENDA (salida debe ser solo uno de estos comandos):\n"
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
                        "- Si el usuario empieza con '/', repite EXACTAMENTE ese comando.\n"
                        "- 'mañana' = hoy+1; 'hoy' = hoy. No inventes interpretaciones adicionales.\n"
                        "- 'No estoy seguro a qué cita te refieres' SOLO si pide borrar/modificar sin contexto claro (p. ej., 'borra esa').\n\n"
                        "ATAJOS CLAVE (mapea a comandos):\n"
                        "- 'todas las citas', 'agenda general', 'agenda completa', 'qué hay en la agenda' → /agenda\n"
                        "- 'qué tengo mañana' → /buscar_fecha YYYY-MM-DD (mañana)\n"
                        "- 'qué tengo hoy' → /buscar_fecha YYYY-MM-DD (hoy)\n\n"
                        "NO AGENDA:\n"
                        "- Responde como humano, claro y breve. Si pide organizar el día, propón plan y al final pregunta si quieres que lo agende en Orbis."
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
    """
    Limpia el texto para TTS:
    - Elimina símbolos y emojis.
    - Normaliza fechas 15/09/2025 -> '15 de septiembre de 2025' y 15/09 -> '15 de septiembre'.
    - Convierte horas 24h (10:00, 13:05, 20.30) a 12h con 'de la mañana/tarde/noche'.
    - Evita leer puntuación innecesaria.
    """
    # 1) Quitar emojis/símbolos (dejamos solo letras, números y espacios)
    limpio = re.sub(r"[^A-Za-zÁÉÍÓÚÜáéíóúüÑñ0-9\s/.:]", " ", texto)

    # 2) Normalizar flechas, guiones, bullets, paréntesis y otros signos frecuentes a espacio
    limpio = re.sub(r"[()→←↑↓➜➡️⬅️➤➔•·_\-\*=\[\]{}<>|#%~\"']", " ", limpio)

    # 3) Fechas dd/mm/yyyy → '15 de septiembre de 2025' y dd/mm → '15 de septiembre'
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

    # 4) Horas HH:MM o HH.MM → 12h natural
    def conv_hora(m):
        h = int(m.group(1))
        mnt = int(m.group(2))
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
            # más natural que “doce de la tarde” → “doce en punto de la tarde”
            return f"{h12} en punto {suf}"
        elif mnt < 10:
            # “tres y 5 de la tarde”
            return f"{h12} y {mnt} {suf}"
        else:
            # “tres {mnt} de la tarde”
            return f"{h12} {mnt} {suf}"

    limpio = re.sub(r"\b(\d{1,2})[:.](\d{2})\b", conv_hora, limpio)

    # 5) Quitar dobles signos/puntos/dos puntos y espacios repetidos
    limpio = re.sub(r"[,:;.\-]{2,}", " ", limpio)
    limpio = re.sub(r"\s+", " ", limpio).strip()

    return limpio

def enviar_audio(chat_id: int | str, texto: str):
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
    finally:
        try:
            if mp3_path and mp3_path.exists():
                mp3_path.unlink()
        except Exception:
            pass

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
                # Si Orbis devolvió texto plano en "respuesta", intentamos extraer items
                elif isinstance(datos_orbis, dict) and isinstance(datos_orbis.get("respuesta"), str):
                    parsed = _parsear_lineas_a_items(datos_orbis["respuesta"])
                    if parsed:
                        ULTIMA_AGENDA[chat_id] = parsed


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
                # 2-bis) ATAJO: “borra … del <fecha>” → /borrar_fecha YYYY-MM-DD (sin esperar a GPT)
        #     Cubre: “borra eso del 10 de septiembre”, “elimina las del 10/09”, etc.
        if re.search(r"\b(borra|elimina|quita|suprime|borre)\b", txt_low):
            # Fecha explícita en el mismo mensaje
            fecha_det = _parsear_fecha_es(txt_low)
            if fecha_det:
                comando = f"/borrar_fecha {fecha_det}"
            else:
                # Frase tipo “todas las del 10” (sin mes)
                m = re.search(r"\btodas?\s+las\s+del\s+(\d{1,2})\b", txt_low)
                if m:
                    dia = int(m.group(1))
                    fecha_inf = _inferir_fecha_dia(dia, chat_id)
                    # Pido confirmación antes de ejecutar
                    PENDIENTE[chat_id] = {"tipo": "borrar_fecha", "fecha_propuesta": fecha_inf, "comando": f"/borrar_fecha {fecha_inf}"}
                    msg = f"¿Borro todas las citas del {fecha_inf} en Orbis?"
                    if prefer_audio: enviar_audio(chat_id, msg)
                    else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
                    return jsonify({"ok": True})

            if 'comando' in locals() and comando:
                # Confirmación para /borrar_todo (por si acaso)
                if comando.startswith("/borrar_todo") and "confirmar" not in comando:
                    msg = "⚠️ ¿Seguro que deseas borrar TODA la agenda? Responde con '/borrar_todo confirmar'."
                    if prefer_audio: enviar_audio(chat_id, msg)
                    else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
                    return jsonify({"ok": True})

                # Ejecutar contra Orbis en modo JSON y redactar natural (igual que tu bloque actual)
                try:
                    r = requests.post(ORBIS_API, json={"texto": comando, "chat_id": chat_id, "modo": "json"}, timeout=10)
                    datos_orbis = r.json()
                except requests.exceptions.Timeout:
                    datos_orbis = {"ok": False, "error": "timeout_orbis"}
                except Exception:
                    datos_orbis = {"ok": False, "error": "respuesta_no_json"}

                print(f"📦 Datos de Orbis (atajo borrar_fecha): {datos_orbis}", flush=True)

                if isinstance(datos_orbis, dict) and datos_orbis.get("ok") and datos_orbis.get("items"):
                    ULTIMA_AGENDA[chat_id] = datos_orbis["items"]
                elif isinstance(datos_orbis, dict) and isinstance(datos_orbis.get("respuesta"), str):
                    parsed = _parsear_lineas_a_items(datos_orbis["respuesta"])
                    if parsed:
                        ULTIMA_AGENDA[chat_id] = parsed

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

        # 2) Heurística: si el usuario menciona 'mañana' + (agenda|citas), crear PENDIENTE y pedir confirmación
        if ("mañana" in txt_low) and (("agenda" in txt_low) or ("citas" in txt_low)):
            PENDIENTE[chat_id] = {"tipo": "buscar_fecha", "fecha": "manana"}
            msg = "¿Quieres que consulte en Orbis tus citas de mañana?"
            if prefer_audio: enviar_audio(chat_id, msg)
            else: requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": msg})
            return jsonify({"ok": True})

        # 2-ter) ATAJO: consulta general de agenda → /agenda (sin esperar al LLM)
        cmd_atajo = detectar_atajo_comando(txt_low)
        if cmd_atajo:
            try:
                r = requests.post(
                    ORBIS_API,
                    json={"texto": cmd_atajo, "chat_id": chat_id, "modo": "json"},
                    timeout=10
                )
                datos_orbis = r.json()
            except requests.exceptions.Timeout:
                datos_orbis = {"ok": False, "error": "timeout_orbis"}
            except Exception:
                datos_orbis = {"ok": False, "error": "respuesta_no_json"}

            print(f"📦 Datos de Orbis (atajo /agenda): {datos_orbis}", flush=True)

            # Actualiza última agenda si aplica
            if isinstance(datos_orbis, dict) and datos_orbis.get("ok") and datos_orbis.get("items"):
                ULTIMA_AGENDA[chat_id] = datos_orbis["items"]
            elif isinstance(datos_orbis, dict) and isinstance(datos_orbis.get("respuesta"), str):
                parsed = _parsear_lineas_a_items(datos_orbis["respuesta"])
                if parsed:
                    ULTIMA_AGENDA[chat_id] = parsed

            # Redacción natural (solo con datos de Orbis)
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
            if prefer_audio:
                enviar_audio(chat_id, texto_final)
            else:
                requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})
            return jsonify({"ok": True})
    
                # 2-quater) ATAJO: reprogramar/modificar usando la última agenda + nueva fecha/hora
        if re.search(r"\b(modifica|midifica|modificar|cambia|cambiar|reprograma|reprogramar|mueve|mover|cámbiala|cambiala)\b", txt_low):
            target = _seleccionar_item_desde_contexto(chat_id, orden)
            nueva_fecha = _parsear_fecha_es(txt_low)
            nueva_hora  = _parsear_hora_es(txt_low)

            if target and (nueva_fecha or nueva_hora):
                old_fecha = target["fecha"]; old_hora = target["hora"]
                if not nueva_fecha: nueva_fecha = old_fecha
                if not nueva_hora:  nueva_hora  = old_hora

                comando = f"/reprogramar {old_fecha} {old_hora} {nueva_fecha} {nueva_hora}"

                try:
                    r = requests.post(
                        ORBIS_API,
                        json={"texto": comando, "chat_id": chat_id, "modo": "json"},
                        timeout=10
                    )
                    datos_orbis = r.json()
                except requests.exceptions.Timeout:
                    datos_orbis = {"ok": False, "error": "timeout_orbis"}
                except Exception:
                    datos_orbis = {"ok": False, "error": "respuesta_no_json"}

                print(f"📦 Datos de Orbis (atajo reprogramar): {datos_orbis}", flush=True)

                # Actualiza última agenda si aplica
                if isinstance(datos_orbis, dict) and datos_orbis.get("ok") and datos_orbis.get("items"):
                    ULTIMA_AGENDA[chat_id] = datos_orbis["items"]
                elif isinstance(datos_orbis, dict) and isinstance(datos_orbis.get("respuesta"), str):
                    parsed = _parsear_lineas_a_items(datos_orbis["respuesta"])
                    if parsed:
                        ULTIMA_AGENDA[chat_id] = parsed

                # Redacción natural solo con datos de Orbis
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

            # Si falta info, deja un pendiente para completar
            if target and not (nueva_fecha or nueva_hora):
                PENDIENTE[chat_id] = {"tipo": "reprogramar", "vieja_fecha": target["fecha"], "vieja_hora": target["hora"]}
                msg = f"¿Para qué fecha y hora quieres mover la cita de {target['fecha']} a las {target['hora']}?"
            else:
                msg = "Necesito que me indiques qué cita (o a partir de la última lista) y la nueva fecha u hora."

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
            comando = _sanitizar_comando_capturado(interpretacion)
        else:
            m = re.search(r"/", interpretacion)
            if m:
                comando = _sanitizar_comando_capturado(interpretacion)



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
    if os.getenv("ENABLE_SCHEDULER", "1") != "1":
        print("⏭️ Scheduler desactivado por ENABLE_SCHEDULER", flush=True)
        return
    schedule.every(1).minutes.do(revisar_agenda_y_enviar_alertas)
    def run_scheduler():
        while True:
            schedule.run_pending()
            time.sleep(1)
    threading.Thread(target=run_scheduler, daemon=True).start()

