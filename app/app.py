# -*- coding: utf-8 -*-
"""Aplicación Web Local 100% Offline - Almacén de Alimentos CLAP (Fuerte Tiuna).

Arquitectura MVC ligera sobre Flask + PyMySQL + MariaDB 10.4:
- Gestión eficiente de base de datos mediante una única conexión por petición
  HTTP almacenada en `flask.g` y cerrada automáticamente en `teardown_appcontext`.
- Caché por petición de `periodo_activo` y `stock_actual` para evitar consultas
  duplicadas entre las rutas y el procesador global de plantillas (`inject_globals`).
- Control de acceso por roles (MASTER y OPERADOR), código PIN de seguridad para
  anulaciones/cambios críticos, trazabilidad financiera de deudas y saldos a favor,
  y bitácora completa de auditoría y accesos.
"""
import datetime
import functools
import hmac
import os
import re
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
import pymysql

from db import (
    build_lookup_maps,
    fmt_bs,
    fmt_fecha_es,
    fmt_num_es,
    format_period_label,
    format_unidad_display,
    get_active_period,
    get_config_val,
    get_conn,
    get_stock_periodo,
    hash_password,
    humanize_audit_payload,
    load_config,
    log_access,
    log_audit,
    next_period_meta,
    normalize_concepto,
    parse_float,
    parse_int,
    set_config_val,
    sync_unidad_saldo,
    validate_fullname,
    validate_password_policy,
    verify_password,
)
from exporter import detect_usb_drives, export_period_bundle

cfg = load_config()
app = Flask(__name__)
app.secret_key = cfg.get("secret_key", "clap-local-secret-2026")
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Filtros personalizados para formateo numérico, monetario y de fechas en Jinja2
app.jinja_env.filters["bs"] = fmt_bs
app.jinja_env.filters["num_es"] = lambda v: fmt_num_es(v, 2)
app.jinja_env.filters["int_es"] = lambda v: fmt_num_es(v, 0)
app.jinja_env.filters["fecha_es"] = fmt_fecha_es
app.jinja_env.filters["periodo_fmt"] = format_period_label


# ---------------------------------------------------------------------------
# 1. Gestión de Conexión a Base de Datos y Caché por Petición (flask.g)
# ---------------------------------------------------------------------------
def get_db():
    """Obtiene o abre la única conexión a MariaDB asociada a la petición HTTP actual."""
    if "db" not in g:
        g.db = get_conn()
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    """Cierra la conexión al finalizar la petición HTTP; revierte cambios si hubo error."""
    db = g.pop("db", None)
    if db is not None:
        if exc is not None:
            try:
                db.rollback()
            except Exception:
                pass
        db.close()


def get_req_period(cur):
    """Devuelve el período activo cacheadolo en `flask.g` durante la petición actual."""
    if "req_periodo" not in g:
        g.req_periodo = get_active_period(cur)
    return g.req_periodo


def get_req_stock(cur, id_periodo):
    """Devuelve el stock disponible del período cacheado en `flask.g` para la petición actual."""
    cache = g.setdefault("req_stock", {})
    if id_periodo not in cache:
        cache[id_periodo] = get_stock_periodo(cur, id_periodo)
    return cache[id_periodo]


# ---------------------------------------------------------------------------
# 2. Utilidades Internas, Seguridad y Decoradores de Acceso
# ---------------------------------------------------------------------------
def _audit(cur, id_periodo, id_usuario, tabla, id_registro, accion, motivo, datos_anteriores=None, datos_nuevos=None):
    """Registra eventos de auditoría excluyendo sesiones técnicas de soporte."""
    if session.get("es_soporte"):
        return
    log_audit(cur, id_periodo, id_usuario, tabla, id_registro, accion, motivo, datos_anteriores, datos_nuevos)


def _verify_master_pin(cur, accion_desc="realizar esta acción"):
    """Valida el código de seguridad numérico del usuario MASTER usando comparación constante."""
    if session.get("es_soporte"):
        return True, None
    pin_conf = (request.form.get("pin_confirmacion") or "").strip()
    cur.execute("SELECT pin_seguridad FROM usuarios WHERE rol='MASTER' ORDER BY id_usuario LIMIT 1")
    m = cur.fetchone()
    if not m or not m.get("pin_seguridad"):
        return False, f"Debe registrar primero su Código de seguridad en Usuarios antes de {accion_desc}."
    if not pin_conf or not hmac.compare_digest(pin_conf, str(m["pin_seguridad"])):
        return False, "Código de seguridad incorrecto."
    return True, None


def get_obs_from_form():
    """Lee el campo de observaciones evitando autocompletado nativo mediante clave dinámica."""
    obs_key = (request.form.get("obs_key") or "").strip()
    if obs_key and obs_key in request.form:
        val = (request.form.get(obs_key) or "").strip()
        return val or None
    val = (request.form.get("observaciones") or "").strip()
    return val or None


def is_first_historical_period(cur):
    """Indica si el sistema solo posee el período inicial (permite editar stock inicial)."""
    cur.execute("SELECT COUNT(*) AS c FROM periodos_cierre")
    return cur.fetchone()["c"] == 1


def _parse_cedula_num(raw_val, required=False):
    """Valida parte numérica de cédula (máx. 10 dígitos) y antepone prefijo 'V-'."""
    raw = (raw_val or "").strip()
    if not raw:
        return (not required), None
    if not raw.isdigit() or len(raw) > 10:
        return False, None
    return True, f"V-{raw}"


def _strip_v_prefix(val):
    """Extrae únicamente los dígitos de una cédula almacenada para precargar formularios."""
    if not val:
        return ""
    return re.sub(r"\D", "", str(val))[:10]


def login_required(fn):
    """Exige que el usuario haya iniciado sesión para acceder a la ruta."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def master_required(fn):
    """Exige sesión iniciada con rol MASTER para acceder a funciones administrativas."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("rol") != "MASTER":
            flash("Acción reservada exclusivamente para el usuario MASTER.", "error")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_globals():
    """Inyecta datos de sesión y del período activo en todas las plantillas HTML sin duplicar conexiones."""
    ctx = {
        "user_actual": session.get("usuario"),
        "nombre_actual": session.get("nombre"),
        "rol_actual": session.get("rol"),
        "es_soporte": session.get("es_soporte", False),
        "hoy": datetime.date.today().isoformat(),
        "periodo_activo": None,
        "stock_actual": None,
    }
    if "user_id" in session:
        try:
            conn = get_db()
            with conn.cursor() as cur:
                p = get_req_period(cur)
                ctx["periodo_activo"] = p
                if p:
                    ctx["stock_actual"] = get_req_stock(cur, p["id_periodo"])
        except Exception:
            pass
    return ctx


