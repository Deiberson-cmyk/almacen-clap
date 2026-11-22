-- ============================================================================
-- ALMACÉN DE ALIMENTOS CLAP - EJÉRCITO BOLIVARIANO (Fuerte Tiuna, Caracas)
-- schema.sql  |  MySQL 5.7+ / 8.x / MariaDB 10.3+  |  100% LOCAL, SIN INTERNET
-- Sin número de guía ni facturación fiscal: cada movimiento recibe un código
-- correlativo interno = prefijo + id autonumérico (ENT-000001, DES-000001, COL-000001).
-- Ejecutar:  mysql -u root -p --default-character-set=utf8mb4 < schema.sql
-- (ARCHIVO GENERADO por tools/generar_sql.py a partir de tools/schema_base.sql)
-- ============================================================================
CREATE DATABASE IF NOT EXISTS almacen_clap_ejercito CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE almacen_clap_ejercito;
SET NAMES utf8mb4;

-- ---------------------------------------------------------------------------
-- 1. USUARIOS (máximo 4: 1 MASTER + 3 OPERADOR). Sin grados militares.
-- ---------------------------------------------------------------------------
CREATE TABLE usuarios (
  id_usuario        INT AUTO_INCREMENT PRIMARY KEY,
  usuario           VARCHAR(30)  NOT NULL UNIQUE,
  nombre_completo   VARCHAR(100) NOT NULL,
  rol               ENUM('MASTER','OPERADOR') NOT NULL,
  password_hash     VARCHAR(255) NOT NULL,
  pin_seguridad     VARCHAR(20)  NULL,
  debe_cambiar_clave TINYINT(1)  NOT NULL DEFAULT 0,
  activo            TINYINT(1)   NOT NULL DEFAULT 1,
  fecha_creacion    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ultimo_acceso     DATETIME     NULL
) ENGINE=InnoDB;

DELIMITER $$
CREATE TRIGGER trg_usuarios_max4 BEFORE INSERT ON usuarios FOR EACH ROW
BEGIN
  IF (SELECT COUNT(*) FROM usuarios) >= 4 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Límite de usuarios alcanzado';
  END IF;
  IF NEW.rol = 'MASTER' AND (SELECT COUNT(*) FROM usuarios WHERE rol='MASTER') >= 1 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Solo puede existir un usuario MASTER';
  END IF;
END$$
DELIMITER ;

-- Único usuario semilla. Clave inicial Alfa01 (PBKDF2-SHA256).
INSERT INTO usuarios (usuario, nombre_completo, rol, password_hash, debe_cambiar_clave)
VALUES ('master', 'Usuario Master', 'MASTER', 'pbkdf2_sha256$200000$JXLffQprqdIVdvSRljcZ6w==$J6Tor4nyPl7KqvMU6lxpjYgadQnfLEl8QLRVOzFgLR0=', 0);

