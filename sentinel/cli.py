"""
Sentinel CLI — entry point.

Usage:
    sentinel backup   jobs/my_job.json
    sentinel gc       jobs/my_job.json [--dry-run]
    sentinel scrub    jobs/my_job.json [--sample 0.01]
    sentinel snapshots jobs/my_job.json
"""

from __future__ import annotations

import getpass
import logging
import os
import sys

import click

from sentinel.config import (
    build_retention_policy,
    build_storage_provider,
    load_job,
    save_job,
)
from sentinel.engine import BackupEngine
from sentinel.mcd.catalog import CatalogManager
from sentinel.rgc.collector import GarbageCollector

# ------------------------------------------------------------------ #
#  Logging setup                                                       #
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("sentinel")


# ------------------------------------------------------------------ #
#  CLI group                                                           #
# ------------------------------------------------------------------ #

@click.group()
@click.option("--debug", is_flag=True, help="Enable DEBUG logging.")
def cli(debug: bool) -> None:
    """Sentinel — modular, storage-agnostic backup system."""
    if debug:
        logging.getLogger().setLevel(logging.DEBUG)


# ------------------------------------------------------------------ #
#  backup                                                              #
# ------------------------------------------------------------------ #

@cli.command()
@click.argument("job_config", type=click.Path(exists=True))
@click.option(
    "--passphrase",
    envvar="SENTINEL_PASSPHRASE",
    default=None,
    help="Encryption passphrase (or set SENTINEL_PASSPHRASE env var).",
)
def backup(job_config: str, passphrase: str | None) -> None:
    """Run a backup job defined by JOB_CONFIG."""
    config = load_job(job_config)

    if not passphrase:
        passphrase = getpass.getpass("Encryption passphrase: ")

    # Restore salt from config (None → first run → generate fresh)
    salt: bytes | None = None
    if config.key_salt_hex:
        salt = bytes.fromhex(config.key_salt_hex)

    storage = build_storage_provider(config.storage)
    catalog = CatalogManager(config.catalog_path)

    engine = BackupEngine(
        job_id=config.job_id,
        catalog=catalog,
        storage=storage,
        passphrase=passphrase,
        salt=salt,
        source_paths=config.source_paths,
        exclusions=config.exclusions,
        max_workers=config.max_workers,
        webhook_url=config.webhook_url,
    )

    # Persist the salt so subsequent runs can re-derive the same key.
    if not config.key_salt_hex:
        config.key_salt_hex = engine.key_salt.hex()
        save_job(config, job_config)
        log.info("Key salt written back to %s", job_config)

    result = engine.run_backup()
    catalog.close()

    if result.status == "success":
        click.echo(
            f"✓ Backup complete | snapshot={result.snapshot_id[:8]}… "
            f"files={result.files_processed} "
            f"uploaded={result.chunks_uploaded} "
            f"deduped={result.chunks_deduped} "
            f"ratio={result.dedup_ratio:.0%} "
            f"time={result.duration_s:.1f}s"
        )
    else:
        click.echo(f"✗ Backup failed: {result.errors}", err=True)
        sys.exit(1)


# ------------------------------------------------------------------ #
#  gc                                                                  #
# ------------------------------------------------------------------ #

@cli.command()
@click.argument("job_config", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Print what would be deleted without deleting.")
def gc(job_config: str, dry_run: bool) -> None:
    """Run the Garbage Collector for the given job."""
    config = load_job(job_config)
    storage = build_storage_provider(config.storage)
    catalog = CatalogManager(config.catalog_path)
    policy = build_retention_policy(config.retention)

    collector = GarbageCollector(catalog=catalog, storage=storage, policy=policy)
    result = collector.run(job_id=config.job_id, dry_run=dry_run)
    catalog.close()

    tag = "[DRY RUN] " if dry_run else ""
    click.echo(
        f"{tag}GC complete: "
        f"pruned={result.snapshots_pruned} snapshots, "
        f"deleted={result.chunks_deleted} chunks"
    )
    if result.errors:
        for err in result.errors:
            click.echo(f"  ! {err}", err=True)


# ------------------------------------------------------------------ #
#  scrub                                                               #
# ------------------------------------------------------------------ #

@cli.command()
@click.argument("job_config", type=click.Path(exists=True))
@click.option("--sample", default=0.01, show_default=True,
              help="Sample fraction (0.0–1.0). Ignored when --full is set.")
@click.option("--full", is_flag=True, help="Verify every stored chunk (deep scan).")
@click.option(
    "--passphrase",
    envvar="SENTINEL_PASSPHRASE",
    default=None,
)
def scrub(job_config: str, sample: float, full: bool, passphrase: str | None) -> None:
    """Verify integrity of stored chunks (sample by default, --full for deep scan)."""
    from sentinel.dpe.crypto import ChunkCipher, derive_key
    from sentinel.scrub import ScrubEngine

    config = load_job(job_config)
    if not config.key_salt_hex:
        click.echo("No key salt in config — run a backup first.", err=True)
        sys.exit(1)

    if not passphrase:
        passphrase = getpass.getpass("Encryption passphrase: ")

    salt = bytes.fromhex(config.key_salt_hex)
    derived = derive_key(passphrase, salt)
    cipher = ChunkCipher(derived.key)

    storage = build_storage_provider(config.storage)
    catalog = CatalogManager(config.catalog_path)

    engine = ScrubEngine(
        job_id=config.job_id,
        catalog=catalog,
        storage=storage,
        cipher=cipher,
    )
    result = engine.run(sample=sample, full=full)
    catalog.close()

    mode = "FULL" if full else f"sample={sample:.0%}"
    click.echo(
        f"Scrub [{mode}]: {result.chunks_ok} OK, {result.chunks_corrupt} CORRUPT "
        f"out of {result.chunks_checked} checked  ({result.duration_s:.1f}s)"
    )

    if result.corrupt_chunk_ids:
        click.echo(f"\nCorrupt chunks ({len(result.corrupt_chunk_ids)}):", err=True)
        for cid in result.corrupt_chunk_ids:
            click.echo(f"  {cid}", err=True)

        if result.affected_snapshot_ids:
            click.echo(
                f"\nAffected snapshots ({len(result.affected_snapshot_ids)}):", err=True
            )
            for sid in result.affected_snapshot_ids:
                click.echo(f"  {sid}", err=True)

        sys.exit(1)


# ------------------------------------------------------------------ #
#  snapshots                                                           #
# ------------------------------------------------------------------ #

@cli.command()
@click.argument("job_config", type=click.Path(exists=True))
def snapshots(job_config: str) -> None:
    """List all snapshots for a job."""
    config = load_job(job_config)
    catalog = CatalogManager(config.catalog_path)
    rows = catalog.list_snapshots(config.job_id)
    catalog.close()

    if not rows:
        click.echo("No snapshots found.")
        return

    header = f"{'snapshot_id':<38} {'status':<12} {'started_at':<26} {'chunks':>8} {'bytes':>12}"
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows:
        click.echo(
            f"{row['snapshot_id']:<38} "
            f"{row['status']:<12} "
            f"{row['started_at'] or '':<26} "
            f"{row['chunk_count'] or 0:>8} "
            f"{row['total_bytes'] or 0:>12,}"
        )


if __name__ == "__main__":
    cli()
