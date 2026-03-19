# CLAUDE.md — Sentinel Backup System

Contexto de desarrollo para Claude Code. Lee este archivo al inicio de cada sesión para tener el estado completo del proyecto.

---

## 1. Descripción General

**Sentinel** es un sistema de backup empresarial modular, con almacenamiento agnóstico, diseñado para ejecutarse en servidores Linux (y opcionalmente Windows). Su propósito es proteger datos críticos con:

- **Deduplicación** basada en Content-Defined Chunking (Rabin fingerprinting)
- **Encriptación extremo a extremo** con AES-256-GCM y derivación de clave Argon2id
- **Compresión** Zstandard por chunk
- **Snapshots** consistentes del SO (LVM, Btrfs, VSS, DirectProvider)
- **Retención GFS** (Grandfather-Father-Son: 7 diarios, 4 semanales, 12 mensuales)
- **API REST + WebSocket** para integración con dashboards y automatización
- **Multi-backend** de almacenamiento: local, S3/MinIO, NFS, SMB

La visión es un sistema listo para producción en entornos PYME y enterprise, comparable a Restic o Borg pero con interfaz web propia y API de integración.

---

## 2. Arquitectura

```
┌─────────────────────────────────────────────────────────────────┐
│                         Interfaces                              │
│   CLI (Click)          API REST (FastAPI)       WebSocket       │
└────────────┬───────────────────┬────────────────────┬───────────┘
             │                   │                    │
             └───────────────────▼────────────────────┘
                         BackupEngine / RestoreEngine
                                   │
          ┌────────────────────────┼──────────────────────────┐
          ▼                        ▼                          ▼
    SSM (Snapshots)         DPE (Pipeline)             MCD (Catálogo)
    LVM / Btrfs /           Rabin Chunking             SQLite WAL
    VSS / Direct            Zstd Compress              ref-counting
          │                 AES-256-GCM enc             deduplicación
          ▼                        │                          │
    CBT (Change                    ▼                          ▼
    Block Tracking)         SPI (Storage)             RGC (Retención)
    mtime/inode/size        Local / S3 /              GFS Policy
    JSON state file         NFS / SMB                 Garbage Collector
```

### Componentes principales

| Componente | Descripción |
|-----------|-------------|
| **BackupEngine** (`sentinel/engine.py`) | Orquestador principal. Coordina SSM → CBT → DPE → SPI → MCD |
| **RestoreEngine** (`sentinel/engine.py`) | Invierte el pipeline: SPI → decrypt → decompress → write |
| **SSM** (`sentinel/ssm/`) | Crea snapshots del SO; detecta automáticamente LVM / Btrfs / VSS |
| **CBT** (`sentinel/ssm/cbt.py`) | Filtra archivos sin cambios por mtime/size/inode para backups incrementales |
| **DPE** (`sentinel/dpe/`) | Pipeline stateless: chunkea, comprime y cifra cada chunk |
| **MCD** (`sentinel/mcd/catalog.py`) | Catálogo SQLite WAL thread-safe con ref-counting para deduplicación |
| **SPI** (`sentinel/spi/`) | Abstracción de storage con implementaciones Local, S3, NFS, SMB |
| **RGC** (`sentinel/rgc/collector.py`) | Aplica política GFS y elimina chunks huérfanos (ref_count == 0) |
| **FastAPI** (`api/`) | REST + WebSocket; orquesta jobs, snapshots, schedules y restore |
| **Frontend** (`web/`) | React 18 + TypeScript + Tailwind CSS + shadcn/ui + Recharts |

---

## 3. Tecnologías

### Backend (Python)

| Tecnología | Versión | Uso |
|-----------|---------|-----|
| Python | 3.11+ (3.12 en Docker) | Runtime principal |
| FastAPI | ≥0.111.0 | API REST + WebSocket |
| Uvicorn | ≥0.29.0 | ASGI server (con websockets + httptools) |
| Pydantic | ≥2.7.0 | Schemas de request/response |
| Click | ≥8.1.7 | CLI |
| SQLite 3 | stdlib (WAL mode) | Catálogo de chunks, snapshots y archivos |
| cryptography | ≥42.0.0 | AES-256-GCM (AESGCM) |
| argon2-cffi | ≥23.1.0 | Argon2id KDF (RFC 9106) |
| zstandard | ≥0.22.0 | Compresión Zstd (bindings Python para libzstd) |
| boto3 / botocore | ≥1.34.0 | S3 / MinIO (multipart upload + retries) |
| APScheduler | ≥3.10.4 | Scheduler de jobs con AsyncIOScheduler + CronTrigger |
| croniter | ≥2.0.1 | Cálculo de next_run_at desde expresiones cron |
| psutil | ≥5.9.0 | Métricas de disco y CPU para el endpoint /api/system/info |
| websockets | ≥12.0 | Soporte WebSocket async |
| requests | ≥2.31.0 | Webhooks de notificación |
| pytest | ≥8.0.0 | Testing |
| httpx | ≥0.27.0 | Tests de API (transporte ASGI) |

