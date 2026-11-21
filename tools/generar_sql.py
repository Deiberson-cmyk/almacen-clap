# -*- coding: utf-8 -*-
"""Genera schema.sql (con catálogo de unidades con código corto separado) desde Dis_unidad.xlsx.
Uso: python tools/generar_sql.py"""
import os
import re
import openpyxl

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(BASE, "Dis_unidad.xlsx")
wb = openpyxl.load_workbook(XLSX, data_only=True)
ws = wb["CON OFICIO"]
ws2 = wb["SIN OFICIO"]


def q(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def split_codigo_nombre(raw):
    m = re.match(r"^(\d{2,4})\s*(.+)$", raw)
    if m:
        cod = m.group(1)
        nom = norm(m.group(2))
        return cod, nom
    return None, raw


PM_CHILD = {"3501", "3509", "351", "352", "353"}
units = []  # [nombre_limpio, codigo, padre_cod, es_agrupador, agrupados, row]
for r in range(3, 96):
    v = ws.cell(r, 2).value
    if v is None:
        continue
    raw = norm(v)
    if raw in PM_CHILD:
        units.append(["UNIDAD POLICIA MILITAR", raw, "35", 0, None, r])
        continue
    cod, nom = split_codigo_nombre(raw)
    es_agr = 1 if raw == "35 POLICIA MILITAR" else 0
    agrup = "Abarca: 35, 3501, 3509, 351, 352, 353" if es_agr else None
    units.append([nom, cod, None, es_agr, agrup, r])

for r in range(2, 6):
    raw = norm(ws2.cell(r, 2).value)
    cod, nom = split_codigo_nombre(raw)
    units.append([nom, cod, None, 0, None, -r])

pairs = [(u[1], u[0]) for u in units]
assert len(pairs) == len(set(pairs)), "pares (codigo, nombre) duplicados"

base = open(os.path.join(BASE, "tools", "schema_base.sql"), encoding="utf-8").read()
ins = [
    "-- ----------------------------------------------------------------------------",
    "-- CATÁLOGO INICIAL DE UNIDADES (%d, extraídas de Dis_unidad.xlsx; código separado)" % len(units),
    "-- ----------------------------------------------------------------------------",
]
for u in units:
    if u[2] is None:
        ins.append(
            "INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (%s, %s, %d, %s);"
            % (q(u[1]) if u[1] else "NULL", q(u[0]), u[3], q(u[4]) if u[4] else "NULL")
        )
for u in units:
    if u[2]:
        ins.append(
            "INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, id_unidad_padre) VALUES (%s, %s, (SELECT p.id_unidad FROM (SELECT id_unidad FROM unidades_militares WHERE codigo_unidad = %s LIMIT 1) p));"
            % (q(u[1]), q(u[0]), q(u[2]))
        )

open(os.path.join(BASE, "schema.sql"), "w", encoding="utf-8", newline="\n").write(
    base.replace("-- @@UNIDADES@@", "\n".join(ins)) + "\n"
)
print("unidades:", len(units))
