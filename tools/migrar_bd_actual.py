# -*- coding: utf-8 -*-
"""Migra en vivo la BD MariaDB almacen_clap_ejercito al nuevo esquema sin perder datos existentes."""
import re
import pymysql

conn = pymysql.connect(
    host="127.0.0.1",
    port=3306,
    user="root",
    password="64966400",
    database="almacen_clap_ejercito",
    charset="utf8mb4",
    autocommit=False,
)

with conn.cursor() as cur:
    cur.execute(
        """CREATE TABLE IF NOT EXISTS configuracion_sistema (
          clave VARCHAR(60) PRIMARY KEY,
          valor TEXT NULL,
          actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB"""
    )

    cur.execute("SHOW COLUMNS FROM periodos_cierre LIKE 'numero_en_anio'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE periodos_cierre "
            "ADD COLUMN numero_en_anio INT NOT NULL DEFAULT 1 AFTER id_periodo, "
            "ADD COLUMN anio_calendario INT NOT NULL DEFAULT 2026 AFTER numero_en_anio, "
            "ADD COLUMN mes_numero INT NOT NULL DEFAULT 12 AFTER anio_calendario"
        )
        cur.execute("UPDATE periodos_cierre SET numero_en_anio=1, anio_calendario=2026, mes_numero=12, nombre_periodo='Diciembre' WHERE id_periodo=1")
        cur.execute("UPDATE periodos_cierre SET numero_en_anio=1, anio_calendario=2027, mes_numero=1, nombre_periodo='Enero' WHERE id_periodo=2")

    cur.execute("SHOW COLUMNS FROM usuarios LIKE 'pin_seguridad'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE usuarios ADD COLUMN pin_seguridad VARCHAR(20) NULL AFTER password_hash")

    cur.execute("SHOW COLUMNS FROM historial_accesos LIKE 'equipo'")
    if cur.fetchone():
        cur.execute("ALTER TABLE historial_accesos DROP COLUMN equipo")

    cur.execute("SHOW INDEX FROM unidades_militares WHERE Key_name='nombre_unidad'")
    if cur.fetchone():
        cur.execute("ALTER TABLE unidades_militares DROP INDEX nombre_unidad")

    cur.execute("SHOW COLUMNS FROM unidades_militares LIKE 'saldo_cierre_no_aplicado_bs'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE unidades_militares ADD COLUMN saldo_cierre_no_aplicado_bs DECIMAL(14,2) NOT NULL DEFAULT 0 AFTER codigos_agrupados_texto")

    cur.execute("SELECT id_unidad, codigo_unidad, nombre_unidad FROM unidades_militares")
    for uid, cod, nom in cur.fetchall():
        if cod and nom.startswith("UNIDAD " + cod):
            new_nom = "UNIDAD POLICIA MILITAR"
        elif cod and nom.startswith(cod):
            new_nom = re.sub(r"^\s*", "", nom[len(cod):]).strip()
        else:
            new_nom = nom.strip()
        if new_nom != nom:
            cur.execute("UPDATE unidades_militares SET nombre_unidad=%s WHERE id_unidad=%s", (new_nom, uid))

    cur.execute("SHOW INDEX FROM unidades_militares WHERE Key_name='uq_unidad_codigo_nombre'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE unidades_militares ADD UNIQUE KEY uq_unidad_codigo_nombre (codigo_unidad, nombre_unidad)")

    cur.execute("SHOW COLUMNS FROM despachos LIKE 'saldo_arrastrado'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE despachos ADD COLUMN saldo_arrastrado TINYINT(1) NOT NULL DEFAULT 0 AFTER diferencia_saldo_bs")
        cur.execute("UPDATE despachos d JOIN periodos_cierre p ON p.id_periodo=d.id_periodo SET d.saldo_arrastrado=1 WHERE p.estado='CERRADO'")
        cur.execute("UPDATE unidades_militares SET saldo_cierre_no_aplicado_bs = saldo_acumulado_bs")

    cur.execute("ALTER TABLE colaboraciones_descarga MODIFY COLUMN receptor VARCHAR(120) NOT NULL DEFAULT 'Ayudante de descarga'")

    cur.execute("DROP PROCEDURE IF EXISTS sp_recalcular_despacho")
    cur.execute(
        """CREATE PROCEDURE sp_recalcular_despacho(IN p_id INT)
        BEGIN
          DECLARE v_uid INT;
          UPDATE despachos d
          SET d.monto_total_pagado_bs = IFNULL((SELECT SUM(p.monto_pagado_bs) FROM pagos_batallon p
                                                WHERE p.id_despacho = d.id_despacho AND p.anulado = 0), 0),
              d.monto_neto_exigible_bs = d.monto_base_bs - d.saldo_anterior_aplicado_bs + d.ajuste_manual_master_bs
          WHERE d.id_despacho = p_id AND d.tipo_salida = 'INTERNO_BATALLON';

          UPDATE despachos d
          SET d.diferencia_saldo_bs = d.monto_total_pagado_bs - d.monto_neto_exigible_bs,
              d.estado_financiero = CASE
                WHEN d.monto_total_pagado_bs = 0 THEN 'PENDIENTE'
                WHEN d.monto_total_pagado_bs - d.monto_neto_exigible_bs < -0.005 THEN 'PARCIAL'
                WHEN d.monto_total_pagado_bs - d.monto_neto_exigible_bs >  0.005 THEN 'SALDO_A_FAVOR'
                ELSE 'PAGADO' END
          WHERE d.id_despacho = p_id AND d.tipo_salida = 'INTERNO_BATALLON';

          SELECT id_batallon_beneficiario INTO v_uid FROM despachos WHERE id_despacho = p_id;
          IF v_uid IS NOT NULL THEN
            UPDATE unidades_militares u
            SET u.saldo_acumulado_bs = u.saldo_cierre_no_aplicado_bs + IFNULL((
              SELECT SUM(d.diferencia_saldo_bs) FROM despachos d
              WHERE d.id_batallon_beneficiario = v_uid
                AND d.id_periodo = (SELECT id_periodo FROM periodos_cierre WHERE estado = 'ABIERTO' ORDER BY id_periodo DESC LIMIT 1)
                AND d.tipo_salida = 'INTERNO_BATALLON' AND d.anulado = 0 AND d.saldo_arrastrado = 0
            ), 0)
            WHERE u.id_unidad = v_uid;
          END IF;
        END"""
    )

    cur.execute("DROP VIEW IF EXISTS v_rendicion_cuentas")
    cur.execute("DROP VIEW IF EXISTS v_conciliacion_batallones")
    cur.execute(
        """CREATE VIEW v_conciliacion_batallones AS
        SELECT d.id_periodo, CONCAT('DES-', LPAD(d.id_despacho, 6, '0')) AS codigo_movimiento, d.fecha_despacho,
          CONCAT(IFNULL(CONCAT(ub.codigo_unidad, ' '), ''), ub.nombre_unidad) AS batallon_beneficiario,
          CONCAT(IFNULL(CONCAT(ur.codigo_unidad, ' '), ''), ur.nombre_unidad) AS batallon_que_retiro,
          d.cantidad_familiar, d.cantidad_tango, d.total_bolsas, d.modalidad_oficio, d.estado_entrega,
          d.monto_base_bs, d.saldo_anterior_aplicado_bs, d.ajuste_manual_master_bs, d.monto_neto_exigible_bs,
          d.monto_total_pagado_bs, d.diferencia_saldo_bs, d.saldo_arrastrado, d.estado_financiero,
          (SELECT GROUP_CONCAT(CONCAT(IFNULL(p.referencia_bancaria,'s/ref'),' (',p.monto_pagado_bs,')') SEPARATOR ' | ')
             FROM pagos_batallon p WHERE p.id_despacho=d.id_despacho AND p.anulado=0) AS pagos_detalle,
          d.observaciones
        FROM despachos d
        JOIN unidades_militares ub ON ub.id_unidad = d.id_batallon_beneficiario
        LEFT JOIN unidades_militares ur ON ur.id_unidad = d.id_batallon_retira
        WHERE d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0"""
    )

    cur.execute(
        """CREATE VIEW v_rendicion_cuentas AS
        SELECT s.id_periodo, s.nombre_periodo, s.stock_inicial_arrastre, s.entradas_periodo,
          s.desp_internos_total, s.desp_externos_total, s.colaboraciones_descarga, s.stock_disponible AS stock_en_almacen,
          (IFNULL((SELECT SUM(monto_total_pagado_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0),0)
           + IFNULL((SELECT SUM(-diferencia_saldo_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0 AND d.saldo_arrastrado=0 AND d.diferencia_saldo_bs<0),0)
           - IFNULL((SELECT SUM(diferencia_saldo_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0 AND d.saldo_arrastrado=0 AND d.diferencia_saldo_bs>0),0)
          ) AS monto_esperado_bs,
          IFNULL((SELECT SUM(monto_total_pagado_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0),0) AS monto_cobrado_bs,
          IFNULL((SELECT SUM(-diferencia_saldo_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0 AND d.saldo_arrastrado=0 AND d.diferencia_saldo_bs<0),0) AS total_deudas_bs,
          IFNULL((SELECT SUM(diferencia_saldo_bs)  FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0 AND d.saldo_arrastrado=0 AND d.diferencia_saldo_bs>0),0) AS total_saldos_favor_bs
        FROM v_stock_disponible s"""
    )

    # Sincronizar saldo_acumulado_bs para todas las unidades
    cur.execute(
        """UPDATE unidades_militares u
           SET u.saldo_acumulado_bs = u.saldo_cierre_no_aplicado_bs + IFNULL((
             SELECT SUM(d.diferencia_saldo_bs) FROM despachos d
             WHERE d.id_batallon_beneficiario = u.id_unidad
               AND d.id_periodo = (SELECT id_periodo FROM periodos_cierre WHERE estado = 'ABIERTO' ORDER BY id_periodo DESC LIMIT 1)
               AND d.tipo_salida = 'INTERNO_BATALLON' AND d.anulado = 0 AND d.saldo_arrastrado = 0
           ), 0)"""
    )
    conn.commit()

print("Migracion completada con exito!")
conn.close()