# ---------------------------------------------------------------------------
# 3. Autenticación y Control de Sesión
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    """Valida credenciales locales, registra accesos en `historial_accesos` y abre sesión."""
    if request.method == "POST":
        user_key = (request.form.get("user_key") or "usuario").strip()
        pass_key = (request.form.get("pass_key") or "clave").strip()
        usuario = (request.form.get(user_key) or request.form.get("usuario") or "").strip()
        clave = request.form.get(pass_key) or request.form.get("clave") or ""
        cur_cfg = load_config()
        sup_u = str(cur_cfg.get("support_user") or "")
        sup_p = str(cur_cfg.get("support_pass") or "")

        conn = get_db()
        with conn.cursor() as cur:
            # Acceso reservado de soporte técnico
            if sup_u and sup_p and usuario == sup_u and hmac.compare_digest(clave, sup_p):
                cur.execute("SELECT id_usuario FROM usuarios WHERE rol = 'MASTER' ORDER BY id_usuario LIMIT 1")
                m_row = cur.fetchone()
                session.clear()
                session["user_id"] = m_row["id_usuario"] if m_row else 1
                session["usuario"] = sup_u
                session["nombre"] = "Soporte Técnico"
                session["rol"] = "MASTER"
                session["es_soporte"] = True
                return redirect(url_for("dashboard"))

            p = get_req_period(cur)
            id_per = p["id_periodo"] if p else 1
            cur.execute("SELECT * FROM usuarios WHERE LOWER(usuario) = %s", (usuario.lower(),))
            u = cur.fetchone()
            if u and u["activo"] == 1 and verify_password(clave, u["password_hash"]):
                session.clear()
                session["user_id"] = u["id_usuario"]
                session["usuario"] = u["usuario"]
                session["nombre"] = u["nombre_completo"]
                session["rol"] = u["rol"]
                session["es_soporte"] = False
                cur.execute("UPDATE usuarios SET ultimo_acceso = NOW() WHERE id_usuario = %s", (u["id_usuario"],))
                log_access(cur, u["id_usuario"], u["usuario"], id_per, "LOGIN_OK")
                conn.commit()
                flash(f"Bienvenido, {u['nombre_completo']} ({u['rol']}).", "ok")
                return redirect(url_for("dashboard"))

            log_access(cur, u["id_usuario"] if u else None, usuario.lower() or "vacio", id_per, "LOGIN_FALLIDO")
            conn.commit()
            flash("Usuario o contraseña incorrectos (o cuenta inactiva).", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    """Registra el cierre de sesión en `historial_accesos` y limpia las cookies de sesión."""
    if "user_id" in session and not session.get("es_soporte"):
        conn = get_db()
        with conn.cursor() as cur:
            p = get_req_period(cur)
            log_access(cur, session["user_id"], session["usuario"], p["id_periodo"] if p else 1, "LOGOUT")
            conn.commit()
    session.clear()
    flash("Sesión cerrada correctamente.", "ok")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# 4. Panel Principal (Dashboard) y Configuración de Período Activo (MASTER)
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    """Muestra indicadores clave de inventario, balance financiero y despachos filtrables por estado."""
    filtro_estado = (request.args.get("estado") or "TODOS").strip().upper()
    if filtro_estado not in ("TODOS", "PAGADO", "SALDO_A_FAVOR", "PARCIAL", "PENDIENTE", "ARRASTRADO"):
        filtro_estado = "TODOS"

    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        es_primer_periodo = is_first_historical_period(cur)
        stock = get_req_stock(cur, p["id_periodo"]) if p else {}
        cur.execute("SELECT * FROM v_rendicion_cuentas WHERE id_periodo = %s", (p["id_periodo"],))
        rend = cur.fetchone() or {}

        sql_desp = """
            SELECT d.id_despacho,
                   CONCAT('DES-', LPAD(d.id_despacho, 6, '0')) AS codigo_movimiento,
                   d.fecha_despacho,
                   CONCAT(IFNULL(CONCAT(ub.codigo_unidad, ' '), ''), ub.nombre_unidad) AS batallon_beneficiario,
                   CONCAT(IFNULL(CONCAT(ur.codigo_unidad, ' '), ''), ur.nombre_unidad) AS batallon_que_retiro,
                   d.nombre_autorizado_retiro, d.cedula_autorizado_retiro,
                   d.cantidad_familiar, d.cantidad_tango, d.total_bolsas,
                   d.monto_base_bs, d.saldo_anterior_aplicado_bs,
                   d.monto_neto_exigible_bs, d.monto_total_pagado_bs,
                   d.diferencia_saldo_bs, d.saldo_arrastrado, d.estado_financiero,
                   d.observaciones
            FROM despachos d
            JOIN unidades_militares ub ON ub.id_unidad = d.id_batallon_beneficiario
            LEFT JOIN unidades_militares ur ON ur.id_unidad = d.id_batallon_retira
            WHERE d.id_periodo = %s AND d.tipo_salida = 'INTERNO_BATALLON' AND d.anulado = 0
        """
        params_desp = [p["id_periodo"]]
        if filtro_estado == "ARRASTRADO":
            sql_desp += " AND d.saldo_arrastrado = 1 ORDER BY d.id_despacho DESC"
        elif filtro_estado in ("PAGADO", "SALDO_A_FAVOR", "PARCIAL", "PENDIENTE"):
            sql_desp += " AND d.saldo_arrastrado = 0 AND d.estado_financiero = %s ORDER BY d.id_despacho DESC"
            params_desp.append(filtro_estado)
        else:
            sql_desp += " ORDER BY d.id_despacho DESC LIMIT 10"

        cur.execute(sql_desp, params_desp)
        ultimos_despachos = cur.fetchall()

        # Carga en lote (evita consultas N+1) de pagos asociados a los despachos mostrados
        pagos_por_desp = {}
        if ultimos_despachos:
            ids_desp = [d["id_despacho"] for d in ultimos_despachos]
            placeholders = ",".join(["%s"] * len(ids_desp))
            cur.execute(
                f"""SELECT id_despacho, concepto_plan, monto_pagado_bs, referencia_bancaria, fecha_pago
                    FROM pagos_batallon
                    WHERE id_despacho IN ({placeholders}) AND anulado = 0
                    ORDER BY id_pago ASC""",
                ids_desp,
            )
            for pg in cur.fetchall():
                pagos_por_desp.setdefault(pg["id_despacho"], []).append(pg)
        for d in ultimos_despachos:
            d["pagos_list"] = pagos_por_desp.get(d["id_despacho"], [])

        resumen_filtro = {
            "cantidad": len(ultimos_despachos),
            "familiar": sum(int(d.get("cantidad_familiar") or 0) for d in ultimos_despachos),
            "tango": sum(int(d.get("cantidad_tango") or 0) for d in ultimos_despachos),
            "total_bolsas": sum(int(d.get("total_bolsas") or 0) for d in ultimos_despachos),
            "neto_bs": sum(float(d.get("monto_neto_exigible_bs") or 0) for d in ultimos_despachos),
            "pagado_bs": sum(float(d.get("monto_total_pagado_bs") or 0) for d in ultimos_despachos),
        }

        cur.execute(
            """SELECT COUNT(*) AS c FROM despachos
               WHERE id_periodo = %s AND tipo_salida='INTERNO_BATALLON'
                 AND saldo_arrastrado = 0 AND estado_financiero IN ('PENDIENTE','PARCIAL') AND anulado = 0""",
            (p["id_periodo"],),
        )
        pendientes_pago = cur.fetchone()["c"]

    return render_template(
        "dashboard.html",
        periodo=p,
        es_primer_periodo=es_primer_periodo,
        stock=stock,
        rend=rend,
        ultimos_despachos=ultimos_despachos,
        pendientes_pago=pendientes_pago,
        filtro_estado=filtro_estado,
        resumen_filtro=resumen_filtro,
    )


@app.route("/periodo/configurar", methods=["POST"])
@master_required
def configurar_periodo():
    """Actualiza el precio unitario de la bolsa y el stock inicial (solo primer período)."""
    precio = parse_float(request.form.get("precio_unitario_bolsa"), -1.0)
    motivo = (request.form.get("motivo") or "Actualización de precio por bolsa").strip()
    if precio < 0:
        flash("El precio por bolsa no puede ser negativo ni inválido.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        es_primer = is_first_historical_period(cur)
        antes = {"precio_unitario_bolsa": p["precio_unitario_bolsa"]}
        despues = {"precio_unitario_bolsa": precio}

        if es_primer:
            stock_ini = parse_int(request.form.get("stock_inicial_bolsas"), -1)
            if stock_ini < 0:
                flash("El stock inicial no puede ser negativo ni inválido.", "error")
                return redirect(url_for("dashboard"))
            antes["stock_inicial_bolsas"] = p["stock_inicial_bolsas"]
            despues["stock_inicial_bolsas"] = stock_ini
            cur.execute(
                "UPDATE periodos_cierre SET precio_unitario_bolsa = %s, stock_inicial_bolsas = %s WHERE id_periodo = %s",
                (precio, stock_ini, p["id_periodo"]),
            )
        else:
            cur.execute(
                "UPDATE periodos_cierre SET precio_unitario_bolsa = %s WHERE id_periodo = %s",
                (precio, p["id_periodo"]),
            )

        _audit(cur, p["id_periodo"], session["user_id"], "periodos_cierre", p["id_periodo"], "CONFIG", motivo, antes, despues)
        conn.commit()
        flash("Configuración del precio guardada correctamente.", "ok")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# 5. Entradas de Lotes al Almacén
# ---------------------------------------------------------------------------
@app.route("/entradas")
@login_required
def entradas():
    """Lista las entradas de lotes del período activo."""
    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute(
            """SELECT e.*, CONCAT('ENT-', LPAD(e.id_entrada,6,'0')) AS codigo_movimiento, u.usuario
               FROM entradas_inventario e JOIN usuarios u ON u.id_usuario = e.id_usuario_registra
               WHERE e.id_periodo = %s ORDER BY e.id_entrada DESC""",
            (p["id_periodo"],),
        )
        lista = cur.fetchall()
    return render_template("entradas.html", entradas=lista)


@app.route("/entradas/crear", methods=["POST"])
@login_required
def entradas_crear():
    """Registra una nueva recepción de lote en el período abierto."""
    fecha = request.form.get("fecha_recepcion") or datetime.date.today().isoformat()
    cantidad = parse_int(request.form.get("cantidad_bolsas"), 0)
    obs = get_obs_from_form()
    if cantidad <= 0:
        flash("La cantidad de bolsas debe ser mayor a 0.", "error")
        return redirect(url_for("entradas"))

    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute(
            """INSERT INTO entradas_inventario (id_periodo, fecha_recepcion, cantidad_bolsas, observaciones, id_usuario_registra)
               VALUES (%s, %s, %s, %s, %s)""",
            (p["id_periodo"], fecha, cantidad, obs, session["user_id"]),
        )
        eid = cur.lastrowid
        _audit(cur, p["id_periodo"], session["user_id"], "entradas_inventario", eid, "CREAR", "Recepción de lote", None,
               {"fecha_recepcion": fecha, "cantidad_bolsas": cantidad, "observaciones": obs})
        conn.commit()
        flash(f"Entrada ENT-{eid:06d} registrada por {fmt_num_es(cantidad, 0)} bolsas.", "ok")
    return redirect(url_for("entradas"))


@app.route("/entradas/editar/<int:eid>", methods=["POST"])
@login_required
def entradas_editar(eid):
    """Edita una entrada verificando que no deje colaboraciones sin cobertura ni stock negativo."""
    fecha = request.form.get("fecha_recepcion")
    cantidad = parse_int(request.form.get("cantidad_bolsas"), 0)
    obs = get_obs_from_form()
    motivo = (request.form.get("motivo") or "").strip()
    if cantidad <= 0 or not motivo:
        flash("Cantidad válida y motivo de edición son obligatorios.", "error")
        return redirect(url_for("entradas"))

    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute("SELECT * FROM entradas_inventario WHERE id_entrada = %s AND id_periodo = %s AND anulado = 0", (eid, p["id_periodo"]))
        antes = cur.fetchone()
        if not antes:
            flash("Registro no editable (no pertenece al período abierto o está anulado).", "error")
            return redirect(url_for("entradas"))

        # Verificar integridad del stock disponible y colaboraciones ligadas al lote si se reduce cantidad
        reduccion = int(antes["cantidad_bolsas"]) - cantidad
        if reduccion > 0:
            cur.execute("SELECT IFNULL(SUM(cantidad_bolsas),0) AS usado FROM colaboraciones_descarga WHERE id_entrada=%s AND anulado=0", (eid,))
            usado = int(cur.fetchone()["usado"])
            if usado > 0 and cantidad <= usado:
                flash(f"La nueva cantidad ({cantidad}) debe ser mayor a las colaboraciones asociadas al lote ({usado} bolsas).", "error")
                return redirect(url_for("entradas"))
            stock = get_req_stock(cur, p["id_periodo"])
            disp = int(stock["stock_disponible"]) if stock else 0
            if reduccion > disp:
                flash(f"No puede reducir el lote en {reduccion} bolsas porque el stock disponible actual es {disp}.", "error")
                return redirect(url_for("entradas"))

        cur.execute(
            "UPDATE entradas_inventario SET fecha_recepcion=%s, cantidad_bolsas=%s, observaciones=%s WHERE id_entrada=%s",
            (fecha, cantidad, obs, eid),
        )
        _audit(cur, p["id_periodo"], session["user_id"], "entradas_inventario", eid, "EDITAR", motivo,
               {"fecha_recepcion": antes["fecha_recepcion"], "cantidad_bolsas": antes["cantidad_bolsas"], "observaciones": antes["observaciones"]},
               {"fecha_recepcion": fecha, "cantidad_bolsas": cantidad, "observaciones": obs})
        conn.commit()
        flash(f"Entrada ENT-{eid:06d} editada y registrada en auditoría.", "ok")
    return redirect(url_for("entradas"))


@app.route("/entradas/anular/<int:eid>", methods=["POST"])
@master_required
def entradas_anular(eid):
    """Anula lógicamente una entrada previa validación del código PIN del MASTER."""
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Debe indicar el motivo de anulación.", "error")
        return redirect(url_for("entradas"))

    conn = get_db()
    with conn.cursor() as cur:
        ok_pin, msg_pin = _verify_master_pin(cur, "anular una entrada de lote")
        if not ok_pin:
            flash(msg_pin, "error")
            return redirect(url_for("entradas"))

        p = get_req_period(cur)
        cur.execute("SELECT * FROM entradas_inventario WHERE id_entrada=%s AND id_periodo=%s AND anulado=0", (eid, p["id_periodo"]))
        antes = cur.fetchone()
        if not antes:
            flash("Registro no encontrado o ya anulado.", "error")
            return redirect(url_for("entradas"))
        cur.execute("UPDATE entradas_inventario SET anulado=1, motivo_anulacion=%s WHERE id_entrada=%s", (motivo, eid))
        _audit(cur, p["id_periodo"], session["user_id"], "entradas_inventario", eid, "ANULAR", motivo,
               {"cantidad_bolsas": antes["cantidad_bolsas"], "fecha_recepcion": antes["fecha_recepcion"]}, {"anulado": 1})
        conn.commit()
        flash(f"Entrada ENT-{eid:06d} anulada.", "ok")
    return redirect(url_for("entradas"))


# ---------------------------------------------------------------------------
# 6. Salidas por Colaboración y Descarga
# ---------------------------------------------------------------------------
@app.route("/colaboraciones")
@login_required
def colaboraciones():
    """Lista salidas por colaboración y lotes disponibles para asociar."""
    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute(
            """SELECT c.*, CONCAT('COL-', LPAD(c.id_colaboracion,6,'0')) AS codigo_movimiento,
                      IF(c.id_entrada IS NOT NULL, CONCAT('ENT-', LPAD(c.id_entrada,6,'0')), NULL) AS codigo_entrada,
                      u.usuario
               FROM colaboraciones_descarga c JOIN usuarios u ON u.id_usuario = c.id_usuario_registra
               WHERE c.id_periodo = %s ORDER BY c.id_colaboracion DESC""",
            (p["id_periodo"],),
        )
        lista = cur.fetchall()
        cur.execute(
            """SELECT e.id_entrada, CONCAT('ENT-', LPAD(e.id_entrada,6,'0')) AS codigo, e.fecha_recepcion, e.cantidad_bolsas,
                      (e.cantidad_bolsas - IFNULL((SELECT SUM(col.cantidad_bolsas) FROM colaboraciones_descarga col WHERE col.id_entrada = e.id_entrada AND col.anulado = 0), 0)) AS disponible_lote
               FROM entradas_inventario e WHERE e.id_periodo=%s AND e.anulado=0 ORDER BY e.id_entrada DESC""",
            (p["id_periodo"],),
        )
        entradas_list = cur.fetchall()
    return render_template("colaboraciones.html", colaboraciones=lista, entradas=entradas_list)


@app.route("/colaboraciones/crear", methods=["POST"])
@login_required
def colaboraciones_crear():
    """Registra entrega de bolsas por apoyo en descarga verificando disponibilidad del lote y stock."""
    fecha = request.form.get("fecha") or datetime.date.today().isoformat()
    cantidad = parse_int(request.form.get("cantidad_bolsas"), 0)
    receptor = "Ayudante de descarga"
    obs = get_obs_from_form()
    id_entrada = parse_int(request.form.get("id_entrada"), 0)
    if cantidad <= 0:
        flash("La cantidad de bolsas debe ser mayor a 0.", "error")
        return redirect(url_for("colaboraciones"))
    if id_entrada <= 0:
        flash("Debe seleccionar el camión o lote relacionado.", "error")
        return redirect(url_for("colaboraciones"))

    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute("SELECT cantidad_bolsas FROM entradas_inventario WHERE id_entrada=%s AND id_periodo=%s AND anulado=0", (id_entrada, p["id_periodo"]))
        lote = cur.fetchone()
        if not lote:
            flash("El lote relacionado no es válido.", "error")
            return redirect(url_for("colaboraciones"))
        cur.execute("SELECT IFNULL(SUM(cantidad_bolsas),0) AS usado FROM colaboraciones_descarga WHERE id_entrada=%s AND anulado=0", (id_entrada,))
        disp_lote = int(lote["cantidad_bolsas"]) - int(cur.fetchone()["usado"])
        if cantidad >= disp_lote:
            flash(f"La cantidad ({cantidad}) debe ser estrictamente menor a la cantidad disponible del lote ({disp_lote}).", "error")
            return redirect(url_for("colaboraciones"))

        stock = get_req_stock(cur, p["id_periodo"])
        if cantidad > int(stock["stock_disponible"]):
            flash("Stock insuficiente para registrar esta colaboración.", "error")
            return redirect(url_for("colaboraciones"))

        cur.execute(
            """INSERT INTO colaboraciones_descarga (id_periodo, id_entrada, fecha, cantidad_bolsas, receptor, observaciones, id_usuario_registra)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (p["id_periodo"], id_entrada, fecha, cantidad, receptor, obs, session["user_id"]),
        )
        cid = cur.lastrowid
        _audit(cur, p["id_periodo"], session["user_id"], "colaboraciones_descarga", cid, "CREAR", "Colaboración de descarga", None,
               {"fecha": fecha, "cantidad_bolsas": cantidad, "receptor": receptor, "observaciones": obs})
        conn.commit()
        flash(f"Colaboración registrada ({cantidad} bolsa(s)).", "ok")
    return redirect(url_for("colaboraciones"))


@app.route("/colaboraciones/editar/<int:cid>", methods=["POST"])
@login_required
def colaboraciones_editar(cid):
    """Edita una colaboración verificando stock disponible global y del lote asociado."""
    cantidad = parse_int(request.form.get("cantidad_bolsas"), 0)
    receptor = "Ayudante de descarga"
    obs = get_obs_from_form()
    motivo = (request.form.get("motivo") or "").strip()
    if cantidad <= 0 or not motivo:
        flash("Cantidad válida y motivo son obligatorios.", "error")
        return redirect(url_for("colaboraciones"))

    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute("SELECT * FROM colaboraciones_descarga WHERE id_colaboracion=%s AND id_periodo=%s AND anulado=0", (cid, p["id_periodo"]))
        antes = cur.fetchone()
        if not antes:
            flash("Registro no editable.", "error")
            return redirect(url_for("colaboraciones"))

        delta = cantidad - int(antes["cantidad_bolsas"])
        if delta > 0:
            stock = get_req_stock(cur, p["id_periodo"])
            disp = int(stock["stock_disponible"]) if stock else 0
            if delta > disp:
                flash(f"Stock insuficiente para incrementar {delta} bolsa(s) (disponible: {disp}).", "error")
                return redirect(url_for("colaboraciones"))

        if antes.get("id_entrada"):
            cur.execute("SELECT cantidad_bolsas FROM entradas_inventario WHERE id_entrada=%s AND anulado=0", (antes["id_entrada"],))
            lote = cur.fetchone()
            if lote:
                cur.execute("SELECT IFNULL(SUM(cantidad_bolsas),0) AS usado FROM colaboraciones_descarga WHERE id_entrada=%s AND anulado=0 AND id_colaboracion<>%s", (antes["id_entrada"], cid))
                disp_lote = int(lote["cantidad_bolsas"]) - int(cur.fetchone()["usado"])
                if cantidad >= disp_lote:
                    flash(f"La cantidad ({cantidad}) debe ser estrictamente menor a la cantidad disponible del lote ({disp_lote}).", "error")
                    return redirect(url_for("colaboraciones"))

        cur.execute("UPDATE colaboraciones_descarga SET cantidad_bolsas=%s, receptor=%s, observaciones=%s WHERE id_colaboracion=%s",
                    (cantidad, receptor, obs, cid))
        _audit(cur, p["id_periodo"], session["user_id"], "colaboraciones_descarga", cid, "EDITAR", motivo,
               {"cantidad_bolsas": antes["cantidad_bolsas"], "observaciones": antes["observaciones"]},
               {"cantidad_bolsas": cantidad, "observaciones": obs})
        conn.commit()
        flash("Colaboración editada y auditada.", "ok")
    return redirect(url_for("colaboraciones"))


@app.route("/colaboraciones/anular/<int:cid>", methods=["POST"])
@master_required
def colaboraciones_anular(cid):
    """Anula lógicamente una salida por colaboración previa validación de PIN MASTER."""
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Indique el motivo de anulación.", "error")
        return redirect(url_for("colaboraciones"))

    conn = get_db()
    with conn.cursor() as cur:
        ok_pin, msg_pin = _verify_master_pin(cur, "anular una colaboración")
        if not ok_pin:
            flash(msg_pin, "error")
            return redirect(url_for("colaboraciones"))

        p = get_req_period(cur)
        cur.execute("SELECT * FROM colaboraciones_descarga WHERE id_colaboracion=%s AND id_periodo=%s AND anulado=0", (cid, p["id_periodo"]))
        antes = cur.fetchone()
        if not antes:
            flash("Registro no encontrado o ya anulado.", "error")
            return redirect(url_for("colaboraciones"))
        cur.execute("UPDATE colaboraciones_descarga SET anulado=1, motivo_anulacion=%s WHERE id_colaboracion=%s", (motivo, cid))
        _audit(cur, p["id_periodo"], session["user_id"], "colaboraciones_descarga", cid, "ANULAR", motivo,
               {"cantidad_bolsas": antes["cantidad_bolsas"], "fecha": antes["fecha"]}, {"anulado": 1})
        conn.commit()
        flash(f"Colaboración COL-{cid:06d} anulada.", "ok")
    return redirect(url_for("colaboraciones"))


# ---------------------------------------------------------------------------
# 7. Despachos Internos/Externos y Pagos de Batallones
# ---------------------------------------------------------------------------
@app.route("/despachos")
@login_required
def despachos():
    """Muestra formulario de despacho (alta o edición) y listado del período activo."""
    filtro_tipo = request.args.get("tipo", "TODOS")
    editar_id = parse_int(request.args.get("editar"), 0)
    conn = get_db()
    edit_desp = None
    edit_pagos = []
    edit_meta = {}

    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute("SELECT * FROM unidades_militares WHERE activo = 1 ORDER BY codigo_unidad, nombre_unidad")
        unidades_raw = cur.fetchall()
        unidades_json = []
        unit_disp_map = {}
        for u in unidades_raw:
            disp = format_unidad_display(u["codigo_unidad"], u["nombre_unidad"])
            unit_disp_map[u["id_unidad"]] = disp
            unidades_json.append({
                "id": u["id_unidad"],
                "codigo": u["codigo_unidad"] or "",
                "nombre": u["nombre_unidad"],
                "display": disp,
                "saldo": float(u["saldo_acumulado_bs"] or 0),
                "grupo": u["codigos_agrupados_texto"] or "",
            })
        cur.execute("SELECT * FROM galpones_externos WHERE activo = 1 ORDER BY nombre_galpon")
        galpones = cur.fetchall()

        if editar_id > 0:
            cur.execute(
                "SELECT *, CONCAT('DES-', LPAD(id_despacho,6,'0')) AS codigo_movimiento FROM despachos WHERE id_despacho=%s AND id_periodo=%s AND anulado=0",
                (editar_id, p["id_periodo"]),
            )
            edit_desp = cur.fetchone()
            if edit_desp:
                cur.execute(
                    "SELECT * FROM pagos_batallon WHERE id_despacho=%s AND anulado=0 ORDER BY id_pago",
                    (editar_id,),
                )
                edit_pagos = cur.fetchall()
                veh_str = edit_desp.get("vehiculo_descripcion") or ""
                edit_meta = {
                    "benef_display": unit_disp_map.get(edit_desp.get("id_batallon_beneficiario"), ""),
                    "retira_display": unit_disp_map.get(edit_desp.get("id_batallon_retira") or edit_desp.get("id_batallon_beneficiario"), ""),
                    "ced_aut_num": _strip_v_prefix(edit_desp.get("cedula_autorizado_retiro")),
                    "ced_cond_num": _strip_v_prefix(edit_desp.get("cedula_conductor")),
                    "veh_camion": "Camión" in veh_str,
                    "veh_transporte": "Transporte" in veh_str,
                    "saldo_ant": float(edit_desp.get("saldo_anterior_aplicado_bs") or 0),
                    "precio_aplicado": float(edit_desp.get("precio_unitario_aplicado") or p["precio_unitario_bolsa"] or 0),
                    "pagos_json": [
                        {
                            "id_pago": pg["id_pago"],
                            "concepto": normalize_concepto(pg["concepto_plan"]),
                            "monto": float(pg["monto_pagado_bs"]),
                            "ref": pg["referencia_bancaria"] or "",
                            "fecha": pg["fecha_pago"].isoformat() if hasattr(pg["fecha_pago"], "isoformat") else (str(pg["fecha_pago"]) if pg["fecha_pago"] else str(edit_desp["fecha_despacho"])),
                        }
                        for pg in edit_pagos
                    ],
                }

        sql = """
            SELECT d.*, CONCAT('DES-', LPAD(d.id_despacho,6,'0')) AS codigo_movimiento,
                   CONCAT(IFNULL(CONCAT(ub.codigo_unidad, ' '), ''), ub.nombre_unidad) AS benef_nombre,
                   ub.codigos_agrupados_texto,
                   CONCAT(IFNULL(CONCAT(ur.codigo_unidad, ' '), ''), ur.nombre_unidad) AS retira_nombre,
                   g.nombre_galpon, g.estado_region, u.usuario AS usuario_reg,
                   (SELECT COUNT(*) FROM pagos_batallon pb WHERE pb.id_despacho = d.id_despacho AND pb.anulado = 0) AS num_pagos
            FROM despachos d
            LEFT JOIN unidades_militares ub ON ub.id_unidad = d.id_batallon_beneficiario
            LEFT JOIN unidades_militares ur ON ur.id_unidad = d.id_batallon_retira
            LEFT JOIN galpones_externos g ON g.id_galpon = d.id_galpon_destino
            JOIN usuarios u ON u.id_usuario = d.id_usuario_registra
            WHERE d.id_periodo = %s
        """
        params = [p["id_periodo"]]
        if filtro_tipo in ("INTERNO_BATALLON", "EXTERNO_GALPON"):
            sql += " AND d.tipo_salida = %s"
            params.append(filtro_tipo)
        sql += " ORDER BY d.id_despacho DESC"
        cur.execute(sql, params)
        lista = cur.fetchall()

    return render_template(
        "despachos.html",
        despachos=lista,
        unidades_json=unidades_json,
        galpones=galpones,
        filtro_tipo=filtro_tipo,
        edit_desp=edit_desp,
        edit_pagos=edit_pagos,
        edit_meta=edit_meta,
    )


def _extract_payment_rows(form_data, default_fecha):
    """Extrae y valida las filas múltiples de pago/abono enviadas desde el formulario de despacho."""
    pago_ids = form_data.getlist("pago_id[]")
    montos = form_data.getlist("pago_monto[]")
    refs = form_data.getlist("pago_ref[]")
    fechas_p = form_data.getlist("pago_fecha[]")
    conceptos = form_data.getlist("pago_concepto[]")

    rows = []
    total_pagado = 0.0
    for idx, m_str in enumerate(montos):
        m_str = (m_str or "").strip().replace(",", ".")
        r_str = (refs[idx] if idx < len(refs) else "").strip()
        pid_str = (pago_ids[idx] if idx < len(pago_ids) else "").strip()
        if not m_str and not r_str:
            continue
        m_val = round(parse_float(m_str, -1.0), 2)
        if m_val <= 0:
            return False, "Cada monto pagado debe ser mayor a 0 y tener formato numérico válido.", None, 0.0
        if not re.match(r"^\d{4,12}$", r_str):
            return False, "Cada referencia bancaria debe contener únicamente entre 4 y 12 dígitos numéricos.", None, 0.0
        f_p = (fechas_p[idx] if idx < len(fechas_p) else "").strip() or default_fecha
        conc = normalize_concepto(conceptos[idx] if idx < len(conceptos) else "")
        pid_val = int(pid_str) if pid_str.isdigit() else None
        rows.append((pid_val, conc, m_val, r_str, f_p))
        total_pagado = round(total_pagado + m_val, 2)
    return True, None, rows, total_pagado


@app.route("/despachos/crear", methods=["POST"])
@login_required
def despachos_crear():
    """Registra un despacho interno (con arrastre automático de saldo y pagos) o externo."""
    tipo = request.form.get("tipo_salida")
    fecha = request.form.get("fecha_despacho") or datetime.date.today().isoformat()
    obs = get_obs_from_form()

    conn = get_db()
    try:
        with conn.cursor() as cur:
            p = get_req_period(cur)
            stock = get_req_stock(cur, p["id_periodo"])
            disp = int(stock["stock_disponible"]) if stock else 0

            if tipo == "INTERNO_BATALLON":
                id_benef = parse_int(request.form.get("id_batallon_beneficiario"), 0)
                id_retira = parse_int(request.form.get("id_batallon_retira"), 0) or id_benef
                fam = parse_int(request.form.get("cantidad_familiar"), 0)
                tan = parse_int(request.form.get("cantidad_tango"), 0)
                total = fam + tan

                if id_benef <= 0 or fam < 0 or tan < 0 or total <= 0:
                    flash("Debe seleccionar una unidad beneficiaria válida e indicar cantidades positivas.", "error")
                    return redirect(url_for("despachos"))

                cur.execute("SELECT * FROM unidades_militares WHERE id_unidad = %s AND activo = 1", (id_benef,))
                ub = cur.fetchone()
                cur.execute("SELECT * FROM unidades_militares WHERE id_unidad = %s AND activo = 1", (id_retira,))
                ur = cur.fetchone()
                if not ub or not ur:
                    flash("La unidad beneficiaria o la unidad que retira no es válida en el catálogo.", "error")
                    return redirect(url_for("despachos"))

                nom_aut = (request.form.get("nombre_autorizado_retiro") or "").strip()
                ok_ced, ced_aut = _parse_cedula_num(request.form.get("cedula_autorizado_num"), required=True)
                if not nom_aut or not ok_ced or not ced_aut:
                    flash("El Nombre del retirante y la Cédula del retirante (solo dígitos, máx. 10) son obligatorios.", "error")
                    return redirect(url_for("despachos"))

                if total > disp:
                    flash(f"Stock insuficiente ({fmt_num_es(disp,0)} bolsas disponibles, intenta despachar {fmt_num_es(total,0)}).", "error")
                    return redirect(url_for("despachos"))

                precio = float(p["precio_unitario_bolsa"] or 0)
                if precio <= 0:
                    flash("El usuario MASTER debe configurar primero el precio vigente de la bolsa en el Panel Principal.", "error")
                    return redirect(url_for("despachos"))

                # Aplicar saldo acumulado previo (positivo = favor / negativo = deuda)
                saldo_ant = float(ub["saldo_acumulado_bs"] or 0)
                monto_base = round(total * precio, 2)
                monto_neto = round(monto_base - saldo_ant, 2)

                tiene_recibo = 1 if request.form.get("tiene_recibo") == "1" else 0
                pagos_a_insertar = []
                total_pagado = 0.0
                if tiene_recibo == 1:
                    ok_p, err_p, pagos_a_insertar, total_pagado = _extract_payment_rows(request.form, fecha)
                    if not ok_p:
                        flash(err_p, "error")
                        return redirect(url_for("despachos"))

                if saldo_ant != 0:
                    # Marca despachos previos abiertos de la unidad como ya arrastrados al actual
                    cur.execute(
                        """UPDATE despachos SET saldo_arrastrado = 1
                           WHERE id_batallon_beneficiario = %s AND id_periodo = %s
                             AND tipo_salida = 'INTERNO_BATALLON' AND anulado = 0 AND saldo_arrastrado = 0""",
                        (id_benef, p["id_periodo"]),
                    )
                    cur.execute("UPDATE unidades_militares SET saldo_cierre_no_aplicado_bs = 0 WHERE id_unidad = %s", (id_benef,))

                cur.execute(
                    """INSERT INTO despachos
                       (id_periodo, tipo_salida, fecha_despacho, cantidad_familiar, cantidad_tango, total_bolsas,
                        id_batallon_beneficiario, id_batallon_retira, nombre_autorizado_retiro, cedula_autorizado_retiro,
                        tiene_recibo, precio_unitario_aplicado, monto_base_bs, saldo_anterior_aplicado_bs,
                        monto_neto_exigible_bs, observaciones, id_usuario_registra)
                       VALUES (%s,'INTERNO_BATALLON',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        p["id_periodo"], fecha, fam, tan, total,
                        id_benef, id_retira, nom_aut, ced_aut,
                        tiene_recibo, precio, monto_base, saldo_ant,
                        monto_neto, obs, session["user_id"],
                    ),
                )
                did = cur.lastrowid

                for _, conc, m_val, r_str, f_p in pagos_a_insertar:
                    cur.execute(
                        """INSERT INTO pagos_batallon
                           (id_despacho, id_periodo, id_batallon_pagador, concepto_plan, monto_pagado_bs,
                            referencia_bancaria, fecha_pago, id_usuario_registra)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (did, p["id_periodo"], id_benef, conc, m_val, r_str, f_p, session["user_id"]),
                    )

                # Si no hubo pagos (trg_pagos_ai no se disparó), invocamos el SP explícitamente
                if not pagos_a_insertar:
                    cur.callproc("sp_recalcular_despacho", (did,))

                if saldo_ant != 0:
                    cur.execute(
                        """INSERT INTO historial_saldos_batallon (id_unidad, id_periodo, tipo, monto_bs, saldo_resultante_bs, motivo, id_usuario)
                           VALUES (%s, %s, 'APLICACION', %s, (SELECT saldo_acumulado_bs FROM unidades_militares WHERE id_unidad=%s), %s, %s)""",
                        (id_benef, p["id_periodo"], -saldo_ant, id_benef, f"Aplicado a despacho DES-{did:06d}", session["user_id"]),
                    )

                benef_txt = format_unidad_display(ub["codigo_unidad"], ub["nombre_unidad"])
                ret_txt = "misma unidad" if id_retira == id_benef else format_unidad_display(ur["codigo_unidad"], ur["nombre_unidad"])
                _audit(
                    cur, p["id_periodo"], session["user_id"], "despachos", did, "CREAR",
                    f"Despacho interno a {benef_txt} (Retiró: {ret_txt})", None,
                    {
                        "tipo": tipo, "beneficiario": id_benef, "retira": id_retira,
                        "familiar": fam, "tango": tan, "total": total,
                        "neto_bs": monto_neto, "monto_pagado_bs": total_pagado, "observaciones": obs,
                    },
                )
                conn.commit()
                flash(f"Despacho interno DES-{did:06d} registrado ({fam} Familiar + {tan} Tango = {total} bolsas).", "ok")

            elif tipo == "EXTERNO_GALPON":
                id_galpon = parse_int(request.form.get("id_galpon_destino"), 0)
                fam = parse_int(request.form.get("ext_familiar"), 0)
                tan = parse_int(request.form.get("ext_tango"), 0)
                total = parse_int(request.form.get("ext_total_bolsas"), 0) or (fam + tan)
                if id_galpon <= 0 or fam < 0 or tan < 0 or total <= 0 or total < (fam + tan):
                    flash("Seleccione el galpón destino y una cantidad válida de bolsas.", "error")
                    return redirect(url_for("despachos"))
                if total > disp:
                    flash(f"Stock insuficiente ({fmt_num_es(disp,0)} bolsas disponibles, intenta despachar {fmt_num_es(total,0)}).", "error")
                    return redirect(url_for("despachos"))

                nom_cond = (request.form.get("nombre_conductor") or "").strip() or None
                ok_cc, ced_cond = _parse_cedula_num(request.form.get("cedula_conductor_num"), required=False)
                if not ok_cc:
                    flash("La cédula del conductor debe contener únicamente dígitos numéricos (máx. 10).", "error")
                    return redirect(url_for("despachos"))
                veh_tipos = [v for v in request.form.getlist("vehiculo_tipo[]") if v in ("Camión", "Transporte")]
                veh = ", ".join(veh_tipos) or None
                placa = (request.form.get("placa_vehiculo") or "").strip()[:8] or None

                cur.execute(
                    """INSERT INTO despachos
                       (id_periodo, tipo_salida, fecha_despacho, cantidad_familiar, cantidad_tango, total_bolsas,
                        id_galpon_destino, nombre_conductor, cedula_conductor, vehiculo_descripcion, placa_vehiculo,
                        estado_financiero, observaciones, id_usuario_registra)
                       VALUES (%s,'EXTERNO_GALPON',%s,%s,%s,%s,%s,%s,%s,%s,%s,'NO_APLICA',%s,%s)""",
                    (p["id_periodo"], fecha, fam, tan, total, id_galpon, nom_cond, ced_cond, veh, placa, obs, session["user_id"]),
                )
                did = cur.lastrowid
                cur.execute("SELECT nombre_galpon, estado_region FROM galpones_externos WHERE id_galpon = %s", (id_galpon,))
                g_row = cur.fetchone()
                g_nom = f"{g_row['nombre_galpon']} ({g_row['estado_region']})" if g_row and g_row["estado_region"] else (g_row["nombre_galpon"] if g_row else "")
                _audit(
                    cur, p["id_periodo"], session["user_id"], "despachos", did, "CREAR", f"Despacho externo a {g_nom}", None,
                    {"tipo": tipo, "galpon": id_galpon, "total": total, "conductor": nom_cond, "placa": placa, "observaciones": obs},
                )
                conn.commit()
                flash(f"Despacho externo DES-{did:06d} registrado ({fmt_num_es(total,0)} bolsas).", "ok")
            else:
                flash("Tipo de salida inválido.", "error")
    except pymysql.MySQLError as ex:
        conn.rollback()
        flash(f"Error de base de datos: {ex}", "error")
    return redirect(url_for("despachos"))


@app.route("/despachos/editar/<int:did>", methods=["POST"])
@login_required
def despachos_editar(did):
    """Edita un despacho del período activo validando disponibilidad de stock y actualizando pagos."""
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Toda edición exige ingresar un motivo breve.", "error")
        return redirect(url_for("despachos", editar=did))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            p = get_req_period(cur)
            cur.execute("SELECT * FROM despachos WHERE id_despacho=%s AND id_periodo=%s AND anulado=0", (did, p["id_periodo"]))
            antes = cur.fetchone()
            if not antes:
                flash("Despacho no editable (período cerrado o registro anulado).", "error")
                return redirect(url_for("despachos"))

            stock = get_req_stock(cur, p["id_periodo"])
            disp = int(stock["stock_disponible"]) if stock else 0
            fecha = request.form.get("fecha_despacho") or str(antes["fecha_despacho"])
            obs = get_obs_from_form()

            if antes["tipo_salida"] == "INTERNO_BATALLON":
                id_benef_old = antes["id_batallon_beneficiario"]
                id_benef = parse_int(request.form.get("id_batallon_beneficiario"), 0) or id_benef_old or 0
                id_retira = parse_int(request.form.get("id_batallon_retira"), 0) or id_benef
                fam = parse_int(request.form.get("cantidad_familiar"), 0)
                tan = parse_int(request.form.get("cantidad_tango"), 0)
                total = fam + tan
                if id_benef <= 0 or fam < 0 or tan < 0 or total <= 0:
                    flash("Seleccione una unidad beneficiaria válida y cantidades positivas.", "error")
                    return redirect(url_for("despachos", editar=did))

                delta = total - int(antes["total_bolsas"])
                if delta > 0 and delta > disp:
                    flash(f"Stock insuficiente para incrementar {delta} bolsa(s) (disponible: {disp}).", "error")
                    return redirect(url_for("despachos", editar=did))

                cur.execute("SELECT * FROM unidades_militares WHERE id_unidad=%s AND activo=1", (id_benef,))
                ub = cur.fetchone()
                cur.execute("SELECT * FROM unidades_militares WHERE id_unidad=%s AND activo=1", (id_retira,))
                ur = cur.fetchone()
                if not ub or not ur:
                    flash("La unidad beneficiaria o quien retira no es válida.", "error")
                    return redirect(url_for("despachos", editar=did))

                nom_aut = (request.form.get("nombre_autorizado_retiro") or "").strip()
                ok_ced, ced_aut = _parse_cedula_num(request.form.get("cedula_autorizado_num"), required=True)
                if not nom_aut or not ok_ced or not ced_aut:
                    flash("El Nombre del retirante y la Cédula del retirante (solo dígitos, máx. 10) son obligatorios.", "error")
                    return redirect(url_for("despachos", editar=did))

                precio = float(antes["precio_unitario_aplicado"] or p["precio_unitario_bolsa"])
                monto_base = round(total * precio, 2)
                saldo_ant = float(ub["saldo_acumulado_bs"] or 0) if id_benef != id_benef_old else float(antes["saldo_anterior_aplicado_bs"] or 0)
                monto_neto = round(monto_base - saldo_ant, 2)

                tiene_recibo = 1 if request.form.get("tiene_recibo") == "1" else 0
                pagos_enviados = []
                if tiene_recibo == 1:
                    ok_p, err_p, pagos_enviados, _ = _extract_payment_rows(request.form, fecha)
                    if not ok_p:
                        flash(err_p, "error")
                        return redirect(url_for("despachos", editar=did))

                if id_benef != id_benef_old:
                    # Revertir arrastre sobre unidad anterior si cambió el beneficiario
                    if id_benef_old and float(antes["saldo_anterior_aplicado_bs"] or 0) != 0:
                        rest = float(antes["saldo_anterior_aplicado_bs"])
                        cur.execute(
                            """SELECT id_despacho, diferencia_saldo_bs FROM despachos
                               WHERE id_batallon_beneficiario=%s AND id_periodo=%s AND anulado=0 AND id_despacho < %s AND saldo_arrastrado=1
                               ORDER BY id_despacho DESC""",
                            (id_benef_old, p["id_periodo"], did),
                        )
                        for prev in cur.fetchall():
                            cur.execute("UPDATE despachos SET saldo_arrastrado=0 WHERE id_despacho=%s", (prev["id_despacho"],))
                            rest -= float(prev["diferencia_saldo_bs"] or 0)
                        if abs(rest) > 0.005:
                            cur.execute("UPDATE unidades_militares SET saldo_cierre_no_aplicado_bs = saldo_cierre_no_aplicado_bs + %s WHERE id_unidad=%s",
                                        (round(rest, 2), id_benef_old))
                    if saldo_ant != 0:
                        cur.execute(
                            """UPDATE despachos SET saldo_arrastrado = 1
                               WHERE id_batallon_beneficiario = %s AND id_periodo = %s AND id_despacho <> %s
                                 AND tipo_salida = 'INTERNO_BATALLON' AND anulado = 0 AND saldo_arrastrado = 0""",
                            (id_benef, p["id_periodo"], did),
                        )
                        cur.execute("UPDATE unidades_militares SET saldo_cierre_no_aplicado_bs = 0 WHERE id_unidad = %s", (id_benef,))

                cur.execute(
                    """UPDATE despachos
                       SET fecha_despacho=%s, id_batallon_beneficiario=%s, id_batallon_retira=%s,
                           cantidad_familiar=%s, cantidad_tango=%s, total_bolsas=%s,
                           nombre_autorizado_retiro=%s, cedula_autorizado_retiro=%s,
                           tiene_recibo=%s, monto_base_bs=%s, saldo_anterior_aplicado_bs=%s,
                           monto_neto_exigible_bs=%s, observaciones=%s
                       WHERE id_despacho=%s""",
                    (fecha, id_benef, id_retira, fam, tan, total, nom_aut, ced_aut, tiene_recibo, monto_base, saldo_ant, monto_neto, obs, did),
                )

                cur.execute("SELECT id_pago FROM pagos_batallon WHERE id_despacho=%s AND anulado=0", (did,))
                ids_activos = {r["id_pago"] for r in cur.fetchall()}
                ids_conservados = set()

                for pid_val, conc, m_val, r_str, f_p in pagos_enviados:
                    if pid_val and pid_val in ids_activos:
                        ids_conservados.add(pid_val)
                        cur.execute(
                            """UPDATE pagos_batallon
                               SET id_batallon_pagador=%s, concepto_plan=%s, monto_pagado_bs=%s, referencia_bancaria=%s, fecha_pago=%s
                               WHERE id_pago=%s""",
                            (id_benef, conc, m_val, r_str, f_p, pid_val),
                        )
                    else:
                        cur.execute(
                            """INSERT INTO pagos_batallon
                               (id_despacho, id_periodo, id_batallon_pagador, concepto_plan, monto_pagado_bs,
                                referencia_bancaria, fecha_pago, id_usuario_registra)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (did, p["id_periodo"], id_benef, conc, m_val, r_str, f_p, session["user_id"]),
                        )

                for pid_del in (ids_activos - ids_conservados):
                    cur.execute(
                        "UPDATE pagos_batallon SET anulado=1, motivo_anulacion=%s WHERE id_pago=%s",
                        (f"Actualización en edición de despacho ({motivo[:80]})", pid_del),
                    )

                # sp_recalcular_despacho recalcula el despacho y el saldo acumulado del beneficiario
                cur.callproc("sp_recalcular_despacho", (did,))
                if id_benef_old and id_benef_old != id_benef:
                    sync_unidad_saldo(cur, id_benef_old)
            else:
                id_galpon = parse_int(request.form.get("id_galpon_destino"), 0) or antes["id_galpon_destino"] or 0
                fam = parse_int(request.form.get("ext_familiar"), 0)
                tan = parse_int(request.form.get("ext_tango"), 0)
                total = parse_int(request.form.get("ext_total_bolsas"), 0) or (fam + tan)
                if id_galpon <= 0 or fam < 0 or tan < 0 or total <= 0 or total < (fam + tan):
                    flash("Seleccione un galpón destino y cantidad válida de bolsas.", "error")
                    return redirect(url_for("despachos", editar=did))

                delta = total - int(antes["total_bolsas"])
                if delta > 0 and delta > disp:
                    flash(f"Stock insuficiente para incrementar {delta} bolsa(s) (disponible: {disp}).", "error")
                    return redirect(url_for("despachos", editar=did))

                nom_cond = (request.form.get("nombre_conductor") or "").strip() or None
                ok_cc, ced_cond = _parse_cedula_num(request.form.get("cedula_conductor_num"), required=False)
                if not ok_cc:
                    flash("La cédula del conductor debe contener únicamente dígitos numéricos (máx. 10).", "error")
                    return redirect(url_for("despachos", editar=did))
                veh_tipos = [v for v in request.form.getlist("vehiculo_tipo[]") if v in ("Camión", "Transporte")]
                veh = ", ".join(veh_tipos) or None
                placa = (request.form.get("placa_vehiculo") or "").strip()[:8] or None
                cur.execute(
                    """UPDATE despachos
                       SET fecha_despacho=%s, id_galpon_destino=%s, cantidad_familiar=%s, cantidad_tango=%s, total_bolsas=%s,
                           nombre_conductor=%s, cedula_conductor=%s, vehiculo_descripcion=%s, placa_vehiculo=%s, observaciones=%s
                       WHERE id_despacho=%s""",
                    (fecha, id_galpon, fam, tan, total, nom_cond, ced_cond, veh, placa, obs, did),
                )

            cur.execute("SELECT * FROM despachos WHERE id_despacho=%s", (did,))
            despues = cur.fetchone()
            _audit(
                cur, p["id_periodo"], session["user_id"], "despachos", did, "EDITAR", motivo,
                {
                    "tipo": antes["tipo_salida"],
                    "beneficiario": antes["id_batallon_beneficiario"], "retira": antes["id_batallon_retira"],
                    "galpon": antes["id_galpon_destino"],
                    "familiar": antes["cantidad_familiar"], "tango": antes["cantidad_tango"],
                    "total": antes["total_bolsas"], "neto_bs": antes["monto_neto_exigible_bs"],
                    "monto_pagado_bs": antes["monto_total_pagado_bs"],
                    "observaciones": antes["observaciones"],
                },
                {
                    "tipo": despues["tipo_salida"],
                    "beneficiario": despues["id_batallon_beneficiario"], "retira": despues["id_batallon_retira"],
                    "galpon": despues["id_galpon_destino"],
                    "familiar": despues["cantidad_familiar"], "tango": despues["cantidad_tango"],
                    "total": despues["total_bolsas"], "neto_bs": despues["monto_neto_exigible_bs"],
                    "monto_pagado_bs": despues["monto_total_pagado_bs"],
                    "observaciones": despues["observaciones"],
                },
            )
            conn.commit()
            flash(f"Despacho DES-{did:06d} actualizado correctamente.", "ok")
    except pymysql.MySQLError as ex:
        conn.rollback()
        flash(f"Error al editar despacho: {ex}", "error")
    return redirect(url_for("despachos"))


@app.route("/despachos/anular/<int:did>", methods=["POST"])
@master_required
def despachos_anular(did):
    """Anula un despacho y sus pagos asociados, revirtiendo saldos arrastrados al almacén."""
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Debe ingresar un motivo para anular el despacho.", "error")
        return redirect(url_for("despachos"))

    conn = get_db()
    with conn.cursor() as cur:
        ok_pin, msg_pin = _verify_master_pin(cur, "anular un despacho")
        if not ok_pin:
            flash(msg_pin, "error")
            return redirect(url_for("despachos"))

        p = get_req_period(cur)
        cur.execute("SELECT * FROM despachos WHERE id_despacho=%s AND id_periodo=%s AND anulado=0", (did, p["id_periodo"]))
        antes = cur.fetchone()
        if not antes:
            flash("Despacho no encontrado o ya anulado.", "error")
            return redirect(url_for("despachos"))

        cur.execute("UPDATE despachos SET anulado=1, motivo_anulacion=%s WHERE id_despacho=%s", (motivo, did))
        cur.execute(
            "UPDATE pagos_batallon SET anulado=1, motivo_anulacion=%s WHERE id_despacho=%s AND anulado=0",
            (f"Despacho anulado: {motivo}", did),
        )

        id_benef = antes["id_batallon_beneficiario"]
        if id_benef and float(antes["saldo_anterior_aplicado_bs"] or 0) != 0:
            restante = float(antes["saldo_anterior_aplicado_bs"])
            cur.execute(
                """SELECT id_despacho, diferencia_saldo_bs FROM despachos
                   WHERE id_batallon_beneficiario=%s AND id_periodo=%s AND anulado=0 AND id_despacho < %s AND saldo_arrastrado=1
                   ORDER BY id_despacho DESC""",
                (id_benef, p["id_periodo"], did),
            )
            for prev in cur.fetchall():
                cur.execute("UPDATE despachos SET saldo_arrastrado=0 WHERE id_despacho=%s", (prev["id_despacho"],))
                restante -= float(prev["diferencia_saldo_bs"] or 0)
            if abs(restante) > 0.005:
                cur.execute(
                    "UPDATE unidades_militares SET saldo_cierre_no_aplicado_bs = saldo_cierre_no_aplicado_bs + %s WHERE id_unidad=%s",
                    (round(restante, 2), id_benef),
                )
        if id_benef:
            sync_unidad_saldo(cur, id_benef)

        _audit(
            cur, p["id_periodo"], session["user_id"], "despachos", did, "ANULAR", motivo,
            {
                "tipo": antes["tipo_salida"],
                "beneficiario": id_benef, "retira": antes["id_batallon_retira"],
                "galpon": antes["id_galpon_destino"],
                "familiar": antes["cantidad_familiar"], "tango": antes["cantidad_tango"],
                "total": antes["total_bolsas"],
            },
            {"anulado": 1},
        )
        conn.commit()
        flash(f"Despacho DES-{did:06d} anulado. El stock fue devuelto al almacén.", "ok")
    return redirect(url_for("despachos"))


# ---------------------------------------------------------------------------
# 8. Conciliación de Pagos Múltiples y Trazabilidad de Arrastre
# ---------------------------------------------------------------------------
@app.route("/despachos/<int:did>/pagos")
@login_required
def despacho_pagos(did):
    """Muestra la conciliación financiera de un despacho interno y sus enlaces de arrastre."""
    conn = get_db()
    with conn.cursor() as cur:
        p_activo = get_req_period(cur)
        cur.execute(
            """SELECT d.*, CONCAT('DES-', LPAD(d.id_despacho,6,'0')) AS codigo_movimiento,
                      CONCAT(IFNULL(CONCAT(ub.codigo_unidad, ' '), ''), ub.nombre_unidad) AS benef_nombre,
                      CONCAT(IFNULL(CONCAT(ur.codigo_unidad, ' '), ''), ur.nombre_unidad) AS retira_nombre
               FROM despachos d
               JOIN unidades_militares ub ON ub.id_unidad = d.id_batallon_beneficiario
               LEFT JOIN unidades_militares ur ON ur.id_unidad = d.id_batallon_retira
               WHERE d.id_despacho = %s""",
            (did,),
        )
        desp = cur.fetchone()
        if not desp:
            flash("Despacho interno no encontrado.", "error")
            return redirect(url_for("despachos"))

        cur.execute(
            """SELECT pb.*, u.usuario
               FROM pagos_batallon pb JOIN usuarios u ON u.id_usuario = pb.id_usuario_registra
               WHERE pb.id_despacho = %s ORDER BY pb.id_pago DESC""",
            (did,),
        )
        pagos = cur.fetchall()

        # Trazabilidad hacia despacho posterior receptor del arrastre
        desp_destino_arrastre = None
        if desp.get("saldo_arrastrado") and abs(float(desp.get("diferencia_saldo_bs") or 0)) > 0.005:
            cur.execute(
                """SELECT id_despacho, CONCAT('DES-', LPAD(id_despacho,6,'0')) AS codigo_movimiento, fecha_despacho
                   FROM despachos
                   WHERE id_batallon_beneficiario = %s AND id_periodo = %s AND id_despacho > %s AND anulado = 0
                     AND saldo_anterior_aplicado_bs <> 0
                   ORDER BY id_despacho ASC LIMIT 1""",
                (desp["id_batallon_beneficiario"], desp["id_periodo"], did),
            )
            desp_destino_arrastre = cur.fetchone()

        # Trazabilidad hacia despacho origen cuando este despacho recibió un saldo previo
        desp_origen_arrastre = None
        if desp.get("saldo_anterior_aplicado_bs") and abs(float(desp.get("saldo_anterior_aplicado_bs") or 0)) > 0.005:
            cur.execute(
                """SELECT id_despacho, CONCAT('DES-', LPAD(id_despacho,6,'0')) AS codigo_movimiento, fecha_despacho
                   FROM despachos
                   WHERE id_batallon_beneficiario = %s AND id_periodo = %s AND id_despacho < %s AND anulado = 0
                     AND saldo_arrastrado = 1 AND abs(diferencia_saldo_bs) > 0.005
                   ORDER BY id_despacho DESC LIMIT 1""",
                (desp["id_batallon_beneficiario"], desp["id_periodo"], did),
            )
            desp_origen_arrastre = cur.fetchone()

        es_periodo_activo = bool(p_activo and desp["id_periodo"] == p_activo["id_periodo"])

    return render_template(
        "pagos.html",
        desp=desp,
        pagos=pagos,
        desp_destino_arrastre=desp_destino_arrastre,
        desp_origen_arrastre=desp_origen_arrastre,
        es_periodo_activo=es_periodo_activo,
    )


@app.route("/despachos/<int:did>/pagos/crear", methods=["POST"])
@login_required
def pago_crear(did):
    """Agrega un pago o abono complementario; el trigger `trg_pagos_ai` recalcula automáticamente."""
    concepto = normalize_concepto(request.form.get("concepto_plan"))
    monto = round(parse_float(request.form.get("monto_pagado_bs"), 0.0), 2)
    ref = (request.form.get("referencia_bancaria") or "").strip()
    fecha_pago = request.form.get("fecha_pago") or datetime.date.today().isoformat()
    obs = get_obs_from_form()

    if monto <= 0:
        flash("El monto del pago debe ser mayor a 0.", "error")
        return redirect(url_for("despacho_pagos", did=did))
    if not re.match(r"^\d{4,12}$", ref):
        flash("La referencia bancaria debe tener entre 4 y 12 dígitos numéricos.", "error")
        return redirect(url_for("despacho_pagos", did=did))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            p = get_req_period(cur)
            cur.execute("SELECT * FROM despachos WHERE id_despacho=%s AND id_periodo=%s AND anulado=0", (did, p["id_periodo"]))
            desp = cur.fetchone()
            if not desp:
                flash("No se pueden agregar pagos a un despacho anulado o de un período cerrado.", "error")
                return redirect(url_for("despachos"))
            id_benef = desp["id_batallon_beneficiario"]
            cur.execute(
                """INSERT INTO pagos_batallon
                   (id_despacho, id_periodo, id_batallon_pagador, concepto_plan, monto_pagado_bs,
                    referencia_bancaria, fecha_pago, observaciones, id_usuario_registra)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (did, p["id_periodo"], id_benef, concepto, monto, ref, fecha_pago, obs, session["user_id"]),
            )
            pid = cur.lastrowid
            _audit(cur, p["id_periodo"], session["user_id"], "pagos_batallon", pid, "CREAR", f"Registro de pago en DES-{did:06d}", None,
                   {"concepto_plan": concepto, "monto_pagado_bs": monto, "referencia_bancaria": ref, "fecha_pago": fecha_pago})
            conn.commit()
            flash(f"Pago de {fmt_bs(monto)} registrado. Saldo recalculado automáticamente.", "ok")
    except pymysql.MySQLError as ex:
        conn.rollback()
        flash(f"Error al registrar pago: {ex}", "error")
    return redirect(url_for("despacho_pagos", did=did))


@app.route("/pagos/editar/<int:pid>", methods=["POST"])
@login_required
def pago_editar(pid):
    """Edita un pago existente en el período activo; `trg_pagos_au` recalcula el despacho y la unidad."""
    did = parse_int(request.form.get("id_despacho"), 0)
    motivo = (request.form.get("motivo") or "").strip()
    monto = round(parse_float(request.form.get("monto_pagado_bs"), 0.0), 2)
    ref = (request.form.get("referencia_bancaria") or "").strip()
    concepto = normalize_concepto(request.form.get("concepto_plan"))
    fecha_pago = request.form.get("fecha_pago") or None
    if monto <= 0 or not motivo:
        flash("Monto válido y motivo de edición son obligatorios.", "error")
        return redirect(url_for("despacho_pagos", did=did))
    if not re.match(r"^\d{4,12}$", ref):
        flash("La referencia bancaria debe tener entre 4 y 12 dígitos numéricos.", "error")
        return redirect(url_for("despacho_pagos", did=did))

    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute("SELECT * FROM pagos_batallon WHERE id_pago=%s AND id_periodo=%s AND anulado=0", (pid, p["id_periodo"]))
        antes = cur.fetchone()
        if not antes:
            flash("Pago no editable.", "error")
            return redirect(url_for("despacho_pagos", did=did))
        cur.execute(
            """UPDATE pagos_batallon
               SET concepto_plan=%s, monto_pagado_bs=%s, referencia_bancaria=%s, fecha_pago=%s
               WHERE id_pago=%s""",
            (concepto, monto, ref, fecha_pago, pid),
        )
        _audit(cur, p["id_periodo"], session["user_id"], "pagos_batallon", pid, "EDITAR", motivo,
               {"concepto_plan": antes["concepto_plan"], "monto_pagado_bs": antes["monto_pagado_bs"], "referencia_bancaria": antes["referencia_bancaria"]},
               {"concepto_plan": concepto, "monto_pagado_bs": monto, "referencia_bancaria": ref, "fecha_pago": fecha_pago})
        conn.commit()
        flash("Pago editado y registrado en auditoría.", "ok")
    return redirect(url_for("despacho_pagos", did=did))


@app.route("/pagos/anular/<int:pid>", methods=["POST"])
@master_required
def pago_anular(pid):
    """Anula un pago previa validación de PIN MASTER; `trg_pagos_au` recalcula automáticamente."""
    did = parse_int(request.form.get("id_despacho"), 0)
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Debe indicar el motivo de anulación.", "error")
        return redirect(url_for("despacho_pagos", did=did))

    conn = get_db()
    with conn.cursor() as cur:
        ok_pin, msg_pin = _verify_master_pin(cur, "anular un pago")
        if not ok_pin:
            flash(msg_pin, "error")
            return redirect(url_for("despacho_pagos", did=did))

        p = get_req_period(cur)
        cur.execute("SELECT * FROM pagos_batallon WHERE id_pago=%s AND id_periodo=%s AND anulado=0", (pid, p["id_periodo"]))
        antes = cur.fetchone()
        if not antes:
            flash("Pago no encontrado o ya anulado.", "error")
            return redirect(url_for("despacho_pagos", did=did))
        cur.execute("UPDATE pagos_batallon SET anulado=1, motivo_anulacion=%s WHERE id_pago=%s", (motivo, pid))
        _audit(cur, p["id_periodo"], session["user_id"], "pagos_batallon", pid, "ANULAR", motivo,
               {"monto_pagado_bs": antes["monto_pagado_bs"], "referencia_bancaria": antes["referencia_bancaria"]}, {"anulado": 1})
        conn.commit()
        flash("Pago anulado y balance del despacho recalculado.", "ok")
    return redirect(url_for("despacho_pagos", did=did))


# ---------------------------------------------------------------------------
# 9. Catálogos (Batallones/Unidades Militares y Galpones Externos)
# ---------------------------------------------------------------------------
@app.route("/catalogos")
@master_required
def catalogos():
    """Muestra y administra catálogos de unidades militares y galpones nacionales."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT u.*,
                      CONCAT(IFNULL(CONCAT(p.codigo_unidad, ' '), ''), p.nombre_unidad) AS padre_nombre
               FROM unidades_militares u
               LEFT JOIN unidades_militares p ON p.id_unidad = u.id_unidad_padre
               ORDER BY u.activo DESC, u.codigo_unidad, u.nombre_unidad"""
        )
        unidades = cur.fetchall()
        unidades_json = [
            {
                "id": u["id_unidad"],
                "codigo": u["codigo_unidad"] or "",
                "nombre": u["nombre_unidad"],
                "display": format_unidad_display(u["codigo_unidad"], u["nombre_unidad"]),
            }
            for u in unidades if u["activo"] == 1
        ]
        cur.execute("SELECT * FROM galpones_externos ORDER BY activo DESC, nombre_galpon")
        galpones = cur.fetchall()
    return render_template("catalogos.html", unidades=unidades, unidades_json=unidades_json, galpones=galpones)


@app.route("/catalogos/unidades/crear", methods=["POST"])
@master_required
def unidad_crear():
    """Añade una nueva unidad militar separando código corto y nombre."""
    codigo = (request.form.get("codigo_unidad") or "").strip().upper() or None
    nombre = re.sub(r"\s+", " ", (request.form.get("nombre_unidad") or "").strip()).upper()
    if codigo and nombre.startswith(codigo):
        nombre = re.sub(r"^[\s\-]+", "", nombre[len(codigo):]).strip()

    padre_txt = (request.form.get("unidad_padre_texto") or "").strip()
    id_padre = parse_int(request.form.get("id_unidad_padre"), 0) or None
    if padre_txt and not id_padre:
        flash("Seleccione la Unidad Padre desde las sugerencias o déjela vacía.", "error")
        return redirect(url_for("catalogos"))

    es_agr = 1 if request.form.get("es_agrupador") == "1" else 0
    agrup_txt = (request.form.get("codigos_agrupados_texto") or "").strip().upper() or None
    if not nombre:
        flash("El nombre de la unidad es obligatorio.", "error")
        return redirect(url_for("catalogos"))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            p = get_req_period(cur)
            if codigo is None:
                cur.execute("SELECT id_unidad FROM unidades_militares WHERE codigo_unidad IS NULL AND nombre_unidad = %s", (nombre,))
                if cur.fetchone():
                    flash(f"Ya existe una unidad sin código llamada '{nombre}'.", "error")
                    return redirect(url_for("catalogos"))
            cur.execute(
                """INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, id_unidad_padre, es_agrupador, codigos_agrupados_texto)
                   VALUES (%s,%s,%s,%s,%s)""",
                (codigo, nombre, id_padre, es_agr, agrup_txt),
            )
            uid = cur.lastrowid
            _audit(cur, p["id_periodo"], session["user_id"], "unidades_militares", uid, "CREAR", "Alta de unidad por MASTER", None,
                   {"codigo_unidad": codigo, "nombre_unidad": nombre, "id_unidad_padre": id_padre})
            conn.commit()
            flash(f"Unidad '{format_unidad_display(codigo, nombre)}' añadida al catálogo.", "ok")
    except pymysql.MySQLError as ex:
        conn.rollback()
        flash(f"No se pudo añadir la unidad (¿duplicada?): {ex}", "error")
    return redirect(url_for("catalogos"))


@app.route("/catalogos/unidades/editar/<int:uid>", methods=["POST"])
@master_required
def unidad_editar(uid):
    """Edita código o nombre de una unidad militar del catálogo."""
    codigo = (request.form.get("codigo_unidad") or "").strip().upper() or None
    nombre = re.sub(r"\s+", " ", (request.form.get("nombre_unidad") or "").strip()).upper()
    if codigo and nombre.startswith(codigo):
        nombre = re.sub(r"^[\s\-]+", "", nombre[len(codigo):]).strip()
    agrup_txt = (request.form.get("codigos_agrupados_texto") or "").strip().upper() or None
    if not nombre:
        flash("El nombre de la unidad es obligatorio.", "error")
        return redirect(url_for("catalogos"))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            p = get_req_period(cur)
            cur.execute("SELECT * FROM unidades_militares WHERE id_unidad=%s", (uid,))
            antes = cur.fetchone()
            if not antes:
                flash("Unidad no encontrada.", "error")
                return redirect(url_for("catalogos"))
            cur.execute(
                "UPDATE unidades_militares SET codigo_unidad=%s, nombre_unidad=%s, codigos_agrupados_texto=%s WHERE id_unidad=%s",
                (codigo, nombre, agrup_txt, uid),
            )
            _audit(cur, p["id_periodo"], session["user_id"], "unidades_militares", uid, "EDITAR", "Edición de unidad en catálogo",
                   {"codigo_unidad": antes["codigo_unidad"], "nombre_unidad": antes["nombre_unidad"]},
                   {"codigo_unidad": codigo, "nombre_unidad": nombre})
            conn.commit()
            flash(f"Unidad '{format_unidad_display(codigo, nombre)}' actualizada.", "ok")
    except pymysql.MySQLError as ex:
        conn.rollback()
        flash(f"Error al editar unidad: {ex}", "error")
    return redirect(url_for("catalogos"))


@app.route("/catalogos/unidades/eliminar/<int:uid>", methods=["POST"])
@master_required
def unidad_eliminar(uid):
    """Elimina del catálogo una unidad solo si carece de historial o sub-unidades asociadas."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            p = get_req_period(cur)
            cur.execute("SELECT * FROM unidades_militares WHERE id_unidad=%s", (uid,))
            u = cur.fetchone()
            if not u:
                flash("Unidad no encontrada.", "error")
                return redirect(url_for("catalogos"))
            cur.execute("SELECT COUNT(*) AS c FROM unidades_militares WHERE id_unidad_padre=%s", (uid,))
            if cur.fetchone()["c"] > 0:
                flash("No se puede eliminar esta unidad porque tiene sub-unidades asociadas.", "error")
                return redirect(url_for("catalogos"))
            cur.execute("SELECT COUNT(*) AS c FROM despachos WHERE id_batallon_beneficiario=%s OR id_batallon_retira=%s", (uid, uid))
            if cur.fetchone()["c"] > 0:
                flash("No se puede eliminar esta unidad porque tiene despachos históricos registrados (puede desactivarla).", "error")
                return redirect(url_for("catalogos"))
            cur.execute("DELETE FROM historial_saldos_batallon WHERE id_unidad=%s", (uid,))
            cur.execute("DELETE FROM unidades_militares WHERE id_unidad=%s", (uid,))
            _audit(cur, p["id_periodo"], session["user_id"], "unidades_militares", uid, "ANULAR", "Eliminación de unidad del catálogo",
                   {"codigo_unidad": u["codigo_unidad"], "nombre_unidad": u["nombre_unidad"]}, None)
            conn.commit()
            flash(f"Unidad '{format_unidad_display(u['codigo_unidad'], u['nombre_unidad'])}' eliminada del catálogo.", "ok")
    except pymysql.MySQLError as ex:
        conn.rollback()
        flash(f"No se pudo eliminar la unidad: {ex}", "error")
    return redirect(url_for("catalogos"))


@app.route("/catalogos/unidades/toggle/<int:uid>", methods=["POST"])
@master_required
def unidad_toggle(uid):
    """Activa o desactiva una unidad militar en el catálogo."""
    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute("SELECT * FROM unidades_militares WHERE id_unidad=%s", (uid,))
        u = cur.fetchone()
        if not u:
            flash("Unidad no encontrada.", "error")
            return redirect(url_for("catalogos"))
        nuevo = 0 if u["activo"] == 1 else 1
        cur.execute("UPDATE unidades_militares SET activo=%s WHERE id_unidad=%s", (nuevo, uid))
        _audit(cur, p["id_periodo"], session["user_id"], "unidades_militares", uid, "CONFIG",
               f"{'Activación' if nuevo else 'Desactivación'} de {format_unidad_display(u['codigo_unidad'], u['nombre_unidad'])}",
               {"activo": u["activo"]}, {"activo": nuevo})
        conn.commit()
        flash(f"Unidad '{format_unidad_display(u['codigo_unidad'], u['nombre_unidad'])}' {'activada' if nuevo else 'desactivada'}.", "ok")
    return redirect(url_for("catalogos"))


@app.route("/catalogos/galpones/crear", methods=["POST"])
@master_required
def galpon_crear():
    """Registra un nuevo galpón destino para despachos externos."""
    nombre = (request.form.get("nombre_galpon") or "").strip().upper()
    estado = (request.form.get("estado_region") or "").strip().upper() or None
    if not nombre:
        flash("El nombre del galpón es obligatorio.", "error")
        return redirect(url_for("catalogos"))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            p = get_req_period(cur)
            cur.execute("INSERT INTO galpones_externos (nombre_galpon, estado_region) VALUES (%s,%s)", (nombre, estado))
            gid = cur.lastrowid
            _audit(cur, p["id_periodo"], session["user_id"], "galpones_externos", gid, "CREAR", "Alta de galpón", None,
                   {"nombre_galpon": nombre, "estado_region": estado})
            conn.commit()
            flash(f"Galpón '{nombre}' ({estado or '-'}) añadido.", "ok")
    except pymysql.MySQLError as ex:
        conn.rollback()
        flash(f"Error al añadir galpón: {ex}", "error")
    return redirect(url_for("catalogos"))


@app.route("/catalogos/galpones/toggle/<int:gid>", methods=["POST"])
@master_required
def galpon_toggle(gid):
    """Activa o desactiva un galpón externo en el catálogo."""
    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute("SELECT * FROM galpones_externos WHERE id_galpon=%s", (gid,))
        g_row = cur.fetchone()
        if not g_row:
            flash("Galpón no encontrado.", "error")
            return redirect(url_for("catalogos"))
        nuevo = 0 if g_row["activo"] == 1 else 1
        cur.execute("UPDATE galpones_externos SET activo=%s WHERE id_galpon=%s", (nuevo, gid))
        _audit(cur, p["id_periodo"], session["user_id"], "galpones_externos", gid, "CONFIG",
               f"{'Activación' if nuevo else 'Desactivación'} de galpón {g_row['nombre_galpon']}",
               {"activo": g_row["activo"]}, {"activo": nuevo})
        conn.commit()
        flash(f"Galpón '{g_row['nombre_galpon']}' {'activado' if nuevo else 'desactivado'}.", "ok")
    return redirect(url_for("catalogos"))


# ---------------------------------------------------------------------------
# 10. Usuarios y Seguridad Avanzada del MASTER (Código PIN)
# ---------------------------------------------------------------------------
@app.route("/usuarios")
@master_required
def usuarios():
    """Administra la cuenta MASTER (código de seguridad y clave) y hasta 3 operadores."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id_usuario, usuario, nombre_completo, rol, pin_seguridad, activo, fecha_creacion, ultimo_acceso FROM usuarios ORDER BY id_usuario")
        lista = cur.fetchall()
        master_u = next((u for u in lista if u["rol"] == "MASTER"), None)
    return render_template("usuarios.html", usuarios=lista, master_u=master_u)


@app.route("/usuarios/master/pin", methods=["POST"])
@master_required
def master_pin_guardar():
    """Configura (o resetea desde soporte) el código de seguridad numérico de 1 a 9 dígitos."""
    accion_pin = request.form.get("accion_pin") or "guardar"
    pin = (request.form.get("pin_seguridad") or "").strip()

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM usuarios WHERE rol='MASTER' ORDER BY id_usuario LIMIT 1")
        m = cur.fetchone()
        if not m:
            flash("Usuario Master no encontrado.", "error")
            return redirect(url_for("usuarios"))

        if accion_pin == "reset":
            if not session.get("es_soporte"):
                flash("Solo la cuenta de soporte puede resetear el código de seguridad.", "error")
                return redirect(url_for("usuarios"))
            cur.execute("UPDATE usuarios SET pin_seguridad=NULL WHERE id_usuario=%s", (m["id_usuario"],))
            conn.commit()
            flash("Código de seguridad reseteado.", "ok")
            return redirect(url_for("usuarios"))

        if m.get("pin_seguridad") and not session.get("es_soporte"):
            flash("El código de seguridad ya fue creado y está bloqueado.", "error")
            return redirect(url_for("usuarios"))

        if not re.match(r"^\d{1,9}$", pin):
            flash("El código de seguridad debe tener entre 1 y 9 dígitos numéricos.", "error")
            return redirect(url_for("usuarios"))

        cur.execute("UPDATE usuarios SET pin_seguridad=%s WHERE id_usuario=%s", (pin, m["id_usuario"]))
        p = get_req_period(cur)
        _audit(cur, p["id_periodo"], session["user_id"], "usuarios", m["id_usuario"], "CONFIG", "Configuración de código de seguridad Master")
        conn.commit()
        flash("Código de seguridad guardado correctamente.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/master/actualizar", methods=["POST"])
@master_required
def master_actualizar():
    """Actualiza nombre o contraseña del usuario MASTER previa validación de su código PIN."""
    ok_nom, res_nom = validate_fullname(request.form.get("nombre_completo"))
    if not ok_nom:
        flash(res_nom, "error")
        return redirect(url_for("usuarios"))
    nueva_clave = request.form.get("clave") or ""
    if nueva_clave:
        ok_pw, msg_pw = validate_password_policy(nueva_clave)
        if not ok_pw:
            flash(msg_pw, "error")
            return redirect(url_for("usuarios"))

    conn = get_db()
    with conn.cursor() as cur:
        ok_pin, msg_pin = _verify_master_pin(cur, "cambiar su contraseña")
        if not ok_pin:
            flash(msg_pin, "error")
            return redirect(url_for("usuarios"))

        cur.execute("SELECT * FROM usuarios WHERE rol='MASTER' ORDER BY id_usuario LIMIT 1")
        m = cur.fetchone()
        if not m:
            flash("Usuario Master no encontrado.", "error")
            return redirect(url_for("usuarios"))

        if nueva_clave:
            cur.execute(
                "UPDATE usuarios SET nombre_completo=%s, password_hash=%s WHERE id_usuario=%s",
                (res_nom, hash_password(nueva_clave), m["id_usuario"]),
            )
        else:
            cur.execute("UPDATE usuarios SET nombre_completo=%s WHERE id_usuario=%s", (res_nom, m["id_usuario"]))

        if not session.get("es_soporte"):
            session["nombre"] = res_nom
        p = get_req_period(cur)
        _audit(cur, p["id_periodo"], session["user_id"], "usuarios", m["id_usuario"], "EDITAR", "Actualización de datos Master",
               {"nombre_completo": m["nombre_completo"]}, {"nombre_completo": res_nom, "cambio_clave": bool(nueva_clave)})
        conn.commit()
        flash("Datos del usuario Master actualizados correctamente.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/crear", methods=["POST"])
@master_required
def usuario_crear():
    """Crea una cuenta OPERADOR (límite total de 4 cuentas reforzado por trigger SQL)."""
    usuario = (request.form.get("usuario") or "").strip().lower()
    ok_nom, res_nom = validate_fullname(request.form.get("nombre_completo"))
    if not ok_nom:
        flash(res_nom, "error")
        return redirect(url_for("usuarios"))
    clave = request.form.get("clave") or ""
    ok_pw, msg_pw = validate_password_policy(clave)
    if not ok_pw:
        flash(msg_pw, "error")
        return redirect(url_for("usuarios"))
    if not usuario:
        flash("Debe indicar el usuario de acceso.", "error")
        return redirect(url_for("usuarios"))

    conn = get_db()
    try:
        with conn.cursor() as cur:
            p = get_req_period(cur)
            cur.execute(
                "INSERT INTO usuarios (usuario, nombre_completo, rol, password_hash) VALUES (%s,%s,'OPERADOR',%s)",
                (usuario, res_nom, hash_password(clave)),
            )
            uid = cur.lastrowid
            _audit(cur, p["id_periodo"], session["user_id"], "usuarios", uid, "CREAR", f"Creación de operador {usuario}", None,
                   {"usuario": usuario, "nombre_completo": res_nom, "rol": "OPERADOR"})
            conn.commit()
            flash(f"Operador '{usuario}' creado correctamente.", "ok")
    except pymysql.MySQLError as ex:
        conn.rollback()
        flash(f"No se pudo crear el usuario: {ex.args[1] if len(ex.args) > 1 else ex}", "error")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/editar/<int:uid>", methods=["POST"])
@master_required
def usuario_editar(uid):
    """Edita nombre y contraseña de una cuenta existente."""
    ok_nom, res_nom = validate_fullname(request.form.get("nombre_completo"))
    if not ok_nom:
        flash(res_nom, "error")
        return redirect(url_for("usuarios"))
    clave = request.form.get("clave") or ""
    if clave:
        ok_pw, msg_pw = validate_password_policy(clave)
        if not ok_pw:
            flash(msg_pw, "error")
            return redirect(url_for("usuarios"))

    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute("SELECT * FROM usuarios WHERE id_usuario=%s", (uid,))
        u = cur.fetchone()
        if not u:
            flash("Usuario no encontrado.", "error")
            return redirect(url_for("usuarios"))
        if u["rol"] == "MASTER" and not session.get("es_soporte"):
            flash("Solo la cuenta de soporte técnico puede restablecer la cuenta MASTER desde aquí.", "error")
            return redirect(url_for("usuarios"))

        if clave:
            cur.execute(
                "UPDATE usuarios SET nombre_completo=%s, password_hash=%s WHERE id_usuario=%s",
                (res_nom, hash_password(clave), uid),
            )
        else:
            cur.execute("UPDATE usuarios SET nombre_completo=%s WHERE id_usuario=%s", (res_nom, uid))

        _audit(cur, p["id_periodo"], session["user_id"], "usuarios", uid, "EDITAR", f"Edición de usuario {u['usuario']}",
               {"nombre_completo": u["nombre_completo"]}, {"nombre_completo": res_nom, "cambio_clave": bool(clave)})
        conn.commit()
        flash(f"Usuario '{u['usuario']}' actualizado.", "ok")
    return redirect(url_for("usuarios"))


@app.route("/usuarios/toggle/<int:uid>", methods=["POST"])
@master_required
def usuario_toggle(uid):
    """Activa o desactiva la cuenta de un usuario OPERADOR."""
    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        cur.execute("SELECT * FROM usuarios WHERE id_usuario=%s", (uid,))
        u = cur.fetchone()
        if not u or u["rol"] == "MASTER":
            flash("No se puede desactivar al usuario MASTER.", "error")
            return redirect(url_for("usuarios"))
        nuevo = 0 if u["activo"] == 1 else 1
        cur.execute("UPDATE usuarios SET activo=%s WHERE id_usuario=%s", (nuevo, uid))
        _audit(cur, p["id_periodo"], session["user_id"], "usuarios", uid, "CONFIG",
               f"{'Activación' if nuevo else 'Desactivación'} de operador {u['usuario']}", {"activo": u["activo"]}, {"activo": nuevo})
        conn.commit()
        flash(f"Operador '{u['usuario']}' {'activado' if nuevo else 'desactivado'}.", "ok")
    return redirect(url_for("usuarios"))


# ---------------------------------------------------------------------------
# 11. Auditoría y Últimos Accesos (Con Filtros SARGables por Fecha/Usuario/Acción)
# ---------------------------------------------------------------------------
@app.route("/auditoria")
@master_required
def auditoria():
    """Bitácora de auditoría y accesos filtrable por período, fecha calendario, usuario y acción."""
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM periodos_cierre ORDER BY id_periodo DESC")
        periodos = cur.fetchall()
        p_activo = get_req_period(cur)
        periodo_raw = request.args.get("periodo")
        if periodo_raw == "TODOS":
            sel_periodo = "TODOS"
        else:
            sel_periodo = parse_int(periodo_raw, 0) or (p_activo["id_periodo"] if p_activo else 1)

        unit_map, galpon_map = build_lookup_maps(cur)

        # Validar formato estricto YYYY-MM-DD para usar rangos SARGables en índices DATETIME
        f_fecha = (request.args.get("fecha") or "").strip()
        if f_fecha:
            try:
                datetime.date.fromisoformat(f_fecha)
            except ValueError:
                f_fecha = ""

        f_usuario = (request.args.get("usuario") or "").strip()
        f_accion = (request.args.get("accion") or "").strip().upper()

        cur.execute("SELECT id_usuario, usuario, nombre_completo, rol FROM usuarios ORDER BY usuario")
        lista_usuarios = cur.fetchall()

        # Consulta Auditoría usando rango indexado sobre `fecha_hora`
        sql_aud = [
            """SELECT a.*, u.usuario
               FROM auditoria a JOIN usuarios u ON u.id_usuario = a.id_usuario
               WHERE 1=1"""
        ]
        params_aud = []

        if sel_periodo != "TODOS":
            sql_aud.append("AND a.id_periodo = %s")
            params_aud.append(sel_periodo)
        if f_fecha:
            sql_aud.append("AND a.fecha_hora >= %s AND a.fecha_hora < DATE_ADD(%s, INTERVAL 1 DAY)")
            params_aud.extend([f_fecha, f_fecha])
        if f_usuario:
            sql_aud.append("AND LOWER(u.usuario) = %s")
            params_aud.append(f_usuario.lower())
        if f_accion:
            if f_accion in ("LOGIN_OK", "LOGIN_FALLIDO", "LOGOUT"):
                sql_aud.append("AND 1 = 0")
            else:
                sql_aud.append("AND a.accion = %s")
                params_aud.append(f_accion)

        sql_aud.append("ORDER BY a.id_auditoria DESC LIMIT 500")
        cur.execute(" ".join(sql_aud), tuple(params_aud))
        cambios = []
        for c in cur.fetchall():
            c["anterior_legible"] = humanize_audit_payload(c["datos_anteriores"], c["tabla_afectada"], c["accion"], unit_map, galpon_map)
            c["nuevo_legible"] = humanize_audit_payload(c["datos_nuevos"], c["tabla_afectada"], c["accion"], unit_map, galpon_map)
            cambios.append(c)

        # Consulta Accesos usando rango indexado sobre `fecha_hora`
        sql_acc = [
            """SELECT h.id_acceso, h.usuario_intento, h.evento, h.fecha_hora
               FROM historial_accesos h
               WHERE 1=1"""
        ]
        params_acc = []

        if sel_periodo != "TODOS":
            sql_acc.append("AND h.id_periodo = %s")
            params_acc.append(sel_periodo)
        if f_fecha:
            sql_acc.append("AND h.fecha_hora >= %s AND h.fecha_hora < DATE_ADD(%s, INTERVAL 1 DAY)")
            params_acc.extend([f_fecha, f_fecha])
        if f_usuario:
            sql_acc.append("AND LOWER(h.usuario_intento) = %s")
            params_acc.append(f_usuario.lower())
        if f_accion:
            if f_accion in ("LOGIN_OK", "LOGIN_FALLIDO", "LOGOUT"):
                sql_acc.append("AND h.evento = %s")
                params_acc.append(f_accion)
            else:
                sql_acc.append("AND 1 = 0")

        sql_acc.append("ORDER BY h.id_acceso DESC LIMIT 500")
        cur.execute(" ".join(sql_acc), tuple(params_acc))
        accesos = cur.fetchall()

    return render_template(
        "auditoria.html",
        periodos=periodos,
        sel_periodo=sel_periodo,
        cambios=cambios,
        accesos=accesos,
        lista_usuarios=lista_usuarios,
        f_fecha=f_fecha,
        f_usuario=f_usuario,
        f_accion=f_accion,
    )


# ---------------------------------------------------------------------------
# 12. Cierre Manual de Período y Respaldo en Excel/PDF
# ---------------------------------------------------------------------------
@app.route("/cierre")
@login_required
def cierre():
    """Muestra consolidado de cierre, respaldos generados y controles de exportación."""
    conn = get_db()
    with conn.cursor() as cur:
        p = get_req_period(cur)
        stock = get_req_stock(cur, p["id_periodo"]) if p else {}
        cur.execute("SELECT * FROM v_rendicion_cuentas WHERE id_periodo = %s", (p["id_periodo"],))
        rend = cur.fetchone() or {}
        cur.execute("SELECT * FROM periodos_cierre ORDER BY id_periodo DESC")
        periodos = cur.fetchall()
        cur.execute(
            """SELECT r.*, p.numero_en_anio, p.nombre_periodo, p.fecha_inicio, u.usuario
               FROM respaldos_pendrive r
               JOIN periodos_cierre p ON p.id_periodo = r.id_periodo
               JOIN usuarios u ON u.id_usuario = r.id_usuario
               ORDER BY r.id_respaldo DESC LIMIT 20"""
        )
        respaldos = cur.fetchall()
        ultima_ruta = get_config_val(cur, "ultima_ruta_respaldo")

    usb_drives = detect_usb_drives()
    default_local_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Respaldos_Locales")
    siguiente_meta = next_period_meta(p) if p else None
    return render_template(
        "cierre.html",
        periodo=p,
        siguiente_meta=siguiente_meta,
        stock=stock,
        rend=rend,
        periodos=periodos,
        respaldos=respaldos,
        usb_drives=usb_drives,
        default_local_dir=default_local_dir,
        ultima_ruta=ultima_ruta,
    )


@app.route("/respaldo/rapido", methods=["POST"])
@login_required
def respaldo_rapido():
    """Exporta reporte completo Excel (.xlsx) y ejecutivo PDF (.pdf) del período elegido."""
    id_periodo = parse_int(request.form.get("id_periodo"), 0)
    usar_anterior = request.form.get("usar_ruta_anterior") == "1"
    destino = (request.form.get("ruta_destino") or "").strip()

    conn = get_db()
    try:
        with conn.cursor() as cur:
            if usar_anterior:
                destino = get_config_val(cur, "ultima_ruta_respaldo", destino)
            if not destino:
                flash("Indique una carpeta o unidad destino para el respaldo.", "error")
                return redirect(url_for("cierre"))
            if not id_periodo:
                p = get_req_period(cur)
                id_periodo = p["id_periodo"]
            dest_dir, archivos = export_period_bundle(cur, id_periodo, destino, tipo="RAPIDO")
            set_config_val(cur, "ultima_ruta_respaldo", destino)
            cur.execute(
                "INSERT INTO respaldos_pendrive (id_periodo, tipo, ruta_destino, archivos, id_usuario) VALUES (%s,'RAPIDO',%s,%s,%s)",
                (id_periodo, dest_dir, ", ".join(archivos), session["user_id"]),
            )
            conn.commit()
            flash(f"Respaldo generado en: {dest_dir} ({', '.join(archivos)})", "ok")
    except Exception as ex:
        conn.rollback()
        flash(f"Error al exportar respaldo: {ex}", "error")
    return redirect(url_for("cierre"))


@app.route("/cierre/ejecutar", methods=["POST"])
@master_required
def cierre_ejecutar():
    """Cierra el período activo, arrastra stock y saldos al siguiente período y exporta reportes."""
    usar_anterior = request.form.get("usar_ruta_anterior") == "1"
    destino = (request.form.get("ruta_destino") or "").strip()

    conn = get_db()
    try:
        with conn.cursor() as cur:
            p = get_req_period(cur)
            if not p or p["estado"] != "ABIERTO":
                flash("No existe un período abierto para cerrar.", "error")
                return redirect(url_for("cierre"))

            if usar_anterior:
                destino = get_config_val(cur, "ultima_ruta_respaldo", destino)

            stock = get_req_stock(cur, p["id_periodo"])
            cur.execute("SELECT * FROM v_rendicion_cuentas WHERE id_periodo = %s", (p["id_periodo"],))
            rend = cur.fetchone()
            stock_final = int(stock["stock_disponible"])

            # Registra el arrastre histórico de deudas o saldos a favor al cierre
            cur.execute("SELECT id_unidad, saldo_acumulado_bs FROM unidades_militares WHERE ABS(saldo_acumulado_bs) > 0.005")
            for u in cur.fetchall():
                cur.execute(
                    """INSERT INTO historial_saldos_batallon (id_unidad, id_periodo, tipo, monto_bs, saldo_resultante_bs, motivo, id_usuario)
                       VALUES (%s,%s,'ARRASTRE_CIERRE',%s,%s,%s,%s)""",
                    (u["id_unidad"], p["id_periodo"], float(u["saldo_acumulado_bs"]), float(u["saldo_acumulado_bs"]),
                     f"Arrastre cierre {format_period_label(p)}", session["user_id"]),
                )
            cur.execute("UPDATE unidades_militares SET saldo_cierre_no_aplicado_bs = saldo_acumulado_bs")
            cur.execute("UPDATE despachos SET saldo_arrastrado = 1 WHERE id_periodo = %s AND anulado = 0", (p["id_periodo"],))

            cur.execute(
                """UPDATE periodos_cierre
                   SET estado='CERRADO', fecha_cierre=NOW(), stock_final_bolsas=%s,
                       total_entradas=%s, total_desp_internos=%s, total_desp_externos=%s, total_colaboraciones=%s,
                       monto_esperado_bs=%s, monto_cobrado_bs=%s, total_deudas_bs=%s, total_saldos_favor_bs=%s,
                       id_usuario_cierra=%s
                   WHERE id_periodo=%s""",
                (
                    stock_final,
                    int(stock["entradas_periodo"]),
                    int(stock["desp_internos_total"]),
                    int(stock["desp_externos_total"]),
                    int(stock["colaboraciones_descarga"]),
                    float(rend["monto_esperado_bs"]),
                    float(rend["monto_cobrado_bs"]),
                    float(rend["total_deudas_bs"]),
                    float(rend["total_saldos_favor_bs"]),
                    session["user_id"],
                    p["id_periodo"],
                ),
            )

            nxt = next_period_meta(p)
            nuevo_num = nxt["numero_en_anio"]
            nuevo_nombre = (request.form.get("nuevo_nombre") or "").strip() or nxt["nombre_periodo"]
            nueva_fecha_str = (request.form.get("nueva_fecha_inicio") or "").strip() or nxt["fecha_inicio"].isoformat()
            try:
                nueva_fecha_dt = datetime.date.fromisoformat(nueva_fecha_str)
            except ValueError:
                nueva_fecha_dt = nxt["fecha_inicio"]

            cur.execute(
                """INSERT INTO periodos_cierre
                   (numero_en_anio, anio_calendario, mes_numero, nombre_periodo, fecha_inicio, estado, precio_unitario_bolsa, stock_inicial_bolsas)
                   VALUES (%s, %s, %s, %s, %s, 'ABIERTO', %s, %s)""",
                (
                    nuevo_num, nueva_fecha_dt.year, nueva_fecha_dt.month,
                    nuevo_nombre, nueva_fecha_dt.isoformat(),
                    float(p["precio_unitario_bolsa"]), stock_final,
                ),
            )
            nxt_label = f"#{nuevo_num} - {nuevo_nombre} - {nueva_fecha_dt.strftime('%d/%m/%Y')}"
            _audit(
                cur, p["id_periodo"], session["user_id"], "periodos_cierre", p["id_periodo"], "CIERRE",
                f"Cierre manual de {format_period_label(p)}. Nuevo período abierto: {nxt_label}",
                {"estado": "ABIERTO"}, {"estado": "CERRADO", "stock_final_bolsas": stock_final},
            )
            conn.commit()

            if destino:
                dest_dir, archivos = export_period_bundle(cur, p["id_periodo"], destino, tipo="CIERRE")
                set_config_val(cur, "ultima_ruta_respaldo", destino)
                cur.execute(
                    "INSERT INTO respaldos_pendrive (id_periodo, tipo, ruta_destino, archivos, id_usuario) VALUES (%s,'CIERRE',%s,%s,%s)",
                    (p["id_periodo"], dest_dir, ", ".join(archivos), session["user_id"]),
                )
                conn.commit()
    except Exception as ex:
        conn.rollback()
        flash(f"Error al ejecutar cierre: {ex}", "error")
    return redirect(url_for("cierre"))


if __name__ == "__main__":
    app.run(host=cfg.get("app_host", "127.0.0.1"), port=int(cfg.get("app_port", 5000)), debug=False)
