# Almacén CLAP – Ejército Bolivariano (Fuerte Tiuna) · Análisis y Arquitectura de BD

## 1. Hallazgos y Criterios de Diseño
* **PC del almacén (`RENDIMIENTO CPU.pdf`):** Equipo con Windows 7 y 2 GB de RAM. MySQL 8 consume entre 400–600 MB y no soporta Windows 7. Por eso el sistema emplea **MariaDB 10.4**, un motor relacional liviano y estable en equipos con recursos limitados (`schema.sql` mantiene compatibilidad total).
* **Operación 100% Local Offline:** El servidor web Flask y el motor de base de datos MariaDB corren localmente en la misma PC (`127.0.0.1`), sin necesidad de internet ni red externa.
* **Catálogo maestro (`Dis_unidad.xlsx`):** 94 unidades militares cargadas con separación entre `codigo_unidad` y `nombre_unidad` (90 en `CON OFICIO`, incluyendo la agrupación `35 POLICIA MILITAR` con sus 5 sub-unidades `3501, 3509, 351, 352, 353`, más 4 en `SIN OFICIO`).

## 2. Archivos de Base de Datos
| Archivo | Uso |
|---|---|
| `schema.sql` | Esquema completo verificado en MariaDB 10.4: 13 tablas, 4 vistas optimizadas, 7 triggers, 1 procedimiento almacenado, cuenta semilla `master` (clave inicial `Alfa01`), 94 unidades |
| `test_schema.sql` | Batería de 9 pruebas SQL de integridad transaccional (usa `START TRANSACTION` / `ROLLBACK` sin alterar datos reales) |
| `tools/generar_sql.py` | Regenera `schema.sql` combinando `tools/schema_base.sql` y `Dis_unidad.xlsx` |
| `tools/schema_base.sql` | Plantilla base DDL con tablas, índices compuestos, triggers, procedimiento y vistas documentadas |

## 3. Reglas de Negocio Implementadas en BD
* **Límite de usuarios (`trg_usuarios_max4`):** Máximo 4 cuentas activas y un único usuario con rol `MASTER`.
* **Inmutabilidad contable (`trg_desp_bd`, `trg_pagos_bd`, `trg_ent_bd`, `trg_col_bd`):** Prohibido el borrado físico (`DELETE`) en entradas, despachos, pagos y colaboraciones. Toda reversión es una **anulación lógica** (`anulado = 1`) con motivo obligatorio y código PIN del `MASTER`, auditada en JSON legible.
* **Recálculo automático de pagos (`sp_recalcular_despacho` + `trg_pagos_ai` / `trg_pagos_au`):** Al insertar o editar un pago (soporta múltiples abonos con concepto libre), MariaDB actualiza en una sola operación el monto cobrado, la diferencia, el estado financiero (`PENDIENTE`, `PARCIAL`, `PAGADO`, `SALDO_A_FAVOR`) y el saldo acumulado de la unidad beneficiaria (`saldo_acumulado_bs`).
* **Arrastre financiero entre despachos y períodos:** `monto_neto_exigible_bs = monto_base_bs - saldo_anterior_aplicado_bs` (donde un saldo anterior positivo descuenta un crédito a favor y uno negativo suma una deuda pendiente).
* **Inventario físico (`v_stock_disponible`):** `stock_disponible = stock_inicial_arrastre + entradas_periodo - desp_internos_total - desp_externos_total - colaboraciones_descarga`.

## 4. Instalación y Verificación Local (Sin Internet)
1. Instalar **MariaDB 10.4** (`mariadb-10.4.34-winx64.msi`) como servicio local.
2. Cargar la base de datos inicial desde consola:
   ```cmd
   mysql -u root -p --default-character-set=utf8mb4 < schema.sql
   ```
3. Ejecutar la prueba de verificación sin alterar los datos:
   ```cmd
   mysql -u root -p --default-character-set=utf8mb4 -t < test_schema.sql
   ```
4. Verificar la rendición de cuentas:
   ```sql
   SELECT * FROM almacen_clap_ejercito.v_rendicion_cuentas;
   ```
5. Ejecutar `app\Iniciar_Almacen_CLAP.bat` para levantar la aplicación en `http://127.0.0.1:5000`.