-- ---------------------------------------------------------------------------
-- 2. CONFIGURACIÓN GENERAL DEL SISTEMA (ej. última ruta de respaldo)
-- ---------------------------------------------------------------------------
CREATE TABLE configuracion_sistema (
  clave          VARCHAR(60) PRIMARY KEY,
  valor          TEXT NULL,
  actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------------
-- 3. PERÍODOS (cierre manual). Un solo período ABIERTO a la vez.
--    Etiqueta estándar: #[numero_en_anio] - [nombre_periodo] - [dd/mm/yyyy]
-- ---------------------------------------------------------------------------
CREATE TABLE periodos_cierre (
  id_periodo           INT AUTO_INCREMENT PRIMARY KEY,
  numero_en_anio       INT NOT NULL DEFAULT 1,
  anio_calendario      INT NOT NULL DEFAULT 2026,
  mes_numero           INT NOT NULL DEFAULT 12,
  nombre_periodo       VARCHAR(60) NOT NULL,
  fecha_inicio         DATE NOT NULL,
  fecha_cierre         DATETIME NULL,
  estado               ENUM('ABIERTO','CERRADO') NOT NULL DEFAULT 'ABIERTO',
  precio_unitario_bolsa DECIMAL(12,2) NOT NULL,
  stock_inicial_bolsas INT NOT NULL DEFAULT 0,
  stock_final_bolsas   INT NULL,
  total_entradas       INT NULL,
  total_desp_internos  INT NULL,
  total_desp_externos  INT NULL,
  total_colaboraciones INT NULL,
  monto_esperado_bs    DECIMAL(16,2) NULL,
  monto_cobrado_bs     DECIMAL(16,2) NULL,
  total_deudas_bs      DECIMAL(16,2) NULL,
  total_saldos_favor_bs DECIMAL(16,2) NULL,
  id_usuario_cierra    INT NULL,
  reabierto_por        INT NULL,
  motivo_reapertura    VARCHAR(255) NULL,
  FOREIGN KEY (id_usuario_cierra) REFERENCES usuarios(id_usuario),
  FOREIGN KEY (reabierto_por) REFERENCES usuarios(id_usuario)
) ENGINE=InnoDB;

INSERT INTO periodos_cierre (numero_en_anio, anio_calendario, mes_numero, nombre_periodo, fecha_inicio, precio_unitario_bolsa, stock_inicial_bolsas)
VALUES (1, 2026, 12, 'Diciembre', '2026-12-01', 0.00, 0);

-- ---------------------------------------------------------------------------
-- 4. HISTORIAL DE ACCESOS y AUDITORÍA (filtrables por período)
-- ---------------------------------------------------------------------------
CREATE TABLE historial_accesos (
  id_acceso   BIGINT AUTO_INCREMENT PRIMARY KEY,
  id_usuario  INT NULL,
  usuario_intento VARCHAR(30) NOT NULL,
  id_periodo  INT NOT NULL,
  evento      ENUM('LOGIN_OK','LOGIN_FALLIDO','LOGOUT') NOT NULL,
  fecha_hora  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario),
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  INDEX idx_acc_periodo (id_periodo, fecha_hora)
) ENGINE=InnoDB;

CREATE TABLE auditoria (
  id_auditoria BIGINT AUTO_INCREMENT PRIMARY KEY,
  id_periodo   INT NOT NULL,
  id_usuario   INT NOT NULL,
  fecha_hora   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  tabla_afectada VARCHAR(50) NOT NULL,
  id_registro  BIGINT NOT NULL,
  accion       ENUM('CREAR','EDITAR','ANULAR','REABRIR','CIERRE','AJUSTE','CONFIG') NOT NULL,
  motivo       VARCHAR(255) NULL,
  datos_anteriores JSON NULL,
  datos_nuevos     JSON NULL,
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario),
  INDEX idx_aud_periodo (id_periodo, fecha_hora),
  INDEX idx_aud_registro (tabla_afectada, id_registro)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------------
-- 5. CATÁLOGOS EDITABLES POR MASTER (Código corto separado del Nombre)
-- ---------------------------------------------------------------------------
CREATE TABLE unidades_militares (
  id_unidad   INT AUTO_INCREMENT PRIMARY KEY,
  codigo_unidad VARCHAR(10) NULL,
  nombre_unidad VARCHAR(150) NOT NULL,
  id_unidad_padre INT NULL,
  es_agrupador TINYINT(1) NOT NULL DEFAULT 0,
  codigos_agrupados_texto VARCHAR(255) NULL,
  saldo_cierre_no_aplicado_bs DECIMAL(14,2) NOT NULL DEFAULT 0,
  saldo_acumulado_bs DECIMAL(14,2) NOT NULL DEFAULT 0,
  activo      TINYINT(1) NOT NULL DEFAULT 1,
  FOREIGN KEY (id_unidad_padre) REFERENCES unidades_militares(id_unidad),
  UNIQUE KEY uq_unidad_codigo_nombre (codigo_unidad, nombre_unidad),
  INDEX idx_unidad_codigo (codigo_unidad)
) ENGINE=InnoDB;

