# -*- coding: utf-8 -*-
"""Exportador de reportes locales (.xlsx y .pdf) hacia Pendrive USB o carpeta local.

Este módulo genera el paquete documental de respaldo y cierre de cada período:
1. Reporte completo en Excel (.xlsx) mediante `openpyxl`, estructurado en 7 hojas:
   Balance_Financiero, Despachos_Internos, Despachos_Externos, Entradas_Lotes,
   Colaboraciones_Descarga, Auditoria y Accesos.
2. Reporte ejecutivo en PDF (.pdf) mediante `fpdf` con inventario consolidado,
   balance financiero y detalle de despachos internos/externos.
"""
import ctypes
import datetime
import os
import re
import string
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from fpdf import FPDF
from db import (
    build_lookup_maps,
    fmt_fecha_es,
    fmt_num_es,
    format_period_label,
    humanize_audit_payload,
)


def detect_usb_drives():
    """Detecta unidades extraíbles (Pendrive USB) conectadas en Windows usando Win32 API."""
    drives = []
    try:
        kernel32 = ctypes.windll.kernel32
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            # El código 2 corresponde a DRIVE_REMOVABLE en kernel32.GetDriveTypeW
            if os.path.exists(root) and kernel32.GetDriveTypeW(root) == 2:
                drives.append(root)
    except Exception:
        pass
    return drives


def _safe_name(text):
    """Sanitiza un texto para usarlo de forma segura como nombre de subcarpeta en Windows."""
    text = re.sub(r"[^A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ ]+", "_", str(text or ""))
    return re.sub(r"\s+", "_", text.strip())[:60] or "Periodo"


def _pdf_safe(text):
    """Codifica cadenas UTF-8 a Latin-1 para compatibilidad con fuentes estándar de FPDF."""
    s = str(text if text is not None else "")
    return s.encode("latin-1", "replace").decode("latin-1")


