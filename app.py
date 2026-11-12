"""
Creators Piazza — servidor web.

Base de datos y análisis de perfiles de influencers para las marcas de Piazza.
La lógica está en creators.py; los datos se guardan en Dropbox (almacen.py).

Acceso: una contraseña para todo el equipo.
  CREATORS_PASSWORD        en texto plano (variable de entorno de Render), o
  CREATORS_PASSWORD_HASH   el hash de werkzeug, para no exponerla.
En Render, sin ninguna de las dos la app no abre (falla cerrada). En local,
sin contraseña no se pide nada.
"""

import os
import secrets

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

load_dotenv()

import almacen
from creators import creators_bp

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # Render termina el TLS en su proxy
app.register_blueprint(creators_bp)

EN_RENDER = bool(os.environ.get("RENDER"))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=EN_RENDER,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

PASSWORD = os.environ.get("CREATORS_PASSWORD", "")
PASSWORD_HASH = os.environ.get("CREATORS_PASSWORD_HASH", "").strip()
HAY_CLAVE = bool(PASSWORD or PASSWORD_HASH)


def _clave_correcta(intento):
    if PASSWORD:
        return secrets.compare_digest(intento.encode(), PASSWORD.encode())
    if PASSWORD_HASH:
        return check_password_hash(PASSWORD_HASH, intento)
    return False


@app.before_request
def _exigir_login():
    if request.endpoint in ("login", "static", "salud"):
        return None
    if not HAY_CLAVE:
        if EN_RENDER:
            return "Falta configurar la contraseña de acceso (CREATORS_PASSWORD en Render).", 503
        return None  # desarrollo local
    if session.get("autenticada"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "La sesión expiró. Vuelve a entrar."}), 401
    return redirect(url_for("login", siguiente=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not HAY_CLAVE:
        return redirect("/creators")
    error = None
    if request.method == "POST":
        if _clave_correcta(request.form.get("password", "")):
            session["autenticada"] = True
            session.permanent = True
            destino = request.form.get("siguiente") or "/creators"
            if not destino.startswith("/") or destino.startswith("//"):
                destino = "/creators"  # solo rutas internas
            return redirect(destino)
        error = "Contraseña incorrecta."
    return render_template("login.html", error=error, siguiente=request.args.get("siguiente", ""))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def inicio():
    return redirect("/creators")


@app.route("/api/salud", endpoint="salud")
def salud():
    return jsonify({"ok": True, "almacen": "dropbox" if almacen.hay_dropbox() else "local"})


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5070)), debug=True)