### Frontend (web/)

| Tecnología | Uso |
|-----------|-----|
| React 18 | UI framework |
| TypeScript | Tipado estático |
| Vite | Bundler + dev server (puerto 5173) |
| Tailwind CSS | Estilos utilitarios |
| shadcn/ui | Componentes base (Button, Card, Badge, Table, Dialog, etc.) |
| @tanstack/react-query | Data fetching y caché de queries |
| Recharts / D3 | Gráficos (d3-scale, d3-shape, etc. en node_modules) |

### Infraestructura

| Tecnología | Uso |
|-----------|-----|
| Docker | Imagen python:3.12-slim para el backend |
| Docker Compose | Orquesta MinIO + sentinel-api + sentinel-web |
| MinIO | S3-compatible object storage (desarrollo/producción on-prem) |
| Node 20 Alpine | Runtime del frontend en Docker |

### Snapshots del SO

| Provider | Plataforma | Mecanismo |
|---------|-----------|-----------|
| `LVMProvider` | Linux | `lvcreate` Copy-on-Write |
| `BtrfsProvider` | Linux | Subvolumes Btrfs |
| `VSSProvider` | Windows | Volume Shadow Copy Service (vssadmin CLI) |
| `DirectProvider` | Cualquiera | Live filesystem (fallback, sin snapshot) |

---

## 4. Estructura de Carpetas

