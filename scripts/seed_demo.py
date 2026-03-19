#!/usr/bin/env python3
"""
Sentinel Demo Seed Script
=========================

Creates realistic test data and runs two real backup jobs so the dashboard
has something meaningful to display immediately.

What it does
------------
1. Creates ./test_data/ with sample documents and binary "photo" files.
2. Writes jobs/local_dev.json (local-storage provider, no MinIO needed).
3. Runs a FULL backup  → catalog gets chunks, one completed snapshot.
4. Modifies one file, then runs an INCREMENTAL backup via CBT →
   demonstrates that only the changed file is re-uploaded.
5. Prints the commands to start the API and web UI.

Usage
-----
    python scripts/seed_demo.py
    # or:
    SENTINEL_PASSPHRASE=my-secret python scripts/seed_demo.py
"""

from __future__ import annotations

import json
import os
import random
import string
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASSPHRASE = os.getenv("SENTINEL_PASSPHRASE", "demo-passphrase-change-me")
JOB_PATH = ROOT / "jobs" / "local_dev.json"
TEST_DIR = ROOT / "test_data"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _rand_text(lines: int, prefix: str = "") -> str:
    words = ["backup", "sentinel", "data", "file", "chunk", "encrypt", "compress", "restore"]
    out = []
    for i in range(lines):
        line = f"{prefix}[{i:05d}] " + " ".join(random.choices(words, k=8))
        out.append(line)
    return "\n".join(out) + "\n"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


def _separator(title: str = "") -> None:
    line = "─" * 60
    if title:
        pad = (58 - len(title)) // 2
        print(f"\n┌{line}┐")
        print(f"│{' ' * pad}{title}{' ' * (58 - pad - len(title))}│")
        print(f"└{line}┘")
    else:
        print(f"\n{'─' * 62}")


# ── Step 1: Create test data ───────────────────────────────────────────────────

def create_test_data() -> None:
    _separator("Creating test data")

    (TEST_DIR / "documents").mkdir(parents=True, exist_ok=True)
    (TEST_DIR / "logs").mkdir(exist_ok=True)
    (TEST_DIR / "photos").mkdir(exist_ok=True)
    (TEST_DIR / "code" / "sentinel").mkdir(parents=True, exist_ok=True)

    # Text files — highly compressible (great for showing compression savings)
    files = {
        TEST_DIR / "documents" / "readme.txt":        _rand_text(500, "DOC"),
        TEST_DIR / "documents" / "report_q1.csv":     "date,amount,category\n" +
            "\n".join(f"2026-{(i%12)+1:02d}-01,{i*17.5:.2f},cat{i%5}" for i in range(2000)),
        TEST_DIR / "documents" / "notes.md":          _rand_text(300, "NOTE"),
        TEST_DIR / "logs"      / "app.log":           _rand_text(1000, "LOG"),
        TEST_DIR / "logs"      / "error.log":         _rand_text(200, "ERR"),
        TEST_DIR / "code"      / "sentinel" / "main.py": textwrap.dedent("""\
            # Auto-generated sample source file
            import os, sys, json
            from pathlib import Path

            def main():
                print("Sentinel backup engine demo")
                for i in range(100):
                    print(f"Processing chunk {i}")

            if __name__ == "__main__":
                main()
            """) * 50,
    }

    total = 0
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
        total += path.stat().st_size
        print(f"  ✓ {path.relative_to(ROOT)}  ({_fmt_bytes(path.stat().st_size)})")

    # Binary files — low compressibility ("photos")
    for i in range(3):
        size = random.randint(256_000, 768_000)
        p = TEST_DIR / "photos" / f"photo_{i:03d}.jpg"
        p.write_bytes(os.urandom(size))
        total += size
        print(f"  ✓ {p.relative_to(ROOT)}  ({_fmt_bytes(size)}, binary)")

    print(f"\n  Total source data: {_fmt_bytes(total)}")


# ── Step 2: Ensure local_dev.json exists ──────────────────────────────────────

