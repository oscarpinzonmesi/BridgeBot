# bridge_bot.py
from flask import Flask, request
import requests, schedule, time, threading
from openai import OpenAI
import re, datetime

# ---------------- CONFIG ----------------
TOKEN_TELEGRAM = "AQUI_VA_TU_TOKEN_DE_TELEGRAM"
URL_ORBIS = "AQUI_VA_URL_DE_ORBIS"   # ej: "https://orbis.midominio.com/api"
OPENAI_KEY = "AQUI_VA_TU_API_KEY_DE_OPENAI"

app = Flask(__name__)
cliente = OpenAI(api_key=OPENAI_KEY)

# Memoria temporal
MEMORIA_LOCAL = {}
ULTIMA_AGENDA = {}

# ---------------- TELEGRAM ----------------
def enviar_mensaje(chat_id, texto):
    """
    Envía un mensaje a Telegram usando requests (sin async/await).
    """
    url = f"https://api.telegram.org/bot{TOKEN_TELEGRAM}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": texto})
    except Exception as e:
        print(f"Error enviando mensaje a Telegram: {e}")

# ---------------- ORBIS ----------------
def _llamar_orbis(comando, chat_id=None, formato="json", timeout_s=10, reintentos=2):
    """
    Envía un comando a Orbis y devuelve la respuesta JSON.
    """
    try:
        resp = requests.post(URL_ORBIS, json={"cmd": comando}, timeout=timeout_s)
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------- GPT ----------------
def consultar_mesa_gpt(mensaje, chat_id):
    """
    Interpreta lo que dice Doctor Mesa y decide si solo responde o si traduce a comando Orbis.
    """
    prompt_sistema = """
    Eres MesaGPT, el asistente personal de Doctor Mesa.
    Orbis es la libreta de su vida: ahí debes registrar, consultar, editar o borrar citas.
    Responde siempre en lenguaje natural, pero cuando sea necesario,
    traduce a comandos de Orbis (/agenda, /registrar, /borrar_todo, /buscar_fecha YYYY-MM-DD, etc.).
    Si Doctor Mesa pide recordatorios (ej: "recuérdame en 5 minutos"),
    programa un recordatorio y notifícalo en Telegram.
    """
    respuesta = cliente.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": mensaje}
        ]
    )
    return respuesta.choices[0].message.content

# ---------------- RECORDATORIOS ----------------
def enviar_recordatorio(chat_id, texto):
    enviar_mensaje(chat_id, f"⏰ Recordatorio: {texto}")

def programar_recordatorio(chat_id, minutos, texto):
    """
    Programa un recordatorio que se enviará después de X minutos.
    """
    def tarea():
        enviar_recordatorio(chat_id, texto)
        return schedule.CancelJob  # se ejecuta una sola vez
    schedule.every(minutos).minutes.do(tarea)

def correr_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(1)

# arrancamos el planificador en segundo plano
threading.Thread(target=correr_scheduler, daemon=True).start()

# ---------------- FLASK / TELEGRAM ----------------
@app.route("/", methods=["POST"])
def webhook():
    data = request.json
    chat_id = data["message"]["chat"]["id"]
    texto = data["message"].get("text", "")

    interpretacion = consultar_mesa_gpt(texto, chat_id)

    # enviar respuesta al chat
    enviar_mensaje(chat_id, f"🤖 MesaGPT interpretó: {interpretacion}")

    return {"ok": True}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
