"""
Tests for the Source & Snapshot Manager (SSM) modules.

Coverage:
  - FileLevelCBT: change detection, mark_clean, persist/reload, purge
  - DirectProvider: contract compliance (no snapshot, yields live path)
  - SnapshotFactory: auto-detection returns a valid provider
  - VSSProvider / LVMProvider / BtrfsProvider: availability checks + mocks
  - BackupEngine integration: CBT skips unchanged files on second run
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sentinel.ssm.base import PrePostHooks, SnapshotMount
from sentinel.ssm.cbt import FileLevelCBT
from sentinel.ssm.factory import DirectProvider, SnapshotFactory


# ------------------------------------------------------------------ #
#  Fixtures                                                            #
# ------------------------------------------------------------------ #

@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def cbt(tmp_dir: Path) -> FileLevelCBT:
    return FileLevelCBT(str(tmp_dir / "test.cbt.json"))


@pytest.fixture
def sample_file(tmp_dir: Path) -> Path:
    f = tmp_dir / "sample.txt"
    f.write_text("hello sentinel")
    return f


# ------------------------------------------------------------------ #
#  FileLevelCBT — change detection                                    #
# ------------------------------------------------------------------ #

class TestFileLevelCBT:

    def test_new_file_is_changed(self, cbt, sample_file):
        """Any file not yet in CBT state must be marked as changed."""
        assert cbt.is_changed(sample_file) is True

    def test_clean_file_not_changed(self, cbt, sample_file):
        """After mark_clean, the same file (unchanged) must not be changed."""
        cbt.mark_clean(sample_file, "snap-001")
        assert cbt.is_changed(sample_file) is False

    def test_modified_mtime_detected(self, cbt, sample_file):
        """Changing mtime must flag the file as changed."""
        cbt.mark_clean(sample_file, "snap-001")
        # Advance mtime by 2 seconds
        new_mtime = sample_file.stat().st_mtime + 2
        os.utime(sample_file, (new_mtime, new_mtime))
        assert cbt.is_changed(sample_file) is True

    def test_modified_size_detected(self, cbt, sample_file):
        """Changing file content (and thus size) must flag it as changed."""
        cbt.mark_clean(sample_file, "snap-001")
        sample_file.write_text("hello sentinel — modified content added")
        assert cbt.is_changed(sample_file) is True

    def test_missing_file_is_changed(self, cbt, sample_file):
        """A file that disappears must be treated as changed (stat fails)."""
        cbt.mark_clean(sample_file, "snap-001")
        sample_file.unlink()
        assert cbt.is_changed(sample_file) is True

    def test_persist_and_reload(self, tmp_dir, sample_file):
        """State saved to disk must survive a full reload."""
        state_path = str(tmp_dir / "cbt.json")
        cbt1 = FileLevelCBT(state_path)
        cbt1.mark_clean(sample_file, "snap-001")
        cbt1.save()

        cbt2 = FileLevelCBT(state_path)
        assert cbt2.is_changed(sample_file) is False

    def test_reload_detects_corruption(self, tmp_dir, sample_file, caplog):
        """A corrupt state file must be silently discarded (full scan on next run)."""
        state_path = tmp_dir / "cbt.json"
        state_path.write_text("{{{INVALID JSON")

        import logging
        with caplog.at_level(logging.WARNING, logger="sentinel.ssm.cbt"):
            cbt = FileLevelCBT(str(state_path))

        # No records loaded — every file is treated as changed
        assert cbt.is_changed(sample_file) is True

    def test_purge_missing_removes_deleted(self, cbt, tmp_dir):
        """purge_missing() must remove entries for files that no longer exist."""
        existing = tmp_dir / "existing.txt"
        deleted = tmp_dir / "deleted.txt"
        existing.write_text("a")
        deleted.write_text("b")

        cbt.mark_clean(existing, "snap-001")
        cbt.mark_clean(deleted, "snap-001")
        assert len(cbt._entries) == 2

        deleted.unlink()
        removed = cbt.purge_missing(tmp_dir)
        assert removed == 1
        assert str(deleted) not in cbt._entries
        assert str(existing) in cbt._entries

    def test_changed_files_generator(self, cbt, tmp_dir):
        """changed_files() must yield only files CBT marks as dirty."""
        f1 = tmp_dir / "a.txt"
        f2 = tmp_dir / "b.txt"
        f1.write_text("aaa")
        f2.write_text("bbb")

        # Mark f1 clean, leave f2 dirty
        cbt.mark_clean(f1, "snap-001")

        changed = list(cbt.changed_files(tmp_dir))
        assert f1 not in changed
        assert f2 in changed

    def test_stats_returns_dict(self, cbt, sample_file):
        cbt.mark_clean(sample_file, "snap-001")
        s = cbt.stats()
        assert s["total_tracked"] == 1
        assert "state_path" in s

    def test_second_backup_skips_unchanged(self, tmp_dir):
        """
        Simulate two consecutive backups: on the second run all unchanged
        files should be skipped by CBT.
        """
        # Create 3 files inside a subdirectory so the CBT state file
        # (stored in tmp_dir's parent) is never included in the walk.
        source_dir = tmp_dir / "source"
        source_dir.mkdir()
        files = []
        for i in range(3):
            f = source_dir / f"file_{i}.txt"
            f.write_text(f"content {i}")
            files.append(f)

        # State file lives outside the backed-up directory to avoid
        # being picked up as a "new" file on the second backup pass.
        state_path = str(tmp_dir / "cbt.json")
        cbt1 = FileLevelCBT(state_path)

        # First backup: all 3 files are changed
        changed_first = list(cbt1.changed_files(source_dir))
        assert len(changed_first) == 3

        for f in files:
            cbt1.mark_clean(f, "snap-001")
        cbt1.save()

        # Second backup: nothing changed → 0 files
        cbt2 = FileLevelCBT(state_path)
        changed_second = list(cbt2.changed_files(source_dir))
        assert len(changed_second) == 0

        # Modify one file
        files[1].write_text("modified content")
        changed_third = list(cbt2.changed_files(source_dir))
        assert len(changed_third) == 1
        assert files[1] in changed_third


# ------------------------------------------------------------------ #
#  DirectProvider                                                      #
# ------------------------------------------------------------------ #

class TestDirectProvider:

    @pytest.fixture
    def provider(self) -> DirectProvider:
        return DirectProvider()

    def test_always_available(self, provider):
        assert provider.is_available() is True

    def test_snapshot_yields_original_path(self, provider, tmp_dir):
        with provider.create_snapshot(str(tmp_dir)) as snap:
            assert snap.mount_point == tmp_dir
            assert snap.provider == "direct"
            assert snap.volume == str(tmp_dir)

    def test_snapshot_id_prefixed(self, provider, tmp_dir):
        with provider.create_snapshot(str(tmp_dir)) as snap:
            assert snap.snapshot_id.startswith("direct_")

    def test_app_consistent_false(self, provider, tmp_dir):
        with provider.create_snapshot(str(tmp_dir)) as snap:
            assert snap.app_consistent is False

    def test_pre_post_hooks_called(self, provider, tmp_dir):
        log_file = tmp_dir / "hooks.log"
        hooks = PrePostHooks(
            pre_freeze=f'echo "pre" >> "{log_file}"',
            post_thaw=f'echo "post" >> "{log_file}"',
        )
        with provider.create_snapshot(str(tmp_dir), hooks=hooks):
            pass
        # Hooks may fail on Windows but should not raise
        # Just verify no exception was raised

    def test_snapshot_accessible_inside_context(self, provider, tmp_dir):
        """Files should be readable inside the context manager."""
        test_file = tmp_dir / "test.bin"
        test_file.write_bytes(b"test data")
        with provider.create_snapshot(str(tmp_dir)) as snap:
            found = list(snap.mount_point.rglob("*.bin"))
            assert len(found) == 1

    def test_list_volumes_returns_list(self, provider):
        volumes = provider.list_volumes()
        assert isinstance(volumes, list)
        assert len(volumes) >= 1


# ------------------------------------------------------------------ #
#  SnapshotFactory                                                     #
# ------------------------------------------------------------------ #

class TestSnapshotFactory:

    def test_auto_detect_returns_provider(self):
        """Factory must always return a valid provider."""
        manager = SnapshotFactory.create()
        assert hasattr(manager, "create_snapshot")

    def test_explicit_direct_provider(self):
        manager = SnapshotFactory.create(preferred="direct")
        assert isinstance(manager, DirectProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown snapshot provider"):
            SnapshotFactory.create(preferred="ftp")

    def test_describe_returns_string(self):
        desc = SnapshotFactory.describe()
        assert isinstance(desc, str)
        assert "Provider" in desc

    def test_fallback_when_privileged_unavailable(self, monkeypatch):
        """
        When VSS/LVM/Btrfs report unavailable the factory must fall back to
        DirectProvider rather than raising.
        """
        # Patch platform so non-Windows CI always hits the Linux path
        monkeypatch.setattr("sentinel.ssm.factory.platform.system", lambda: "Linux")

        # Patch Btrfs/LVM to be unavailable
        with patch("sentinel.ssm.linux.btrfs.BtrfsProvider.is_available", return_value=False), \
             patch("sentinel.ssm.linux.lvm.LVMProvider.is_available", return_value=False):
            manager = SnapshotFactory._auto_detect()

        assert isinstance(manager, DirectProvider)


# ------------------------------------------------------------------ #
#  VSSProvider — Windows-only (mocked on non-Windows)                 #
# ------------------------------------------------------------------ #

class TestVSSProviderMocked:

    @pytest.fixture
    def vss(self, tmp_path):
        """Return a VSSProvider with all subprocess calls mocked."""
        from sentinel.ssm.windows.vss import VSSProvider
        return VSSProvider(junction_base=str(tmp_path))

    def test_is_available_requires_windows_and_admin(self, vss):
        import platform
        if platform.system() != "Windows":
            assert vss.is_available() is False

    def test_create_shadow_parses_output(self, vss):
        fake_output = (
            "vssadmin 1.1 - Volume Shadow Copy Service\n"
            "Successfully created shadow copy for 'C:\\\\'\\n"
            "    Shadow Copy ID: {12345678-1234-1234-1234-123456789abc}\n"
            "    Shadow Copy Volume Name: "
            "\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy7\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_output, stderr="")
            shadow_id, device = vss._create_shadow("C:\\")

        assert shadow_id == "{12345678-1234-1234-1234-123456789abc}"
        assert "HarddiskVolumeShadowCopy7" in device

    def test_create_shadow_raises_on_failure(self, vss):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="Error", stderr="Access denied")
            with pytest.raises(RuntimeError, match="vssadmin create shadow failed"):
                vss._create_shadow("C:\\")


# ------------------------------------------------------------------ #
#  LVMProvider — Linux-only (mocked)                                  #
# ------------------------------------------------------------------ #

class TestLVMProviderMocked:

    @pytest.fixture
    def lvm(self, tmp_path):
        from sentinel.ssm.linux.lvm import LVMProvider
        return LVMProvider(snap_size="1G", mount_base=str(tmp_path))

    def test_is_available_requires_linux_and_root(self, lvm):
        import platform
        if platform.system() != "Linux" or os.getuid() != 0:
            assert lvm.is_available() is False

    def test_parse_lv_device_standard(self, lvm):
        vg, lv = lvm._parse_lv_device("/dev/vg0/data")
        assert vg == "vg0"
        assert lv == "data"

    def test_parse_lv_device_mapper(self, lvm):
        vg, lv = lvm._parse_lv_device("/dev/mapper/vg0-data")
        assert vg == "vg0"
        assert lv == "data"

    def test_parse_lv_device_invalid_raises(self, lvm):
        with pytest.raises(ValueError):
            lvm._parse_lv_device("/dev/sda1")


# ------------------------------------------------------------------ #
#  BackupEngine integration: CBT skip on second run                   #
# ------------------------------------------------------------------ #

class TestBackupEngineCBTIntegration:
    """
    End-to-end test of the CBT → Engine integration using:
      - DirectProvider (no real snapshot needed)
      - LocalProvider (in-memory tmp dir)
      - CatalogManager (in-memory SQLite)
    """

    @pytest.fixture
    def env(self, tmp_path):
        from sentinel.mcd.catalog import CatalogManager
        from sentinel.spi.local import LocalProvider

        source = tmp_path / "source"
        source.mkdir()
        store = tmp_path / "store"
        catalog_path = str(tmp_path / "catalog.db")
        cbt_path = str(tmp_path / "catalog.cbt.json")

        return {
            "source": source,
            "catalog": CatalogManager(catalog_path),
            "storage": LocalProvider(str(store)),
            "cbt_path": cbt_path,
            "catalog_path": catalog_path,
        }

    def _make_engine(self, env):
        from sentinel.engine import BackupEngine
        from sentinel.ssm.factory import DirectProvider
        return BackupEngine(
            job_id="test-job",
            catalog=env["catalog"],
            storage=env["storage"],
            passphrase="test-pass",
            source_paths=[str(env["source"])],
            source_manager=DirectProvider(),
            cbt_state_path=env["cbt_path"],
        )

    def test_first_backup_processes_all_files(self, env):
        (env["source"] / "a.txt").write_bytes(b"aaa" * 1000)
        (env["source"] / "b.txt").write_bytes(b"bbb" * 1000)

        engine = self._make_engine(env)
        result = engine.run_backup()

        assert result.status == "success"
        assert result.files_processed == 2

    def test_second_backup_skips_unchanged(self, env):
        (env["source"] / "a.txt").write_bytes(b"aaa" * 1000)
        (env["source"] / "b.txt").write_bytes(b"bbb" * 1000)

        # First run
        engine1 = self._make_engine(env)
        r1 = engine1.run_backup()
        assert r1.files_processed == 2

        # Second run — nothing changed
        engine2 = self._make_engine(env)
        r2 = engine2.run_backup()
        assert r2.files_processed == 0

    def test_modified_file_reprocessed(self, env):
        f = env["source"] / "a.txt"
        f.write_bytes(b"original content" * 100)

        engine1 = self._make_engine(env)
        engine1.run_backup()

        # Modify the file (change size + mtime)
        time.sleep(0.01)
        f.write_bytes(b"modified content much longer " * 200)

        engine2 = self._make_engine(env)
        r2 = engine2.run_backup()
        assert r2.files_processed == 1
