# Almacén de Alimentos CLAP · Ejército Bolivariano (Fuerte Tiuna)

Sistema web local **100% offline** para el control integral de inventario de bolsas CLAP, despachos internos y externos, conciliación de pagos múltiples, trazabilidad financiera entre períodos, auditoría de seguridad y exportación documental a Excel y PDF.

Diseñado sobre arquitectura MVC ligera (**Flask + PyMySQL + MariaDB**) pensada para correr en computadoras locales sin acceso a internet ni dependencias en la nube (compatible incluso con equipos de bajos recursos / Windows 7, 2 GB de RAM).

---

## Funcionalidades Principales

* **Control de Inventario y Lotes:** Registro de entradas por lote, cálculo automático de stock disponible (`v_stock_disponible`) y registro de salidas por colaboración en descarga ligadas a su lote de origen.
* **Despachos Internos y Externos:**
  * **Retiro Interno (Batallones):** Selección predictiva de 94 unidades militares, desglose Plan Familiar / Plan Tango, registro de retirante autorizado y liquidación financiera automática.
  * **Retiro Externo (Galpones Nacionales):** Control de destino, conductor y vehículo sin cobro monetario.
* **Pagos Múltiples y Trazabilidad Financiera:** Soporte para múltiples referencias bancarias y conceptos libres o predefinidos (`Pago unificado`, `Abono complementario`, o concepto personalizado). Arrastre automático y enlazado bidireccionalmente de **deudas pendientes** o **saldos a favor** entre despachos y períodos.
* **Roles, Seguridad y Auditoría:**
  * Cifrado de contraseñas con **PBKDF2-SHA256** (200.000 iteraciones + sal aleatoria).
  * Roles diferenciados: **MASTER** (administración total, anulaciones, catálogos, auditoría y cierre) y hasta 3 **OPERADOR** (operación diaria).
  * Código de seguridad numérico (`PIN` de 1 a 9 dígitos) exigido al `MASTER` para anular registros o cambiar su clave.
  * Inmutabilidad contable por *triggers* SQL (`DELETE` físico bloqueado; solo anulación lógica con motivo obligatorio).
  * Bitácora legible de auditoría y accesos filtrable por **período, fecha exacta, usuario y acción/evento**.
* **Cierre Manual y Exportación:** Cierre de período contable con traslado de stock y saldos, y generación automática de reporte completo **Excel (`.xlsx`, 7 hojas)** y reporte ejecutivo **PDF (`.pdf`)** en carpeta local o Pendrive USB.

---

## Requisitos Previos del Equipo

Antes de descargar o instalar el proyecto, asegúrese de contar con:

1. **Sistema Operativo:** Windows 7 / 10 / 11 (compatible también con Linux ajustando la ruta de lanzamiento).
2. **Python 3.10 o superior** (con `pip` habilitado).
3. **MariaDB 10.3+** (recomendado **MariaDB 10.4**) o **MySQL 5.7+ / 8.x** instalado localmente como servicio en el puerto `3306`.
4. **Git** (opcional, solo si desea clonar el repositorio desde consola; también puede descargarse en archivo ZIP).

---

## Guía Paso a Paso de Descarga e Instalación

### 1. Descargar el proyecto
* **Opción A — Con Git:**
  ```bash
  git clone <URL_DEL_REPOSITORIO> "Almacén de Alimentos"
  cd "Almacén de Alimentos"
  ```
* **Opción B — Sin Git (ZIP):**
  Haga clic en el botón verde **Code → Download ZIP** en GitHub, descomprima la carpeta en su equipo (por ejemplo en `C:\Almacén de Alimentos`) y abra una consola dentro de ella.

### 2. Instalar dependencias de Python
* **En un equipo con internet:**
  ```cmd
  pip install -r requirements.txt
  ```
* **Para trasladar a un equipo sin internet (vía Pendrive USB):**
  1. En una PC con internet, descargue los paquetes en una carpeta para llevar en pendrive:
     ```cmd
     mkdir paquetes_offline
     pip download -r requirements.txt -d paquetes_offline
     ```
  2. Copie el proyecto junto con `paquetes_offline` al equipo destino sin internet e instale localmente:
     ```cmd
     pip install --no-index --find-links=paquetes_offline -r requirements.txt
     ```

### 3. Crear la Base de Datos (`almacen_clap_ejercito`)
Con el servicio de MariaDB/MySQL activo, importe el esquema inicial desde consola (le pedirá la contraseña de `root`):
```cmd
mysql -u root -p --default-character-set=utf8mb4 < schema.sql
```
Esto creará la base de datos, 13 tablas, 4 vistas optimizadas, 7 triggers, 1 procedimiento almacenado (`sp_recalcular_despacho`), el catálogo base de 94 unidades militares, el período inicial `#1 - Diciembre` y el usuario semilla `master`.

