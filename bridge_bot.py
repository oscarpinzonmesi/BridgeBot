import os
import json
import requests
from flask import Flask, request, jsonify
from openai import OpenAI

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("TELEGRAM_TOKEN")     # Bot de Telegram
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")   # Tu clave de OpenAI
ORBIS_FILE = "agenda.json"                     # Base de datos local (JSON)

BRIDGE_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}"

# Inicializar Flask y OpenAI
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# === Funciones de AGENDA (Orbis ejecutor) ===
def cargar_agenda():
    if not os.path.exists(ORBIS_FILE):
        return {}
    with open(ORBIS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def guardar_agenda(agenda):
    with open(ORBIS_FILE, "w", encoding="utf-8") as f:
        json.dump(agenda, f, ensure_ascii=False, indent=4)

def orbis(texto: str) -> str:
    partes = texto.strip().split()
    comando = partes[0].lower() if partes else ""

    if comando == "/agenda":
        agenda = cargar_agenda()
        if not agenda:
            return "📭 No tienes tareas guardadas."
        return "📝 Agenda:\n" + "\n".join([f"{h} → {t}" for h, t in agenda.items()])

    elif comando == "/registrar":
        try:
            hora = partes[1]
            tarea = " ".join(partes[2:])
            agenda = cargar_agenda()
            agenda[hora] = tarea
            guardar_agenda(agenda)
            return f"✅ Guardado: {hora} → {tarea}"
        except:
            return "❌ Usa el formato: /registrar 09:00 Reunión"

    elif comando == "/borrar":
        try:
            hora = partes[1]
            agenda = cargar_agenda()
            if hora in agenda:
                del agenda[hora]
                guardar_agenda(agenda)
                return f"🗑️ Borrada la tarea de las {hora}"
            else:
                return "❌ No hay nada guardado en esa hora."
        except:
            return "❌ Usa el formato: /borrar 09:00"

    elif comando == "/buscar":
        try:
            nombre = " ".join(partes[1:]).lower()
            agenda = cargar_agenda()
            resultados = [f"{h} → {t}" for h, t in agenda.items() if nombre in t.lower()]
            if resultados:
                return "📌 Encontré estas citas:\n" + "\n".join(resultados)
            else:
                return f"❌ No hay citas con {nombre}."
        except:
            return "❌ Usa el formato: /buscar nombre"

    else:
        return "🤔 No entendí. Usa /agenda, /registrar, /borrar o /buscar."


# === MesaGPT (interpreto tus mensajes) ===
def interpretar_mensaje(texto: str) -> str:
    try:
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """Eres MesaGPT, secretario digital de Doctor Mesa. 
Tu tarea es interpretar lo que él pide y, si corresponde a la agenda, generar un comando Orbis (/agenda, /registrar, /borrar, /buscar). 
Si no es agenda, responde directamente con texto. 
Si él pide respuesta en audio, genera la respuesta en texto pero marca que debe ir como audio."""},
                {"role": "user", "content": texto}
            ]
        )
        return respuesta.choices[0].message.content.strip()
    except Exception as e:
        print("❌ Error en MesaGPT:", str(e), flush=True)
        return "⚠️ No pude comunicarme."


# === Enviar audio a Telegram ===
def responder_con_audio(chat_id, texto):
    try:
        speech = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=texto
        )
        audio_path = "respuesta.ogg"
        with open(audio_path, "wb") as f:
            f.write(speech.read())

        url = f"{BRIDGE_API}/sendVoice"
        with open(audio_path, "rb") as f:
            requests.post(url, data={"chat_id": chat_id}, files={"voice": f})
    except Exception as e:
        print("❌ Error generando audio:", str(e), flush=True)


# === Webhook de Telegram ===
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]

    # Caso texto
    if "text" in data["message"]:
        texto = data["message"]["text"]
        print(f"📩 Telegram → Doctor: {texto}", flush=True)

        respuesta_mesa = interpretar_mensaje(texto)
        print(f"🤖 MesaGPT interpretó: {texto} → {respuesta_mesa}", flush=True)

        if respuesta_mesa.startswith("/"):
            respuesta = orbis(respuesta_mesa)
            requests.post(f"{BRIDGE_API}/sendMessage", json={"chat_id": chat_id, "text": respuesta})
        elif "[AUDIO]" in respuesta_mesa:
            responder_con_audio(chat_id, respuesta_mesa.replace("[AUDIO]", "").strip())
        else:
            requests.post(f"{BRIDGE_API}/sendMessage", json={"chat_id": chat_id, "text": respuesta_mesa})

    # Caso voz
    elif "voice" in data["message"]:
        file_id = data["message"]["voice"]["file_id"]
        file_info = requests.get(f"{BRIDGE_API}/getFile?file_id={file_id}").json()
        file_path = file_info["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BRIDGE_TOKEN}/{file_path}"
        voz = requests.get(file_url)
        with open("voz.ogg", "wb") as f:
            f.write(voz.content)

        with open("voz.ogg", "rb") as audio_file:
            transcript = client.audio.transcriptions.create(model="whisper-1", file=audio_file)

        orden = transcript.text
        print(f"🎤 Telegram → Doctor (voz): {orden}", flush=True)

        respuesta_mesa = interpretar_mensaje(orden)
        print(f"🤖 MesaGPT interpretó: {orden} → {respuesta_mesa}", flush=True)

        if respuesta_mesa.startswith("/"):
            respuesta = orbis(respuesta_mesa)
            requests.post(f"{BRIDGE_API}/sendMessage", json={"chat_id": chat_id, "text": respuesta})
        elif "[AUDIO]" in respuesta_mesa:
            responder_con_audio(chat_id, respuesta_mesa.replace("[AUDIO]", "").strip())
        else:
            requests.post(f"{BRIDGE_API}/sendMessage", json={"chat_id": chat_id, "text": respuesta_mesa})

    return {"ok": True}


@app.route("/ping", methods=["GET"])
def home():
    return "✅ BridgeBot activo como secretario de Doctor Mesa"
