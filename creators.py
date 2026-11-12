"""
Creators Piazza — base de datos y análisis de perfiles de influencers.

Qué hace:
  - El equipo registra un perfil (usuario de Instagram + datos de contacto,
    país, marcas). La app consulta la cuenta en la API oficial de Meta
    (Business Discovery), calcula métricas, detecta señales de alerta, pide un
    análisis cualitativo a Claude y le pone un score de 0 a 100.
  - Cada nuevo análisis guarda una foto del perfil en el historial, así con el
    tiempo se ve cómo crece (o cómo salta) cada cuenta.
  - Campañas: se cargan los influencers que participaron, lo que costó cada uno
    y los enlaces de sus publicaciones. Las métricas públicas (likes,
    comentarios, vistas) se traen de Instagram; las privadas (alcance,
    impresiones, clics, ventas) las carga el equipo con lo que manda el
    influencer. Con eso se arma el reporte: CPE, CPM y comparación.

Dónde viven los datos: en Dropbox, /creators-piazza/datos.json (ver almacen.py;
el disco de Render se borra en cada deploy). Se deja además una copia diaria
en /creators-piazza/respaldos/.

Variables de entorno (en Render):
  META_ACCESS_TOKEN   token de larga duración de la app de Meta.
  META_IG_USER_ID     id de la cuenta de Instagram profesional de la marca
                      desde la que se consulta (no es el usuario, es el número).
  META_API_VERSION    opcional, default v25.0.
  ANTHROPIC_API_KEY   clave de la API de Claude para el análisis con IA.
  CREATORS_MODELO     opcional, default claude-sonnet-5-5.

Sin META_* la app sigue funcionando en modo manual (el equipo carga
seguidores y promedios a mano). Sin ANTHROPIC_API_KEY no hay análisis con IA y
el score se calcula solo con las métricas.

Limitación de Meta: Business Discovery solo ve cuentas profesionales
(creador o empresa). Las cuentas personales van en modo manual.

Todas las pantallas piden la contraseña de la herramienta (app.py).
"""

import csv
import io
import json
import os
import re
import statistics
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import requests
from flask import Blueprint, Response, jsonify, render_template, request

sys.path.insert(0, os.path.dirname(__file__))
import almacen

creators_bp = Blueprint("creators", __name__)

# ─── Catálogos ────────────────────────────────────────────────────────────────

# Contexto de cada marca para el análisis de afinidad. Ajustar si cambia el
# posicionamiento: es lo que lee la IA para decidir si el perfil encaja.
MARCAS = {
    "OP": "Ocean Pacific: marca californiana de estilo de vida playero y surf; ropa, calzado y accesorios casuales, con líneas de running (OP Running Club) y niños.",
    "Lotto": "Lotto: marca deportiva italiana con herencia en fútbol y tenis; hoy también athleisure y línea deportiva de mujer.",
    "Patrick": "Patrick: marca de origen francés con herencia en fútbol; foco en guayos/botines y calzado deportivo.",
    "Ecko": "Ecko: marca de streetwear y cultura urbana (hip hop, skate, arte callejero).",
    "K-Swiss": "K-Swiss: marca estadounidense con herencia en tenis; zapatillas de estilo clásico y casual.",
    "Rockland": "Rockland: calzado de estilo de vida.",
}

PLATAFORMAS = ["instagram", "tiktok", "otra"]

ESTADOS = ["Candidato", "Contactado", "Negociando", "Activo", "Pausado", "Descartado"]
ESTADOS_CAMPANA = ["Planificada", "Activa", "Cerrada"]

PAISES = [
    "Panamá", "Costa Rica", "Guatemala", "El Salvador", "Honduras", "Nicaragua",
    "Belice", "México", "Colombia", "Venezuela", "Ecuador", "Perú", "Bolivia",
    "Chile", "Argentina", "Uruguay", "Paraguay", "República Dominicana",
    "Puerto Rico", "Cuba", "Jamaica", "Trinidad y Tobago", "Aruba", "Curazao",
    "Barbados", "Bahamas", "Haití", "Estados Unidos",
]

# Engagement esperado (%) por tamaño de cuenta en Instagram: (bajo, alto).
# Son referencias aproximadas de la industria, no una norma: sirven para
# comparar perfiles entre sí. Ajustar con la experiencia de Piazza.
NIVELES_CUENTA = [
    ("Nano", 0, 10_000, (2.5, 6.0)),
    ("Micro", 10_000, 50_000, (1.5, 4.0)),
    ("Medio", 50_000, 250_000, (1.0, 3.0)),
    ("Macro", 250_000, 1_000_000, (0.7, 2.0)),
    ("Mega", 1_000_000, float("inf"), (0.4, 1.5)),
]

PESOS = {"engagement": 30, "autenticidad": 25, "afinidad": 25, "actividad": 20}

# Para detectar menciones en los textos de los posts.
COMPETENCIA = [
    "nike", "adidas", "puma", "reebok", "new balance", "newbalance", "fila",
    "umbro", "kappa", "vans", "converse", "skechers", "under armour",
    "underarmour", "asics", "mizuno", "joma", "diadora", "quiksilver",
    "billabong", "rip curl", "ripcurl", "hurley", "volcom", "le coq sportif",
    "champion", "jordan", "on running", "hoka", "saucony", "brooks",
]
PROPIAS = {
    "OP": ["oceanpacific", "ocean pacific", "opoceanpacific", "oprunningclub"],
    "Lotto": ["lotto", "lottosport"],
    "Patrick": ["#patrick", "@patrick", "patricksport"],
    "Ecko": ["ecko", "eckounltd", "ecko unltd"],
    "K-Swiss": ["kswiss", "k-swiss", "k swiss"],
    "Rockland": ["rockland"],
}
PALABRAS_SORTEO = ["sorteo", "giveaway", "concurso", "regalo", "gana ", "ganador"]

# ─── Almacenamiento ───────────────────────────────────────────────────────────

RUTA_DATOS = "datos.json"

_lock = threading.RLock()
_cache = {"datos": None, "ts": 0.0}
_ultimo_respaldo = {"fecha": ""}
CACHE_TTL = 15


def _vacio():
    return {"version": 1, "perfiles": {}, "campanas": {}}


def _leer():
    """Los datos completos. Cacheados unos segundos para no ir a Dropbox en cada clic."""
    with _lock:
        if _cache["datos"] is not None and time.time() - _cache["ts"] < CACHE_TTL:
            return _cache["datos"]
        contenido = almacen.leer(RUTA_DATOS)  # errores de red se propagan
        datos = json.loads(contenido.decode("utf-8")) if contenido else _vacio()
        datos.setdefault("perfiles", {})
        datos.setdefault("campanas", {})
        _cache.update(datos=datos, ts=time.time())
        return datos


def _escribir(datos):
    contenido = json.dumps(datos, ensure_ascii=False, indent=1).encode("utf-8")
    almacen.escribir(RUTA_DATOS, contenido)
    hoy = _hoy()
    if _ultimo_respaldo["fecha"] != hoy:
        try:
            almacen.escribir(f"respaldos/datos-{hoy}.json", contenido)
            _ultimo_respaldo["fecha"] = hoy
        except Exception as e:  # el respaldo no debe frenar el guardado
            print(f"[creators] No se pudo guardar el respaldo: {e}")
    _cache.update(datos=datos, ts=time.time())


def _modificar(fn):
    """Lee lo último, aplica fn(datos) y guarda. Devuelve lo que devuelva fn."""
    with _lock:
        _cache["ts"] = 0.0  # forzar lectura fresca antes de escribir
        datos = _leer()
        resultado = fn(datos)
        _escribir(datos)
        return resultado


# ─── Utilidades ───────────────────────────────────────────────────────────────

def _ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hoy():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _id():
    return uuid.uuid4().hex[:10]


def _texto(v, maximo=300):
    return str(v if v is not None else "").strip()[:maximo]


