# -*- coding: utf-8 -*-
"""Genera schema.sql (catálogo de unidades) y seed_movimientos_excel.sql desde Dis_unidad.xlsx.
Uso: python tools/generar_sql.py   (requiere openpyxl; solo en la laptop de desarrollo)"""
import re, datetime, os, openpyxl

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(BASE, "Dis_unidad.xlsx")
PRECIO = 857.48
wb = openpyxl.load_workbook(XLSX, data_only=True)
ws = wb["CON OFICIO"]
ws2 = wb["SIN OFICIO"]

def q(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"

def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()

def codigo(nombre):
    m = re.match(r"^(\d{2,4})(?=\s|CIA)", nombre)
    return m.group(1) if m else None

def fecha(v):
    if isinstance(v, datetime.datetime):
        return v.date().isoformat()
    if isinstance(v, str):
        d = re.sub(r"\D", "", v)
        if len(d) == 8:  # ddmmyyyy
            return f"{d[4:]}-{d[2:4]}-{d[:2]}"
    return None

PM_CHILD = {"3501", "3509", "351", "352", "353"}
units = []  # (nombre, codigo, padre_nombre, es_agrupador, agrupados, row)
pm_name = None
for r in range(3, 96):
    v = ws.cell(r, 2).value
    if v is None:
        continue
    raw = norm(v)
    if raw in PM_CHILD:
        units.append(("UNIDAD %s (POLICIA MILITAR)" % raw, raw, "35 POLICIA MILITAR", 0, None, r))
        continue
    units.append([raw, codigo(raw), None, 0, None, r])
    if raw == "35 POLICIA MILITAR":
        units[-1][3] = 1
        units[-1][4] = "Abarca: 35, 3501, 3509, 351, 352, 353"
for r in range(2, 6):
    units.append([norm(ws2.cell(r, 2).value), None, None, 0, None, -r])
names = [u[0] for u in units]
assert len(names) == len(set(names)), "nombres duplicados"

# ---------- schema.sql ----------
base = open(os.path.join(BASE, "tools", "schema_base.sql"), encoding="utf-8").read()
ins = ["-- ----------------------------------------------------------------------------",
       "-- CATÁLOGO INICIAL DE UNIDADES (%d, extraídas de Dis_unidad.xlsx; editable por MASTER)" % len(units),
       "-- ----------------------------------------------------------------------------"]
for u in units:
    if u[2] is None:
        ins.append("INSERT INTO unidades_militares (nombre_unidad, codigo_unidad, es_agrupador, codigos_agrupados_texto) VALUES (%s, %s, %d, %s);" %
                   (q(u[0]), q(u[1]) if u[1] else "NULL", u[3], q(u[4]) if u[4] else "NULL"))
for u in units:
    if u[2]:
        ins.append("INSERT INTO unidades_militares (nombre_unidad, codigo_unidad, id_unidad_padre) VALUES (%s, %s, (SELECT p.id_unidad FROM (SELECT id_unidad FROM unidades_militares WHERE nombre_unidad = %s) p));" %
                   (q(u[0]), q(u[1]), q(u[2])))
open(os.path.join(BASE, "schema.sql"), "w", encoding="utf-8", newline="\n").write(base.replace("-- @@UNIDADES@@", "\n".join(ins)))

# ---------- seed_movimientos_excel.sql ----------
def num(v):
    return float(v) if isinstance(v, (int, float)) else 0.0

out = ["-- Movimientos reales del Excel (Período 1, precio %.2f). OPCIONAL: solo para pruebas/migración." % PRECIO,
       "-- Pagos corregidos: fila 93 IESEOFANB (la hoja sumaba la fila equivocada).",
       "USE almacen_clap_ejercito;", ""]
notas = []
for u in units:
    r = u[5]
    if r < 0:
        continue
    nombre = u[0]
    if nombre.startswith("UNIDAD ") and u[2]:
        continue
    fam, tan = int(num(ws.cell(r, 3).value)), int(num(ws.cell(r, 4).value))
    if fam + tan == 0:
        continue
    obs = ws.cell(r, 17).value
    # pagos: [(plan, monto, ref, fecha)]
    pagos = []
    rows = [r]
    if r == 64: rows = [64, 65, 66]
    if r == 80: rows = [80, 81]
    refN, fN = ws.cell(r, 15).value, ws.cell(r, 16).value
    for rr in rows:
        h, k = ws.cell(rr, 8).value, ws.cell(rr, 11).value
        if isinstance(h, (int, float)) and h:
            pagos.append(("PLAN_FAMILIAR", h, ws.cell(rr, 9).value or refN, ws.cell(rr, 10).value or fN))
        if isinstance(k, (int, float)) and k:
            pagos.append(("PLAN_TANGO", k, ws.cell(rr, 12).value or refN, ws.cell(rr, 13).value or fN))
    if not pagos:
        n = ws.cell(r, 14).value
        if isinstance(n, (int, float)) and n:
            pagos.append(("UNIFICADO_AMBOS", n, refN, fN))
    primera = [fecha(p[3]) for p in pagos if fecha(p[3])]
    fdesp = min(primera) if primera else "2026-09-10"
    retira = nombre
    if r == 39:
        retira = "7001 CIA MANDO Y SERVICIO"
    base_bs = round((fam + tan) * PRECIO, 2)
    estado_ent = "DESPACHADO"
    out.append("INSERT INTO despachos (id_periodo, tipo_salida, fecha_despacho, id_batallon_beneficiario, id_batallon_retira, cantidad_familiar, cantidad_tango, total_bolsas, modalidad_oficio, tiene_recibo, estado_entrega, precio_unitario_aplicado, monto_base_bs, monto_neto_exigible_bs, observaciones, id_usuario_registra)")
    out.append("VALUES (1, 'INTERNO_BATALLON', %s, (SELECT id_unidad FROM unidades_militares WHERE nombre_unidad=%s), (SELECT id_unidad FROM unidades_militares WHERE nombre_unidad=%s), %d, %d, %d, 'CON_OFICIO', 1, %s, %.2f, %.2f, %.2f, %s, 1);" %
              (q(fdesp), q(nombre), q(retira), fam, tan, fam + tan, q(estado_ent), PRECIO, base_bs, base_bs, q(obs) if obs else "NULL"))
    for plan, monto, ref, fp in pagos:
        f = fecha(fp)
        out.append("INSERT INTO pagos_batallon (id_despacho, id_periodo, id_batallon_pagador, concepto_plan, monto_pagado_bs, referencia_bancaria, fecha_pago, sin_fecha_comprobante, id_usuario_registra) VALUES ((SELECT id_despacho FROM despachos WHERE id_batallon_beneficiario=(SELECT id_unidad FROM unidades_militares WHERE nombre_unidad=%s) AND id_periodo=1 AND tipo_salida='INTERNO_BATALLON'), 1, (SELECT id_unidad FROM unidades_militares WHERE nombre_unidad=%s), %s, %.2f, %s, %s, %d, 1);" %
                   (q(nombre), q(nombre), q(plan), float(monto), q(str(ref).strip()) if ref else "NULL", q(f) if f else "NULL", 0 if f else 1))
    out.append("")
open(os.path.join(BASE, "seed_movimientos_excel.sql"), "w", encoding="utf-8", newline="\n").write("\n".join(out))
print("unidades:", len(units), "| despachos seed:", sum(1 for l in out if l.startswith("INSERT INTO despachos")),
      "| pagos seed:", sum(1 for l in out if l.startswith("INSERT INTO pagos_batallon")))
