-- ============================================================================
-- ALMACÉN DE ALIMENTOS CLAP - EJÉRCITO BOLIVARIANO (Fuerte Tiuna, Caracas)
-- Archivo base: tools/schema_base.sql
-- Motor compatible: MySQL 5.7+ / 8.x / MariaDB 10.3+ (Probado en MariaDB 10.4)
-- Operación: 100% Local Offline (sin red ni dependencias externas).
--
-- Convención de códigos correlativos internos (sin facturación fiscal):
--   - Entradas de lotes:      ENT-000001
--   - Despachos de bolsas:    DES-000001
--   - Colaboraciones/descarga: COL-000001
--
-- Ejecución desde consola MariaDB/MySQL:
--   mysql -u root -p --default-character-set=utf8mb4 < schema.sql
-- (El archivo schema.sql se regenera ejecutando: python tools/generar_sql.py)
-- ============================================================================

CREATE DATABASE IF NOT EXISTS almacen_clap_ejercito
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE almacen_clap_ejercito;
SET NAMES utf8mb4;

-- ----------------------------------------------------------------------------
-- 1. USUARIOS DEL SISTEMA
--    Control de acceso por roles (máximo 4 cuentas: 1 MASTER + 3 OPERADOR).
--    Las contraseñas se almacenan cifradas con PBKDF2-SHA256 (200.000 iter.).
--    El código PIN de seguridad (máx. 9 dígitos) autoriza anulaciones y cambios
--    críticos realizados por el usuario MASTER.
-- ----------------------------------------------------------------------------
CREATE TABLE usuarios (
  id_usuario      INT AUTO_INCREMENT PRIMARY KEY,
  usuario         VARCHAR(30)  NOT NULL UNIQUE,       -- Nombre corto de acceso (en minúsculas)
  nombre_completo VARCHAR(100) NOT NULL,              -- Nombre y apellido del funcionario
  rol             ENUM('MASTER','OPERADOR') NOT NULL, -- Rol administrativo o operativo
  password_hash   VARCHAR(255) NOT NULL,              -- Hash PBKDF2-SHA256 con sal aleatoria
  pin_seguridad   VARCHAR(20)  NULL,                  -- Código de seguridad del MASTER (1-9 dígitos)
  activo          TINYINT(1)   NOT NULL DEFAULT 1,    -- 1 = Cuenta activa, 0 = Desactivada
  fecha_creacion  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  ultimo_acceso   DATETIME     NULL                   -- Fecha/hora del último LOGIN_OK
) ENGINE=InnoDB;

-- Trigger de integridad: impide crear más de 4 usuarios o más de 1 usuario MASTER.
DELIMITER $$
CREATE TRIGGER trg_usuarios_max4 BEFORE INSERT ON usuarios FOR EACH ROW
BEGIN
  IF (SELECT COUNT(*) FROM usuarios) >= 4 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Límite de usuarios alcanzado';
  END IF;
  IF NEW.rol = 'MASTER' AND (SELECT COUNT(*) FROM usuarios WHERE rol = 'MASTER') >= 1 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Solo puede existir un usuario MASTER';
  END IF;
END$$
DELIMITER ;

-- Usuario semilla inicial: cuenta MASTER con contraseña inicial Alfa01.
INSERT INTO usuarios (usuario, nombre_completo, rol, password_hash)
VALUES (
  'master',
  'Usuario Master',
  'MASTER',
  'pbkdf2_sha256$200000$JXLffQprqdIVdvSRljcZ6w==$J6Tor4nyPl7KqvMU6lxpjYgadQnfLEl8QLRVOzFgLR0='
);