def export_period_bundle(cur, id_periodo, base_dest_dir, tipo="RAPIDO"):
    """Extrae los datos del período y genera únicamente los archivos .xlsx y .pdf."""
    cur.execute("SELECT * FROM periodos_cierre WHERE id_periodo = %s", (id_periodo,))
    periodo = cur.fetchone()
    if not periodo:
        raise ValueError("Período no encontrado")

    # Construir carpeta ordenada: Respaldos_CLAP_FuerteTiuna/<Año>/P<N>_<Mes>_<Tipo>_<Fecha>/
    now = datetime.datetime.now()
    anio = str(periodo.get("anio_calendario") or (periodo["fecha_inicio"].year if periodo.get("fecha_inicio") else now.year))
    num_anio = int(periodo.get("numero_en_anio") or id_periodo)
    tag = "Cierre" if tipo == "CIERRE" else "Respaldo"
    subcarpeta = f"P{num_anio:02d}_{_safe_name(periodo['nombre_periodo'])}_{tag}_{now.strftime('%Y-%m-%d_%H%M')}"
    dest_dir = os.path.join(base_dest_dir, "Respaldos_CLAP_FuerteTiuna", anio, subcarpeta)
    os.makedirs(dest_dir, exist_ok=True)

    archivos = []

    # Consultas consolidadas de vistas y tablas históricas del período
    cur.execute("SELECT * FROM v_rendicion_cuentas WHERE id_periodo = %s", (id_periodo,))
    rend = cur.fetchone() or {}
    cur.execute("SELECT * FROM v_stock_disponible WHERE id_periodo = %s", (id_periodo,))
    stock = cur.fetchone() or {}

    cur.execute(
        """SELECT CONCAT('ENT-', LPAD(e.id_entrada,6,'0')) AS codigo, e.fecha_recepcion, e.cantidad_bolsas,
                  e.observaciones, u.usuario, e.anulado, e.motivo_anulacion
           FROM entradas_inventario e JOIN usuarios u ON u.id_usuario = e.id_usuario_registra
           WHERE e.id_periodo = %s ORDER BY e.id_entrada""",
        (id_periodo,),
    )
    entradas = cur.fetchall()

    cur.execute(
        """SELECT * FROM v_conciliacion_batallones WHERE id_periodo = %s ORDER BY codigo_movimiento""",
        (id_periodo,),
    )
    desp_internos = cur.fetchall()

    cur.execute(
        """SELECT CONCAT('DES-', LPAD(d.id_despacho,6,'0')) AS codigo, d.fecha_despacho,
                  g.nombre_galpon, g.estado_region, d.cantidad_familiar, d.cantidad_tango, d.total_bolsas,
                  d.nombre_conductor, d.cedula_conductor, d.vehiculo_descripcion, d.placa_vehiculo,
                  d.observaciones
           FROM despachos d
           JOIN galpones_externos g ON g.id_galpon = d.id_galpon_destino
           WHERE d.id_periodo = %s AND d.tipo_salida = 'EXTERNO_GALPON' AND d.anulado = 0
           ORDER BY d.id_despacho""",
        (id_periodo,),
    )
    desp_externos = cur.fetchall()

    cur.execute(
        """SELECT CONCAT('COL-', LPAD(c.id_colaboracion,6,'0')) AS codigo, c.fecha, c.cantidad_bolsas,
                  c.receptor, c.observaciones, u.usuario
           FROM colaboraciones_descarga c JOIN usuarios u ON u.id_usuario = c.id_usuario_registra
           WHERE c.id_periodo = %s AND c.anulado = 0 ORDER BY c.id_colaboracion""",
        (id_periodo,),
    )
    colabs = cur.fetchall()

    cur.execute(
        """SELECT a.fecha_hora, u.usuario, a.accion, a.tabla_afectada, a.motivo,
                  a.datos_anteriores, a.datos_nuevos
           FROM auditoria a JOIN usuarios u ON u.id_usuario = a.id_usuario
           WHERE a.id_periodo = %s ORDER BY a.id_auditoria DESC""",
        (id_periodo,),
    )
    unit_map, galpon_map = build_lookup_maps(cur)
    auditoria = [
        {
            "fecha_hora": fmt_fecha_es(a["fecha_hora"]),
            "usuario": a["usuario"],
            "accion": a["accion"],
            "motivo": a["motivo"] or "-",
            "anterior": humanize_audit_payload(a["datos_anteriores"], a["tabla_afectada"], a["accion"], unit_map, galpon_map),
            "nuevo": humanize_audit_payload(a["datos_nuevos"], a["tabla_afectada"], a["accion"], unit_map, galpon_map),
        }
        for a in cur.fetchall()
    ]

    cur.execute(
        """SELECT h.fecha_hora, h.usuario_intento, h.evento
           FROM historial_accesos h WHERE h.id_periodo = %s ORDER BY h.id_acceso DESC""",
        (id_periodo,),
    )
    accesos = cur.fetchall()

    # 1) Reporte completo en Excel (.xlsx)
    xlsx_name = f"reporte_completo_{anio}_P{num_anio:02d}.xlsx"
    xlsx_path = os.path.join(dest_dir, xlsx_name)
    _build_excel(xlsx_path, periodo, stock, rend, entradas, desp_internos, desp_externos, colabs, auditoria, accesos)
    archivos.append(xlsx_name)

    # 2) Reporte ejecutivo en PDF (.pdf)
    pdf_name = f"reporte_ejecutivo_{anio}_P{num_anio:02d}.pdf"
    pdf_path = os.path.join(dest_dir, pdf_name)
    _build_pdf(pdf_path, periodo, stock, rend, desp_internos, desp_externos)
    archivos.append(pdf_name)

    return dest_dir, archivos


def _style_header(ws, row_idx, col_count):
    """Aplica estilo verde institucional, fuente blanca y alineación centrada a una fila cabecera."""
    fill = PatternFill("solid", fgColor="1B4332")
    font = Font(bold=True, color="FFFFFF", size=10)
    border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )
    for c in range(1, col_count + 1):
        cell = ws.cell(row_idx, c)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border