def ensure_job_config() -> None:
    _separator("Job configuration")

    if JOB_PATH.exists():
        print(f"  ✓ Found existing config: {JOB_PATH.relative_to(ROOT)}")
        return

    cfg = {
        "job_id": "local-dev",
        "source_paths": [str(TEST_DIR)],
        "exclusions": ["**/.git", "**/__pycache__", "**/*.pyc"],
        "catalog_path": str(ROOT / "sentinel_catalog_dev.db"),
        "compression_level": 3,
        "max_workers": 2,
        "webhook_url": None,
        "key_salt_hex": None,
        "storage": {
            "provider": "local",
            "base_path": str(ROOT / "sentinel_store_dev"),
        },
        "retention": {"daily": 7, "weekly": 4, "monthly": 12},
    }
    JOB_PATH.parent.mkdir(exist_ok=True)
    JOB_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"  ✓ Created {JOB_PATH.relative_to(ROOT)}")


# ── Step 3: Run a backup ───────────────────────────────────────────────────────

def run_backup(label: str) -> None:
    _separator(label)

    from sentinel.config import build_retention_policy, build_storage_provider, load_job, save_job
    from sentinel.engine import BackupEngine
    from sentinel.mcd.catalog import CatalogManager

    cfg = load_job(JOB_PATH)
    catalog = CatalogManager(cfg.catalog_path)
    storage = build_storage_provider(cfg.storage)
    salt = bytes.fromhex(cfg.key_salt_hex) if cfg.key_salt_hex else None

    processed: list[str] = []

    def _on_progress(ev: dict) -> None:
        if ev.get("event") == "FILE_DONE":
            processed.append(ev.get("file", ""))
            print(
                f"  [{ev['files_processed']:>4}] {ev['file']:<35} "
                f"↑{ev['chunks_uploaded']:>3} ded{ev['chunks_deduped']:>3}",
                end="\r",
            )

    engine = BackupEngine(
        job_id=cfg.job_id,
        catalog=catalog,
        storage=storage,
        passphrase=PASSPHRASE,
        salt=salt,
        source_paths=cfg.source_paths,
        exclusions=cfg.exclusions,
        max_workers=cfg.max_workers,
        on_progress=_on_progress,
    )

    t0 = time.monotonic()
    result = engine.run_backup()
    elapsed = time.monotonic() - t0

    # Clear the carriage-return line
    print(" " * 80, end="\r")

    # Persist salt
    if not cfg.key_salt_hex:
        cfg.key_salt_hex = engine.key_salt.hex()
        save_job(cfg, JOB_PATH)

    status_icon = "✅" if result.status == "success" else "❌"
    print(f"  {status_icon} Status:          {result.status}")
    print(f"     Files processed: {result.files_processed}")
    print(f"     Files via CBT:   {result.files_skipped_cbt} skipped (unchanged)")
    print(f"     Chunks uploaded: {result.chunks_uploaded}")
    print(f"     Chunks deduped:  {result.chunks_deduped}")
    print(f"     Original data:   {_fmt_bytes(result.bytes_original)}")
    print(f"     Stored:          {_fmt_bytes(result.bytes_stored)}")
    print(f"     Duration:        {elapsed:.1f}s")
    if result.errors:
        for e in result.errors:
            print(f"     ⚠ {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║           Sentinel Backup — Demo Seed Script                 ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    create_test_data()
    ensure_job_config()

    # First backup — all files are new, everything gets uploaded
    run_backup("Run 1 — Full backup (all files new)")

    # Mutate two files to demonstrate CBT on the second run
    _separator("Modifying files for incremental run")
    readme = TEST_DIR / "documents" / "readme.txt"
    log = TEST_DIR / "logs" / "app.log"
    readme.write_text(_rand_text(600, "UPDATED"))
    log.write_text(_rand_text(1200, "NEW-LOG"))
    print(f"  ✎  Modified: {readme.relative_to(ROOT)}")
    print(f"  ✎  Modified: {log.relative_to(ROOT)}")

    # Second backup — CBT should skip unmodified files
    run_backup("Run 2 — Incremental backup (CBT active)")

    # Done
    _separator("Done — start the application")
    print()
    print("  API server (terminal 1):")
    print("    SENTINEL_PASSPHRASE=demo-passphrase-change-me \\")
    print("    SENTINEL_JOBS_DIR=jobs \\")
    print("    uvicorn api.main:app --reload --port 8000")
    print()
    print("  Web UI (terminal 2):")
    print("    cd web && npm run dev")
    print()
    print("  Dashboard → http://localhost:5173")
    print("  API docs  → http://localhost:8000/docs")
    print()


if __name__ == "__main__":
    main()
