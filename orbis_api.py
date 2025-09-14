from flask import Flask, request, jsonify
import os

app = Flask(__name__)

# ===============================
# Seguridad con API KEY
# ===============================
ORBIS_API_KEY = os.environ.get("ORBIS_API_KEY", "changeme")

def check_auth(req):
    auth = req.headers.get("Authorization", "")
    return auth == f"Bearer {ORBIS_API_KEY}"

# ===============================
# Memoria temporal (simulación Orbis)
# ===============================
AGENDA = {}

# ===============================
# Endpoints
# ===============================

@app.route("/agenda", methods=["POST"])
def agenda():
    if not check_auth(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    fecha = request.json.get("fecha")
    citas = AGENDA.get(fecha, [])
    return jsonify({"ok": True, "citas": citas})

@app.route("/registrar", methods=["POST"])
def registrar():
    if not check_auth(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    data = request.json
    fecha, hora, descripcion = data.get("fecha"), data.get("hora"), data.get("descripcion")
    if not fecha or not hora or not descripcion:
        return jsonify({"ok": False, "error": "Faltan datos"}), 400
    if fecha not in AGENDA:
        AGENDA[fecha] = []
    AGENDA[fecha].append({"hora": hora, "descripcion": descripcion})
    return jsonify({"ok": True, "mensaje": "Cita registrada con éxito"})

@app.route("/borrar", methods=["POST"])
def borrar():
    if not check_auth(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    fecha, hora = request.json.get("fecha"), request.json.get("hora")
    if fecha in AGENDA:
        AGENDA[fecha] = [c for c in AGENDA[fecha] if c["hora"] != hora]
    return jsonify({"ok": True, "mensaje": "Cita eliminada"})

@app.route("/borrar_todo", methods=["POST"])
def borrar_todo():
    if not check_auth(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    fecha = request.json.get("fecha")
    AGENDA[fecha] = []
    return jsonify({"ok": True, "mensaje": f"Todas las citas de {fecha} eliminadas"})

@app.route("/buscar", methods=["POST"])
def buscar():
    if not check_auth(request):
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    query = request.json.get("query", "").lower()
    resultados = []
    for fecha, citas in AGENDA.items():
        for c in citas:
            if query in c["descripcion"].lower():
                resultados.append({"fecha": fecha, **c})
    return jsonify({"ok": True, "citas": resultados})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