CREATE TABLE galpones_externos (
  id_galpon   INT AUTO_INCREMENT PRIMARY KEY,
  nombre_galpon VARCHAR(120) NOT NULL UNIQUE,
  estado_region VARCHAR(60) NULL,
  activo      TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------------
-- 6. MOVIMIENTOS DE INVENTARIO
-- ---------------------------------------------------------------------------
CREATE TABLE entradas_inventario (
  id_entrada INT AUTO_INCREMENT PRIMARY KEY,
  id_periodo INT NOT NULL,
  fecha_recepcion DATE NOT NULL,
  cantidad_bolsas INT NOT NULL CHECK (cantidad_bolsas > 0),
  observaciones VARCHAR(255) NULL,
  id_usuario_registra INT NOT NULL,
  fecha_registro DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  anulado TINYINT(1) NOT NULL DEFAULT 0,
  motivo_anulacion VARCHAR(255) NULL,
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_usuario_registra) REFERENCES usuarios(id_usuario)
) ENGINE=InnoDB;

CREATE TABLE colaboraciones_descarga (
  id_colaboracion INT AUTO_INCREMENT PRIMARY KEY,
  id_periodo INT NOT NULL,
  id_entrada INT NULL,
  fecha DATE NOT NULL,
  cantidad_bolsas INT NOT NULL CHECK (cantidad_bolsas > 0),
  receptor VARCHAR(120) NOT NULL DEFAULT 'Ayudante de descarga',
  observaciones VARCHAR(255) NULL,
  id_usuario_registra INT NOT NULL,
  fecha_registro DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  anulado TINYINT(1) NOT NULL DEFAULT 0,
  motivo_anulacion VARCHAR(255) NULL,
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_entrada) REFERENCES entradas_inventario(id_entrada),
  FOREIGN KEY (id_usuario_registra) REFERENCES usuarios(id_usuario)
) ENGINE=InnoDB;

CREATE TABLE despachos (
  id_despacho INT AUTO_INCREMENT PRIMARY KEY,
  id_periodo INT NOT NULL,
  tipo_salida ENUM('INTERNO_BATALLON','EXTERNO_GALPON') NOT NULL,
  fecha_despacho DATE NOT NULL,
  cantidad_familiar INT NOT NULL DEFAULT 0,
  cantidad_tango    INT NOT NULL DEFAULT 0,
  total_bolsas      INT NOT NULL,
  id_batallon_beneficiario INT NULL,
  id_batallon_retira       INT NULL,
  nombre_autorizado_retiro VARCHAR(100) NULL,
  cedula_autorizado_retiro VARCHAR(15) NULL,
  modalidad_oficio ENUM('CON_OFICIO','SIN_OFICIO') NULL,
  tiene_recibo     TINYINT(1) NULL,
  id_galpon_destino INT NULL,
  nombre_conductor  VARCHAR(100) NULL,
  cedula_conductor  VARCHAR(15) NULL,
  vehiculo_descripcion VARCHAR(100) NULL,
  placa_vehiculo    VARCHAR(15) NULL,
  estado_entrega ENUM('PENDIENTE_RETIRO','DESPACHADO') NOT NULL DEFAULT 'DESPACHADO',
  precio_unitario_aplicado DECIMAL(12,2) NULL,
  monto_base_bs            DECIMAL(14,2) NULL,
  saldo_anterior_aplicado_bs DECIMAL(14,2) NOT NULL DEFAULT 0,
  ajuste_manual_master_bs  DECIMAL(14,2) NOT NULL DEFAULT 0,
  monto_neto_exigible_bs   DECIMAL(14,2) NULL,
  monto_total_pagado_bs    DECIMAL(14,2) NOT NULL DEFAULT 0,
  diferencia_saldo_bs      DECIMAL(14,2) NOT NULL DEFAULT 0,
  saldo_arrastrado         TINYINT(1) NOT NULL DEFAULT 0,
  estado_financiero ENUM('NO_APLICA','PENDIENTE','PARCIAL','PAGADO','SALDO_A_FAVOR') NOT NULL DEFAULT 'NO_APLICA',
  observaciones VARCHAR(255) NULL,
  id_usuario_registra INT NOT NULL,
  fecha_registro DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  anulado TINYINT(1) NOT NULL DEFAULT 0,
  motivo_anulacion VARCHAR(255) NULL,
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_batallon_beneficiario) REFERENCES unidades_militares(id_unidad),
  FOREIGN KEY (id_batallon_retira) REFERENCES unidades_militares(id_unidad),
  FOREIGN KEY (id_galpon_destino) REFERENCES galpones_externos(id_galpon),
  FOREIGN KEY (id_usuario_registra) REFERENCES usuarios(id_usuario),
  CONSTRAINT chk_desp_total CHECK (total_bolsas > 0 AND total_bolsas >= cantidad_familiar + cantidad_tango),
  CONSTRAINT chk_desp_interno CHECK (tipo_salida <> 'INTERNO_BATALLON' OR
        (id_batallon_beneficiario IS NOT NULL AND total_bolsas = cantidad_familiar + cantidad_tango)),
  CONSTRAINT chk_desp_externo CHECK (tipo_salida <> 'EXTERNO_GALPON' OR id_galpon_destino IS NOT NULL),
  INDEX idx_desp_periodo (id_periodo, tipo_salida),
  INDEX idx_desp_benef (id_batallon_beneficiario)
) ENGINE=InnoDB;