```
sentinel/                         # Raíz del proyecto
├── sentinel/                     # Paquete Python principal
│   ├── __init__.py
│   ├── config.py                 # JobConfig, StorageConfig, RetentionConfig, ScheduleConfig
│   ├── engine.py                 # BackupEngine + RestoreEngine (orquestadores)
│   ├── cli.py                    # Entry point CLI (Click): backup, gc, scrub, snapshots
│   ├── dpe/                      # Data Processing Engine
│   │   ├── chunker.py            # RabinChunker: CDC con polinomio GF(2^64), ventana 64B
│   │   ├── crypto.py             # derive_key (Argon2id) + ChunkCipher (AES-256-GCM)
│   │   └── pipeline.py           # ProcessingPipeline: hash→compress→encrypt, ThreadPoolExecutor
│   ├── mcd/                      # Metadata & Catalog Database
│   │   └── catalog.py            # CatalogManager: SQLite WAL, thread-local connections, ref-counting
│   ├── rgc/                      # Retention & Garbage Collector
│   │   └── collector.py          # GarbageCollector + select_snapshots_to_prune (GFS)
│   ├── spi/                      # Storage Provider Interface
│   │   ├── base.py               # IStorageProvider (ABC): upload/download/delete/list_chunks
│   │   ├── local.py              # LocalProvider: filesystem shardado por prefijo hex
│   │   ├── s3.py                 # S3Provider: boto3 multipart + exponential backoff (5 retries)
│   │   ├── nfs.py                # NFSProvider: mount point en runtime
│   │   └── smb.py                # SMBProvider: share path SMB/CIFS
│   └── ssm/                      # Source & Snapshot Manager
│       ├── base.py               # ISourceManager (ABC) + PrePostHooks + SnapshotInfo
│       ├── factory.py            # SnapshotFactory.create(): auto-detección VSS→Btrfs→LVM→Direct
│       ├── cbt.py                # FileLevelCBT: JSON state con mtime/size/inode por archivo
│       ├── linux/
│       │   ├── lvm.py            # LVMProvider: lvcreate/lvremove CoW snapshots
│       │   └── btrfs.py          # BtrfsProvider: subvolumes Btrfs
│       └── windows/
│           └── vss.py            # VSSProvider: VSS via vssadmin CLI (requiere Admin)
│
├── api/                          # FastAPI REST + WebSocket (Sprint 6)
│   ├── main.py                   # App FastAPI: lifespan, CORS, WebSocket /ws/jobs/{job_id}
│   ├── schemas.py                # Modelos Pydantic de request/response
│   ├── deps.py                   # Dependency injection, RunState, RestoreState
│   ├── scheduler.py              # APScheduler: init_scheduler, shutdown_scheduler
│   └── routers/
│       ├── jobs.py               # CRUD jobs + POST /run (async 202)
│       ├── snapshots.py          # List, detail, delete snapshot; POST restore
│       ├── restore.py            # GET /restore/{id}/status
│       ├── storage.py            # GET stats + GET health por job_id
│       ├── scheduler.py          # GET list, PUT update, DELETE schedule
│       ├── exclusions.py         # CRUD de exclusion templates
│       └── system.py             # GET /system/info, GET /system/activity
│
├── web/                          # Frontend React + TypeScript
│   ├── vite.config.ts            # Vite: proxy /api y /ws → API backend
│   ├── tailwind.config.ts        # Tailwind CSS config
│   ├── tsconfig.json
│   ├── index.html
│   └── src/
│       ├── main.tsx              # Entry point: React 18 + ReactQuery + StrictMode
│       ├── App.tsx               # Router principal (pendiente de desarrollo completo)
│       ├── index.css             # Estilos globales Tailwind
│       ├── lib/
│       │   ├── ws.ts             # WebSocket client helper
│       │   └── utils.ts          # Utilidades (cn para classnames)
│       └── components/ui/        # Componentes shadcn/ui base
│           ├── button.tsx
│           ├── card.tsx
│           ├── badge.tsx
│           ├── progress.tsx
│           ├── input.tsx
│           ├── label.tsx
│           ├── dialog.tsx
│           └── table.tsx
│
├── jobs/                         # Configuraciones de jobs (JSON)
│   ├── example_job.json          # Ejemplo: S3/MinIO + retención GFS
│   ├── local_dev.json            # Dev: storage filesystem local
│   └── Copias_Diarias.json       # Job de backup diario configurado
│
├── scripts/
│   └── seed_demo.py              # Crea test_data/ y ejecuta 2 backups demo (full + incremental)
│
├── tests/                        # Test suite (pytest)
│   ├── test_dpe.py               # Tests unitarios del DPE
│   └── test_api.py               # Smoke tests de la API (httpx + ASGI)
│
├── test_data/                    # Datos de prueba generados por seed_demo.py
│   ├── documents/                # Archivos de texto comprimibles
│   ├── logs/                     # Archivos de log
│   ├── photos/                   # Binarios no-comprimibles (JPEG sintéticos)
│   └── code/                     # Código fuente Python
│
├── Dockerfile                    # python:3.12-slim; instala deps + paquete sentinel
├── docker-compose.yml            # MinIO + minio-init + sentinel-api + sentinel-web
├── Makefile                      # Targets de desarrollo: install, seed, api, web, test, docker-*
├── requirements.txt              # Dependencias Python con versiones mínimas
├── pyproject.toml                # setuptools metadata; pytest config (asyncio_mode=auto)
├── .env.example                  # Template de variables de entorno
└── .gitignore
```

---

## 5. Estado Actual del Desarrollo

### Fases del plan

| Fase | Estado | Descripción |
|------|--------|-------------|
| **Fase 1 — Core Engine** | ✅ Completa | SSM (snapshots), CBT (change tracking), DPE (chunking + crypto + compresión), MCD (catálogo SQLite), SPI (storage abstraction), BackupEngine + RestoreEngine |
| **Fase 2 — CLI + Retención** | ✅ Completa | CLI Click (backup, gc, scrub, snapshots), RGC (política GFS + garbage collector) |
| **Fase 3 — API REST** | ✅ Completa | FastAPI con todos los routers, WebSocket para streaming de progreso, APScheduler para jobs programados |
| **Fase 4 — Frontend** | 🔄 En progreso | Scaffolding React + TS + Tailwind + shadcn/ui creado; componentes base listos; páginas principales pendientes |
| **Fase 5 — Hardening** | ⏳ Pendiente | Audit logging, verificación de integridad (scrub con reparación), mejoras NFS/SMB, tests de integración completos |

### Lo que está implementado

