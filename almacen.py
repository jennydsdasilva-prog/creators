"""
Dónde guarda Creators Piazza sus datos.

Render borra el disco en cada reinicio y en cada deploy, así que los datos
viven en el Dropbox de Piazza, en su propia carpeta (CREATORS_DROPBOX_DIR,
por defecto /creators-piazza). Sin credenciales de Dropbox (desarrollo local)
se usa un archivo al lado del código.

Variables de entorno:
  DROPBOX_APP_KEY + DROPBOX_REFRESH_TOKEN                      (recomendado)
  DROPBOX_APP_SECRET                                           (solo si el token
                                   se sacó con el flujo clásico; con PKCE no hace falta)
  DROPBOX_TOKEN                                                (alternativa, vence)
  CREATORS_DROPBOX_DIR                                         (opcional)
"""

import os

CARPETA = os.environ.get("CREATORS_DROPBOX_DIR", "/creators-piazza").rstrip("/")
LOCAL = os.path.join(os.path.dirname(__file__), "datos_local")

_cliente = {"dbx": None}


def _dbx():
    if _cliente["dbx"] is not None:
        return _cliente["dbx"]
    import dropbox
    refresh = os.environ.get("DROPBOX_REFRESH_TOKEN", "")
    key = os.environ.get("DROPBOX_APP_KEY", "")
    secret = os.environ.get("DROPBOX_APP_SECRET", "")
    token = os.environ.get("DROPBOX_TOKEN", "")
    if refresh and key:
        _cliente["dbx"] = dropbox.Dropbox(oauth2_refresh_token=refresh, app_key=key, app_secret=secret or None)
    elif token:
        _cliente["dbx"] = dropbox.Dropbox(oauth2_access_token=token)
    return _cliente["dbx"]


def hay_dropbox():
    return bool((os.environ.get("DROPBOX_REFRESH_TOKEN") and os.environ.get("DROPBOX_APP_KEY"))
                or os.environ.get("DROPBOX_TOKEN"))


def leer(nombre):
    """Bytes del archivo, o None si todavía no existe. Los errores de red se propagan."""
    if hay_dropbox():
        from dropbox.exceptions import ApiError
        try:
            _, resp = _dbx().files_download(f"{CARPETA}/{nombre}")
            return resp.content
        except ApiError as e:
            if e.error.is_path() and e.error.get_path().is_not_found():
                return None
            raise
    ruta = os.path.join(LOCAL, nombre)
    if not os.path.exists(ruta):
        return None
    with open(ruta, "rb") as f:
        return f.read()


def escribir(nombre, contenido):
    if hay_dropbox():
        import dropbox
        _dbx().files_upload(contenido, f"{CARPETA}/{nombre}", mode=dropbox.files.WriteMode.overwrite, mute=True)
        return
    ruta = os.path.join(LOCAL, nombre)
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "wb") as f:
        f.write(contenido)


def estado():
    """Para la pantalla de salud: dónde se está guardando."""
    return {"dropbox": hay_dropbox(), "carpeta": CARPETA if hay_dropbox() else LOCAL}
