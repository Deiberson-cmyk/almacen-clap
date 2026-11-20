# Almacén CLAP – Ejército Bolivariano (Fuerte Tiuna) · Análisis y Arquitectura de BD

## 1. Hallazgos
**PC del almacén (RENDIMIENTO CPU.pdf):** Windows 7, 2 GB de RAM. Un MySQL 8 completo consume 400–600 MB y MySQL 8 no soporta Windows 7. Por eso el Paso 3 usará un motor local liviano; `schema.sql` es compatible con MySQL 5.7 / MariaDB 10.3+ (MariaDB sí corre bien en Windows 7 con poca RAM).

**SQL vs MySQL:** SQL es el *lenguaje*; MySQL es un *programa* que lo ejecuta. "Sin conexión" significa que el servidor corre en la misma PC (`localhost`), sin red ni internet.

**Dis_unidad.xlsx**
- 94 unidades: 90 en `CON OFICIO` (incluye 3501, 3509, 351, 352, 353 agrupadas bajo `35 POLICIA MILITAR`) + 4 en `SIN OFICIO`.
- 8.160 bolsas (4.414 Familiar, 3.746 Tango) a 857,48 Bs → 6.997.036,80 Bs esperados. Pagos reales: 6.828.323,36 Bs.
- La hoja dice cobrado 6.925.205,08: **error de 96.881,72 Bs** por la fórmula de IESEOFANB (fila 93 suma la fila 94).
- Pagos múltiples: Liceo Ayacucho (3 abonos), COLOP‑CEOFANB (2 abonos). Pago único para ambos planes en varias unidades (filas 60–63, 70).
- Retiros por otra unidad (solo en observaciones): Banda Marcial (3101), 7001 retiró por Escuadrón Cmdo Aéreo, Brig. 4 de Febrero, DIGECAFA.
- Deudas anotadas: 312 (570,72), 313 (1.410), 714 AVEJB (2.489,76), O'Leary (10.198,48), Montilla (20), DIGECAFA (10). Sin pagar: Centro Mantenimiento Fuerte Tiuna, Servicio de Alimentación, Círculo Militar.
- Inconsistencias: fila 41 (el Excel anota "faltó 2.489,76" pero pagó 18.947 de más según las cifras), fechas `S/F`, `16/09//2026`, `12/092026`, `01/102026`; referencias a veces numéricas, a veces texto (con ceros a la izquierda: se guardan como texto).
- Hoja SIN OFICIO: 128 bolsas sin desglose Familiar/Tango, sin pago → no se cargaron como movimientos.

## 2. Archivos
| Archivo | Uso |
|---|---|
| `schema.sql` | Base completa: 12 tablas, 4 vistas, triggers, usuario `master`, 94 unidades |
| `seed_movimientos_excel.sql` | OPCIONAL: 85 despachos y 115 pagos reales (pruebas) |
| `tools/generar_sql.py`, `tools/schema_base.sql` | Regeneran los anteriores desde el Excel |

## 3. Reglas implementadas en BD
- Máximo 4 usuarios, un solo MASTER (trigger). La clave del `master` se fija al primer inicio (`PENDIENTE_PRIMER_INICIO`).
- Prohibido `DELETE` en entradas, despachos, pagos y colaboraciones (trigger): solo anulación con motivo.
- Pagos múltiples por despacho; el trigger recalcula pagado, diferencia y estado (`PENDIENTE/PARCIAL/PAGADO/SALDO_A_FAVOR`).
- Saldo anterior: `neto = base − saldo_anterior(+favor/−deuda) + ajuste_master`.
- Stock = arrastre + entradas − despachos (DESPACHADO) − colaboraciones (vista `v_stock_disponible`).
- Sin guía ni facturación; códigos `ENT-/DES-/COL-` + id con ceros.

## 4. Instalación (sin internet)
1. En la laptop (con internet) descarga el instalador de MariaDB 10.x (o MySQL 5.7 para Windows) y guárdalo en el pendrive junto con este repositorio.
2. En la PC del almacén instala desde el pendrive (instalador MSI, usuario `root` con clave), marca "Instalar como servicio".
3. Abre la consola de MariaDB/MySQL y ejecuta: `mysql -u root -p --default-character-set=utf8mb4 < schema.sql`
4. (Pruebas) `mysql -u root -p < seed_movimientos_excel.sql`
5. Verifica: `SELECT * FROM almacen_clap_ejercito.v_rendicion_cuentas;`

## 5. GitHub
Usa un repositorio **privado** (datos militares). `.gitignore` excluye el Excel y el PDF.
```
git remote add origin https://github.com/<tu-usuario>/almacen-clap.git
git push -u origin master
```
En la otra PC: `git clone` o descarga ZIP desde GitHub y cópialo al pendrive.

> `schema.sql` no pudo ejecutarse aquí (no hay MySQL instalado); está pendiente probarlo en tu laptop.