- Backup incremental con CBT (Change Block Tracking) basado en mtime/size/inode
- Deduplicación de chunks por SHA-256 con ref-counting en catálogo
- Chunking Rabin CDC (avg 64KB, min 32KB, max 128KB) — mismo polinomio que Restic
- Encriptación AES-256-GCM por chunk con IV aleatorio de 12 bytes
- Derivación de clave Argon2id (time=3, mem=64MB, parallelism=4)
- Compresión Zstandard nivel configurable (default 3)
- Snapshots LVM, Btrfs, VSS y DirectProvider con auto-detección
- Storage backends: Local (filesystem shardado), S3/MinIO (multipart + retries), NFS, SMB
- Catálogo SQLite WAL modo thread-safe con conexiones thread-local
- Retención GFS: 7 diarios, 4 semanales, 12 mensuales (configurables)
- Garbage collector en dos fases: decremento ref_count → eliminación de huérfanos
- Restore completo (todas las rutas) y selectivo (subset de archivos)
- Catalog backup cifrado subido al backend de storage tras cada backup
- API REST completa: jobs CRUD, snapshots, restore, storage stats, scheduler, exclusiones, system info
- WebSocket `/ws/jobs/{job_id}` con eventos JOB_START, FILE_DONE, JOB_SUCCESS, JOB_FAILED
- Notificaciones webhook (POST JSON a URL configurable)
- Scheduling con APScheduler + cron expressions + croniter para next_run_at
- Docker Compose con MinIO, API y frontend

### Lo que falta

- **Frontend**: páginas completas (Dashboard, Jobs, Snapshots, Restore, Settings)
- **Scrub con reparación**: verificación de integridad de chunks almacenados
- **NFS/SMB**: retry logic y checksumming (S3 ya los tiene)
- **Audit logging**: registro inmutable de operaciones para compliance
- **Tests de integración**: cobertura completa de API y engine
- **Monitoreo**: métricas Prometheus/Grafana (opcional)

---

## 6. Características Implementadas

### Change Block Tracking (CBT)
Archivo: `sentinel/ssm/cbt.py`

Registra `mtime`, `size` e `inode` de cada archivo en un JSON (`*.cbt.json`) junto al catálogo. En cada backup incremental, `is_changed()` compara el estado actual con el registrado y salta archivos no modificados. `mark_clean()` actualiza el registro tras un backup exitoso. `purge_missing()` elimina entradas de archivos borrados.

### Deduplicación
Archivos: `sentinel/dpe/pipeline.py`, `sentinel/mcd/catalog.py`, `sentinel/engine.py`

Cada chunk recibe un hash SHA-256 antes de comprimirse y cifrarse. `CatalogManager.chunk_exists()` verifica si ya está almacenado. Si existe, `increment_ref()` aumenta el ref_count sin subir el chunk. Si no existe, se sube y se registra con ref_count=1. La GC solo elimina chunks con ref_count=0.

### Rabin Chunking (CDC)
Archivo: `sentinel/dpe/chunker.py`

Implementación del algoritmo Rabin fingerprinting con el mismo polinomio irreducible que usa Restic (`0x3DA3358B4DC173`). Ventana deslizante de 64 bytes. Los límites de chunk se detectan cuando `fingerprint & MASK == 0`. Parámetros: avg=64KB, min=32KB, max=128KB. Thread-safe (estado mutable en el frame del generador, no en `self`).

### AES-256-GCM
Archivo: `sentinel/dpe/crypto.py`

`derive_key()` usa Argon2id (RFC 9106) para derivar 32 bytes de clave desde la passphrase + salt. El salt (32 bytes aleatorios) se persiste en el JSON del job para re-derivar en restore. `ChunkCipher.encrypt()` genera un IV aleatorio de 12 bytes por chunk y produce `[12B IV][ciphertext + 16B GCM tag]`. La passphrase nunca se escribe en disco (zero-knowledge).

### Zstandard (Zstd)
Archivo: `sentinel/dpe/pipeline.py`

Cada chunk se comprime con Zstd antes de cifrarse. Se instancia un `ZstdCompressor` fresco por llamada para garantizar thread-safety. Nivel configurable por job (1-22, default 3). La pipeline es stateless y se ejecuta en `ThreadPoolExecutor`.

---

## 7. Comandos Importantes

### Desarrollo local

```bash
# Instalar todas las dependencias (Python + Node)
make install

# Crear datos de prueba y ejecutar 2 backups demo (full + incremental)
SENTINEL_PASSPHRASE=mi-passphrase make seed

# Levantar API en puerto 8000
SENTINEL_PASSPHRASE=mi-passphrase make api

# Levantar frontend Vite en puerto 5173
make web

# Modo dev completo (seed + api + web en background)
SENTINEL_PASSPHRASE=mi-passphrase make dev
```

