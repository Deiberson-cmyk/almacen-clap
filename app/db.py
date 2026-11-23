# -*- coding: utf-8 -*-
"""Capa de Acceso a Datos (DAL), seguridad, validaciones y utilidades del sistema.

Responsabilidades principales de este módulo:
1. Conexión segura a MariaDB/MySQL local mediante el conector `pymysql`:
   - Utiliza `DictCursor` para devolver cada fila como diccionario clave-valor.
   - Usa codificación `utf8mb4` para soportar tildes, eñes y símbolos en español.
   - Configura `autocommit=False` para garantizar atomicidad transaccional ACID:
     ningún conjunto de escrituras queda guardado a medias ante un fallo
     (se persiste explícitamente con `conn.commit()` o se revierte con `conn.rollback()`).
2. Caché en memoria del archivo `config.json` con detección de cambios por fecha
   de modificación (`mtime`), evitando lecturas repetitivas en disco.
3. Seguridad criptográfica de contraseñas con PBKDF2-HMAC-SHA256 (200.000 iteraciones)
   y comparación de hashes en tiempo constante (`hmac.compare_digest`).
4. Validaciones de entrada (conversión numérica tolerante, políticas de clave,
   normalización de conceptos de pago).
5. Formateo de números y fechas al estándar venezolano/hispano, sincronización de
   saldos por batallón y registro legible de la bitácora de auditoría y accesos.
"""
import base64
import datetime
import decimal
import hashlib
import hmac
import json
import os
import re
import pymysql
from pymysql.cursors import DictCursor

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# Caché de configuración en memoria para evitar E/S repetitiva en disco
_CONFIG_CACHE = None
_CONFIG_MTIME = None

MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}

SECUENCIAS_TRIVIALES = (
    "0123", "1234", "2345", "3456", "4567", "5678", "6789",
    "9876", "8765", "7654", "6543", "5432", "4321", "3210",
    "abcd", "bcde", "cdef", "qwer", "asdf", "zxcv",
)


# ---------------------------------------------------------------------------
# 1. Configuración y Conexión a Base de Datos MariaDB
# ---------------------------------------------------------------------------
def load_config():
    """Carga y cachea en memoria `config.json`, recargando solo si cambia en disco."""
    global _CONFIG_CACHE, _CONFIG_MTIME
    mtime = os.path.getmtime(CONFIG_PATH)
    if _CONFIG_CACHE is None or mtime != _CONFIG_MTIME:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            _CONFIG_CACHE = json.load(f)
        _CONFIG_MTIME = mtime
    return _CONFIG_CACHE


def get_conn():
    """Abre y devuelve una nueva conexión transaccional hacia MariaDB/MySQL local."""
    cfg = load_config()
    return pymysql.connect(
        host=cfg["db_host"],
        port=int(cfg.get("db_port", 3306)),
        user=cfg["db_user"],
        password=cfg["db_password"],
        database=cfg["db_name"],
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
    )