-- ----------------------------------------------------------------------------
-- 2. CONFIGURACIÓN GENERAL DEL SISTEMA
--    Almacena parámetros persistentes clave-valor (ej. última ruta de respaldo).
-- ----------------------------------------------------------------------------
CREATE TABLE configuracion_sistema (
  clave          VARCHAR(60) PRIMARY KEY,
  valor          TEXT        NULL,
  actualizado_en DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- 3. PERÍODOS CONTABLES Y DE INVENTARIO
--    Cada período agrupa ingresos, salidas y pagos hasta su cierre manual.
--    Solo existe un período en estado 'ABIERTO' simultáneamente.
--    Etiqueta estándar en UI: #[numero_en_anio] - [nombre_periodo] - [dd/mm/yyyy]
-- ----------------------------------------------------------------------------
CREATE TABLE periodos_cierre (
  id_periodo            INT AUTO_INCREMENT PRIMARY KEY,
  numero_en_anio        INT           NOT NULL DEFAULT 1,     -- Correlativo mensual dentro del año
  anio_calendario       INT           NOT NULL DEFAULT 2026,
  mes_numero            INT           NOT NULL DEFAULT 12,
  nombre_periodo        VARCHAR(60)   NOT NULL,               -- Nombre del mes / período
  fecha_inicio          DATE          NOT NULL,
  fecha_cierre          DATETIME      NULL,
  estado                ENUM('ABIERTO','CERRADO') NOT NULL DEFAULT 'ABIERTO',
  precio_unitario_bolsa DECIMAL(12,2) NOT NULL,               -- Precio vigente de 1 bolsa (Bs.)
  stock_inicial_bolsas  INT           NOT NULL DEFAULT 0,     -- Arrastre del cierre anterior
  -- Campos instantáneos congelados al ejecutar el cierre manual:
  stock_final_bolsas    INT           NULL,
  total_entradas        INT           NULL,
  total_desp_internos   INT           NULL,
  total_desp_externos   INT           NULL,
  total_colaboraciones  INT           NULL,
  monto_esperado_bs     DECIMAL(16,2) NULL,
  monto_cobrado_bs      DECIMAL(16,2) NULL,
  total_deudas_bs       DECIMAL(16,2) NULL,
  total_saldos_favor_bs DECIMAL(16,2) NULL,
  id_usuario_cierra     INT           NULL,
  FOREIGN KEY (id_usuario_cierra) REFERENCES usuarios(id_usuario),
  INDEX idx_periodo_estado (estado)
) ENGINE=InnoDB;

-- Período semilla inicial (#1 - Diciembre - 01/12/2026).
INSERT INTO periodos_cierre (numero_en_anio, anio_calendario, mes_numero, nombre_periodo, fecha_inicio, precio_unitario_bolsa, stock_inicial_bolsas)
VALUES (1, 2026, 12, 'Diciembre', '2026-12-01', 0.00, 0);

-- ----------------------------------------------------------------------------
-- 4. SEGURIDAD: HISTORIAL DE ACCESOS Y BITÁCORA DE AUDITORÍA
--    Permiten filtrar por período, fecha exacta, usuario y acción/evento.
--    Sus índices por fecha y acción hacen eficientes las búsquedas de auditoría.
-- ----------------------------------------------------------------------------
CREATE TABLE historial_accesos (
  id_acceso       BIGINT AUTO_INCREMENT PRIMARY KEY,
  id_usuario      INT         NULL,                           -- NULL si el usuario no existe
  usuario_intento VARCHAR(30) NOT NULL,                       -- Usuario digitado al iniciar sesión
  id_periodo      INT         NOT NULL,
  evento          ENUM('LOGIN_OK','LOGIN_FALLIDO','LOGOUT') NOT NULL,
  fecha_hora      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario),
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  INDEX idx_acc_periodo (id_periodo, fecha_hora),
  INDEX idx_acc_fecha   (fecha_hora),
  INDEX idx_acc_evento  (evento, fecha_hora)
) ENGINE=InnoDB;

CREATE TABLE auditoria (
  id_auditoria     BIGINT AUTO_INCREMENT PRIMARY KEY,
  id_periodo       INT         NOT NULL,
  id_usuario       INT         NOT NULL,
  fecha_hora       DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  tabla_afectada   VARCHAR(50) NOT NULL,
  id_registro      BIGINT      NOT NULL,
  accion           ENUM('CREAR','EDITAR','ANULAR','CIERRE','AJUSTE','CONFIG') NOT NULL,
  motivo           VARCHAR(255) NULL,
  datos_anteriores JSON        NULL,                          -- Estado previo en JSON
  datos_nuevos     JSON        NULL,                          -- Estado posterior en JSON
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario),
  INDEX idx_aud_periodo  (id_periodo, fecha_hora),
  INDEX idx_aud_registro (tabla_afectada, id_registro),
  INDEX idx_aud_fecha    (fecha_hora),
  INDEX idx_aud_accion   (accion, fecha_hora)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- 5. CATÁLOGOS EDITABLES POR MASTER (Unidades Militares y Galpones Externos)
--    Separa código corto (ej. '3103') del nombre oficial de la unidad.
--    Almacena el saldo acumulado (positivo = saldo a favor, negativo = deuda).
-- ----------------------------------------------------------------------------
CREATE TABLE unidades_militares (
  id_unidad                  INT AUTO_INCREMENT PRIMARY KEY,
  codigo_unidad              VARCHAR(10)   NULL,
  nombre_unidad              VARCHAR(150)  NOT NULL,
  id_unidad_padre            INT           NULL,
  es_agrupador               TINYINT(1)    NOT NULL DEFAULT 0,
  codigos_agrupados_texto    VARCHAR(255)  NULL,
  saldo_cierre_no_aplicado_bs DECIMAL(14,2) NOT NULL DEFAULT 0, -- Arrastre traído de período cerrado
  saldo_acumulado_bs         DECIMAL(14,2) NOT NULL DEFAULT 0, -- Saldo neto vivo del período abierto
  activo                     TINYINT(1)    NOT NULL DEFAULT 1,
  FOREIGN KEY (id_unidad_padre) REFERENCES unidades_militares(id_unidad),
  UNIQUE KEY uq_unidad_codigo_nombre (codigo_unidad, nombre_unidad)
) ENGINE=InnoDB;

CREATE TABLE galpones_externos (
  id_galpon     INT AUTO_INCREMENT PRIMARY KEY,
  nombre_galpon VARCHAR(120) NOT NULL UNIQUE,
  estado_region VARCHAR(60)  NULL,
  activo        TINYINT(1)   NOT NULL DEFAULT 1
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- 6. MOVIMIENTOS DE INVENTARIO (Entradas, Colaboraciones y Despachos)
--    Ningún registro operativo puede eliminarse (DELETE bloqueado por trigger);
--    toda corrección o reverso se hace mediante edición o anulación lógica.
-- ----------------------------------------------------------------------------
CREATE TABLE entradas_inventario (
  id_entrada          INT AUTO_INCREMENT PRIMARY KEY,
  id_periodo          INT          NOT NULL,
  fecha_recepcion     DATE         NOT NULL,
  cantidad_bolsas     INT          NOT NULL CHECK (cantidad_bolsas > 0),
  observaciones       VARCHAR(255) NULL,
  id_usuario_registra INT          NOT NULL,
  fecha_registro      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  anulado             TINYINT(1)   NOT NULL DEFAULT 0,
  motivo_anulacion    VARCHAR(255) NULL,
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_usuario_registra) REFERENCES usuarios(id_usuario),
  INDEX idx_ent_periodo_anulado (id_periodo, anulado)
) ENGINE=InnoDB;

CREATE TABLE colaboraciones_descarga (
  id_colaboracion     INT AUTO_INCREMENT PRIMARY KEY,
  id_periodo          INT          NOT NULL,
  id_entrada          INT          NULL,                      -- Lote/camión asociado
  fecha               DATE         NOT NULL,
  cantidad_bolsas     INT          NOT NULL CHECK (cantidad_bolsas > 0),
  receptor            VARCHAR(120) NOT NULL DEFAULT 'Ayudante de descarga',
  observaciones       VARCHAR(255) NULL,
  id_usuario_registra INT          NOT NULL,
  fecha_registro      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  anulado             TINYINT(1)   NOT NULL DEFAULT 0,
  motivo_anulacion    VARCHAR(255) NULL,
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_entrada) REFERENCES entradas_inventario(id_entrada),
  FOREIGN KEY (id_usuario_registra) REFERENCES usuarios(id_usuario),
  INDEX idx_col_periodo_anulado (id_periodo, anulado),
  INDEX idx_col_entrada_anulado (id_entrada, anulado)
) ENGINE=InnoDB;

