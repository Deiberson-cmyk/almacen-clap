-- test_schema.sql : prueba de la base. Usa datos ficticios y al final hace ROLLBACK.
-- Ejecutar: mysql -u root -p --default-character-set=utf8mb4 -t < test_schema.sql
USE almacen_clap_ejercito;
SELECT '1) Tablas (esperado 12)' AS prueba, COUNT(*) AS resultado FROM information_schema.tables
  WHERE table_schema='almacen_clap_ejercito' AND table_type='BASE TABLE';
SELECT '2) Unidades (esperado 94)' AS prueba, COUNT(*) AS resultado FROM unidades_militares;
SELECT '3) Hijas de Policia Militar (esperado 5)' AS prueba, COUNT(*) AS resultado FROM unidades_militares
  WHERE id_unidad_padre = (SELECT id_unidad FROM unidades_militares WHERE nombre_unidad='35 POLICIA MILITAR');
SELECT '4) Usuarios (esperado 1 master)' AS prueba, usuario, rol FROM usuarios;

START TRANSACTION;
-- Datos ficticios del período 1: stock 100 + entrada 5000, precio 900
UPDATE periodos_cierre SET stock_inicial_bolsas=100, precio_unitario_bolsa=900 WHERE id_periodo=1;
INSERT INTO entradas_inventario (id_periodo, fecha_recepcion, cantidad_bolsas, id_usuario_registra) VALUES (1,'2026-12-01',5000,1);
INSERT INTO galpones_externos (nombre_galpon, estado_region) VALUES ('Galpon de prueba','Zulia');

-- Despacho interno: 40 bolsas (37 Tango + 3 Familiar) = 36000 Bs
INSERT INTO despachos (id_periodo,tipo_salida,fecha_despacho,id_batallon_beneficiario,id_batallon_retira,cantidad_familiar,cantidad_tango,total_bolsas,modalidad_oficio,estado_entrega,precio_unitario_aplicado,monto_base_bs,monto_neto_exigible_bs,id_usuario_registra)
VALUES (1,'INTERNO_BATALLON','2026-12-02',(SELECT id_unidad FROM unidades_militares WHERE nombre_unidad LIKE '3001 %'),
        (SELECT id_unidad FROM unidades_militares WHERE nombre_unidad LIKE '3101 %'),3,37,40,'CON_OFICIO','DESPACHADO',900,36000,36000,1);
SET @d = LAST_INSERT_ID();
-- Pago incompleto, luego complemento redondeado hacia arriba
INSERT INTO pagos_batallon (id_despacho,id_periodo,id_batallon_pagador,concepto_plan,monto_pagado_bs,referencia_bancaria,fecha_pago,id_usuario_registra)
VALUES (@d,1,(SELECT id_batallon_beneficiario FROM despachos WHERE id_despacho=@d),'PLAN_TANGO',30000,'1234','2026-12-02',1);
SELECT '5) Tras pago de 30000 (esperado PARCIAL, diferencia -6000)' AS prueba, estado_financiero, diferencia_saldo_bs FROM despachos WHERE id_despacho=@d;
INSERT INTO pagos_batallon (id_despacho,id_periodo,id_batallon_pagador,concepto_plan,monto_pagado_bs,referencia_bancaria,fecha_pago,id_usuario_registra)
VALUES (@d,1,(SELECT id_batallon_beneficiario FROM despachos WHERE id_despacho=@d),'ABONO_COMPLEMENTARIO',6020,'5678','2026-12-03',1);
SELECT '6) Tras 2do pago de 6020 (esperado SALDO_A_FAVOR, diferencia 20, pagado 36020)' AS prueba, estado_financiero, diferencia_saldo_bs, monto_total_pagado_bs FROM despachos WHERE id_despacho=@d;

-- Despacho externo y colaboración
INSERT INTO despachos (id_periodo,tipo_salida,fecha_despacho,total_bolsas,id_galpon_destino,nombre_conductor,cedula_conductor,placa_vehiculo,estado_entrega,id_usuario_registra)
VALUES (1,'EXTERNO_GALPON','2026-12-03',1000,(SELECT id_galpon FROM galpones_externos LIMIT 1),'Conductor Prueba','V-1','AB123CD','DESPACHADO',1);
INSERT INTO colaboraciones_descarga (id_periodo,fecha,cantidad_bolsas,receptor,id_usuario_registra) VALUES (1,'2026-12-01',2,'Ayudante',1);
SELECT '7) Stock (esperado: inicial 100, entradas 5000, internos 40, externos 1000, colab 2, disponible 4058)' AS prueba,
       stock_inicial_arrastre, entradas_periodo, desp_internos_total, desp_externos_total, colaboraciones_descarga, stock_disponible
FROM v_stock_disponible WHERE id_periodo=1;
SELECT '8) Rendicion' AS prueba, monto_esperado_bs, monto_cobrado_bs, total_saldos_favor_bs FROM v_rendicion_cuentas WHERE id_periodo=1;
SELECT '9) Conciliacion' AS prueba, codigo_movimiento, batallon_beneficiario, batallon_que_retiro, cantidad_familiar, cantidad_tango, pagos_detalle FROM v_conciliacion_batallones;
ROLLBACK;

-- Pruebas de rechazo: cada una DEBE dar ERROR (si alguna se ejecuta sin error, hay un fallo)
-- (el cliente se detiene en el primer error; ejecútalas una por una, ver guía)
-- INSERT INTO usuarios (usuario,nombre_completo,rol,password_hash) VALUES ('op1','Op 1','OPERADOR','x'),('op2','Op 2','OPERADOR','x'),('op3','Op 3','OPERADOR','x'),('op4','Op 4','OPERADOR','x');
-- INSERT INTO usuarios (usuario,nombre_completo,rol,password_hash) VALUES ('m2','Otro master','MASTER','x');
-- DELETE FROM despachos;