*(Opcional)* Para verificar la integridad transaccional sin alterar datos reales (ejecuta `ROLLBACK` al terminar):
```cmd
mysql -u root -p --default-character-set=utf8mb4 -t < test_schema.sql
```

### 4. Configurar credenciales locales (`app/config.json`)
Por seguridad, las contraseñas reales **no** se suben a GitHub. Cree su configuración local copiando la plantilla:
```cmd
copy app\config.example.json app\config.json
```
Abra `app/config.json` y coloque su contraseña real de MariaDB/MySQL en `db_password` y una frase aleatoria en `secret_key`:
```json
{
  "db_host": "127.0.0.1",
  "db_port": 3306,
  "db_user": "root",
  "db_password": "TU_PASSWORD_MARIADB",
  "db_name": "almacen_clap_ejercito",
  "app_host": "127.0.0.1",
  "app_port": 5000,
  "secret_key": "CAMBIA_ESTA_CLAVE_SECRETA_LOCAL",
  "support_user": "",
  "support_pass": ""
}
```

### 5. Iniciar el Sistema
* En Windows, haga doble clic en **`app/Iniciar_Almacen_CLAP.bat`** (abre el navegador automáticamente y arranca el servidor local).
* O manualmente desde consola:
  ```cmd
  cd app
  python app.py
  ```
* Ingrese en su navegador web a: **`http://127.0.0.1:5000`**

---

## Primer Inicio de Sesión y Configuración Recomendada

Al entrar por primera vez, utilice las credenciales iniciales del sistema:
* **Usuario:** `master`
* **Contraseña inicial:** `Alfa01`

Una vez dentro, se recomienda realizar en orden:
1. Ir a **Usuarios** y crear su **Código de seguridad** numérico de `MASTER` (hasta 9 dígitos) y actualizar la contraseña inicial por una definitiva.
2. Ir al **Panel Principal** y establecer el **Precio Unitario por Bolsa** y el **Stock Inicial** del primer período.
3. Si necesita cuentas operativas diarias, crear hasta 3 usuarios con rol `OPERADOR` desde **Usuarios**.

---

## Estructura del Repositorio

```text
Almacén de Alimentos/
├── README.md                        # Portada y guía de instalación/uso
├── DOCUMENTACION_PROYECTO.md        # Documentación técnica/académica completa (arquitectura y diagrama ER)
├── ANALISIS_Y_ARQUITECTURA_BD.md    # Decisiones de arquitectura, hardware y reglas de BD
├── requirements.txt                 # Dependencias Python (Flask, PyMySQL, openpyxl, fpdf2)
├── schema.sql                       # Script SQL autogenerado para desplegar la BD desde cero
├── test_schema.sql                  # Batería de pruebas SQL transaccionales (ROLLBACK)
├── tools/
│   ├── schema_base.sql              # Plantilla DDL documentada (tablas, índices, triggers, SP, vistas)
│   └── generar_sql.py               # Regenera schema.sql desde schema_base.sql y Dis_unidad.xlsx
└── app/
    ├── Iniciar_Almacen_CLAP.bat     # Lanzador directo en Windows
    ├── config.example.json          # Plantilla segura para crear app/config.json
    ├── app.py                       # Servidor Flask, rutas HTTP, lógica y seguridad
    ├── db.py                        # Conexión PyMySQL, validaciones, criptografía y auditoría
    ├── exporter.py                  # Exportación de reportes Excel (.xlsx) y PDF (.pdf)
    ├── static/
    │   └── style.css                # Hoja de estilos local offline
    └── templates/                   # 11 plantillas HTML Jinja2 (base, dashboard, despachos, pagos, etc.)
```

---

## Documentación Adicional y Aviso de Seguridad

* Consulte **[DOCUMENTACION_PROYECTO.md](DOCUMENTACION_PROYECTO.md)** para revisar el diagrama Entidad–Relación en Mermaid, flujo transaccional en `flask.g` y diccionario completo de tablas, vistas y triggers.
* Consulte **[ANALISIS_Y_ARQUITECTURA_BD.md](ANALISIS_Y_ARQUITECTURA_BD.md)** para detalles de compatibilidad con Windows 7 / MariaDB 10.4.
* **Importante:** Mantenga el repositorio de GitHub en modo **Privado** si contiene datos sensibles institucionales y nunca suba su archivo local `app/config.json`, instaladores `.msi` ni carpetas de respaldos generados (`Respaldos_Locales/`).