CREATE TABLE despachos (
  id_despacho                INT AUTO_INCREMENT PRIMARY KEY,
  id_periodo                 INT           NOT NULL,
  tipo_salida                ENUM('INTERNO_BATALLON','EXTERNO_GALPON') NOT NULL,
  fecha_despacho             DATE          NOT NULL,
  cantidad_familiar          INT           NOT NULL DEFAULT 0,
  cantidad_tango             INT           NOT NULL DEFAULT 0,
  total_bolsas               INT           NOT NULL,
  -- Campos para despachos internos a batallones (Fuerte Tiuna):
  id_batallon_beneficiario   INT           NULL,
  id_batallon_retira         INT           NULL,
  nombre_autorizado_retiro   VARCHAR(100)  NULL,
  cedula_autorizado_retiro   VARCHAR(15)   NULL,
  tiene_recibo               TINYINT(1)    NULL,
  -- Campos para despachos externos a galpones nacionales:
  id_galpon_destino          INT           NULL,
  nombre_conductor           VARCHAR(100)  NULL,
  cedula_conductor           VARCHAR(15)   NULL,
  vehiculo_descripcion       VARCHAR(100)  NULL,
  placa_vehiculo             VARCHAR(15)   NULL,
  -- Liquidación financiera del despacho interno:
  precio_unitario_aplicado   DECIMAL(12,2) NULL,
  monto_base_bs              DECIMAL(14,2) NULL,              -- total_bolsas * precio_unitario_aplicado
  saldo_anterior_aplicado_bs DECIMAL(14,2) NOT NULL DEFAULT 0,-- (+favor / -deuda) aplicada al emitir
  monto_neto_exigible_bs     DECIMAL(14,2) NULL,              -- monto_base_bs - saldo_anterior_aplicado_bs
  monto_total_pagado_bs      DECIMAL(14,2) NOT NULL DEFAULT 0,-- Suma de pagos activos en pagos_batallon
  diferencia_saldo_bs        DECIMAL(14,2) NOT NULL DEFAULT 0,-- pagado - exigible
  saldo_arrastrado           TINYINT(1)    NOT NULL DEFAULT 0,-- 1 = Saldo ya trasladado al siguiente despacho/cierre
  estado_financiero          ENUM('NO_APLICA','PENDIENTE','PARCIAL','PAGADO','SALDO_A_FAVOR') NOT NULL DEFAULT 'NO_APLICA',
  observaciones              VARCHAR(255)  NULL,
  id_usuario_registra        INT           NOT NULL,
  fecha_registro             DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  anulado                    TINYINT(1)    NOT NULL DEFAULT 0,
  motivo_anulacion           VARCHAR(255)  NULL,
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_batallon_beneficiario) REFERENCES unidades_militares(id_unidad),
  FOREIGN KEY (id_batallon_retira) REFERENCES unidades_militares(id_unidad),
  FOREIGN KEY (id_galpon_destino) REFERENCES galpones_externos(id_galpon),
  FOREIGN KEY (id_usuario_registra) REFERENCES usuarios(id_usuario),
  CONSTRAINT chk_desp_total   CHECK (total_bolsas > 0 AND total_bolsas >= cantidad_familiar + cantidad_tango),
  CONSTRAINT chk_desp_interno CHECK (tipo_salida <> 'INTERNO_BATALLON' OR
        (id_batallon_beneficiario IS NOT NULL AND total_bolsas = cantidad_familiar + cantidad_tango)),
  CONSTRAINT chk_desp_externo CHECK (tipo_salida <> 'EXTERNO_GALPON' OR id_galpon_destino IS NOT NULL),
  INDEX idx_desp_periodo (id_periodo, tipo_salida, anulado, saldo_arrastrado),
  INDEX idx_desp_benef   (id_batallon_beneficiario, id_periodo, anulado, saldo_arrastrado)
) ENGINE=InnoDB;

