import os
import requests
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

# === CONFIG ===
BRIDGE_TOKEN = os.getenv("TELEGRAM_TOKEN")
ORBIS_API = os.getenv("ORBIS_API")             # Ej: https://orbis-xxx.onrender.com/procesar
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

BRIDGE_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendMessage"
TELEGRAM_API = f"https://api.telegram.org/bot{BRIDGE_TOKEN}"

client = OpenAI(api_key=OPENAI_API_KEY)


# === Función: MesaGPT actúa como secretario ===
def mesa_secretario(chat_id: str, texto: str):
    """
    MesaGPT interpreta el texto, decide si hablar directo
    o consultar a Orbis, y devuelve una respuesta natural.
    """
    try:
        # 1. Interpretar intención del usuario
        respuesta = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """Eres MesaGPT, secretario legal y de agenda.
- El usuario siempre te habla en lenguaje natural.
- Si la consulta es de agenda, tradúcela a un comando (/agenda, /registrar HH:MM tarea, /borrar HH:MM, /buscar NOMBRE) SOLO INTERNAMENTE.
- Envía ese comando a Orbis.
- Luego transforma la respuesta de Orbis en un mensaje natural para el usuario.
- Nunca muestres comandos al usuario.
- Siempre responde con frases claras como si fueras su asistente humano."""},
                {"role": "user", "content": texto}
            ]
        )
        interpretacion = respuesta.choices[0].message.content.strip()

        # 2. Si MesaGPT dice que hay que ir a Orbis
        if interpretacion.startswith("/"):
            r = requests.post(ORBIS_API, json={"texto": interpretacion})
            try:
                respuesta_orbis = r.json().get("respuesta", "")
            except Exception:
                respuesta_orbis = "⚠️ Orbis no devolvió datos válidos"

            # 3. Reformular en lenguaje humano
            final = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Eres un secretario que transforma datos crudos en una respuesta natural y clara para el Doctor."},
                    {"role": "user", "content": f"Datos de Orbis: {respuesta_orbis}"}
                ]
            )
            texto_final = final.choices[0].message.content.strip()
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto_final})
        else:
            # 4. Si no es agenda, responder directo
            requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": interpretacion})

    except Exception as e:
        print("❌ Error en mesa_secretario:", str(e), flush=True)
        requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": "⚠️ Hubo un error procesando tu mensaje."})


# === TELEGRAM WEBHOOK ===
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True)

    if "message" not in data:
        return {"ok": True}

    chat_id = data["message"]["chat"]["id"]

    if "text" in data["message"]:
        texto = data["message"]["text"]
        print(f"📩 Telegram → Doctor: {texto}", flush=True)
        mesa_secretario(chat_id, texto)

    elif "voice" in data["message"]:
        file_id = data["message"]["voice"]["file_id"]
        print(f"🎤 Telegram → Doctor (voz): {file_id}", flush=True)

        # Descargar y transcribir voz con Whisper
        r = requests.get(f"{TELEGRAM_API}/getFile?file_id={file_id}").json()
        file_path = r["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BRIDGE_TOKEN}/{file_path}"
        voice_file = requests.get(file_url)
        with open("voice.ogg", "wb") as f:
            f.write(voice_file.content)

        with open("voice.ogg", "rb") as audio:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio
            )
        texto = transcript.text
        print(f"📝 Transcripción: {texto}", flush=True)
        mesa_secretario(chat_id, texto)

    return {"ok": True}

# === Función: responder con audio (TTS) ===
def responder_con_audio(chat_id: str, texto: str):
    try:
        # 1. Generar audio con OpenAI
        respuesta_audio = client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",   # puedes cambiar la voz (ej: "verse", "sage")
            input=texto
        )

        # 2. Guardar en archivo .ogg
        with open("respuesta.ogg", "wb") as f:
            f.write(respuesta_audio.content)

        # 3. Enviar a Telegram como nota de voz
        with open("respuesta.ogg", "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{BRIDGE_TOKEN}/sendVoice",
                data={"chat_id": chat_id},
                files={"voice": f}
            )
        print("📢 Audio enviado a Telegram", flush=True)
    except Exception as e:
        print("❌ Error generando/enviando audio:", str(e), flush=True)
        # fallback: enviar texto si falla
        requests.post(BRIDGE_API, json={"chat_id": chat_id, "text": texto})

@app.route("/ping", methods=["GET"])
def home():
    return "✅ BridgeBot activo como secretario"