# ---------------------------------------------------------------------------
# 2. Criptografía y Validaciones de Seguridad / Formularios
# ---------------------------------------------------------------------------
def hash_password(password: str, iterations: int = 200000) -> str:
    """Genera un hash seguro PBKDF2-SHA256 con sal aleatoria de 16 bytes."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """Verifica una contraseña contra su hash almacenado en tiempo constante."""
    try:
        algo, it_str, salt_b64, hash_b64 = stored_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(it_str))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def validate_fullname(nombre: str):
    """Valida que el nombre completo contenga al menos 2 letras y solo caracteres alfabéticos."""
    nombre = re.sub(r"\s+", " ", (nombre or "").strip())
    if len(nombre) < 2:
        return False, "El nombre completo debe tener al menos 2 caracteres."
    if not re.match(r"^[A-Za-zÁÉÍÓÚáéíóúÑñÜü\s]+$", nombre):
        return False, "El nombre completo solo debe contener letras y espacios (sin números ni símbolos)."
    return True, nombre


def validate_password_policy(pw: str):
    """Aplica la política de contraseñas: mayúscula inicial, mín. 2 dígitos y sin secuencias."""
    if not pw or len(pw) < 4:
        return False, "La contraseña debe tener al menos 4 caracteres."
    if not re.match(r"^[A-ZÁÉÍÓÚÑ]", pw):
        return False, "La contraseña debe iniciar con una letra mayúscula."
    digits = sum(1 for c in pw if c.isdigit())
    if digits < 2:
        return False, "La contraseña debe contener al menos dos (2) números."
    if re.search(r"(.)\1\1", pw):
        return False, "La contraseña no puede contener caracteres repetidos consecutivos (ej. 0000, 1111, 2222)."
    low = pw.lower()
    for seq in SECUENCIAS_TRIVIALES:
        if seq in low:
            return False, f"La contraseña no puede contener secuencias triviales ({seq})."
    return True, pw


def parse_int(val, default=0) -> int:
    """Convierte de forma segura un valor de formulario a entero sin lanzar ValueError."""
    try:
        s = str(val if val is not None else "").strip()
        if not s:
            return default
        return int(float(s.replace(",", ".")))
    except (ValueError, TypeError):
        return default


def parse_float(val, default=0.0) -> float:
    """Convierte de forma segura un valor numérico (aceptando coma o punto decimal) a float."""
    try:
        s = str(val if val is not None else "").strip().replace(",", ".")
        if not s:
            return default
        return float(s)
    except (ValueError, TypeError):
        return default


def normalize_concepto(raw: str) -> str:
    """Unifica códigos legados y recorta conceptos personalizados a un máximo de 100 caracteres."""
    concepto = (raw or "").strip()
    if not concepto or concepto == "UNIFICADO_AMBOS":
        return "Pago unificado"
    if concepto == "ABONO_COMPLEMENTARIO":
        return "Abono complementario"
    return concepto[:100]


# ---------------------------------------------------------------------------
# 3. Formateo de Cifras, Fechas, Períodos y Unidades
# ---------------------------------------------------------------------------
def fmt_num_es(val, decimals=2) -> str:
    """Formatea números con punto de miles y coma decimal (ej. 1.250,50)."""
    try:
        n = float(val or 0)
    except Exception:
        n = 0.0
    s = f"{n:,.{decimals}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def fmt_bs(val) -> str:
    """Formatea un importe monetario añadiendo el sufijo 'Bs.'."""
    return f"{fmt_num_es(val, 2)} Bs."


def fmt_fecha_es(val) -> str:
    """Convierte fechas o datetimes SQL a formato legible dd/mm/yyyy [HH:MM]."""
    if not val:
        return "-"
    if isinstance(val, datetime.datetime):
        return val.strftime("%d/%m/%Y %H:%M")
    if isinstance(val, datetime.date):
        return val.strftime("%d/%m/%Y")
    s = str(val)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s[:10])
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return s


def format_period_label(p) -> str:
    """Genera la etiqueta estándar de período: '#[num] - [Mes] - [dd/mm/yyyy]'."""
    if not p:
        return ""
    num = p.get("numero_en_anio") or p.get("id_periodo") or 1
    mes = (p.get("nombre_periodo") or "").strip()
    if mes.lower().startswith("período") or mes.lower().startswith("periodo"):
        mes_num = p.get("mes_numero") or (p["fecha_inicio"].month if hasattr(p.get("fecha_inicio"), "month") else 12)
        mes = MESES_ES.get(int(mes_num), mes)
    fini = fmt_fecha_es(p.get("fecha_inicio"))
    return f"#{num} - {mes} - {fini}"


def next_period_meta(p) -> dict:
    """Calcula automáticamente número, año, mes y fecha de inicio del período siguiente."""
    fi = p.get("fecha_inicio")
    cur_mes = int(p.get("mes_numero") or (fi.month if hasattr(fi, "month") else 12))
    cur_anio = int(p.get("anio_calendario") or (fi.year if hasattr(fi, "year") else 2026))
    cur_num = int(p.get("numero_en_anio") or 1)
    if cur_mes >= 12:
        next_mes = 1
        next_anio = cur_anio + 1
        next_num = 1
    else:
        next_mes = cur_mes + 1
        next_anio = cur_anio
        next_num = cur_num + 1
    next_nombre = MESES_ES[next_mes]
    next_fecha = datetime.date(next_anio, next_mes, 1)
    return {
        "numero_en_anio": next_num,
        "anio_calendario": next_anio,
        "mes_numero": next_mes,
        "nombre_periodo": next_nombre,
        "fecha_inicio": next_fecha,
    }


def format_unidad_display(codigo, nombre) -> str:
    """Combina código corto y nombre oficial de la unidad militar para la interfaz."""
    nom = (nombre or "").strip()
    cod = (codigo or "").strip()
    return f"{cod} {nom}".strip() if cod else nom


# ---------------------------------------------------------------------------
# 4. Consultas de Estado Operativo, Configuración y Sincronización de Saldos
# ---------------------------------------------------------------------------
def _json_default(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return str(obj)


def to_json(data):
    """Serializa un payload Python a JSON UTF-8 manejando fechas y objetos Decimal."""
    if data is None:
        return None
    if isinstance(data, str):
        return json.dumps({"detalle": data}, ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False, default=_json_default)


def get_active_period(cur):
    """Devuelve el único período actualmente abierto ('ABIERTO')."""
    cur.execute("SELECT * FROM periodos_cierre WHERE estado = 'ABIERTO' ORDER BY id_periodo DESC LIMIT 1")
    return cur.fetchone()


def get_stock_periodo(cur, id_periodo):
    """Consulta la vista v_stock_disponible para obtener entradas, salidas y stock disponible."""
    cur.execute("SELECT * FROM v_stock_disponible WHERE id_periodo = %s", (id_periodo,))
    return cur.fetchone()


def get_config_val(cur, clave, default=None):
    """Lee un parámetro de configuración persistido en `configuracion_sistema`."""
    cur.execute("SELECT valor FROM configuracion_sistema WHERE clave = %s", (clave,))
    row = cur.fetchone()
    return row["valor"] if row and row["valor"] is not None else default


def set_config_val(cur, clave, valor):
    """Inserta o actualiza un parámetro clave-valor en `configuracion_sistema`."""
    cur.execute(
        """INSERT INTO configuracion_sistema (clave, valor) VALUES (%s, %s)
           ON DUPLICATE KEY UPDATE valor = VALUES(valor)""",
        (clave, valor),
    )


def sync_unidad_saldo(cur, id_unidad):
    """Recalcula `saldo_acumulado_bs` de una unidad militar (arrastre + despachos vigentes)."""
    if not id_unidad:
        return
    p = get_active_period(cur)
    id_per = p["id_periodo"] if p else 0
    cur.execute(
        """UPDATE unidades_militares u
           SET u.saldo_acumulado_bs = u.saldo_cierre_no_aplicado_bs + IFNULL((
             SELECT SUM(d.diferencia_saldo_bs)
             FROM despachos d
             WHERE d.id_batallon_beneficiario = u.id_unidad
               AND d.id_periodo = %s
               AND d.tipo_salida = 'INTERNO_BATALLON'
               AND d.anulado = 0
               AND d.saldo_arrastrado = 0
           ), 0)
           WHERE u.id_unidad = %s""",
        (id_per, id_unidad),
    )


# ---------------------------------------------------------------------------
# 5. Bitácora de Seguridad (Accesos y Auditoría Humanizada)
# ---------------------------------------------------------------------------
def log_access(cur, id_usuario, usuario_intento, id_periodo, evento):
    """Registra eventos de autenticación (LOGIN_OK, LOGIN_FALLIDO, LOGOUT)."""
    cur.execute(
        """INSERT INTO historial_accesos (id_usuario, usuario_intento, id_periodo, evento)
           VALUES (%s, %s, %s, %s)""",
        (id_usuario, (usuario_intento or "")[:30], id_periodo, evento),
    )


def log_audit(cur, id_periodo, id_usuario, tabla, id_registro, accion, motivo, datos_anteriores=None, datos_nuevos=None):
    """Registra cada creación, edición, anulación, configuración o cierre en `auditoria`."""
    cur.execute(
        """INSERT INTO auditoria
           (id_periodo, id_usuario, tabla_afectada, id_registro, accion, motivo, datos_anteriores, datos_nuevos)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            id_periodo,
            id_usuario,
            tabla,
            int(id_registro),
            accion,
            (motivo or "")[:255] if motivo else None,
            to_json(datos_anteriores),
            to_json(datos_nuevos),
        ),
    )