CREATE TABLE pagos_batallon (
  id_pago             INT AUTO_INCREMENT PRIMARY KEY,
  id_despacho         INT           NOT NULL,
  id_periodo          INT           NOT NULL,
  id_batallon_pagador INT           NOT NULL,
  concepto_plan       VARCHAR(100)  NOT NULL DEFAULT 'Pago unificado', -- Libre o predefinido
  monto_pagado_bs     DECIMAL(14,2) NOT NULL CHECK (monto_pagado_bs > 0),
  referencia_bancaria VARCHAR(30)   NULL,
  fecha_pago          DATE          NULL,
  observaciones       VARCHAR(255)  NULL,
  id_usuario_registra INT           NOT NULL,
  fecha_registro      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  anulado             TINYINT(1)    NOT NULL DEFAULT 0,
  motivo_anulacion    VARCHAR(255)  NULL,
  FOREIGN KEY (id_despacho) REFERENCES despachos(id_despacho),
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_batallon_pagador) REFERENCES unidades_militares(id_unidad),
  FOREIGN KEY (id_usuario_registra) REFERENCES usuarios(id_usuario),
  INDEX idx_pago_despacho (id_despacho, anulado)
) ENGINE=InnoDB;

CREATE TABLE historial_saldos_batallon (
  id_hist             INT AUTO_INCREMENT PRIMARY KEY,
  id_unidad           INT           NOT NULL,
  id_periodo          INT           NOT NULL,
  tipo                ENUM('ARRASTRE_CIERRE','AJUSTE_MASTER','APLICACION') NOT NULL,
  monto_bs            DECIMAL(14,2) NOT NULL,
  saldo_resultante_bs DECIMAL(14,2) NOT NULL,
  motivo              VARCHAR(255)  NULL,
  id_usuario          INT           NOT NULL,
  fecha_hora          DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (id_unidad) REFERENCES unidades_militares(id_unidad),
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario)
) ENGINE=InnoDB;

