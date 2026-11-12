# Creators Piazza

Base de datos y análisis de perfiles de influencers para las marcas de Piazza Internacional.
Herramienta propia, separada del Photo Tool, con su propia contraseña.

## Qué hace

- **Registrar perfil**: se escribe el usuario de Instagram. La app consulta la cuenta por la API oficial de Meta,
  calcula métricas (engagement, comentarios por like, frecuencia, vistas de reels, menciones a la competencia),
  marca alertas (likes comprados, follow/unfollow, sorteos, crecimiento raro) y pide a Claude un análisis de
  afinidad con las marcas elegidas.
- **Score de 0 a 100**: engagement 30 % · autenticidad 25 % · afinidad 25 % · actividad 20 %.
  Cada alerta grave resta 6 puntos (máximo 18). Niveles: Recomendado (75+), Potencial (60+), Revisar (45+), No recomendado.
- **Historial**: cada vez que se reanaliza se guarda una foto. Conviene reanalizar una vez al mes.
- **Campañas**: influencers, costo, enlaces de las publicaciones y métricas privadas (alcance, impresiones, clics, ventas)
  que manda el influencer. Likes, comentarios y vistas se traen solos de Instagram.
- **Reporte**: CPE, CPM, costo por venta, engagement en campaña contra el habitual y conclusiones con IA. Se imprime a PDF.
- **Exportar**: botón “Descargar Excel (CSV)” en el panel.

Los datos se guardan en Dropbox: `/creators-piazza/datos.json`, con una copia diaria en `/creators-piazza/respaldos/`.

## Variables de entorno (Render → Environment)

| Variable | Para qué |
|---|---|
| `CREATORS_PASSWORD` | Contraseña de acceso del equipo (obligatoria) |
| `DROPBOX_APP_KEY`, `DROPBOX_REFRESH_TOKEN` | App de Dropbox "Creators Piazza Intl" (carpeta propia), donde se guardan los datos (obligatorias). `DROPBOX_APP_SECRET` solo si el token no se sacó con PKCE |
| `CREATORS_DROPBOX_DIR` | Carpeta en Dropbox (default `/creators-piazza`) |
| `META_ACCESS_TOKEN` | Token de Meta para consultar Instagram |
| `META_IG_USER_ID` | Número de id de la cuenta de Instagram profesional desde la que se consulta |
| `ANTHROPIC_API_KEY` | Clave de la API de Claude para el análisis con IA |
| `META_API_VERSION` | Opcional (default `v25.0`) |
| `CREATORS_MODELO` | Opcional (default `claude-sonnet-5-5`) |

Sin las de Meta, los perfiles se cargan a mano. Sin la de Claude, el score se calcula solo con métricas.

## Publicar en Render

1. En **render.com** → *New* → **Blueprint** → elegir el repositorio `creators`. Render lee `render.yaml`.
2. Completar las variables que pide: contraseña, las tres de Dropbox (pueden ser las mismas que usa el Photo Tool) y,
   cuando estén, las de Meta y Claude.
3. Al terminar, la herramienta queda en `https://creators-piazza.onrender.com` (o el nombre que asigne Render).

En el plan gratis el servicio se duerme tras 15 minutos sin uso y tarda unos 30-50 segundos en despertar.

## Correr en local

```
pip install -r requirements.txt
cp .env.example .env   # completar
python app.py          # http://localhost:5070
```
Sin credenciales de Dropbox, los datos se guardan en `datos_local/`.

## Conectar Instagram (app de Meta)

1. Una cuenta de Instagram de alguna marca tiene que ser **profesional (empresa)** y estar **vinculada a una página de Facebook**.
2. En **developers.facebook.com** → *Mis apps* → *Crear app*. Tipo: empresa. Agregar el producto **Instagram (API con inicio de sesión de Facebook)**.
3. En **Herramientas → Explorador de la API Graph**: elegir la app, *Generar token de acceso* y dar los permisos
   `instagram_basic`, `pages_show_list`, `pages_read_engagement` y `business_management`.
4. En el mismo explorador consultar `me/accounts?fields=name,instagram_business_account`.
   El número de `instagram_business_account.id` es el **META_IG_USER_ID**.
5. El token del explorador dura horas. Para uno estable:
   - **Recomendado**: en *Business Manager → Configuración del negocio → Usuarios del sistema*, crear un usuario del sistema,
     asignarle la app y la página, y generar un token con los mismos permisos (no vence).
   - Alternativa: extenderlo en el *Depurador de tokens* (dura 60 días; hay que renovarlo).
6. Cargar `META_ACCESS_TOKEN` y `META_IG_USER_ID` en Render y guardar (Render redespliega solo).

La app puede quedar en modo desarrollo: solo la usan personas con rol en ella.
**Limitación de Meta:** solo se pueden consultar cuentas **profesionales** (creador o empresa). Las personales se cargan a mano
marcando “Cargar las métricas a mano”. Meta tampoco deja leer los comentarios de otras cuentas: para que la IA juzgue mejor la
autenticidad, se pueden pegar comentarios de muestra en la ficha.

## Conectar la IA (Claude)

1. En **console.anthropic.com** crear una cuenta de la empresa, cargar saldo y generar una **API key**.
2. Cargarla en Render como `ANTHROPIC_API_KEY`.

Cada análisis de perfil cuesta unos pocos centavos de dólar.