### Tests

```bash
# Todos los tests
make test

# Solo tests del DPE (unitarios)
make test-dpe

# Solo smoke tests de la API
make test-api

# Con cobertura
pytest tests/ -v --cov=sentinel --cov-report=html
```

### Docker Compose

```bash
# Levantar todo (MinIO + API + Web) en background
make docker-up
# Equivalente: docker compose up -d --build

# Ver logs de todos los servicios
make docker-logs
# Equivalente: docker compose logs -f

# Parar contenedores
make docker-down

# Servicios y puertos:
#   MinIO API:      http://localhost:9000
#   MinIO Console:  http://localhost:9001
#   Sentinel API:   http://localhost:8000
#   Frontend:       http://localhost:5173
#   API Docs:       http://localhost:8000/docs
```

### CLI de Sentinel

```bash
# Backup de un job
python -m sentinel.cli backup jobs/mi_job.json

# Garbage collection (retención GFS)
python -m sentinel.cli gc jobs/mi_job.json

# Listar snapshots
python -m sentinel.cli snapshots jobs/mi_job.json

# Restore de un snapshot
python -m sentinel.cli restore jobs/mi_job.json --snapshot-id <uuid> --destination /tmp/restore

# Verificación de integridad (scrub)
python -m sentinel.cli scrub jobs/mi_job.json
```

### API REST (ejemplos curl)

```bash
# Health check
curl http://localhost:8000/healthz

# Listar jobs
curl http://localhost:8000/api/jobs

# Disparar backup
curl -X POST http://localhost:8000/api/jobs/mi-job-id/run

# Estado del último run
curl http://localhost:8000/api/jobs/mi-job-id/runs/latest

# Listar snapshots
curl http://localhost:8000/api/snapshots

# Ver docs interactivos
open http://localhost:8000/docs
```

### Limpieza

```bash
# Eliminar artefactos de build (pycache, dist, etc.)
make clean

# Eliminar datos de desarrollo (catálogo, store, test_data)
make clean-dev
```

---

## 8. Decisiones de Diseño

### ADR-001: Rabin CDC compatible con Restic
Se eligió el mismo polinomio irreducible que Restic (`0x3DA3358B4DC173`) para el chunking CDC. Esto garantiza que los límites de chunk son reproducibles y predecibles para los mismos datos, maximizando la deduplicación entre runs. Tamaños (32KB min, 64KB avg, 128KB max) son potencias de dos para que la máscara sea un simple AND bit a bit.

### ADR-002: Argon2id para KDF (no PBKDF2 ni bcrypt)
Argon2id (RFC 9106) fue elegido sobre PBKDF2 y bcrypt porque es resistente tanto a ataques GPU como a ataques side-channel de timing. Parámetros conservadores: time_cost=3, memory_cost=64MB, parallelism=4. El salt de 32 bytes se persiste en el JSON del job y nunca la clave derivada ni la passphrase.

### ADR-003: IV aleatorio por chunk (no contador)
Cada chunk recibe un IV de 12 bytes generado con `os.urandom()`. Se descartó usar contadores para evitar que errores de implementación o reordenamientos reutilicen nonces. El espacio de 2^96 hace negligible la probabilidad de colisión incluso con millones de chunks.

### ADR-004: SQLite WAL en lugar de PostgreSQL
Para un sistema de backup que corre típicamente en el mismo host que los datos, SQLite en WAL mode ofrece suficiente concurrencia (lectores no bloquean escritores), cero infraestructura adicional, y un catálogo portable como un solo archivo. Las conexiones son thread-local para evitar contención.

### ADR-005: Ref-counting en lugar de mark-and-sweep para GC
El garbage collector usa ref-counting explícito (`ref_count` en la tabla `chunks`). Cuando un snapshot se elimina, se decrementan los ref_counts de sus chunks. Los chunks con ref_count=0 son huérfanos y se eliminan en dos fases (primero del SPI, luego del catálogo). Esto evita scans completos del catálogo en cada GC.

### ADR-006: IStorageProvider como abstracción de storage
La interfaz `IStorageProvider` define solo 4 operaciones: `upload_chunk`, `download_chunk`, `delete_chunk`, `list_chunks`. Todas son idempotentes (seguras para retry). Esto permite añadir nuevos backends sin modificar el engine. S3Provider implementa exponential backoff con full jitter (máx 5 retries).