CREATE TABLE respaldos_pendrive (
  id_respaldo  INT AUTO_INCREMENT PRIMARY KEY,
  id_periodo   INT          NOT NULL,
  tipo         ENUM('CIERRE','RAPIDO') NOT NULL,
  ruta_destino VARCHAR(255) NOT NULL,
  archivos     TEXT         NULL,
  id_usuario   INT          NOT NULL,
  fecha_hora   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (id_periodo) REFERENCES periodos_cierre(id_periodo),
  FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario)
) ENGINE=InnoDB;

-- ----------------------------------------------------------------------------
-- 7. PROCEDIMIENTO DE RECÁLCULO FINANCIERO Y TRIGGERS DE PROTECCIÓN
--    sp_recalcular_despacho consolida pagos, actualiza el estado financiero del
--    despacho en una sola escritura y sincroniza el saldo acumulado del batallón.
-- ----------------------------------------------------------------------------
DELIMITER $$
CREATE PROCEDURE sp_recalcular_despacho(IN p_id INT)
BEGIN
  DECLARE v_uid INT DEFAULT NULL;
  DECLARE v_base DECIMAL(14,2) DEFAULT 0;
  DECLARE v_saldo_ant DECIMAL(14,2) DEFAULT 0;
  DECLARE v_pagado DECIMAL(14,2) DEFAULT 0;
  DECLARE v_neto DECIMAL(14,2) DEFAULT 0;
  DECLARE v_dif DECIMAL(14,2) DEFAULT 0;
  DECLARE v_periodo_abierto INT DEFAULT NULL;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_uid = NULL;

  SELECT id_batallon_beneficiario, IFNULL(monto_base_bs, 0), IFNULL(saldo_anterior_aplicado_bs, 0)
  INTO v_uid, v_base, v_saldo_ant
  FROM despachos
  WHERE id_despacho = p_id AND tipo_salida = 'INTERNO_BATALLON';

  IF v_uid IS NOT NULL THEN
    SELECT IFNULL(SUM(monto_pagado_bs), 0)
    INTO v_pagado
    FROM pagos_batallon
    WHERE id_despacho = p_id AND anulado = 0;

    SET v_neto = v_base - v_saldo_ant;
    SET v_dif  = v_pagado - v_neto;

    -- Actualiza el despacho en una sola operación de escritura
    UPDATE despachos
    SET monto_total_pagado_bs = v_pagado,
        monto_neto_exigible_bs = v_neto,
        diferencia_saldo_bs = v_dif,
        estado_financiero = CASE
          WHEN v_pagado = 0 AND v_neto > 0.005 THEN 'PENDIENTE'
          WHEN v_dif < -0.005 THEN 'PARCIAL'
          WHEN v_dif >  0.005 THEN 'SALDO_A_FAVOR'
          ELSE 'PAGADO' END
    WHERE id_despacho = p_id;

    -- Sincroniza saldo acumulado vigente de la unidad beneficiaria en el período abierto
    SELECT id_periodo INTO v_periodo_abierto
    FROM periodos_cierre WHERE estado = 'ABIERTO' ORDER BY id_periodo DESC LIMIT 1;

    UPDATE unidades_militares u
    SET u.saldo_acumulado_bs = u.saldo_cierre_no_aplicado_bs + IFNULL((
      SELECT SUM(d.diferencia_saldo_bs)
      FROM despachos d
      WHERE d.id_batallon_beneficiario = v_uid
        AND d.id_periodo = IFNULL(v_periodo_abierto, 0)
        AND d.tipo_salida = 'INTERNO_BATALLON'
        AND d.anulado = 0
        AND d.saldo_arrastrado = 0
    ), 0)
    WHERE u.id_unidad = v_uid;
  END IF;