_FIELD_LABELS = {
    "nombre_completo": "Nombre completo",
    "usuario": "Usuario",
    "rol": "Rol",
    "activo": "Activo",
    "cambio_clave": "Cambió clave",
    "nombre_periodo": "Mes/Nombre",
    "fecha_inicio": "Fecha de inicio",
    "precio_unitario_bolsa": "Precio por bolsa",
    "stock_inicial_bolsas": "Stock inicial",
    "fecha_recepcion": "Fecha recepción",
    "cantidad_bolsas": "Bolsas",
    "observaciones": "Observación",
    "receptor": "Receptor",
    "cantidad_familiar": "Plan Familiar",
    "cantidad_tango": "Plan Tango",
    "total_bolsas": "Total bolsas",
    "monto_pagado_bs": "Monto pagado",
    "referencia_bancaria": "Referencia",
    "fecha_pago": "Fecha de pago",
    "concepto_plan": "Concepto",
    "nombre_unidad": "Unidad",
    "codigo_unidad": "Código",
    "nombre_galpon": "Galpón",
    "estado_region": "Estado",
    "nombre_conductor": "Conductor",
    "placa_vehiculo": "Placa",
    "nombre_autorizado_retiro": "Autorizado",
    "cedula_autorizado_retiro": "Cédula",
    "diferencia_saldo_bs": "Diferencia saldo",
    "stock_final_bolsas": "Stock final",
    "anulado": "Anulado",
}


