# -*- coding: utf-8 -*-
"""Genera schema.sql (con catálogo de unidades) desde Dis_unidad.xlsx.
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

print('unidades:', len(units))