END$$

-- Triggers automáticos tras insertar o actualizar un pago
CREATE TRIGGER trg_pagos_ai AFTER INSERT ON pagos_batallon FOR EACH ROW
BEGIN CALL sp_recalcular_despacho(NEW.id_despacho); END$$

CREATE TRIGGER trg_pagos_au AFTER UPDATE ON pagos_batallon FOR EACH ROW
BEGIN CALL sp_recalcular_despacho(NEW.id_despacho); END$$

-- Triggers de bloqueo contra eliminación física (solo se permite anulación lógica)
CREATE TRIGGER trg_desp_bd BEFORE DELETE ON despachos FOR EACH ROW
BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Prohibido eliminar: use anulación (MASTER)'; END$$

CREATE TRIGGER trg_pagos_bd BEFORE DELETE ON pagos_batallon FOR EACH ROW
BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Prohibido eliminar: use anulación (MASTER)'; END$$

CREATE TRIGGER trg_ent_bd BEFORE DELETE ON entradas_inventario FOR EACH ROW
BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Prohibido eliminar: use anulación (MASTER)'; END$$

CREATE TRIGGER trg_col_bd BEFORE DELETE ON colaboraciones_descarga FOR EACH ROW
BEGIN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Prohibido eliminar: use anulación (MASTER)'; END$$
DELIMITER ;

-- ----------------------------------------------------------------------------
-- 8. VISTAS OPTIMIZADAS (Mediante JOINs agrupados de una sola pasada por tabla)
--    Alimentan la interfaz web, los KPIs del dashboard y los reportes Excel/PDF.
-- ----------------------------------------------------------------------------

-- Vista 1: Consolidado de inventario por período (entradas, salidas internas, externas y colaboraciones)
CREATE VIEW v_inventario_periodo AS
SELECT
  p.id_periodo,
  p.nombre_periodo,
  p.estado,
  p.stock_inicial_bolsas AS stock_inicial_arrastre,
  IFNULL(ent.entradas_periodo, 0)         AS entradas_periodo,
  IFNULL(desp.desp_internos_familiar, 0)  AS desp_internos_familiar,
  IFNULL(desp.desp_internos_tango, 0)     AS desp_internos_tango,
  IFNULL(desp.desp_internos_total, 0)     AS desp_internos_total,
  IFNULL(desp.desp_externos_total, 0)     AS desp_externos_total,
  IFNULL(col.colaboraciones_descarga, 0)  AS colaboraciones_descarga
FROM periodos_cierre p
LEFT JOIN (
  SELECT id_periodo, SUM(cantidad_bolsas) AS entradas_periodo
  FROM entradas_inventario
  WHERE anulado = 0
  GROUP BY id_periodo
) ent ON ent.id_periodo = p.id_periodo
LEFT JOIN (
  SELECT id_periodo,
    SUM(CASE WHEN tipo_salida = 'INTERNO_BATALLON' THEN cantidad_familiar ELSE 0 END) AS desp_internos_familiar,
    SUM(CASE WHEN tipo_salida = 'INTERNO_BATALLON' THEN cantidad_tango    ELSE 0 END) AS desp_internos_tango,
    SUM(CASE WHEN tipo_salida = 'INTERNO_BATALLON' THEN total_bolsas      ELSE 0 END) AS desp_internos_total,
    SUM(CASE WHEN tipo_salida = 'EXTERNO_GALPON'   THEN total_bolsas      ELSE 0 END) AS desp_externos_total
  FROM despachos
  WHERE anulado = 0
  GROUP BY id_periodo
) desp ON desp.id_periodo = p.id_periodo
LEFT JOIN (
  SELECT id_periodo, SUM(cantidad_bolsas) AS colaboraciones_descarga
  FROM colaboraciones_descarga
  WHERE anulado = 0
  GROUP BY id_periodo
) col ON col.id_periodo = p.id_periodo;

