// Creators Piazza — utilidades compartidas por las pantallas.
const CR = (() => {
  async function api(url, opciones = {}) {
    const op = { headers: { "Content-Type": "application/json" }, ...opciones };
    if (op.body && typeof op.body !== "string") op.body = JSON.stringify(op.body);
    let r;
    try { r = await fetch(url, op); } catch (e) { throw new Error("No hay conexión con el servidor."); }
    const tipo = r.headers.get("content-type") || "";
    if (!tipo.includes("json")) {
      if (r.redirected || r.status === 401) { location.href = "/login?siguiente=" + encodeURIComponent(location.pathname); }
      throw new Error("Respuesta inesperada del servidor (" + r.status + ").");
    }
    const datos = await r.json();
    if (r.status === 401) { location.href = "/login?siguiente=" + encodeURIComponent(location.pathname); }
    if (!r.ok) { const e = new Error(datos.error || "Error " + r.status); e.datos = datos; e.status = r.status; throw e; }
    return datos;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function num(n, dec = 0) {
    if (n === null || n === undefined || n === "" || isNaN(n)) return "—";
    return Number(n).toLocaleString("es-PA", { maximumFractionDigits: dec, minimumFractionDigits: 0 });
  }
  function corto(n) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    n = Number(n);
    if (n >= 1e6) return (n / 1e6).toLocaleString("es-PA", { maximumFractionDigits: 1 }) + " M";
    if (n >= 1e4) return Math.round(n / 1e3).toLocaleString("es-PA") + " k";
    if (n >= 1e3) return (n / 1e3).toLocaleString("es-PA", { maximumFractionDigits: 1 }) + " k";
    return num(n);
  }
  const pct = (n, dec = 2) => (n === null || n === undefined ? "—" : num(n, dec) + "%");
  const dinero = (n, moneda = "USD") => (n === null || n === undefined || n === "" ? "—" :
    Number(n).toLocaleString("es-PA", { style: "currency", currency: moneda || "USD", maximumFractionDigits: n < 10 ? 3 : n >= 1000 ? 0 : 2 }));
  function fecha(iso) {
    if (!iso) return "—";
    const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
    return isNaN(d) ? iso : d.toLocaleDateString("es-PA", { day: "numeric", month: "short", year: "numeric" });
  }
  function claseScore(s) {
    if (s === null || s === undefined) return "cr-s-nada";
    return s >= 75 ? "cr-s-alto" : s >= 60 ? "cr-s-medio" : s >= 45 ? "cr-s-bajo" : "cr-s-malo";
  }
  const score = (s) => `<span class="cr-score ${claseScore(s)}">${s ?? "—"}</span>`;
  function avatar(foto, usuario, grande = false) {
    const ini = esc((usuario || "?").slice(0, 2).toUpperCase());
    const cls = "cr-avatar" + (grande ? " grande" : "");
    if (!foto) return `<span class="${cls}">${ini}</span>`;
    // Las fotos de Instagram vencen: si no carga, se muestran las iniciales.
    return `<img class="${cls}" src="${esc(foto)}" alt="" referrerpolicy="no-referrer"
      onerror="this.outerHTML='<span class=&quot;${cls}&quot;>${ini}</span>'">`;
  }
  const chips = (lista) => (lista || []).map((x) => `<span class="cr-chip">${esc(x)}</span>`).join("");
  function cargando(ver, texto) {
    let el = document.getElementById("cr-cargando");
    if (!el) {
      el = document.createElement("div");
      el.id = "cr-cargando"; el.className = "cr-cargando";
      el.innerHTML = '<div><span class="spinner"></span><div id="cr-cargando-txt"></div></div>';
      document.body.appendChild(el);
    }
    document.getElementById("cr-cargando-txt").textContent = texto || "Cargando…";
    el.classList.toggle("ver", !!ver);
  }
  return { api, esc, num, corto, pct, dinero, fecha, score, claseScore, avatar, chips, cargando };
})();