def _autofit(ws):
    """Ajusta el ancho de cada columna de la hoja de Excel al contenido máximo."""
    for col in ws.columns:
        max_len = 10
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        for cell in col:
            val = str(cell.value or "")
            if len(val) > max_len:
                max_len = min(len(val), 48)
        ws.column_dimensions[col_letter].width = max_len + 3


def _build_excel(path, periodo, stock, rend, entradas, desp_int, desp_ext, colabs, auditoria, accesos):
    """Construye el libro Excel completo con 7 hojas temáticas y formatos contables."""
    wb = openpyxl.Workbook()
    etiqueta = format_period_label(periodo)

    border_thin = Border(
        left=Side(style="thin", color="D0D0D0"),
        right=Side(style="thin", color="D0D0D0"),
        top=Side(style="thin", color="D0D0D0"),
        bottom=Side(style="thin", color="D0D0D0"),
    )

    # Hoja 1: Balance_Financiero (Resumen de Inventario y Rendición Financiera)
    ws = wb.active
    ws.title = "Balance_Financiero"
    ws["A1"] = "Almacén de Alimentos CLAP"
    ws["A1"].font = Font(bold=True, size=13, color="1B4332")
    ws["A2"] = f"Fuerte Tiuna, Caracas - Reporte Ejecutivo | Período: {etiqueta} | Estado: {periodo['estado']} | Precio Bolsa: {fmt_num_es(periodo['precio_unitario_bolsa'], 2)}"
    ws["A2"].font = Font(italic=True, size=10)

    ws.append(["", ""])  # Fila 3 separadora
    ws.append(["Resumen de Inventario", "Cantidad"])
    _style_header(ws, 4, 2)

    inv_rows = [
        ("Stock Inicial (Arrastre cierre anterior)", int(stock.get("stock_inicial_arrastre", 0))),
        ("Entradas de Lotes en el Período", int(stock.get("entradas_periodo", 0))),
        ("Despachos Internos - Plan Familiar", int(stock.get("desp_internos_familiar", 0))),
        ("Despachos Internos - Plan Tango", int(stock.get("desp_internos_tango", 0))),
        ("Total Despachos Internos (Batallones)", int(stock.get("desp_internos_total", 0))),
        ("Total Despachos Externos (Galpones)", int(stock.get("desp_externos_total", 0))),
        ("Salidas por Colaboración y Descarga", int(stock.get("colaboraciones_descarga", 0))),
        ("STOCK DISPONIBLE EN ALMACÉN", int(stock.get("stock_disponible", 0))),
    ]
    for row in inv_rows:
        ws.append(list(row))

    for r in range(5, 13):
        c1, c2 = ws.cell(r, 1), ws.cell(r, 2)
        c1.border = border_thin
        c2.border = border_thin
        c1.alignment = Alignment(horizontal="left", vertical="center")
        c2.alignment = Alignment(horizontal="right", vertical="center")
        c2.number_format = "#,##0"
        if r == 12:
            c1.font = Font(bold=True)
            c2.font = Font(bold=True)

    ws.append(["", ""])  # Fila 13 separadora
    ws.append(["Balance Financiero", "Monto"])
    _style_header(ws, 14, 2)

    fin_rows = [
        ("Monto Total Esperado", float(rend.get("monto_esperado_bs", 0))),
        ("Monto Total Cobrado", float(rend.get("monto_cobrado_bs", 0))),
        ("Total Deudas Pendientes", float(rend.get("total_deudas_bs", 0))),
        ("Total Saldos a Favor", float(rend.get("total_saldos_favor_bs", 0))),
    ]
    for row in fin_rows:
        ws.append(list(row))

    for r in range(15, 19):
        c1, c2 = ws.cell(r, 1), ws.cell(r, 2)
        c1.border = border_thin
        c2.border = border_thin
        c1.alignment = Alignment(horizontal="left", vertical="center")
        c2.alignment = Alignment(horizontal="right", vertical="center")
        c2.number_format = "#,##0.00"

    _autofit(ws)

    # Hoja 2: Despachos_Internos
    ws2 = wb.create_sheet("Despachos_Internos")
    h2 = [
        "Código", "Fecha", "Batallón Beneficiario", "Batallón que Retiró",
        "Familiar", "Tango", "Total Bolsas",
        "Subtotal Bolsas", "Saldo Ant. Aplicado",
        "Total Definitivo", "Pagado", "Diferencia", "Estado Pago",
        "Detalle Pagos / Refs", "Observaciones",
    ]
    ws2.append(h2)
    _style_header(ws2, 1, len(h2))
    for d in desp_int:
        if d.get("saldo_arrastrado"):
            dif = float(d.get("diferencia_saldo_bs") or 0)
            if dif > 0.005:
                est = f"A FAVOR (+{fmt_num_es(dif, 2)})"
            elif dif < -0.005:
                est = f"DEUDA ({fmt_num_es(dif, 2)})"
            else:
                est = "PAGADO (Solvente)"
        else:
            est = d["estado_financiero"]
        ws2.append([
            d["codigo_movimiento"], fmt_fecha_es(d["fecha_despacho"]), d["batallon_beneficiario"],
            d["batallon_que_retiro"] or d["batallon_beneficiario"],
            int(d["cantidad_familiar"]), int(d["cantidad_tango"]), int(d["total_bolsas"]),
            float(d["monto_base_bs"] or 0), float(d["saldo_anterior_aplicado_bs"] or 0),
            float(d["monto_neto_exigible_bs"] or 0),
            float(d["monto_total_pagado_bs"] or 0), float(d["diferencia_saldo_bs"] or 0),
            est, d["pagos_detalle"] or "", d["observaciones"] or "",
        ])
    for r in range(2, ws2.max_row + 1):
        for c in range(1, len(h2) + 1):
            cell = ws2.cell(r, c)
            cell.border = border_thin
            if c in (1, 2, 13):
                cell.alignment = Alignment(horizontal="center", vertical="center")
            elif c in (5, 6, 7):
                cell.alignment = Alignment(horizontal="right", vertical="center")
                cell.number_format = "#,##0"
            elif c in (8, 9, 10, 11, 12):
                cell.alignment = Alignment(horizontal="right", vertical="center")
                cell.number_format = "#,##0.00"
            else:
                cell.alignment = Alignment(horizontal="left", vertical="center")
    _autofit(ws2)

    # Hoja 3: Despachos_Externos
    ws3 = wb.create_sheet("Despachos_Externos")
    h3 = ["Código", "Fecha", "Galpón Destino", "Estado/Región", "Familiar", "Tango", "Total Bolsas", "Conductor", "Cédula", "Vehículo", "Placa", "Observaciones"]
    ws3.append(h3)
    _style_header(ws3, 1, len(h3))
    for d in desp_ext:
        ws3.append([
            d["codigo"], fmt_fecha_es(d["fecha_despacho"]), d["nombre_galpon"], d["estado_region"] or "",
            int(d["cantidad_familiar"]), int(d["cantidad_tango"]), int(d["total_bolsas"]),
            d["nombre_conductor"] or "", d["cedula_conductor"] or "",
            d["vehiculo_descripcion"] or "", d["placa_vehiculo"] or "",
            d["observaciones"] or "",
        ])
    _autofit(ws3)

    # Hoja 4: Entradas_Lotes
    ws4 = wb.create_sheet("Entradas_Lotes")
    h4 = ["Código", "Fecha Recepción", "Cantidad Bolsas", "Observaciones", "Usuario", "Estado"]
    ws4.append(h4)
    _style_header(ws4, 1, len(h4))
    for e in entradas:
        ws4.append([
            e["codigo"], fmt_fecha_es(e["fecha_recepcion"]), int(e["cantidad_bolsas"]),
            e["observaciones"] or "", e["usuario"], "ANULADO" if e["anulado"] else "ACTIVO",
        ])
    _autofit(ws4)

    # Hoja 5: Colaboraciones_Descarga
    ws5 = wb.create_sheet("Colaboraciones_Descarga")
    h5 = ["Código", "Fecha", "Bolsas", "Receptor", "Observaciones", "Usuario"]
    ws5.append(h5)
    _style_header(ws5, 1, len(h5))
    for c in colabs:
        ws5.append([c["codigo"], fmt_fecha_es(c["fecha"]), int(c["cantidad_bolsas"]), c["receptor"] or "Ayudante de descarga", c["observaciones"] or "", c["usuario"]])
    _autofit(ws5)

    # Hoja 6: Auditoria
    ws6 = wb.create_sheet("Auditoria")
    h6 = ["Fecha/Hora", "Usuario", "Acción", "Motivo", "Anterior", "Nuevo"]
    ws6.append(h6)
    _style_header(ws6, 1, len(h6))
    for a in auditoria:
        ws6.append([a["fecha_hora"], a["usuario"], a["accion"], a["motivo"], a["anterior"], a["nuevo"]])
    _autofit(ws6)

    # Hoja 7: Accesos
    ws7 = wb.create_sheet("Accesos")
    h7 = ["Fecha/Hora", "Usuario", "Evento"]
    ws7.append(h7)
    _style_header(ws7, 1, len(h7))
    for ac in accesos:
        ws7.append([fmt_fecha_es(ac["fecha_hora"]), ac["usuario_intento"], ac["evento"]])
    _autofit(ws7)

    wb.save(path)


