"""
Configuration loading and job definition for Sentinel.

Job definitions are stored as JSON files. Sensitive values like the passphrase
should be supplied via environment variables and are never written to disk.

Example job file: jobs/example_job.json
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from sentinel.mcd.catalog import CatalogManager
from sentinel.rgc.collector import RetentionPolicy
from sentinel.spi.base import IStorageProvider
from sentinel.spi.local import LocalProvider
from sentinel.spi.s3 import S3Provider


# ------------------------------------------------------------------ #
#  Data classes                                                        #
# ------------------------------------------------------------------ #

@dataclass
class ScheduleConfig:
    """Optional recurring schedule for a job (APScheduler cron)."""
    enabled: bool = False
    cron: Optional[str] = None          # e.g. "0 2 * * *"
    timezone: str = "UTC"
    next_run_at: Optional[str] = None   # ISO8601, updated by scheduler


@dataclass
class StorageConfig:
    provider: str                      # "local" | "s3" | "smb" | "nfs"
    # -- Local / NFS / SMB (mounted path) --
    base_path: Optional[str] = None
    # -- S3 / MinIO --
    bucket: Optional[str] = None
    prefix: str = "chunks/"
    endpoint_url: Optional[str] = None
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    region: str = "us-east-1"
    # -- SMB --
    smb_server: Optional[str] = None    # e.g. \\server\share
    smb_username: Optional[str] = None
    smb_password: Optional[str] = None
    # -- NFS --
    nfs_mount: Optional[str] = None     # e.g. server:/export/path


@dataclass
class RetentionConfig:
    daily: int = 7
    weekly: int = 4
    monthly: int = 12


@dataclass
class JobConfig:
    job_id: str
    source_paths: List[str]
    catalog_path: str = "sentinel_catalog.db"
    exclusions: List[str] = field(default_factory=list)
    compression_level: int = 3
    max_workers: int = 4
    webhook_url: Optional[str] = None
    # salt is stored as a hex string in the config; None on first run.
    key_salt_hex: Optional[str] = None
    storage: StorageConfig = field(default_factory=lambda: StorageConfig(provider="local", base_path="./sentinel_store"))
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    schedule: Optional[ScheduleConfig] = None


# ------------------------------------------------------------------ #
#  Loaders                                                             #
# ------------------------------------------------------------------ #

def load_job(config_path: str | Path) -> JobConfig:
    """
    Load a job definition from a JSON file.

    Nested dicts for ``storage``, ``retention``, and ``schedule`` are
    automatically converted to their respective dataclass instances.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Job config not found: {path}")

    with path.open() as fh:
        raw: dict = json.load(fh)

    storage_raw = raw.pop("storage", {})
    retention_raw = raw.pop("retention", {})
    schedule_raw = raw.pop("schedule", None)

    storage = StorageConfig(**storage_raw)
    retention = RetentionConfig(**retention_raw) if retention_raw else RetentionConfig()
    schedule = ScheduleConfig(**schedule_raw) if schedule_raw else None

    return JobConfig(storage=storage, retention=retention, schedule=schedule, **raw)


def save_job(config: JobConfig, config_path: str | Path) -> None:
    """Persist the job config back to disk (e.g. after updating key_salt_hex)."""
    path = Path(config_path)
    data = {
        "job_id": config.job_id,
        "source_paths": config.source_paths,
        "catalog_path": config.catalog_path,
        "exclusions": config.exclusions,
        "compression_level": config.compression_level,
        "max_workers": config.max_workers,
        "webhook_url": config.webhook_url,
        "key_salt_hex": config.key_salt_hex,
        "storage": {
            k: v for k, v in config.storage.__dict__.items() if v is not None
        },
        "retention": config.retention.__dict__,
    }
    if config.schedule is not None:
        data["schedule"] = {
            k: v for k, v in config.schedule.__dict__.items() if v is not None
        }
    path.write_text(json.dumps(data, indent=2))


def build_storage_provider(cfg: StorageConfig) -> IStorageProvider:
    """Instantiate the correct IStorageProvider from a StorageConfig."""
    if cfg.provider == "local":
        if not cfg.base_path:
            raise ValueError("local provider requires base_path")
        return LocalProvider(cfg.base_path)

    if cfg.provider == "s3":
        if not cfg.bucket:
            raise ValueError("s3 provider requires bucket")
        return S3Provider(
            bucket=cfg.bucket,
            prefix=cfg.prefix,
            endpoint_url=cfg.endpoint_url,
            aws_access_key_id=cfg.aws_access_key_id or os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=cfg.aws_secret_access_key or os.getenv("AWS_SECRET_ACCESS_KEY"),
            region=cfg.region,
        )

    if cfg.provider == "nfs":
        from sentinel.spi.nfs import NFSProvider
        if not cfg.nfs_mount:
            raise ValueError("nfs provider requires nfs_mount")
        return NFSProvider(nfs_mount=cfg.nfs_mount)

    if cfg.provider == "smb":
        from sentinel.spi.smb import SMBProvider
        if not cfg.base_path:
            raise ValueError("smb provider requires base_path (mounted share path)")
        return SMBProvider(
            smb_server=cfg.smb_server or "",
            base_path=cfg.base_path,
        )

    raise ValueError(f"Unknown storage provider: {cfg.provider!r}")


def build_retention_policy(cfg: RetentionConfig) -> RetentionPolicy:
    return RetentionPolicy(
        daily=cfg.daily,
        weekly=cfg.weekly,
        monthly=cfg.monthly,
    )