CREATE TABLE pagos_batallon (
  id_pago INT AUTO_INCREMENT PRIMARY KEY,
  id_despacho INT NOT NULL,
  id_periodo INT NOT NULL,
  id_batallon_pagador INT NOT NULL,
  concepto_plan ENUM('PLAN_FAMILIAR','PLAN_TANGO','UNIFICADO_AMBOS','ABONO_COMPLEMENTARIO') NOT NULL,
  monto_pagado_bs DECIMAL(14,2) NOT NULL CHECK (monto_pagado_bs > 0),
  referencia_bancaria VARCHAR(30) NULL,
  fecha_pago DATE NULL,
  sin_fecha_comprobante TINYINT(1) NOT NULL DEFAULT 0,
  observaciones VARCHAR(255) NULL,
  id_usuario_registra INT NOT NULL,
  fecha_registro DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  anulado TINYINT(1) NOT NULL DEFAULT 0,
  motivo_anulacion VARCHAR(255) NULL,
  FOREIGN KEY (id_despacho) REFERENCES despachos(id_despacho),
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_batallon_pagador) REFERENCES unidades_militares(id_unidad),
  FOREIGN KEY (id_usuario_registra) REFERENCES usuarios(id_usuario),
  INDEX idx_pago_despacho (id_despacho)
) ENGINE=InnoDB;

CREATE TABLE historial_saldos_batallon (
  id_hist INT AUTO_INCREMENT PRIMARY KEY,
  id_unidad INT NOT NULL,
  id_periodo INT NOT NULL,
  tipo ENUM('ARRASTRE_CIERRE','AJUSTE_MASTER','APLICACION') NOT NULL,
  monto_bs DECIMAL(14,2) NOT NULL,
  saldo_resultante_bs DECIMAL(14,2) NOT NULL,
  motivo VARCHAR(255) NULL,
  id_usuario INT NOT NULL,
  fecha_hora DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (id_unidad) REFERENCES unidades_militares(id_unidad),
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario)
) ENGINE=InnoDB;

CREATE TABLE respaldos_pendrive (
  id_respaldo INT AUTO_INCREMENT PRIMARY KEY,
  id_periodo INT NOT NULL,
  tipo ENUM('CIERRE','RAPIDO') NOT NULL,
  ruta_destino VARCHAR(255) NOT NULL,
  archivos TEXT NULL,
  id_usuario INT NOT NULL,
  fecha_hora DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario)
) ENGINE=InnoDB;

-- ---------------------------------------------------------------------------
-- 7. RECÁLCULO AUTOMÁTICO DE PAGOS Y PROTECCIÓN CONTRA BORRADO
-- ---------------------------------------------------------------------------
DELIMITER $$
CREATE PROCEDURE sp_recalcular_despacho(IN p_id INT)
BEGIN
  DECLARE v_uid INT;
  DECLARE v_per INT;

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

  SELECT id_batallon_beneficiario, id_periodo INTO v_uid, v_per
  FROM despachos WHERE id_despacho = p_id;

  IF v_uid IS NOT NULL THEN
    UPDATE unidades_militares u
    SET u.saldo_acumulado_bs = u.saldo_cierre_no_aplicado_bs + IFNULL((
      SELECT SUM(d.diferencia_saldo_bs)
      FROM despachos d
      WHERE d.id_batallon_beneficiario = v_uid
        AND d.id_periodo = (SELECT id_periodo FROM periodos_cierre WHERE estado = 'ABIERTO' ORDER BY id_periodo DESC LIMIT 1)
        AND d.tipo_salida = 'INTERNO_BATALLON'
        AND d.anulado = 0
        AND d.saldo_arrastrado = 0
    ), 0)
    WHERE u.id_unidad = v_uid;
  END IF;