def _build_pdf(path, periodo, stock, rend, desp_int, desp_ext):
    """Genera el reporte ejecutivo imprimible en formato PDF."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    etiqueta = format_period_label(periodo)

    # Encabezado institucional
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, _pdf_safe("Almacén de Alimentos CLAP"), ln=True, align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, _pdf_safe("Fuerte Tiuna, Caracas - Reporte Ejecutivo"), ln=True, align="C")
    pdf.ln(3)

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, _pdf_safe(f"Periodo: {etiqueta} ({periodo['estado']}) | Precio Bolsa: {fmt_num_es(periodo['precio_unitario_bolsa'], 2)}"), ln=True)
    pdf.cell(0, 6, _pdf_safe(f"Generado: {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}"), ln=True)
    pdf.ln(3)

    # Tabla 1: Resumen de Inventario (cabecera centrada)
    pdf.set_fill_color(27, 67, 50)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(130, 7, _pdf_safe("Resumen de Inventario"), border=1, fill=True, align="C")
    pdf.cell(60, 7, _pdf_safe("Cantidad"), border=1, fill=True, align="C", ln=True)

    pdf.set_text_color(0, 0, 0)
    inv_items = [
        ("Stock Inicial (Arrastre del cierre anterior)", stock.get("stock_inicial_arrastre", 0)),
        ("(+) Entradas de Lotes recibidos en el periodo", stock.get("entradas_periodo", 0)),
        ("(-) Despachos Internos - Plan Familiar", stock.get("desp_internos_familiar", 0)),
        ("(-) Despachos Internos - Plan Tango", stock.get("desp_internos_tango", 0)),
        ("(-) Total Despachos Internos a Batallones", stock.get("desp_internos_total", 0)),
        ("(-) Despachos Externos a Galpones Nacionales", stock.get("desp_externos_total", 0)),
        ("(-) Salidas por Colaboracion y Descarga", stock.get("colaboraciones_descarga", 0)),
        ("(=) STOCK DISPONIBLE EN ALMACEN", stock.get("stock_disponible", 0)),
    ]
    for label, val in inv_items:
        bold = "B" if label.startswith("(=)") else ""
        pdf.set_font("Helvetica", bold, 10)
        pdf.cell(130, 6.5, _pdf_safe(label), border=1)
        pdf.cell(60, 6.5, fmt_num_es(val, 0), border=1, align="R", ln=True)

    # Tabla 2: Balance Financiero (cabecera centrada, montos limpios sin 'Bs.')
    pdf.ln(4)
    pdf.set_fill_color(27, 67, 50)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(130, 7, _pdf_safe("Balance Financiero"), border=1, fill=True, align="C")
    pdf.cell(60, 7, _pdf_safe("Monto"), border=1, fill=True, align="C", ln=True)

    pdf.set_text_color(0, 0, 0)
    fin_items = [
        ("Monto Total Esperado (Despachos Internos)", rend.get("monto_esperado_bs", 0)),
        ("Monto Total Cobrado (Transferencias Registradas)", rend.get("monto_cobrado_bs", 0)),
        ("Total Deudas Pendientes por Cobrar", rend.get("total_deudas_bs", 0)),
        ("Total Saldos a Favor (Redondeos / Excedentes)", rend.get("total_saldos_favor_bs", 0)),
    ]
    for label, val in fin_items:
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(130, 6.5, _pdf_safe(label), border=1)
        pdf.cell(60, 6.5, _pdf_safe(fmt_num_es(val, 2)), border=1, align="R", ln=True)

    # Tabla 3: Detalle de Despachos Internos
    if desp_int:
        pdf.ln(5)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, _pdf_safe(f"DETALLE DE DESPACHOS INTERNOS ({len(desp_int)} registros)"), ln=True)
        pdf.set_fill_color(45, 106, 79)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(22, 6, "Codigo", 1, 0, "C", True)
        pdf.cell(66, 6, "Batallon Beneficiario", 1, 0, "C", True)
        pdf.cell(14, 6, "Fam.", 1, 0, "C", True)
        pdf.cell(14, 6, "Tango", 1, 0, "C", True)
        pdf.cell(26, 6, "Neto", 1, 0, "C", True)
        pdf.cell(26, 6, "Pagado", 1, 0, "C", True)
        pdf.cell(22, 6, "Estado", 1, 1, "C", True)

        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 7.5)
        for d in desp_int:
            pdf.cell(22, 5.5, _pdf_safe(d["codigo_movimiento"]), 1, 0, "C")
            pdf.cell(66, 5.5, _pdf_safe(str(d["batallon_beneficiario"])[:36]), 1)
            pdf.cell(14, 5.5, str(int(d["cantidad_familiar"])), 1, 0, "R")
            pdf.cell(14, 5.5, str(int(d["cantidad_tango"])), 1, 0, "R")
            pdf.cell(26, 5.5, fmt_num_es(d["monto_neto_exigible_bs"], 2), 1, 0, "R")
            pdf.cell(26, 5.5, fmt_num_es(d["monto_total_pagado_bs"], 2), 1, 0, "R")
            if d.get("saldo_arrastrado"):
                dif = float(d.get("diferencia_saldo_bs") or 0)
                est = "A FAVOR" if dif > 0.005 else ("DEUDA" if dif < -0.005 else "PAGADO")
            else:
                est = d["estado_financiero"]
            pdf.cell(22, 5.5, _pdf_safe(est), 1, 1, "C")

    # Sección 4: Despachos Externos a Galpones Nacionales
    if desp_ext:
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, _pdf_safe(f"DESPACHOS EXTERNOS A GALPONES NACIONALES ({len(desp_ext)} registros)"), ln=True)
        pdf.set_font("Helvetica", "", 8)
        for d in desp_ext:
            line = f"{d['codigo']} | {fmt_fecha_es(d['fecha_despacho'])} | {d['nombre_galpon']} ({d['estado_region'] or '-'}) | {fmt_num_es(d['total_bolsas'], 0)} bolsas | Cond.: {d['nombre_conductor'] or '-'} ({d['placa_vehiculo'] or '-'})"
            pdf.cell(0, 5.5, _pdf_safe(line), ln=True)

    pdf.output(path)