### ADR-007: SSM con context manager para garantizar limpieza
Todos los providers de snapshot implementan `create_snapshot()` como context manager (`__enter__`/`__exit__`). Esto garantiza que el snapshot se elimina siempre al salir del bloque, incluso si hay excepciones. Previene acumulación de snapshots LVM huérfanos en caso de crash.

### ADR-008: FastAPI con jobs en background (run_in_executor)
Los backups se ejecutan en `asyncio.get_event_loop().run_in_executor(None, ...)` para no bloquear el event loop de FastAPI. El `RunState` se mantiene en memoria durante el job y se persiste al catálogo al finalizar. El `ws_manager` hace broadcast de eventos al WebSocket del job correspondiente.

### ADR-009: React + TypeScript para el frontend (no Vue)
Se eligió React 18 + TypeScript sobre Vue 3 por ecosistema más amplio de componentes (shadcn/ui, @tanstack/react-query) y mayor familiaridad en el equipo. Vite como bundler por velocidad de HMR. Tailwind CSS para estilos sin CSS-in-JS. El proxy de Vite redirige `/api` y `/ws` al backend en desarrollo y en Docker.

---

## 9. Servidor de Producción

| Dato | Valor |
|------|-------|
| **Nombre** | mariaserver |
| **IP** | 192.168.1.87 |
| **Usuario** | ozurek |
| **SO** | Linux (Ubuntu/Debian) |
| **Directorio del proyecto** | `/home/ozurek/sentinel` |

### Acceso

```bash
ssh ozurek@192.168.1.87
cd /home/ozurek/sentinel
```

### Variables de entorno en producción

Crear `/home/ozurek/sentinel/.env` (no commitear):

```env
SENTINEL_PASSPHRASE=<passphrase-segura>
SENTINEL_JOBS_DIR=/home/ozurek/sentinel/jobs
SENTINEL_CORS_ORIGINS=http://192.168.1.87:5173,http://192.168.1.87:3000
MINIO_ROOT_USER=sentinel_admin
MINIO_ROOT_PASSWORD=<password-seguro>
MINIO_API_PORT=9000
MINIO_CONSOLE_PORT=9001
API_PORT=8000
WEB_PORT=5173
```

> **IMPORTANTE**: El `docker-compose.yml` actual tiene la passphrase hardcodeada como `"Jun10rtup4p4"`. Antes de producción real, migrar a variables de entorno desde `.env`.

---

## 10. Próximos Pasos

### Prioridad Alta

1. **Frontend — Dashboard principal**
   - Vista general: estadísticas de jobs, último backup, espacio usado
   - Componentes: `JobCard`, `SnapshotTable`, `StorageChart` (Recharts)
   - Consumir endpoints: `GET /api/jobs`, `GET /api/storage/{job_id}/stats`

2. **Frontend — Página de Jobs**
   - CRUD completo de jobs (crear, editar, eliminar)
   - Botón "Run backup" con estado en tiempo real via WebSocket
   - Formulario de configuración: source paths, exclusiones, storage, retención, schedule

3. **Frontend — Página de Snapshots**
   - Lista paginada con filtros por job y fecha
   - Detalle: archivos en el snapshot, métricas de deduplicación
   - Acción de restore con selector de archivos y destino

4. **Corregir bug en engine.py**
   - Hay dos definiciones de `_walk_changed()` (líneas ~269 y ~295)
   - La segunda sobreescribe a la primera; eliminar la primera (sin el `yield from []` incorrecto)

### Prioridad Media

5. **Scrub con reparación**
   - Verificar integridad de chunks almacenados (re-descarga y verifica GCM tag)
   - Reportar chunks corruptos; marcar en catálogo
   - CLI: `sentinel scrub jobs/mi_job.json`

6. **NFS/SMB — retry logic**
   - Añadir exponential backoff similar a S3Provider
   - Verificar checksums post-upload para detectar corrupción silenciosa

7. **Tests de integración completos**
   - `tests/test_api.py` con cobertura de todos los endpoints
   - Test de backup + restore end-to-end
   - Test de GC con datos reales

### Prioridad Baja

8. **Audit logging**
   - Tabla `audit_log` en SQLite: quién hizo qué y cuándo
   - Endpoint `GET /api/audit` para compliance

9. **Métricas Prometheus**
   - Endpoint `/metrics` con contadores de chunks, bytes, duración de jobs

10. **Mejora del docker-compose.yml**
    - Mover passphrase y credenciales a `.env` (ya está en `.env.example`)
    - Añadir restart policy (`unless-stopped`) a los servicios productivos