END$$

CREATE TRIGGER trg_pagos_ai AFTER INSERT ON pagos_batallon FOR EACH ROW
BEGIN CALL sp_recalcular_despacho(NEW.id_despacho); END$$
CREATE TRIGGER trg_pagos_au AFTER UPDATE ON pagos_batallon FOR EACH ROW
BEGIN CALL sp_recalcular_despacho(NEW.id_despacho); END$$

CREATE TRIGGER trg_desp_bd BEFORE DELETE ON despachos FOR EACH ROW
BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Prohibido eliminar: use anulación (MASTER)'; END$$
CREATE TRIGGER trg_pagos_bd BEFORE DELETE ON pagos_batallon FOR EACH ROW
BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Prohibido eliminar: use anulación (MASTER)'; END$$
CREATE TRIGGER trg_ent_bd BEFORE DELETE ON entradas_inventario FOR EACH ROW
BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Prohibido eliminar: use anulación (MASTER)'; END$$
CREATE TRIGGER trg_col_bd BEFORE DELETE ON colaboraciones_descarga FOR EACH ROW
BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Prohibido eliminar: use anulación (MASTER)'; END$$
DELIMITER ;

-- ---------------------------------------------------------------------------
-- 8. VISTAS (base de la UI y de los reportes de cierre)
-- ---------------------------------------------------------------------------
CREATE VIEW v_inventario_periodo AS
SELECT p.id_periodo, p.nombre_periodo, p.estado,
  p.stock_inicial_bolsas AS stock_inicial_arrastre,
  IFNULL((SELECT SUM(cantidad_bolsas) FROM entradas_inventario e WHERE e.id_periodo=p.id_periodo AND e.anulado=0),0) AS entradas_periodo,
  IFNULL((SELECT SUM(cantidad_familiar) FROM despachos d WHERE d.id_periodo=p.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.estado_entrega='DESPACHADO' AND d.anulado=0),0) AS desp_internos_familiar,
  IFNULL((SELECT SUM(cantidad_tango)   FROM despachos d WHERE d.id_periodo=p.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.estado_entrega='DESPACHADO' AND d.anulado=0),0) AS desp_internos_tango,
  IFNULL((SELECT SUM(total_bolsas) FROM despachos d WHERE d.id_periodo=p.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.estado_entrega='DESPACHADO' AND d.anulado=0),0) AS desp_internos_total,
  IFNULL((SELECT SUM(total_bolsas) FROM despachos d WHERE d.id_periodo=p.id_periodo AND d.tipo_salida='EXTERNO_GALPON' AND d.estado_entrega='DESPACHADO' AND d.anulado=0),0) AS desp_externos_total,
  IFNULL((SELECT SUM(cantidad_bolsas) FROM colaboraciones_descarga c WHERE c.id_periodo=p.id_periodo AND c.anulado=0),0) AS colaboraciones_descarga,
  IFNULL((SELECT SUM(total_bolsas) FROM despachos d WHERE d.id_periodo=p.id_periodo AND d.estado_entrega='PENDIENTE_RETIRO' AND d.anulado=0),0) AS reservado_pendiente_retiro
FROM periodos_cierre p;

CREATE VIEW v_stock_disponible AS
SELECT v.*, (v.stock_inicial_arrastre + v.entradas_periodo - v.desp_internos_total
             - v.desp_externos_total - v.colaboraciones_descarga) AS stock_disponible
FROM v_inventario_periodo v;

CREATE VIEW v_conciliacion_batallones AS
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
WHERE d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0;