def _numero(v):
    """'12.500', '12,5k', '1.2M', 3400 → float, o None si está vacío o no es número."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(" ", "")
    mult = 1
    if s.endswith("k"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        partes = s.split(",")
        s = s.replace(",", "") if len(partes[-1]) == 3 and mult == 1 else s.replace(",", ".")
    elif s.count(".") == 1 and len(s.split(".")[-1]) == 3 and mult == 1:
        s = s.replace(".", "")  # 12.500 = doce mil quinientos
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s) * mult
    except ValueError:
        return None


def limpiar_usuario(v):
    """'@Usuario', 'instagram.com/usuario/?hl=es' → 'usuario'."""
    s = _texto(v, 200)
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").strip().strip("/").lower()
    return s if re.fullmatch(r"[a-z0-9._]{1,30}", s) else ""


def _lista(v, permitidos=None, maximo=20):
    if isinstance(v, str):
        v = [x for x in re.split(r"[,;\n]", v)]
    salida = []
    for x in v or []:
        x = _texto(x, 60)
        if x and x not in salida and (permitidos is None or x in permitidos):
            salida.append(x)
    return salida[:maximo]


def nivel_cuenta(seguidores):
    for nombre, desde, hasta, rango in NIVELES_CUENTA:
        if desde <= (seguidores or 0) < hasta:
            return nombre, rango
    return NIVELES_CUENTA[0][0], NIVELES_CUENTA[0][3]


def _fecha(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except Exception:
        return None


def shortcode(enlace):
    m = re.search(r"instagram\.com/(?:[A-Za-z0-9._]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", enlace or "")
    return m.group(1) if m else ""


# ─── Meta (Instagram Business Discovery) ──────────────────────────────────────

class ErrorMeta(Exception):
    pass


def meta_configurado():
    return bool(os.environ.get("META_ACCESS_TOKEN") and os.environ.get("META_IG_USER_ID"))


CAMPOS_POST = "caption,like_count,comments_count,view_count,timestamp,media_type,media_product_type,permalink"
CAMPOS_POST_BASICOS = "caption,like_count,comments_count,timestamp,media_type,permalink"


def _consulta_meta(usuario, campos_media, limite=25, despues=None):
    version = os.environ.get("META_API_VERSION", "v25.0")
    ig = os.environ.get("META_IG_USER_ID", "")
    media = f"media.limit({limite})"
    if despues:
        media = f"media.after({despues}).limit({limite})"
    campos = (
        f"business_discovery.username({usuario})"
        "{username,name,biography,website,followers_count,follows_count,"
        f"media_count,profile_picture_url,{media}{{{campos_media}}}}}"
    )
    try:
        r = requests.get(
            f"https://graph.facebook.com/{version}/{ig}",
            params={"fields": campos, "access_token": os.environ.get("META_ACCESS_TOKEN", "")},
            timeout=30,
        )
    except requests.RequestException as e:
        raise ErrorMeta(f"No se pudo conectar con Instagram: {e}")
    datos = r.json() if r.content else {}
    if r.status_code != 200 or "error" in datos:
        err = datos.get("error", {})
        codigo, sub = err.get("code"), err.get("error_subcode")
        if codigo == 190:
            raise ErrorMeta("El token de Meta venció o no es válido. Hay que renovarlo en Render (META_ACCESS_TOKEN).")
        if codigo == 110 or sub in (2207013, 2207014) or "not be found" in (err.get("message") or "").lower():
            raise ErrorMeta(f"Instagram no encontró @{usuario} como cuenta profesional. Si es una cuenta personal, carga los datos a mano.")
        if codigo in (4, 17, 32, 613):
            raise ErrorMeta("Meta limitó las consultas por un rato (demasiadas seguidas). Prueba de nuevo en unos minutos.")
        raise ErrorMeta(f"Meta respondió con un error: {err.get('message') or r.status_code}")
    return datos.get("business_discovery") or {}


def consultar_instagram(usuario, paginas=1):
    """Perfil + últimos posts. paginas>1 trae más posts (para campañas)."""
    try:
        bd = _consulta_meta(usuario, CAMPOS_POST)
        campos = CAMPOS_POST
    except ErrorMeta as e:
        if not str(e).startswith("Meta respondió con un error"):
            raise
        # Error genérico: puede ser un campo que esta versión de la API no acepta.
        bd = _consulta_meta(usuario, CAMPOS_POST_BASICOS)
        campos = CAMPOS_POST_BASICOS
    posts = list((bd.get("media") or {}).get("data") or [])
    despues = (((bd.get("media") or {}).get("paging") or {}).get("cursors") or {}).get("after")
    for _ in range(paginas - 1):
        if not despues:
            break
        extra = _consulta_meta(usuario, campos, limite=50, despues=despues)
        posts += list((extra.get("media") or {}).get("data") or [])
        despues = (((extra.get("media") or {}).get("paging") or {}).get("cursors") or {}).get("after")

    return {
        "datos": {
            "nombre": _texto(bd.get("name"), 120),
            "biografia": _texto(bd.get("biography"), 600),
            "web": _texto(bd.get("website"), 300),
            "seguidores": bd.get("followers_count"),
            "seguidos": bd.get("follows_count"),
            "publicaciones": bd.get("media_count"),
            "foto": bd.get("profile_picture_url") or "",
        },
        "posts": [_post_de_meta(p) for p in posts],
    }


def _post_de_meta(p):
    tipo = p.get("media_product_type") or p.get("media_type") or ""
    tipo = {"REELS": "Reel", "FEED": "Post", "VIDEO": "Video", "IMAGE": "Foto",
            "CAROUSEL_ALBUM": "Carrusel", "STORY": "Historia"}.get(tipo, tipo.title())
    if p.get("media_type") == "CAROUSEL_ALBUM":
        tipo = "Carrusel"
    return {
        "fecha": p.get("timestamp") or "",
        "tipo": tipo,
        "likes": p.get("like_count"),  # None si el dueño ocultó los likes
        "comentarios": p.get("comments_count"),
        "vistas": p.get("view_count"),
        "enlace": p.get("permalink") or "",
        "texto": _texto(p.get("caption"), 400),
    }


# ─── Métricas y alertas ───────────────────────────────────────────────────────

def calcular_metricas(datos, posts, historial=None):
    seguidores = datos.get("seguidores") or 0
    seguidos = datos.get("seguidos")
    ahora = datetime.now(timezone.utc)

    con_fecha = [(p, _fecha(p.get("fecha", ""))) for p in posts]
    con_fecha.sort(key=lambda x: x[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    # Los posts de menos de 48 h todavía están juntando interacciones.
    maduros = [p for p, f in con_fecha if f is None or (ahora - f).total_seconds() > 48 * 3600]
    muestra = (maduros if len(maduros) >= 6 else [p for p, _ in con_fecha])[:20]

    likes_ocultos = any(p.get("likes") is None for p in muestra) and bool(muestra)
    inter = [(p.get("likes") or 0) + (p.get("comentarios") or 0) for p in muestra]
    likes = [p.get("likes") or 0 for p in muestra]
    coms = [p.get("comentarios") or 0 for p in muestra]
    vistas = [p["vistas"] for p in muestra if p.get("vistas")]

    m = {
        "seguidores": seguidores,
        "seguidos": seguidos,
        "posts_analizados": len(muestra),
        "likes_ocultos": likes_ocultos,
    }
    nivel, rango = nivel_cuenta(seguidores)
    m["nivel_cuenta"], m["er_referencia"] = nivel, list(rango)

    if muestra and seguidores:
        m["interacciones_prom"] = round(statistics.mean(inter), 1)
        m["likes_prom"] = round(statistics.mean(likes), 1)
        m["comentarios_prom"] = round(statistics.mean(coms), 1)
        m["er"] = round(statistics.mean(inter) / seguidores * 100, 2)
        m["er_mediana"] = round(statistics.median(inter) / seguidores * 100, 2)
        m["ratio_comentarios"] = round(sum(coms) / sum(likes) * 100, 2) if sum(likes) else None
        media = statistics.mean(inter)
        m["variacion"] = round(statistics.pstdev(inter) / media, 2) if media and len(inter) > 2 else None
        if vistas:
            m["vistas_prom"] = round(statistics.mean(vistas))
            m["tasa_vistas"] = round(statistics.mean(vistas) / seguidores * 100, 1)
        mediana = statistics.median(inter)
        m["virales"] = sum(1 for x in inter if mediana and x > 3 * mediana)
        reels = sum(1 for p in muestra if p.get("tipo") in ("Reel", "Video"))
        m["pct_reels"] = round(reels / len(muestra) * 100)

    fechas = [f for _, f in con_fecha if f]
    if fechas:
        m["dias_ultimo_post"] = max(0, (ahora - fechas[0]).days)
        ventana = fechas[: min(len(fechas), 20)]
        dias = max(1, (ventana[0] - ventana[-1]).days)
        m["posts_semana"] = round(len(ventana) / dias * 7, 1) if len(ventana) > 1 else None

    textos = " \n ".join((p.get("texto") or "").lower() for p in posts)
    m["menciones_competencia"] = sorted({c for c in COMPETENCIA if re.search(r"(?<![a-z])[@#]?" + re.escape(c) + r"(?![a-z])", textos)})
    m["menciones_propias"] = sorted({marca for marca, claves in PROPIAS.items() if any(k in textos for k in claves)})
    if posts:
        sorteos = sum(1 for p in posts if any(w in (p.get("texto") or "").lower() for w in PALABRAS_SORTEO))
        m["pct_sorteos"] = round(sorteos / len(posts) * 100)

    m["crecimiento_30d"] = _crecimiento(historial or [], seguidores)
    return m


def metricas_manuales(manual, historial=None):
    """Para cuentas personales o sin Meta: el equipo carga los promedios."""
    seguidores = _numero(manual.get("seguidores")) or 0
    likes = _numero(manual.get("likes_prom"))
    coms = _numero(manual.get("comentarios_prom"))
    m = {
        "seguidores": int(seguidores) or None,  # None = todavía sin dato
        "seguidos": int(_numero(manual.get("seguidos"))) if _numero(manual.get("seguidos")) is not None else None,
        "posts_analizados": 0,
        "likes_ocultos": False,
        "manual": True,
    }
    nivel, rango = nivel_cuenta(seguidores)
    m["nivel_cuenta"] = nivel if seguidores else None
    m["er_referencia"] = list(rango)
    if seguidores and (likes is not None or coms is not None):
        inter = (likes or 0) + (coms or 0)
        m["interacciones_prom"] = round(inter, 1)
        m["likes_prom"], m["comentarios_prom"] = likes, coms
        m["er"] = m["er_mediana"] = round(inter / seguidores * 100, 2)
        m["ratio_comentarios"] = round(coms / likes * 100, 2) if likes and coms is not None else None
    vistas = _numero(manual.get("vistas_prom"))
    if vistas and seguidores:
        m["vistas_prom"], m["tasa_vistas"] = round(vistas), round(vistas / seguidores * 100, 1)
    if _numero(manual.get("posts_semana")) is not None:
        m["posts_semana"] = _numero(manual.get("posts_semana"))
    if _numero(manual.get("dias_ultimo_post")) is not None:
        m["dias_ultimo_post"] = int(_numero(manual.get("dias_ultimo_post")))
    m["menciones_competencia"], m["menciones_propias"] = [], []
    m["crecimiento_30d"] = _crecimiento(historial or [], seguidores)
    return m


def _crecimiento(historial, seguidores):
    """% de crecimiento contra la foto más vieja de los últimos ~30 días."""
    ahora = datetime.now(timezone.utc)
    previas = []
    for h in historial:
        f = _fecha(h.get("fecha", "") + "T00:00:00+00:00") if len(h.get("fecha", "")) == 10 else _fecha(h.get("fecha", ""))
        if f and 3 <= (ahora - f).days <= 45 and h.get("seguidores"):
            previas.append((f, h["seguidores"]))
    if not previas or not seguidores:
        return None
    _, antes = min(previas)
    return round((seguidores - antes) / antes * 100, 1)


def alertas(m):
    """Señales a revisar, en lenguaje de equipo. (texto, gravedad)."""
    a = []
    er, (bajo, alto) = m.get("er"), m.get("er_referencia", [1, 3])
    if m.get("likes_ocultos"):
        a.append(("La cuenta oculta los likes: el engagement se calculó solo con comentarios y puede estar subestimado. Pide sus estadísticas.", "info"))
    if er is not None:
        if er < bajo * 0.4:
            a.append((f"Engagement muy bajo para su tamaño ({er}% vs. {bajo}–{alto}% esperado). Posible audiencia inactiva o seguidores comprados.", "alta"))
        elif er < bajo:
            a.append((f"Engagement por debajo de lo esperado para su tamaño ({m['nivel_cuenta']}): {er}% vs. {bajo}–{alto}%.", "media"))
        elif er > alto * 2.5:
            a.append((f"Engagement inusualmente alto ({er}%). Puede ser una comunidad muy fiel, un viral reciente o interacciones compradas: revisa los comentarios.", "media"))
    rc = m.get("ratio_comentarios")
    if rc is not None and not m.get("likes_ocultos"):
        if rc < 0.3 and (m.get("likes_prom") or 0) > 200:
            a.append((f"Muy pocos comentarios por cada like ({rc}%). Patrón típico de likes comprados.", "alta"))
        elif rc > 15 and (m.get("seguidores") or 0) > 5000:
            a.append((f"Muchos comentarios por cada like ({rc}%). Puede venir de sorteos o grupos de engagement.", "media"))
    seg, sigue = m.get("seguidores") or 0, m.get("seguidos")
    if sigue and seg > 3000 and sigue > seg * 0.8:
        a.append((f"Sigue a casi tantas cuentas como lo siguen ({sigue:,} / {seg:,}). Señal de crecimiento por follow/unfollow.".replace(",", "."), "media"))
    if m.get("variacion") is not None and m["variacion"] > 1.3:
        a.append(("Interacciones muy desiguales entre posts: depende de pocos virales o sorteos.", "baja"))
    if (m.get("pct_sorteos") or 0) >= 30:
        a.append((f"{m['pct_sorteos']}% de sus posts recientes son sorteos o concursos.", "media"))
    if m.get("dias_ultimo_post") is not None and m["dias_ultimo_post"] > 30:
        a.append((f"No publica hace {m['dias_ultimo_post']} días.", "media"))
    if m.get("crecimiento_30d") is not None and m["crecimiento_30d"] > 25:
        a.append((f"Creció {m['crecimiento_30d']}% en seguidores en el último mes. Si no hubo un viral, puede ser compra de seguidores.", "alta"))
    if m.get("menciones_competencia"):
        a.append(("Menciona marcas de la competencia: " + ", ".join(m["menciones_competencia"]) + ". Revisa si tiene exclusividad.", "media"))
    return [{"texto": t, "gravedad": g} for t, g in a]


# ─── Score ────────────────────────────────────────────────────────────────────

def _score_engagement(m):
    er = m.get("er")
    if er is None:
        return None
    bajo, alto = m["er_referencia"]
    if er < bajo:
        s = er / bajo * 60
    elif er <= alto:
        s = 60 + (er - bajo) / (alto - bajo) * 35
    else:
        s = 95
    if er > alto * 2.5:
        s = 70  # demasiado alto para ser creíble sin revisar
    return round(max(0, min(100, s)))


def _score_autenticidad(m):
    if m.get("er") is None:
        return None
    s = 85
    bajo, _ = m["er_referencia"]
    rc = m.get("ratio_comentarios")
    if rc is not None and not m.get("likes_ocultos"):
        if rc < 0.3 and (m.get("likes_prom") or 0) > 200:
            s -= 25
        elif rc > 15 and (m.get("seguidores") or 0) > 5000:
            s -= 10
    if m["er"] < bajo * 0.4:
        s -= 25
    seg, sigue = m.get("seguidores") or 0, m.get("seguidos")
    if sigue and seg > 3000 and sigue > seg * 0.8:
        s -= 15
    if m.get("variacion") is not None and m["variacion"] > 1.3:
        s -= 8
    if (m.get("pct_sorteos") or 0) >= 30:
        s -= 10
    if m.get("crecimiento_30d") is not None and m["crecimiento_30d"] > 25:
        s -= 15
    return max(0, min(100, s))


def _score_actividad(m):
    pps, dias = m.get("posts_semana"), m.get("dias_ultimo_post")
    if pps is None and dias is None:
        return None
    s = 60
    if pps is not None:
        s = 100 if pps >= 3 else 85 if pps >= 2 else 65 if pps >= 1 else 40 if pps >= 0.5 else 20
    if dias is not None:
        s -= 35 if dias > 30 else 12 if dias > 14 else 0
    return max(0, min(100, s))


def calcular_score(m, ia=None, afinidad_equipo=None):
    """afinidad_equipo: nota de 1 a 5 que pone el equipo en la ficha."""
    partes = {
        "engagement": _score_engagement(m),
        "autenticidad": _score_autenticidad(m),
        "afinidad": None,
        "actividad": _score_actividad(m),
    }
    if ia:
        if isinstance(ia.get("afinidad"), (int, float)):
            partes["afinidad"] = max(0, min(100, round(ia["afinidad"])))
        if isinstance(ia.get("autenticidad"), (int, float)) and partes["autenticidad"] is not None:
            # Las métricas pesan más que la lectura de la IA: son datos, no opinión.
            partes["autenticidad"] = round(partes["autenticidad"] * 0.6 + ia["autenticidad"] * 0.4)
    if afinidad_equipo in (1, 2, 3, 4, 5):
        nota = afinidad_equipo * 20
        # Si hay también lectura de Claude, se promedian: el equipo conoce las marcas,
        # Claude leyó el contenido.
        partes["afinidad"] = nota if partes["afinidad"] is None else round((nota + partes["afinidad"]) / 2)
    con_valor = {k: v for k, v in partes.items() if v is not None}
    if not con_valor:
        return {**partes, "total": None, "nivel": "Sin datos"}
    if partes["engagement"] is None:
        # Sin engagement no hay score: la afinidad sola daría una nota engañosa.
        return {**partes, "total": None, "nivel": "Falta engagement"}
    peso = sum(PESOS[k] for k in con_valor)
    total = round(sum(v * PESOS[k] for k, v in con_valor.items()) / peso)
    # Una alerta grave (likes comprados, crecimiento sospechoso…) pesa aunque el
    # resto de las métricas se vea bien.
    total = max(0, total - min(18, 6 * sum(1 for a in alertas(m) if a["gravedad"] == "alta")))
    nivel = "Recomendado" if total >= 75 else "Potencial" if total >= 60 else "Revisar" if total >= 45 else "No recomendado"
    return {**partes, "total": total, "nivel": nivel}


# ─── Análisis con IA (Claude) ─────────────────────────────────────────────────

def ia_configurada():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


class ErrorIA(Exception):
    pass


HERRAMIENTA_PERFIL = {
    "name": "registrar_analisis",
    "description": "Registra el análisis del perfil de influencer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "afinidad": {"type": "integer", "description": "0-100: qué tan bien encaja con las marcas indicadas (estilo, temas, público, valores)."},
            "autenticidad": {"type": "integer", "description": "0-100: qué tan genuina parece la cuenta y su comunidad según textos, comentarios de muestra y métricas."},
            "resumen": {"type": "string", "description": "2 a 3 frases: quién es, de qué habla y si conviene."},
            "fortalezas": {"type": "array", "items": {"type": "string"}},
            "riesgos": {"type": "array", "items": {"type": "string"}},
            "nichos": {"type": "array", "items": {"type": "string"}, "description": "Temas principales, cortos (menos de 20 caracteres cada uno)."},
            "tono": {"type": "string", "description": "Tono y estilo del contenido, en pocas palabras."},
            "publico_estimado": {"type": "string", "description": "Público probable (edad, género, país) inferido del contenido, aclarando que es una estimación."},
            "mejor_marca": {"type": "string", "description": "De las marcas indicadas, con cuál encaja mejor."},
            "ideas_colaboracion": {"type": "array", "items": {"type": "string"}, "description": "2 o 3 ideas concretas de contenido con la marca."},
            "recomendacion": {"type": "string", "enum": ["Avanzar", "Revisar", "Descartar"]},
        },
        "required": ["afinidad", "autenticidad", "resumen", "fortalezas", "riesgos", "nichos", "recomendacion"],
    },
}

HERRAMIENTA_CAMPANA = {
    "name": "registrar_conclusiones",
    "description": "Registra las conclusiones del reporte de campaña.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resumen": {"type": "string", "description": "3 a 4 frases con el balance de la campaña."},
            "destacados": {"type": "array", "items": {"type": "string"}},
            "a_mejorar": {"type": "array", "items": {"type": "string"}},
            "recomendaciones": {"type": "array", "items": {"type": "string"}, "description": "Con quién repetir, con quién no, y qué cambiar."},
        },
        "required": ["resumen", "destacados", "a_mejorar", "recomendaciones"],
    },
}


def _llamar_claude(sistema, contenido, herramienta, max_tokens=2000):
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": os.environ.get("ANTHROPIC_API_KEY", ""),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": os.environ.get("CREATORS_MODELO", "claude-sonnet-5-5"),
                "max_tokens": max_tokens,
                "system": sistema,
                "tools": [herramienta],
                "tool_choice": {"type": "tool", "name": herramienta["name"]},
                "messages": [{"role": "user", "content": contenido}],
            },
            timeout=120,
        )
    except requests.RequestException as e:
        raise ErrorIA(f"No se pudo conectar con la IA: {e}")
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except Exception:
            msg = r.text[:200]
        if r.status_code == 401:
            msg = "la clave ANTHROPIC_API_KEY no es válida"
        raise ErrorIA(f"La IA respondió con un error ({r.status_code}): {msg}")
    for bloque in r.json().get("content", []):
        if bloque.get("type") == "tool_use":
            return bloque.get("input") or {}
    raise ErrorIA("La IA no devolvió un análisis.")


SISTEMA_PERFIL = (
    "Eres analista de marketing de influencers de Piazza Internacional, distribuidor de marcas de calzado "
    "y estilo de vida en Latinoamérica y el Caribe. Evalúas perfiles de Instagram para colaboraciones. "
    "Sé directo y concreto, en español neutro. Basa todo en los datos recibidos; si algo es una inferencia, dilo. "
    "No inventes cifras. Las métricas ya vienen calculadas: interprétalas, no las recalcules."
)


def contexto_perfil(perfil, m, alertas_lista):
    """Texto con todo lo que se sabe del perfil, para Claude (API o chat)."""
    marcas = perfil.get("marcas") or list(MARCAS.keys())
    contexto_marcas = "\n".join(f"- {MARCAS.get(x, x)}" for x in marcas)
    posts = perfil.get("posts") or []
    lineas_posts = []
    for p in posts[:20]:
        lineas_posts.append(
            f"[{(p.get('fecha') or '')[:10]} · {p.get('tipo')} · {p.get('likes') if p.get('likes') is not None else 'likes ocultos'} likes · "
            f"{p.get('comentarios')} coment."
            + (f" · {p['vistas']} vistas" if p.get("vistas") else "")
            + f"] {(p.get('texto') or '').replace(chr(10), ' ')[:300]}"
        )
    datos = perfil.get("datos") or {}
    metricas_txt = {k: v for k, v in m.items() if k not in ("menciones_propias",)}
    contenido = (
        f"Marcas para las que se evalúa:\n{contexto_marcas}\n\n"
        f"Perfil: {'@' + perfil['usuario'] if perfil.get('usuario') else 'sin usuario registrado'} "
        f"({datos.get('nombre') or perfil.get('nombre_contacto') or 'sin nombre'}) · plataforma: {perfil.get('plataforma') or 'instagram'}\n"
        f"En análisis desde: {perfil.get('en_analisis_desde') or 'sin dato'} · Origen: {perfil.get('origen') or 'sin dato'} · "
        f"Tarifa/costo: {perfil.get('tarifa') or 'sin dato'}\n"
        f"País indicado por el equipo: {perfil.get('pais') or 'sin dato'}\n"
        f"Nicho indicado por el equipo: {perfil.get('nicho') or 'sin dato'}\n"
        f"Biografía: {datos.get('biografia') or 'sin dato'}\n"
        f"Web: {datos.get('web') or 'sin dato'}\n\n"
        f"Métricas calculadas: {json.dumps(metricas_txt, ensure_ascii=False)}\n\n"
        "Alertas automáticas:\n" + ("\n".join("- " + x["texto"] for x in alertas_lista) or "- ninguna") + "\n\n"
        "Posts recientes:\n" + ("\n".join(lineas_posts) or "(no hay posts: cuenta cargada a mano)") + "\n\n"
        "Comentarios de muestra que pegó el equipo:\n" + (perfil.get("comentarios_muestra") or "(no hay)") + "\n\n"
        "Notas del equipo:\n" + (perfil.get("notas") or "(no hay)")
    )
    return contenido


def analizar_con_ia(perfil, m, alertas_lista):
    contenido = contexto_perfil(perfil, m, alertas_lista)
    res = _llamar_claude(SISTEMA_PERFIL, contenido, HERRAMIENTA_PERFIL)
    res["fecha"] = _ahora()
    res["nichos"] = [_texto(x, 20) for x in res.get("nichos") or []][:6]
    return res


# ─── Análisis desde el chat de Claude (sin API) ───────────────────────────────
# La herramienta arma un texto con los datos; el equipo lo pega en Claude, que
# responde con un bloque JSON; ese bloque se pega de vuelta en la ficha.

def _formato_json(herramienta):
    props = herramienta["input_schema"]["properties"]
    lineas = []
    claves = list(props)
    for k, v in props.items():
        tipo = {"integer": "número 0-100", "string": "texto", "array": "lista de textos"}.get(v["type"], v["type"])
        if "enum" in v:
            tipo = " | ".join(f'"{x}"' for x in v["enum"])
        coma = "," if k != claves[-1] else ""
        nota = f"  // {v['description']}" if v.get("description") else ""
        lineas.append(f'  "{k}": {tipo}{coma}{nota}')
    return "{\n" + "\n".join(lineas) + "\n}"


def texto_para_chat_perfil(perfil):
    m = perfil.get("metricas") or {}
    return (
        SISTEMA_PERFIL + "\n\n"
        "Analiza este perfil de influencer. Responde con un breve comentario si quieres y, al final, "
        "con UN bloque JSON con exactamente estos campos (es lo que se pega de vuelta en la herramienta):\n"
        + _formato_json(HERRAMIENTA_PERFIL) + "\n\n"
        "──── Datos del perfil ────\n" + contexto_perfil(perfil, m, perfil.get("alertas") or [])
    )


def texto_para_chat_campana(c, rep):
    return (
        SISTEMA_CAMPANA + "\n\n"
        "Escribe las conclusiones de este reporte de campaña. Responde con un breve comentario si quieres y, "
        "al final, con UN bloque JSON con exactamente estos campos:\n"
        + _formato_json(HERRAMIENTA_CAMPANA) + "\n\n──── Datos ────\n" + contexto_campana(c, rep)
    )


def leer_json_pegado(texto, herramienta):
    """Saca el bloque JSON de lo que respondió Claude y lo valida contra el esquema."""
    texto = texto or ""
    bloque = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", texto, re.S)
    candidato = bloque.group(1) if bloque else None
    if not candidato:
        ini, fin = texto.find("{"), texto.rfind("}")
        candidato = texto[ini:fin + 1] if ini != -1 and fin > ini else ""
    try:
        datos = json.loads(candidato)
    except ValueError:
        raise ValueError("No encontré el bloque JSON en lo que pegaste. Copia la respuesta completa de Claude, incluido el bloque entre llaves { }.")
    if not isinstance(datos, dict):
        raise ValueError("El bloque pegado no tiene el formato esperado.")
    esquema = herramienta["input_schema"]
    faltan = [k for k in esquema["required"] if k not in datos]
    if faltan:
        raise ValueError("Al análisis le faltan campos: " + ", ".join(faltan) + ".")
    limpio = {}
    for k, v in esquema["properties"].items():
        if k not in datos:
            continue
        val = datos[k]
        if v["type"] == "integer":
            n = _numero(val)
            if n is None:
                raise ValueError(f"El campo {k} tiene que ser un número.")
            limpio[k] = max(0, min(100, round(n)))
        elif v["type"] == "array":
            limpio[k] = [_texto(x, 300) for x in (val if isinstance(val, list) else [val]) if _texto(x)][:8]
        else:
            limpio[k] = _texto(val, 1500)
            if "enum" in v and limpio[k] not in v["enum"]:
                limpio[k] = next((e for e in v["enum"] if e.lower() == limpio[k].lower()), v["enum"][1])
    limpio["fecha"] = _ahora()
    limpio["fuente"] = "chat"
    if "nichos" in limpio:
        limpio["nichos"] = [_texto(x, 20) for x in limpio["nichos"]][:6]
    return limpio


# ─── Análisis completo de un perfil ───────────────────────────────────────────

def analizar(perfil, con_ia=True):
    """Actualiza el perfil en el lugar: datos, posts, métricas, alertas, IA, score e historial."""
    errores = []
    usar_manual = (perfil.get("modo") == "manual" or not meta_configurado() or not perfil.get("usuario")
                   or (perfil.get("plataforma") or "instagram") != "instagram")

    if not usar_manual:
        try:
            ig = consultar_instagram(perfil["usuario"])
            perfil["datos"], perfil["posts"] = ig["datos"], ig["posts"]
            perfil["fuente"] = "meta"
        except ErrorMeta as e:
            usar_manual = True
            if _numero((perfil.get("manual") or {}).get("seguidores")):
                errores.append(f"Se usaron los datos cargados a mano porque Instagram no respondió: {e}")
            else:
                errores.append(str(e))

    if usar_manual:
        perfil["fuente"] = ("navegador" if (perfil.get("posts") or (perfil.get("datos") or {}).get("seguidores"))
                            and not _numero((perfil.get("manual") or {}).get("seguidores")) else "manual")
        manual = perfil.get("manual") or {}
        datos = perfil.get("datos") or {}
        datos["seguidores"] = int(_numero(manual.get("seguidores")) or 0) or datos.get("seguidores")
        perfil["datos"] = datos
        leidos = perfil.get("posts") or (perfil.get("datos") or {}).get("seguidores")
        if leidos and not _numero(manual.get("seguidores")):
            # Datos leídos antes desde el navegador: se recalculan con esos posts.
            m = calcular_metricas(perfil.get("datos") or {}, perfil["posts"], perfil.get("historial"))
        else:
            m = metricas_manuales(manual, perfil.get("historial"))
        if not m.get("seguidores"):
            errores.append("Faltan datos: carga al menos seguidores y promedio de likes y comentarios.")
    else:
        m = calcular_metricas(perfil["datos"], perfil["posts"], perfil.get("historial"))

    lista_alertas = alertas(m)
    ia = perfil.get("ia")
    if con_ia and ia_configurada() and (m.get("seguidores") or perfil.get("posts")):
        try:
            ia = analizar_con_ia(perfil, m, lista_alertas)
        except ErrorIA as e:
            errores.append(str(e))

    perfil["metricas"] = m
    perfil["alertas"] = lista_alertas
    perfil["ia"] = ia
    perfil["score"] = calcular_score(m, ia, perfil.get("afinidad_equipo"))
    perfil["errores"] = errores
    perfil["analizado"] = _ahora()

    if m.get("seguidores"):
        hist = [h for h in perfil.get("historial") or [] if h.get("fecha") != _hoy()]
        hist.append({"fecha": _hoy(), "seguidores": m.get("seguidores"), "er": m.get("er"),
                     "score": perfil["score"].get("total")})
        perfil["historial"] = hist[-120:]
    return perfil


# ─── Perfiles: campos editables ───────────────────────────────────────────────

CAMPOS_MANUALES = ("seguidores", "seguidos", "likes_prom", "comentarios_prom", "vistas_prom", "posts_semana", "dias_ultimo_post")


def _aplicar_campos(perfil, body):
    textos = {"origen": 80, "nombre_contacto": 120, "pais": 60, "ciudad": 80, "nicho": 120, "tiktok": 80,
              "email": 160, "telefono": 60, "tarifa": 120, "notas": 4000, "responsable": 80,
              "comentarios_muestra": 6000}
    for campo, maximo in textos.items():
        if campo in body:
            perfil[campo] = _texto(body.get(campo), maximo)
    if "marcas" in body:
        perfil["marcas"] = _lista(body.get("marcas"), permitidos=MARCAS)
    if "etiquetas" in body:
        perfil["etiquetas"] = [_texto(x, 20) for x in _lista(body.get("etiquetas"))]
    if body.get("estado") in ESTADOS:
        perfil["estado"] = body["estado"]
    if "afinidad_equipo" in body:
        n = _numero(body.get("afinidad_equipo"))
        perfil["afinidad_equipo"] = int(n) if n in (1, 2, 3, 4, 5) else None
    if "en_analisis_desde" in body:
        f = _texto(body.get("en_analisis_desde"), 10)
        if f == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", f):
            perfil["en_analisis_desde"] = f
    if body.get("plataforma") in PLATAFORMAS:
        perfil["plataforma"] = body["plataforma"]
    if "usuario" in body and body.get("usuario") is not None and "id" in perfil:
        perfil["usuario"] = limpiar_usuario(body.get("usuario")) or perfil.get("usuario", "")
    if body.get("modo") in ("auto", "manual"):
        perfil["modo"] = body["modo"]
    if isinstance(body.get("manual"), dict):
        perfil["manual"] = {k: _texto(body["manual"].get(k), 30) for k in CAMPOS_MANUALES}
    perfil["actualizado"] = _ahora()


def resumen_perfil(p):
    m, s, d = p.get("metricas") or {}, p.get("score") or {}, p.get("datos") or {}
    return {
        "id": p["id"], "usuario": p["usuario"], "nombre": d.get("nombre") or p.get("nombre_contacto") or "",
        "foto": d.get("foto") or "", "pais": p.get("pais") or "", "marcas": p.get("marcas") or [],
        "nicho": p.get("nicho") or "", "estado": p.get("estado") or "Candidato",
        "etiquetas": p.get("etiquetas") or [],
        "plataforma": p.get("plataforma") or "instagram",
        "en_analisis_desde": p.get("en_analisis_desde") or (p.get("creado") or "")[:10],
        "seguidores": m.get("seguidores"), "er": m.get("er"), "nivel_cuenta": m.get("nivel_cuenta"),
        "score": s.get("total"), "nivel": s.get("nivel"),
        "recomendacion": (p.get("ia") or {}).get("recomendacion"),
        "marca_sugerida": (p.get("ia") or {}).get("mejor_marca") or "",
        "nichos": (p.get("ia") or {}).get("nichos") or [],
        "alertas_altas": sum(1 for a in p.get("alertas") or [] if a.get("gravedad") == "alta"),
        "fuente": p.get("fuente"), "actualizado": p.get("analizado") or p.get("actualizado"),
    }


# ─── Campañas ─────────────────────────────────────────────────────────────────

METRICAS_POST = ("likes", "comentarios", "vistas", "alcance", "impresiones", "clics", "guardados", "compartidos", "ventas")


def _limpiar_participantes(lista, perfiles):
    salida = []
    for x in (lista or [])[:200]:
        pid = _texto(x.get("perfil_id"), 20)
        if pid not in perfiles or any(s["perfil_id"] == pid for s in salida):
            continue
        posts = []
        for p in (x.get("posts") or [])[:60]:
            enlace = _texto(p.get("enlace"), 300)
            if not enlace:
                continue
            post = {"enlace": enlace, "tipo": _texto(p.get("tipo"), 20),
                    "fuente": p.get("fuente") if p.get("fuente") in ("meta", "manual") else "manual",
                    "actualizado": _texto(p.get("actualizado"), 40)}
            for k in METRICAS_POST:
                v = _numero(p.get(k))
                post[k] = int(v) if v is not None else None
            posts.append(post)
        salida.append({
            "perfil_id": pid,
            "costo": _numero(x.get("costo")),
            "entregables": _texto(x.get("entregables"), 300),
            "codigo": _texto(x.get("codigo"), 60),
            "notas": _texto(x.get("notas"), 1000),
            "posts": posts,
        })
    return salida


def _aplicar_campana(c, body, perfiles):
    for campo, maximo in {"nombre": 120, "pais": 60, "objetivo": 600, "moneda": 6, "notas": 4000,
                          "inicio": 10, "fin": 10}.items():
        if campo in body:
            c[campo] = _texto(body.get(campo), maximo)
    if "marca" in body:
        c["marca"] = body["marca"] if body["marca"] in MARCAS else ""
    if "presupuesto" in body:
        c["presupuesto"] = _numero(body.get("presupuesto"))
    if body.get("estado") in ESTADOS_CAMPANA:
        c["estado"] = body["estado"]
    if "participantes" in body:
        c["participantes"] = _limpiar_participantes(body.get("participantes"), perfiles)
    c["actualizado"] = _ahora()


def actualizar_posts_campana(c, perfiles):
    """Trae de Instagram likes, comentarios y vistas de cada post cargado."""
    if not meta_configurado():
        return ["Instagram no está conectado (faltan META_ACCESS_TOKEN y META_IG_USER_ID)."]
    avisos = []
    for part in c.get("participantes") or []:
        perfil = perfiles.get(part["perfil_id"])
        pendientes = {shortcode(p["enlace"]): p for p in part.get("posts") or [] if shortcode(p["enlace"])}
        if not perfil or not pendientes or perfil.get("modo") == "manual" or not perfil.get("usuario"):
            continue  # las cuentas personales se cargan a mano
        try:
            ig = consultar_instagram(perfil["usuario"], paginas=3)
        except ErrorMeta as e:
            avisos.append(f"@{perfil['usuario']}: {e}")
            continue
        por_codigo = {shortcode(p["enlace"]): p for p in ig["posts"]}
        for codigo, post in pendientes.items():
            encontrado = por_codigo.get(codigo)
            if not encontrado:
                avisos.append(f"@{perfil['usuario']}: no se encontró el post {codigo} entre sus últimas publicaciones (o es una historia/colaboración).")
                continue
            for k in ("likes", "comentarios", "vistas"):
                if encontrado.get(k) is not None:
                    post[k] = encontrado[k]
            post["tipo"] = encontrado.get("tipo") or post.get("tipo")
            post["fuente"] = "meta"
            post["actualizado"] = _ahora()
    c["actualizado"] = _ahora()
    return avisos


def reporte_campana(c, perfiles):
    filas = []
    tot = {k: 0 for k in METRICAS_POST}
    tot.update(costo=0, interacciones=0, posts=0, seguidores=0, base_cpm=0)
    for part in c.get("participantes") or []:
        p = perfiles.get(part["perfil_id"]) or {}
        m = p.get("metricas") or {}
        suma = {k: sum((x.get(k) or 0) for x in part.get("posts") or []) for k in METRICAS_POST}
        hay = {k: any(x.get(k) is not None for x in part.get("posts") or []) for k in METRICAS_POST}
        inter = suma["likes"] + suma["comentarios"] + suma["guardados"] + suma["compartidos"]
        n = len(part.get("posts") or [])
        costo = part.get("costo") or 0
        seg = m.get("seguidores") or 0
        base_cpm = suma["impresiones"] if hay["impresiones"] else suma["alcance"] if hay["alcance"] else suma["vistas"]
        er_campana = round((suma["likes"] + suma["comentarios"]) / n / seg * 100, 2) if n and seg else None
        filas.append({
            "perfil_id": part["perfil_id"], "usuario": p.get("usuario") or p.get("nombre_contacto") or "?",
            "nombre": (p.get("datos") or {}).get("nombre") or "", "seguidores": seg,
            "costo": costo, "posts": n, "interacciones": inter,
            **{k: (suma[k] if hay[k] else None) for k in METRICAS_POST},
            "cpe": round(costo / inter, 3) if costo and inter else None,
            "cpm": round(costo / base_cpm * 1000, 2) if costo and base_cpm else None,
            "cpm_base": "impresiones" if hay["impresiones"] else "alcance" if hay["alcance"] else "vistas",
            "costo_venta": round(costo / suma["ventas"], 2) if costo and suma["ventas"] else None,
            "er_campana": er_campana, "er_habitual": m.get("er"),
            "vs_habitual": round(er_campana / m["er"] * 100) if er_campana and m.get("er") else None,
        })
        for k in METRICAS_POST:
            tot[k] += suma[k]
        tot["costo"] += costo
        tot["interacciones"] += inter
        tot["posts"] += n
        tot["seguidores"] += seg
        if base_cpm:
            tot["base_cpm"] += base_cpm
            tot["costo_con_base"] = tot.get("costo_con_base", 0) + costo
    # CPM total: solo con los influencers que tienen impresiones, alcance o vistas,
    # cada uno con la mejor base que tenga. Mezclar costo total con impresiones
    # parciales inflaría el número.
    base = tot.pop("base_cpm")
    costo_base = tot.pop("costo_con_base", 0)
    tot["cpe"] = round(tot["costo"] / tot["interacciones"], 3) if tot["costo"] and tot["interacciones"] else None
    tot["cpm"] = round(costo_base / base * 1000, 2) if costo_base and base else None
    tot["costo_venta"] = round(tot["costo"] / tot["ventas"], 2) if tot["costo"] and tot["ventas"] else None
    tot["influencers"] = len(filas)
    filas.sort(key=lambda f: (f["cpe"] is None, f["cpe"] or 0))
    return {"filas": filas, "totales": tot}


SISTEMA_CAMPANA = (
    "Eres analista de marketing de influencers de Piazza Internacional (calzado y estilo de vida, Latinoamérica). "
    "Escribe conclusiones de un reporte de campaña para el equipo y la gerencia: directo, en español neutro, "
    "basado solo en los números recibidos. CPE = costo por interacción; CPM = costo por mil impresiones "
    "(o alcance/vistas si no hay impresiones). 'vs_habitual' compara el engagement de la campaña con el habitual "
    "del influencer (100 = igual). Si faltan datos para concluir algo, dilo."
)


def contexto_campana(c, rep):
    return (
        f"Campaña: {c.get('nombre')} · Marca: {c.get('marca')} · País: {c.get('pais')} · "
        f"{c.get('inicio')} a {c.get('fin')} · Moneda: {c.get('moneda') or 'USD'}\n"
        f"Objetivo: {c.get('objetivo') or 'sin dato'}\nPresupuesto: {c.get('presupuesto')}\n\n"
        f"Totales: {json.dumps(rep['totales'], ensure_ascii=False)}\n\n"
        f"Por influencer: {json.dumps(rep['filas'], ensure_ascii=False)}"
    )


def conclusiones_campana(c, rep):
    contenido = contexto_campana(c, rep)
    res = _llamar_claude(SISTEMA_CAMPANA, contenido, HERRAMIENTA_CAMPANA, max_tokens=1500)
    res["fecha"] = _ahora()
    return res


# ─── Páginas ──────────────────────────────────────────────────────────────────

def _contexto():
    return {"marcas": list(MARCAS.keys()), "estados": ESTADOS, "estados_campana": ESTADOS_CAMPANA,
            "paises": PAISES, "meta_ok": meta_configurado(), "ia_ok": ia_configurada()}


@creators_bp.route("/creators")
def panel():
    return render_template("creators/panel.html", **_contexto())


@creators_bp.route("/creators/nuevo")
def nuevo():
    return render_template("creators/nuevo.html", **_contexto())


@creators_bp.route("/creators/p/<pid>")
def ficha(pid):
    return render_template("creators/ficha.html", pid=pid, **_contexto())


@creators_bp.route("/creators/campanas")
def campanas():
    return render_template("creators/campanas.html", **_contexto())


@creators_bp.route("/creators/campanas/<cid>")
def campana(cid):
    return render_template("creators/campana.html", cid=cid, **_contexto())


@creators_bp.route("/creators/campanas/<cid>/reporte")
def reporte(cid):
    return render_template("creators/reporte.html", cid=cid, **_contexto())


# ─── API ──────────────────────────────────────────────────────────────────────

def _error(msg, codigo=400):
    return jsonify({"error": msg}), codigo


def _body():
    return request.get_json(silent=True) or {}


@creators_bp.errorhandler(Exception)
def _fallo(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    print(f"[creators] Error: {e!r}")
    return _error(f"Error inesperado: {e}", 500)


@creators_bp.route("/api/creators/estado")
def api_estado():
    return jsonify(_contexto())


@creators_bp.route("/api/creators/perfiles")
def api_perfiles():
    datos = _leer()
    en_campana = {}
    for c in datos["campanas"].values():
        if c.get("estado") == "Activa":
            for part in c.get("participantes") or []:
                en_campana[part["perfil_id"]] = c.get("nombre")
    lista = []
    for p in datos["perfiles"].values():
        r = resumen_perfil(p)
        r["campana_activa"] = en_campana.get(p["id"])
        lista.append(r)
    lista.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    return jsonify({"perfiles": lista})


def _clave_duplicado(p):
    """Usuario si lo tiene; si no, el nombre normalizado."""
    if p.get("usuario"):
        return "u:" + p["usuario"]
    return "n:" + re.sub(r"\s+", " ", (p.get("nombre_contacto") or "").strip().lower())


def _nuevo_perfil(body):
    """Arma un perfil desde el cuerpo de la petición. Devuelve (perfil, error)."""
    usuario = limpiar_usuario(body.get("usuario"))
    nombre = _texto(body.get("nombre_contacto"), 120)
    if not usuario and _texto(body.get("usuario")) and not nombre:
        return None, "Escribe un usuario válido (letras, números, punto o guion bajo)."
    if not usuario and not nombre:
        return None, "Escribe el usuario de Instagram o, si no lo tienes todavía, el nombre del perfil."
    perfil = {"id": _id(), "usuario": usuario, "plataforma": "instagram", "estado": "Candidato",
              "marcas": [], "creado": _ahora(), "historial": [], "modo": "auto",
              "en_analisis_desde": _hoy()}
    _aplicar_campos(perfil, body)
    return perfil, None


@creators_bp.route("/api/creators/perfiles", methods=["POST"])
def api_crear():
    body = _body()
    perfil, error = _nuevo_perfil(body)
    if error:
        return _error(error)
    clave = _clave_duplicado(perfil)
    nombre = f"@{perfil['usuario']}" if perfil["usuario"] else perfil.get("nombre_contacto")
    existente = next((p for p in _leer()["perfiles"].values() if _clave_duplicado(p) == clave), None)
    if existente:
        return jsonify({"error": f"{nombre} ya está registrado.", "id": existente["id"]}), 409
    analizar(perfil, con_ia=bool(body.get("con_ia", True)))  # fuera del lock: tarda

    def guardar(datos):
        if any(_clave_duplicado(p) == clave for p in datos["perfiles"].values()):
            return False
        datos["perfiles"][perfil["id"]] = perfil
        return True
    if not _modificar(guardar):
        return _error(f"{nombre} se registró mientras tanto desde otra pestaña.", 409)
    return jsonify({"perfil": perfil})


@creators_bp.route("/api/creators/importar", methods=["POST"])
def api_importar():
    """Carga varios perfiles de una vez (por ejemplo, desde listas en Excel o PDF).
    No consulta Instagram ni la IA: quedan registrados para analizarlos después.
    Los que ya existen (mismo usuario, o mismo nombre si no tienen usuario) se saltan."""
    lista = (_body().get("perfiles") or [])[:500]
    nuevos, saltados, errores = [], [], []
    for body in lista:
        perfil, error = _nuevo_perfil(body)
        if error:
            errores.append({"perfil": body.get("usuario") or body.get("nombre_contacto"), "error": error})
            continue
        analizar(perfil, con_ia=False)
        perfil["errores"] = [e for e in perfil.get("errores") or [] if not e.startswith("Faltan datos")]
        nuevos.append(perfil)

    def guardar(datos):
        claves = {_clave_duplicado(p) for p in datos["perfiles"].values()}
        for p in nuevos:
            k = _clave_duplicado(p)
            if k in claves:
                saltados.append(p.get("usuario") or p.get("nombre_contacto"))
                continue
            claves.add(k)
            datos["perfiles"][p["id"]] = p
    _modificar(guardar)
    return jsonify({"creados": len(nuevos) - len(saltados), "saltados": saltados, "errores": errores})


@creators_bp.route("/api/creators/perfiles/<pid>")
def api_perfil(pid):
    datos = _leer()
    p = datos["perfiles"].get(pid)
    if not p:
        return _error("Perfil no encontrado.", 404)
    campanas_de = []
    for c in datos["campanas"].values():
        part = next((x for x in c.get("participantes") or [] if x["perfil_id"] == pid), None)
        if part:
            campanas_de.append({"id": c["id"], "nombre": c.get("nombre"), "marca": c.get("marca"),
                                "estado": c.get("estado"), "inicio": c.get("inicio"),
                                "costo": part.get("costo"), "posts": len(part.get("posts") or [])})
    return jsonify({"perfil": p, "campanas": campanas_de})


@creators_bp.route("/api/creators/perfiles/<pid>", methods=["PATCH"])
def api_editar(pid):
    body = _body()

    nuevo_usuario = limpiar_usuario(body.get("usuario")) if body.get("usuario") else ""
    if nuevo_usuario:
        otro = next((p for p in _leer()["perfiles"].values() if p.get("usuario") == nuevo_usuario and p["id"] != pid), None)
        if otro:
            return jsonify({"error": f"@{nuevo_usuario} ya está registrado en otro perfil.", "id": otro["id"]}), 409

    def editar(datos):
        p = datos["perfiles"].get(pid)
        if p:
            _aplicar_campos(p, body)
            if p.get("metricas"):  # la afinidad del equipo cambia el score sin reanalizar
                p["score"] = calcular_score(p["metricas"], p.get("ia"), p.get("afinidad_equipo"))
        return p
    p = _modificar(editar)
    return jsonify({"perfil": p}) if p else _error("Perfil no encontrado.", 404)


@creators_bp.route("/api/creators/perfiles/<pid>/analizar", methods=["POST"])
def api_analizar(pid):
    body = _body()
    p = _leer()["perfiles"].get(pid)
    if not p:
        return _error("Perfil no encontrado.", 404)
    p = json.loads(json.dumps(p))  # copia: el análisis tarda y no se toca el cache
    _aplicar_campos(p, body)
    analizar(p, con_ia=bool(body.get("con_ia", True)))

    def guardar(datos):
        if pid not in datos["perfiles"]:
            return False
        # Conserva lo que el equipo haya editado mientras corría el análisis.
        actual = datos["perfiles"][pid]
        for k in ("datos", "posts", "metricas", "alertas", "ia", "score", "errores", "analizado",
                  "historial", "fuente", "manual", "modo", "comentarios_muestra"):
            actual[k] = p.get(k)
        actual["actualizado"] = _ahora()
        return actual
    actual = _modificar(guardar)
    return jsonify({"perfil": actual}) if actual else _error("Perfil no encontrado.", 404)


@creators_bp.route("/api/creators/perfiles/<pid>/instagram", methods=["POST"])
def api_cargar_instagram(pid):
    """Recibe datos públicos del perfil leídos desde el navegador (seguidores, bio y
    últimos posts con likes y comentarios) y recalcula métricas, alertas y score.
    Sirve mientras no esté conectada la API de Meta."""
    body = _body()
    d = body.get("datos") or {}
    posts = []
    for x in (body.get("posts") or [])[:30]:
        posts.append({
            "fecha": _texto(x.get("fecha"), 40), "tipo": _texto(x.get("tipo"), 20),
            "likes": int(x["likes"]) if isinstance(x.get("likes"), (int, float)) and x["likes"] >= 0 else None,
            "comentarios": int(x.get("comentarios") or 0),
            "vistas": int(x["vistas"]) if isinstance(x.get("vistas"), (int, float)) and x["vistas"] > 0 else None,
            "enlace": _texto(x.get("enlace"), 300), "texto": _texto(x.get("texto"), 400),
        })
    datos_ig = {
        "nombre": _texto(d.get("nombre"), 120), "biografia": _texto(d.get("biografia"), 600),
        "web": _texto(d.get("web"), 300),
        "seguidores": int(_numero(d.get("seguidores")) or 0) or None,
        "seguidos": int(_numero(d.get("seguidos")) or 0) or None,
        "publicaciones": int(_numero(d.get("publicaciones")) or 0) or None,
        "foto": "",
    }

    def guardar(datos):
        p = datos["perfiles"].get(pid)
        if not p:
            return None
        p["datos"], p["posts"], p["fuente"] = datos_ig, posts, "navegador"
        p["manual"] = {k: "" for k in CAMPOS_MANUALES}
        m = calcular_metricas(datos_ig, posts, p.get("historial"))
        p["metricas"], p["alertas"] = m, alertas(m)
        p["score"] = calcular_score(m, p.get("ia"), p.get("afinidad_equipo"))
        p["errores"], p["analizado"], p["actualizado"] = [], _ahora(), _ahora()
        if m.get("seguidores"):
            hist = [h for h in p.get("historial") or [] if h.get("fecha") != _hoy()]
            hist.append({"fecha": _hoy(), "seguidores": m["seguidores"], "er": m.get("er"), "score": p["score"].get("total")})
            p["historial"] = hist[-120:]
        return p
    p = _modificar(guardar)
    return jsonify({"perfil": resumen_perfil(p)}) if p else _error("Perfil no encontrado.", 404)


@creators_bp.route("/api/creators/perfiles/<pid>/para-claude")
def api_para_claude(pid):
    p = _leer()["perfiles"].get(pid)
    if not p:
        return _error("Perfil no encontrado.", 404)
    return jsonify({"texto": texto_para_chat_perfil(p)})


@creators_bp.route("/api/creators/perfiles/<pid>/analisis", methods=["POST"])
def api_pegar_analisis(pid):
    try:
        ia = leer_json_pegado(_body().get("texto"), HERRAMIENTA_PERFIL)
    except ValueError as e:
        return _error(str(e))

    def guardar(datos):
        p = datos["perfiles"].get(pid)
        if p:
            p["ia"] = ia
            p["score"] = calcular_score(p.get("metricas") or {}, ia, p.get("afinidad_equipo"))
            p["actualizado"] = _ahora()
        return p
    p = _modificar(guardar)
    return jsonify({"perfil": p}) if p else _error("Perfil no encontrado.", 404)


@creators_bp.route("/api/creators/perfiles/<pid>", methods=["DELETE"])
def api_borrar(pid):
    def borrar(datos):
        if pid not in datos["perfiles"]:
            return False
        del datos["perfiles"][pid]
        for c in datos["campanas"].values():
            c["participantes"] = [x for x in c.get("participantes") or [] if x["perfil_id"] != pid]
        return True
    return jsonify({"ok": True}) if _modificar(borrar) else _error("Perfil no encontrado.", 404)


@creators_bp.route("/api/creators/exportar.csv")
def api_exportar():
    salida = io.StringIO()
    w = csv.writer(salida)
    w.writerow(["Usuario", "Enlace", "Nombre", "País", "Ciudad", "Marcas", "Nicho", "Estado", "Seguidores",
                "Nivel cuenta", "Engagement %", "Score", "Nivel", "Recomendación IA", "Email", "Teléfono",
                "Tarifa", "TikTok", "Etiquetas", "En análisis desde", "Origen", "Analizado"])
    for p in _leer()["perfiles"].values():
        r, m = resumen_perfil(p), p.get("metricas") or {}
        w.writerow([f"@{p['usuario']}" if p.get("usuario") else "", f"https://instagram.com/{p['usuario']}" if p.get("usuario") else "", r["nombre"], p.get("pais", ""),
                    p.get("ciudad", ""), ", ".join(r["marcas"]), p.get("nicho", ""), r["estado"],
                    m.get("seguidores") or "", m.get("nivel_cuenta") or "",
                    str(m.get("er") or "").replace(".", ","), r["score"] if r["score"] is not None else "",
                    r["nivel"] or "", r["recomendacion"] or "", p.get("email", ""), p.get("telefono", ""),
                    p.get("tarifa", ""), p.get("tiktok", ""), ", ".join(r["etiquetas"]),
                    r["en_analisis_desde"], p.get("origen", ""), (r["actualizado"] or "")[:10]])
    # BOM para que Excel abra bien los acentos.
    return Response("\ufeff" + salida.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=creators-piazza-{_hoy()}.csv"})


@creators_bp.route("/api/creators/campanas")
def api_campanas():
    datos = _leer()
    lista = []
    for c in datos["campanas"].values():
        rep = reporte_campana(c, datos["perfiles"])
        lista.append({k: c.get(k) for k in ("id", "nombre", "marca", "pais", "estado", "inicio", "fin",
                                             "presupuesto", "moneda")}
                     | {"influencers": rep["totales"]["influencers"], "costo": rep["totales"]["costo"],
                        "interacciones": rep["totales"]["interacciones"], "cpe": rep["totales"]["cpe"]})
    lista.sort(key=lambda c: c.get("inicio") or "", reverse=True)
    return jsonify({"campanas": lista})


@creators_bp.route("/api/creators/campanas", methods=["POST"])
def api_crear_campana():
    body = _body()
    if not _texto(body.get("nombre")):
        return _error("La campaña necesita un nombre.")
    c = {"id": _id(), "estado": "Planificada", "moneda": "USD", "participantes": [], "creado": _ahora()}

    def crear(datos):
        _aplicar_campana(c, body, datos["perfiles"])
        datos["campanas"][c["id"]] = c
        return c
    return jsonify({"campana": _modificar(crear)})


def _campana_completa(datos, c):
    perfiles = {}
    for part in c.get("participantes") or []:
        p = datos["perfiles"].get(part["perfil_id"])
        if p:
            perfiles[p["id"]] = resumen_perfil(p)
    return {"campana": c, "perfiles": perfiles, "reporte": reporte_campana(c, datos["perfiles"])}


@creators_bp.route("/api/creators/campanas/<cid>")
def api_campana(cid):
    datos = _leer()
    c = datos["campanas"].get(cid)
    if not c:
        return _error("Campaña no encontrada.", 404)
    return jsonify(_campana_completa(datos, c))


@creators_bp.route("/api/creators/campanas/<cid>", methods=["PATCH"])
def api_editar_campana(cid):
    body = _body()

    def editar(datos):
        c = datos["campanas"].get(cid)
        if not c:
            return None
        _aplicar_campana(c, body, datos["perfiles"])
        return _campana_completa(datos, c)
    res = _modificar(editar)
    return jsonify(res) if res else _error("Campaña no encontrada.", 404)


@creators_bp.route("/api/creators/campanas/<cid>", methods=["DELETE"])
def api_borrar_campana(cid):
    def borrar(datos):
        return datos["campanas"].pop(cid, None) is not None
    return jsonify({"ok": True}) if _modificar(borrar) else _error("Campaña no encontrada.", 404)


@creators_bp.route("/api/creators/campanas/<cid>/actualizar", methods=["POST"])
def api_actualizar_campana(cid):
    datos = _leer()
    c = datos["campanas"].get(cid)
    if not c:
        return _error("Campaña no encontrada.", 404)
    copia = json.loads(json.dumps(c))
    avisos = actualizar_posts_campana(copia, datos["perfiles"])

    def guardar(datos):
        actual = datos["campanas"].get(cid)
        if not actual:
            return None
        # Solo se pisan las métricas públicas de los posts que siguen existiendo.
        nuevos = {(x["perfil_id"], p["enlace"]): p for x in copia["participantes"] for p in x["posts"]}
        for x in actual.get("participantes") or []:
            for p in x.get("posts") or []:
                n = nuevos.get((x["perfil_id"], p["enlace"]))
                if n and n.get("fuente") == "meta":
                    for k in ("likes", "comentarios", "vistas", "tipo", "fuente", "actualizado"):
                        p[k] = n.get(k)
        actual["actualizado"] = _ahora()
        return _campana_completa(datos, actual)
    res = _modificar(guardar)
    if not res:
        return _error("Campaña no encontrada.", 404)
    res["avisos"] = avisos
    return jsonify(res)


@creators_bp.route("/api/creators/campanas/<cid>/para-claude")
def api_campana_para_claude(cid):
    datos = _leer()
    c = datos["campanas"].get(cid)
    if not c:
        return _error("Campaña no encontrada.", 404)
    return jsonify({"texto": texto_para_chat_campana(c, reporte_campana(c, datos["perfiles"]))})


@creators_bp.route("/api/creators/campanas/<cid>/conclusiones-pegadas", methods=["POST"])
def api_pegar_conclusiones(cid):
    try:
        conc = leer_json_pegado(_body().get("texto"), HERRAMIENTA_CAMPANA)
    except ValueError as e:
        return _error(str(e))

    def guardar(datos):
        c = datos["campanas"].get(cid)
        if c:
            c["conclusiones"] = conc
            return _campana_completa(datos, c)
    res = _modificar(guardar)
    return jsonify(res) if res else _error("Campaña no encontrada.", 404)


@creators_bp.route("/api/creators/campanas/<cid>/conclusiones", methods=["POST"])
def api_conclusiones(cid):
    if not ia_configurada():
        return _error("La IA no está configurada (falta ANTHROPIC_API_KEY en Render).")
    datos = _leer()
    c = datos["campanas"].get(cid)
    if not c:
        return _error("Campaña no encontrada.", 404)
    try:
        conc = conclusiones_campana(c, reporte_campana(c, datos["perfiles"]))
    except ErrorIA as e:
        return _error(str(e), 502)

    def guardar(datos):
        actual = datos["campanas"].get(cid)
        if actual:
            actual["conclusiones"] = conc
            return _campana_completa(datos, actual)
    res = _modificar(guardar)
    return jsonify(res) if res else _error("Campaña no encontrada.", 404)