def _human_value(key, val, unit_map, galpon_map):
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return "Sí" if val else "No"
    if key in ("activo", "anulado"):
        return "Sí" if int(val) == 1 else "No"
    if key in ("beneficiario", "id_batallon_beneficiario", "retira", "id_batallon_retira", "id_unidad_padre"):
        return unit_map.get(int(val), str(val)) if str(val).isdigit() else str(val)
    if key in ("galpon", "id_galpon_destino"):
        return galpon_map.get(int(val), str(val)) if str(val).isdigit() else str(val)
    if key.endswith("_bs") or key in ("precio_unitario_bolsa", "precio_unitario_aplicado", "neto_bs"):
        return fmt_bs(val)
    if key in ("fecha_recepcion", "fecha_despacho", "fecha_pago", "fecha_inicio", "fecha"):
        return fmt_fecha_es(val)
    if key == "concepto_plan":
        return normalize_concepto(str(val))
    return str(val)


def humanize_audit_payload(raw_json, tabla, accion, unit_map, galpon_map):
    """Transforma un JSON almacenado en auditoría en una descripción legible para humanos."""
    if not raw_json:
        return "-"
    try:
        data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except Exception:
        return str(raw_json).replace("{", "").replace("}", "").replace('"', "")

    if not isinstance(data, dict):
        return str(data)
    if "detalle" in data and len(data) == 1:
        return str(data["detalle"])

    # Descripción enriquecida cuando se audita un movimiento de despacho
    if tabla == "despachos" and ("beneficiario" in data or "id_batallon_beneficiario" in data or "galpon" in data or "id_galpon_destino" in data):
        tipo = data.get("tipo") or data.get("tipo_salida")
        if tipo == "INTERNO_BATALLON" or "beneficiario" in data or "id_batallon_beneficiario" in data:
            bid = data.get("beneficiario") or data.get("id_batallon_beneficiario")
            rid = data.get("retira") or data.get("id_batallon_retira")
            b_nom = unit_map.get(int(bid), f"Unidad {bid}") if bid else "-"
            r_nom = "misma unidad" if (not rid or str(rid) == str(bid)) else unit_map.get(int(rid), f"Unidad {rid}")
            fam = data.get("familiar", data.get("cantidad_familiar", 0))
            tan = data.get("tango", data.get("cantidad_tango", 0))
            tot = data.get("total", data.get("total_bolsas", int(fam or 0) + int(tan or 0)))
            neto = data.get("neto_bs", data.get("monto_neto_exigible_bs"))
            msg = f"Despacho interno a {b_nom} (Retiró: {r_nom}) · {fam} Familiar + {tan} Tango = {tot} bolsas"
            if neto is not None:
                msg += f" · Total: {fmt_bs(neto)}"
            return msg
        if tipo == "EXTERNO_GALPON" or "galpon" in data or "id_galpon_destino" in data:
            gid = data.get("galpon") or data.get("id_galpon_destino")
            g_nom = galpon_map.get(int(gid), f"Galpón {gid}") if gid else "-"
            tot = data.get("total", data.get("total_bolsas", 0))
            cond = data.get("conductor") or data.get("nombre_conductor")
            placa = data.get("placa") or data.get("placa_vehiculo")
            msg = f"Despacho externo a {g_nom} · {tot} bolsas"
            if cond:
                msg += f" · Conductor: {cond}"
            if placa:
                msg += f" ({placa})"
            return msg

    parts = []
    # 'estado_entrega' se mantiene en skip_keys para preservar legibilidad de registros históricos antiguos
    skip_keys = {
        "id_entrada", "id_despacho", "id_pago", "id_colaboracion",
        "id_usuario", "id_periodo", "id_usuario_registra", "fecha_registro",
        "nuevo_periodo", "estado_entrega", "saldo_arrastrado", "motivo_anulacion",
    }
    for k, v in data.items():
        if k in skip_keys:
            continue
        if k == "cambio_clave":
            if v:
                parts.append("Contraseña actualizada")
            continue
        hv = _human_value(k, v, unit_map, galpon_map)
        if hv is None:
            continue
        label = _FIELD_LABELS.get(k, k.replace("_", " ").capitalize())
        if len(data) == 1 and k == "nombre_completo":
            return hv
        parts.append(f"{label}: {hv}")
    return " · ".join(parts) if parts else "-"


def build_lookup_maps(cur):
    """Construye diccionarios id->nombre para resolver rápidamente IDs en auditoría/reportes."""
    cur.execute("SELECT id_unidad, codigo_unidad, nombre_unidad FROM unidades_militares")
    unit_map = {r["id_unidad"]: format_unidad_display(r["codigo_unidad"], r["nombre_unidad"]) for r in cur.fetchall()}
    cur.execute("SELECT id_galpon, nombre_galpon, estado_region FROM galpones_externos")
    galpon_map = {
        r["id_galpon"]: f"{r['nombre_galpon']} ({r['estado_region']})" if r["estado_region"] else r["nombre_galpon"]
        for r in cur.fetchall()
    }
    return unit_map, galpon_map