CREATE VIEW v_rendicion_cuentas AS
SELECT s.id_periodo, s.nombre_periodo, s.stock_inicial_arrastre, s.entradas_periodo,
  s.desp_internos_total, s.desp_externos_total, s.colaboraciones_descarga, s.stock_disponible AS stock_en_almacen,
  (IFNULL((SELECT SUM(monto_total_pagado_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0),0)
   + IFNULL((SELECT SUM(-diferencia_saldo_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0 AND d.saldo_arrastrado=0 AND d.diferencia_saldo_bs<0),0)
   - IFNULL((SELECT SUM(diferencia_saldo_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0 AND d.saldo_arrastrado=0 AND d.diferencia_saldo_bs>0),0)
  ) AS monto_esperado_bs,
  IFNULL((SELECT SUM(monto_total_pagado_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0),0) AS monto_cobrado_bs,
  IFNULL((SELECT SUM(-diferencia_saldo_bs) FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0 AND d.saldo_arrastrado=0 AND d.diferencia_saldo_bs<0),0) AS total_deudas_bs,
  IFNULL((SELECT SUM(diferencia_saldo_bs)  FROM despachos d WHERE d.id_periodo=s.id_periodo AND d.tipo_salida='INTERNO_BATALLON' AND d.anulado=0 AND d.saldo_arrastrado=0 AND d.diferencia_saldo_bs>0),0) AS total_saldos_favor_bs
FROM v_stock_disponible s;

-- ----------------------------------------------------------------------------
-- CATÁLOGO INICIAL DE UNIDADES (94, extraídas de Dis_unidad.xlsx; código separado)
-- ----------------------------------------------------------------------------
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('3001', 'COMPAÑÍA DEL CUARTEL GENERAL', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('31', 'BRIGADA DE INFANTERIA MECANIZADA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('3101', 'COMPAÑÍA DE COMANDO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('3103', 'COMPAÑÍA DE ABASTECIMIENTO Y TRANSPORTE', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('3104', 'COMPAÑÍA DE COMUNICACIONES', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('3107', 'COMPAÑÍA DE INGENIEROS', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('3108', 'COMPAÑÍA DE MANTENIMIENTO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('3109', 'COMPAÑÍA DE FRANCOTIRADORES', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('311', 'BTN INF MEC LIBERTADOR SIMON BOLIVAR', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('3110', 'COMPAÑÍA DE SANIDAD', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('312', 'G.C.M G/B JUAN PABLO AYALA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('313', 'BATALLON DE CARIBES', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('34', 'BRIGADAS DE COMUNICACIONES ESTRATEGICAS', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('3401', 'CIA COMANDO Y SERV TCNEL JUAN NAVARRO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('341', 'BATALLON DE COMUNICACIONES ESTRATEGICAS', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('35', 'POLICIA MILITAR', 1, 'Abarca: 35, 3501, 3509, 351, 352, 353');
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('355', 'BATALLON DE P.M', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, '3ER N.F.T.P', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('6001', 'COMPAÑÍA DE COMANDO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('608', 'BATALLON DE APOYO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('61', 'BRIG ACOND. ING CODAZZI', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('6101', 'COMPAÑÍA DE COMANDO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('6108', 'COMPAÑÍA DE MANTENIMIENTO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('611', 'BTN ING CONS Y MANTTO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('612', 'BTN ACOND ING (TSM)', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('614', 'BTN ACOND ING T.S.M', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('64', 'BRIGADA DE ING FERROVIARIOS', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('6401', 'COMPAÑÍA DE COMANDO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('643', 'BATALLON DE ING FERROVIARIO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, '6TO CUERPO DE INGENIEROS', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('7001', 'CIA MANDO Y SERVICIO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'ESCUADRON MANDO Y SERVICIO CMDO AEREO DEL EJERCITO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('713', 'BTN ESP.RECONOCIMIENTO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('714', 'CTRO MNTNTO. AVEJB', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('715', 'CTRO ABAST. AVIACION', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('82', 'BRIGADA LOGISTICA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('8201', 'CIA DE CMDO Y SERV', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('8202', 'CIA MANTO COMS', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('8203', 'CIA SANIDAD', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('824', 'BTN APOY LOG', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES ('991', 'BATALLON DE FUERZAS ESPERCIALES', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'BRIGADA DE COMNS AUDIOVISUALES 4 DE FEBRERO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'UNIDAD MOVIL TV CACIQUE TIUNA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'BTN CTEL OLEARY', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'CAJA DE AHORRO DEL EJERCITO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'CENTRO DE MANTENIMIENTO FUERTE TIUNA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'CENTRO RECEP Y CTRL DE SUMINISTROS (CERECOSE)', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'COMPAÑÍA DE MANDO Y SERVICIO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'COMANDO DE AVIACION DEL EJERCITO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'COMANDANCIA GENERAL', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'CTRO. ADIEST. FORM. REENT . CACIQUE TIUNA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'DIRECCION CONST Y MANTTO DICOMAEJB SEDE', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'DIRECCION DE LOGISTICA CIA APOYO Y SERV CRAL MONTILLA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'ESC. ECOLOGICO BOLIVARIANA EJ SIMON RODRIGUEZ', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'ESCUELA EQUITACION NEGRO PRIMERO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'LIC.,MIL.GRAL. DEL PUEBLO SOBERANO EZEQUIEL ZAMORA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'LICEO MILITAR GRAN MARISCAL DE AYACUCHO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'PRESCOLAR NEGRA MATEA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'RESIDENCIA DE OFICIALES DEL EJERCITO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'SERVICIO DE ALIMENTACION', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'SERVICIO DE SANIDAD DEL EJERCITO BOLIVARIANO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'SERVICIO DE INTENDENCIA DEL EJERCITO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'CEMANVETAC', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'PROYECTO SCORPION', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'COMPLEJO ANDRES ELOIS BLANCO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'ACADEMIA MILITAR DE VENEZUELA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'ACADEMIA MILITAR OFICIALES DE TROPA HUGO CHAVEZ', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'AMBULATORIO MILITAR DR FELIPE TAMARIZ', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'BATALLON APOYO DEL SERVICIO CEOFANB', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'BATALLON DE APOYO DE LA REDI CAPITAL', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'BATALLON DE APOYO LOGISTICO DEL COLOP-CEOFANB', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'CIA APOYO Y SERVICIO TCNEL HRCH', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'CIRCULO MILITAR', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'DIRECCION DE GEOGRAFICA Y CARTOGRAFIA DE LA FANB', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'UNIDAD ESPECIAL DE EXPLORACION GEOGRAFICA MILITAR', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'HOSPITAL MILITAR DR CARLOS ARVELO', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'INS.ALTOS ESTU.SEGURI.NACION GRAN MARISCAL SUCRE', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'SECRETARIO PERMANENTE DE REG. Y ALIST.DEFE.INT. SEPRADIN', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'UNIDA DE APOYO DE EXPLORACION RADIOELECTRONICA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'UNIVERSIDAD NAC EXPERIMENTAL FANB UNEFA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'FEDERACION DEPORTIVA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'CIRCUITO JUDICIAL PENAL MILITAR', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'IESEOFANB', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'SERVICIO DE TRANSPORTE', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'ZODI MIRANDA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'PTO ABASTECIMIENTO CARACAS', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'COMANDO LOGISTICO OPERACIONAL CEOFANB', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'ESCUELA DE FORMACION DE OFICIALES DE TROPA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, es_agrupador, codigos_agrupados_texto) VALUES (NULL, 'ESCUELA SUPERIOR DE GUERRA CONJUNTA', 0, NULL);
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, id_unidad_padre) VALUES ('3501', 'UNIDAD POLICIA MILITAR', (SELECT p.id_unidad FROM (SELECT id_unidad FROM unidades_militares WHERE codigo_unidad = '35' LIMIT 1) p));
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, id_unidad_padre) VALUES ('3509', 'UNIDAD POLICIA MILITAR', (SELECT p.id_unidad FROM (SELECT id_unidad FROM unidades_militares WHERE codigo_unidad = '35' LIMIT 1) p));
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, id_unidad_padre) VALUES ('351', 'UNIDAD POLICIA MILITAR', (SELECT p.id_unidad FROM (SELECT id_unidad FROM unidades_militares WHERE codigo_unidad = '35' LIMIT 1) p));
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, id_unidad_padre) VALUES ('352', 'UNIDAD POLICIA MILITAR', (SELECT p.id_unidad FROM (SELECT id_unidad FROM unidades_militares WHERE codigo_unidad = '35' LIMIT 1) p));
INSERT INTO unidades_militares (codigo_unidad, nombre_unidad, id_unidad_padre) VALUES ('353', 'UNIDAD POLICIA MILITAR', (SELECT p.id_unidad FROM (SELECT id_unidad FROM unidades_militares WHERE codigo_unidad = '35' LIMIT 1) p));