-- Vista 2: Stock físico disponible en almacén
CREATE VIEW v_stock_disponible AS
SELECT
  v.*,
  (v.stock_inicial_arrastre
   + v.entradas_periodo
   - v.desp_internos_total
   - v.desp_externos_total
   - v.colaboraciones_descarga) AS stock_disponible
FROM v_inventario_periodo v;

-- Vista 3: Detalle financiero de despachos internos a batallones y sus pagos
CREATE VIEW v_conciliacion_batallones AS
SELECT
  d.id_periodo,
  CONCAT('DES-', LPAD(d.id_despacho, 6, '0')) AS codigo_movimiento,
  d.fecha_despacho,
  CONCAT(IFNULL(CONCAT(ub.codigo_unidad, ' '), ''), ub.nombre_unidad) AS batallon_beneficiario,
  CONCAT(IFNULL(CONCAT(ur.codigo_unidad, ' '), ''), ur.nombre_unidad) AS batallon_que_retiro,
  d.cantidad_familiar,
  d.cantidad_tango,
  d.total_bolsas,
  d.monto_base_bs,
  d.saldo_anterior_aplicado_bs,
  d.monto_neto_exigible_bs,
  d.monto_total_pagado_bs,
  d.diferencia_saldo_bs,
  d.saldo_arrastrado,
  d.estado_financiero,
  (SELECT GROUP_CONCAT(CONCAT(IFNULL(p.referencia_bancaria, 's/ref'), ' (', p.monto_pagado_bs, ')') SEPARATOR ' | ')
   FROM pagos_batallon p
   WHERE p.id_despacho = d.id_despacho AND p.anulado = 0) AS pagos_detalle,
  d.observaciones
FROM despachos d
JOIN unidades_militares ub ON ub.id_unidad = d.id_batallon_beneficiario
LEFT JOIN unidades_militares ur ON ur.id_unidad = d.id_batallon_retira
WHERE d.tipo_salida = 'INTERNO_BATALLON' AND d.anulado = 0;

-- Vista 4: Rendición de cuentas ejecutiva (inventario + totales financieros por período)
CREATE VIEW v_rendicion_cuentas AS
SELECT
  s.id_periodo,
  s.nombre_periodo,
  s.stock_inicial_arrastre,
  s.entradas_periodo,
  s.desp_internos_total,
  s.desp_externos_total,
  s.colaboraciones_descarga,
  s.stock_disponible AS stock_en_almacen,
  (IFNULL(fin.monto_cobrado_bs, 0)
   + IFNULL(fin.total_deudas_bs, 0)
   - IFNULL(fin.total_saldos_favor_bs, 0)) AS monto_esperado_bs,
  IFNULL(fin.monto_cobrado_bs, 0)          AS monto_cobrado_bs,
  IFNULL(fin.total_deudas_bs, 0)           AS total_deudas_bs,
  IFNULL(fin.total_saldos_favor_bs, 0)     AS total_saldos_favor_bs
FROM v_stock_disponible s
LEFT JOIN (
  SELECT
    id_periodo,
    SUM(monto_total_pagado_bs) AS monto_cobrado_bs,
    SUM(CASE WHEN saldo_arrastrado = 0 AND diferencia_saldo_bs < 0 THEN -diferencia_saldo_bs ELSE 0 END) AS total_deudas_bs,
    SUM(CASE WHEN saldo_arrastrado = 0 AND diferencia_saldo_bs > 0 THEN  diferencia_saldo_bs ELSE 0 END) AS total_saldos_favor_bs
  FROM despachos
  WHERE tipo_salida = 'INTERNO_BATALLON' AND anulado = 0
  GROUP BY id_periodo
) fin ON fin.id_periodo = s.id_periodo;

-- ----------------------------------------------------------------------------
-- CATÁLOGO INICIAL DE UNIDADES (94 unidades extraídas de Dis_unidad.xlsx)
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

