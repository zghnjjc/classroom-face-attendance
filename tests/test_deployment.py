"""临时数据验证发行入口、内容签名、完整备份及失败/中断回退。"""

import json
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import time
import tkinter as tk
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app_entry import role_command
from classroom_attendance.core.app_errors import BusyError, ValidationError
from classroom_attendance.storage.attendance_store import AttendanceStore
from _face_test_support import authorize_faces
from classroom_attendance.storage.backup_service import (JOURNAL_NAME, MANAGED_NAMES, create_backup, database_summary,
                            inspect_backup, recover_interrupted, restore_backup, SCHEMA_VERSION)
from classroom_attendance.storage.data_migration import migrate_legacy
from classroom_attendance.faces.face_utils import save_gray
from classroom_attendance.core.file_integrity import file_sha256
from classroom_attendance.storage.recovery_app import RecoveryApp, authorize_restore
from classroom_attendance.core.runtime_guard import InstanceLease, MaintenanceLease
from classroom_attendance.faces.train import dataset_signature, manifest_path, model_health, train_model, training_paths


def hold_instance(directory, pipe):
    with InstanceLease(directory):
        pipe.send(True)
        pipe.recv()
    pipe.close()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="classroom-deployment-")
        self.addCleanup(self.temporary.cleanup)
        # Windows TEMP may contain an 8.3 alias; production paths are resolved.
        self.folder = Path(self.temporary.name).resolve()
        self.source = self.folder / "source"
        self.source.mkdir()
        self.target = self.folder / "target"
        self.store = AttendanceStore(self.source / "attendance.db", seed_path=None)
        self.store.save_student("001", "虚构同学", "演示班")
        authorize_faces(self.store)
        self.dataset = self.source / "dataset"
        self.dataset.mkdir()
        save_gray(self.dataset / "001.0.jpg", np.random.default_rng(3).integers(50,200,(160,160),dtype=np.uint8))
        self.model = self.source / "face_model.yml"
        self.manifest = self.source / "model_manifest.json"
        train_model(self.dataset, self.model, {"001"}, _lease=False)
        manifest_path(self.model).replace(self.manifest)
        self.archive = self.folder / "backup.zip"

    def backup(self):
        return create_backup(self.store, self.archive, model=self.model, manifest=self.manifest, dataset=self.dataset)

    def restore(self, summary=None, **kwargs):
        summary = summary or inspect_backup(self.archive)
        return restore_backup(self.archive, self.target, expected_sha256=summary["archive_sha256"], **kwargs)

    def rewrite_archive(self, changes):
        with zipfile.ZipFile(self.archive) as archive:
            files = {item.filename: archive.read(item) for item in archive.infolist()}
        files.update(changes)
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)

    def target_fingerprint(self):
        return {p.relative_to(self.target).as_posix(): p.read_bytes()
                for p in self.target.rglob("*") if p.is_file() and p.relative_to(self.target).parts[0] in MANAGED_NAMES}

    def test_source_and_frozen_entry_commands_share_role_and_data_directory(self):
        with patch("app_entry.sys.frozen", False, create=True):
            source = role_command("student", self.target)
        with patch("app_entry.sys.frozen", True, create=True):
            frozen = role_command("admin", self.target)
        self.assertTrue(source[1].endswith("app_entry.py"))
        self.assertEqual(frozen[1:3], ["--role", "admin"])
        self.assertEqual(Path(source[-1]), self.target)
        with self.assertRaises(ValueError):
            role_command("arbitrary-script", self.target)

    def test_chosen_data_directory_is_saved_and_environment_takes_priority(self):
        from classroom_attendance.core.app_config import configured_data_dir, save_data_location
        with patch('classroom_attendance.core.app_config.default_data_dir',return_value=self.folder / 'settings'), patch('classroom_attendance.core.app_config.PROGRAM_DIR', self.folder / 'program'), patch.dict(os.environ,{'CLASSROOM_DATA_DIR':''}):
            save_data_location(self.target)
            self.assertEqual(configured_data_dir(),self.target)
            with patch.dict(os.environ,{'CLASSROOM_DATA_DIR':str(self.source)}):
                self.assertEqual(configured_data_dir(),self.source)

    def test_invalid_saved_directory_is_rejected_instead_of_opening_empty_default(self):
        from classroom_attendance.core.app_config import configured_data_dir
        preference = self.folder / 'data_location.json'
        preference.write_text('{broken',encoding='utf-8')
        with patch('classroom_attendance.core.app_config.default_data_dir',return_value=self.folder), patch('classroom_attendance.core.app_config.PROGRAM_DIR', self.folder / 'program'), patch.dict(os.environ,{'CLASSROOM_DATA_DIR':''}):
            with self.assertRaises(ValueError):
                configured_data_dir()

    def test_content_signature_survives_copy_and_timestamp_change(self):
        signature = dataset_signature(training_paths(self.dataset))
        sample = self.dataset / "001.0.jpg"
        os.utime(sample, (1000000000,1000000000))
        self.assertEqual(dataset_signature(training_paths(self.dataset)), signature)
        self.assertTrue(model_health(model=self.model, dataset=self.dataset, manifest_file=self.manifest)[0])

    def test_file_hash_cache_reuses_reads_and_changes_invalidate_cache(self):
        from classroom_attendance.core.file_integrity import _read_hash
        sample = self.dataset / "001.0.jpg"
        with patch("classroom_attendance.core.file_integrity._read_hash", wraps=_read_hash) as reader:
            file_sha256(sample)
            file_sha256(sample)
            self.assertEqual(reader.call_count, 1)
            sample.write_bytes(sample.read_bytes() + b"changed")
            file_sha256(sample)
            self.assertEqual(reader.call_count, 2)

    def test_backup_includes_inventory_counts_and_healthy_model(self):
        info = self.backup()
        summary = inspect_backup(self.archive)
        self.assertEqual(info["format_version"], 2)
        self.assertEqual(summary["students"], 1)
        self.assertTrue(summary["model_ready"])
        self.assertIn("dataset/001.0.jpg", info["files"])

    def test_restore_to_another_directory_preserves_model_and_records(self):
        self.backup()
        result = self.restore()
        self.assertFalse(result['model_ready'])
        self.assertTrue(result['source_model_ready'])
        self.assertTrue(result['authorization_review_required'])
        self.assertEqual(database_summary(self.target / "attendance.db")["students"], 1)
        self.assertTrue(model_health(model=self.target / "face_model.yml", dataset=self.target / "dataset",
                                    manifest_file=self.target / "model_manifest.json", fresh=True)[0])
        from classroom_attendance.faces.face_utils import load_recognizer, read_gray
        from classroom_attendance.faces.train import preprocess
        recognizer = load_recognizer(self.target / 'face_model.yml')
        label, distance = recognizer.predict(preprocess(read_gray(self.target / 'dataset' / '001.0.jpg')))
        self.assertEqual(recognizer.getLabelInfo(label),'001')
        self.assertLess(distance,1)
        self.assertTrue((self.target / "restore_status.json").exists())
        self.assertFalse((self.target / JOURNAL_NAME).exists())

    def test_restore_retains_original_data_in_pre_restore_snapshot(self):
        self.backup()
        old = AttendanceStore(self.target / "attendance.db", seed_path=None)
        old.save_student("009", "原目录同学", "原班")
        (self.target / "logs").mkdir()
        (self.target / "logs" / "keep.log").write_text("keep", encoding="utf-8")
        result = self.restore()
        previous = Path(result["previous_snapshot"])
        self.assertEqual(inspect_backup(result['previous_backup'])['students'],1)
        self.assertEqual(database_summary(previous / "attendance.db")["students"], 1)
        with closing(sqlite3.connect(previous / "attendance.db")) as connection:
            self.assertEqual(connection.execute("SELECT student_id FROM students").fetchone()[0], "009")
        self.assertEqual((self.target / "logs" / "keep.log").read_text(), "keep")

    def test_bad_file_hash_is_rejected_before_changing_target(self):
        self.backup()
        old = AttendanceStore(self.target / "attendance.db", seed_path=None)
        old.save_student("009", "原目录同学")
        before = self.target_fingerprint()
        self.rewrite_archive({"dataset/001.0.jpg": b"tampered"})
        with self.assertRaises(ValueError):
            self.restore()
        self.assertEqual(self.target_fingerprint(), before)

    def test_missing_or_extra_inventory_members_are_rejected(self):
        self.backup()
        self.rewrite_archive({"dataset/001.1.jpg": b"extra"})
        with self.assertRaises(ValueError):
            inspect_backup(self.archive)

    def test_path_traversal_is_rejected_without_writing_outside_stage(self):
        self.backup()
        self.rewrite_archive({"../escaped.txt": b"unsafe"})
        with self.assertRaises(ValueError):
            inspect_backup(self.archive)
        self.assertFalse((self.folder / "escaped.txt").exists())

    def test_windows_reserved_device_names_are_rejected(self):
        self.backup()
        self.rewrite_archive({'dataset/NUL.001.0.jpg':b'unsafe'})
        with self.assertRaises(ValueError):
            inspect_backup(self.archive)

    def test_legacy_directory_is_not_silently_bypassed_by_new_default(self):
        from classroom_attendance.core.app_startup import checked_store
        from classroom_attendance.storage.data_migration import LegacyDataFound
        with patch('classroom_attendance.core.app_startup.DATABASE_PATH',self.target / 'attendance.db'), patch('classroom_attendance.core.app_startup.PROGRAM_DIR',self.source), \
                patch('classroom_attendance.core.app_startup.HAS_DATA_LOCATION_PREFERENCE',False), patch.dict(os.environ,{'CLASSROOM_DATA_DIR':''}):
            with self.assertRaises(LegacyDataFound):
                checked_store()
        self.assertFalse((self.target / 'attendance.db').exists())

    def test_duplicate_zip_members_are_rejected(self):
        import warnings
        self.backup()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(self.archive, "a") as archive:
                archive.writestr("attendance.db", b"duplicate")
        with self.assertRaises(ValueError):
            inspect_backup(self.archive)

    def test_unknown_backup_and_database_versions_are_rejected(self):
        self.backup()
        with zipfile.ZipFile(self.archive) as archive:
            info = json.loads(archive.read("backup_info.json"))
        info["format_version"] = 999
        self.rewrite_archive({"backup_info.json": json.dumps(info)})
        with self.assertRaises(ValueError):
            inspect_backup(self.archive)
        with self.store.connect(write=True) as connection:
            self.store._set(connection,"schema_version","999")
        with self.assertRaises(ValueError):
            database_summary(self.store.path)

    def test_changed_archive_requires_new_confirmation(self):
        self.backup()
        summary = inspect_backup(self.archive)
        self.store.save_student("002", "另一同学")
        self.backup()
        with self.assertRaises(ValueError):
            self.restore(summary)
        self.assertFalse((self.target / "attendance.db").exists())

    def test_legacy_zip_requires_confirmation_and_does_not_enable_model(self):
        self.backup()
        self.rewrite_archive({"backup_info.json": json.dumps({"created_at":"2026-01-01T09:00:00","schema_version":SCHEMA_VERSION})})
        summary = inspect_backup(self.archive)
        self.assertTrue(summary["legacy"])
        with self.assertRaises(ValueError):
            self.restore(summary)
        self.restore(summary, allow_legacy=True)
        self.assertFalse((self.target / "face_model.yml").exists())
        self.assertTrue((self.target / "dataset" / "001.0.jpg").exists())

    def test_legacy_healthy_model_is_upgraded_inside_new_backup_only(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["version"] = 2
        manifest.pop("signature_algorithm")
        manifest["dataset_signature"] = dataset_signature(training_paths(self.dataset), version=2)
        self.manifest.write_text(json.dumps(manifest),encoding="utf-8")
        original = self.manifest.read_bytes()
        self.backup()
        self.assertEqual(self.manifest.read_bytes(), original)
        self.assertTrue(inspect_backup(self.archive)["model_ready"])
        self.restore()
        self.assertEqual(json.loads((self.target / "model_manifest.json").read_text())["version"], 3)

    def test_stale_model_backup_keeps_original_but_restores_for_retraining(self):
        save_gray(self.dataset / "001.0.jpg", np.random.default_rng(8).integers(50,200,(160,160),dtype=np.uint8))
        self.backup()
        self.assertFalse(inspect_backup(self.archive)["model_ready"])
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(archive.read("face_model.yml"),self.model.read_bytes())
        self.restore()
        self.assertFalse((self.target / "face_model.yml").exists())

    def test_active_window_registration_blocks_restore(self):
        self.backup()
        with InstanceLease(self.target):
            with self.assertRaises(BusyError):
                self.restore()
        self.restore()

    def test_other_process_blocks_restore_until_it_releases_instance(self):
        self.backup()
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=hold_instance,args=(self.target,child),daemon=True)
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(5))
            self.assertTrue(parent.recv())
            with self.assertRaises(BusyError):
                self.restore()
            parent.send(True)
            process.join(5)
            self.assertEqual(process.exitcode,0)
            self.restore()
        finally:
            if process.is_alive():
                process.terminate()
                process.join(2)
            parent.close()

    def test_maintenance_blocks_new_instances_and_stale_markers_are_cleaned(self):
        self.target.mkdir()
        markers = self.target / ".instances"
        markers.mkdir()
        (markers / "stale.lock").write_bytes(b"0")
        with MaintenanceLease(self.target):
            self.assertFalse((markers / "stale.lock").exists())
            with self.assertRaises(BusyError):
                with InstanceLease(self.target):
                    pass

    def test_failure_mid_replacement_rolls_back_original_files(self):
        self.backup()
        old = AttendanceStore(self.target / "attendance.db", seed_path=None)
        old.save_student("009", "原目录同学")
        before = self.target_fingerprint()
        original_replace, failed = Path.replace, []
        def fail_once(path, target):
            if Path(target) == self.target / "face_model.yml" and not failed:
                failed.append(True)
                raise OSError("simulated disk failure")
            return original_replace(path,target)
        with patch.object(Path,"replace",fail_once):
            with self.assertRaises(OSError):
                self.restore()
        self.assertTrue(failed)
        self.assertEqual(self.target_fingerprint(), before)
        self.assertFalse((self.target / JOURNAL_NAME).exists())

    def test_interrupted_restore_journal_rolls_back_on_recovery(self):
        old = AttendanceStore(self.target / "attendance.db", seed_path=None)
        old.save_student("009", "原目录同学")
        token = "a" * 32
        previous = self.target / "backups" / ("pre_restore_" + token)
        previous.mkdir(parents=True)
        old.path.replace(previous / "attendance.db")
        (self.target / "attendance.db").write_bytes(b"partial replacement")
        (self.target / "dataset").mkdir()
        (self.target / JOURNAL_NAME).write_text(json.dumps({"version":1,"phase":"moving",
            "stage":".restore-stage-"+token,"previous":"backups/pre_restore_"+token,
            "original":["attendance.db"]}),encoding="utf-8")
        self.assertTrue(recover_interrupted(self.target))
        self.assertEqual(database_summary(self.target / "attendance.db")["students"],1)
        self.assertFalse((self.target / "dataset").exists())
        self.assertFalse(recover_interrupted(self.target))

    def test_committed_journal_keeps_restored_data_and_previous_snapshot(self):
        self.backup()
        old = AttendanceStore(self.target / 'attendance.db', seed_path=None)
        old.save_student('009','原目录同学')
        result = self.restore()
        token = 'b' * 32
        previous = Path(result['previous_snapshot'])
        (self.target / JOURNAL_NAME).write_text(json.dumps({'version':1,'phase':'committed',
            'stage':'.restore-stage-'+token,'previous':previous.relative_to(self.target).as_posix(),
            'original':['attendance.db']}),encoding='utf-8')
        self.assertTrue(recover_interrupted(self.target))
        with closing(sqlite3.connect(self.target / 'attendance.db')) as connection:
            self.assertEqual(connection.execute('SELECT student_id FROM students').fetchone()[0],'001')
        self.assertTrue((previous / 'attendance.db').exists())

    def test_tampered_journal_cannot_move_outside_data_directory(self):
        self.target.mkdir()
        (self.target / JOURNAL_NAME).write_text(json.dumps({"version":1,"phase":"moving",
            "stage":"../escape","previous":"../original","original":[]}),encoding="utf-8")
        with self.assertRaises(ValueError):
            recover_interrupted(self.target)

    def test_migration_preserves_source_and_rejects_existing_target(self):
        before = {p.relative_to(self.source).as_posix():p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        migrate_legacy(self.source,self.target)
        for name,content in before.items():
            self.assertEqual((self.source / name).read_bytes(),content)
        self.assertEqual(database_summary(self.target / "attendance.db")["students"],1)
        with self.assertRaises(ValueError):
            migrate_legacy(self.source,self.target)

    def test_restore_authentication_retains_existing_lockout(self):
        self.store.setup_admin("example-password-2026")
        with self.assertRaises(ValidationError):
            authorize_restore(self.source,"wrong")
        self.assertEqual(self.store.admin_auth_status()["failures"],1)
        authorize_restore(self.source,"example-password-2026")
        for _ in range(5):
            with self.assertRaises(ValidationError):
                authorize_restore(self.source,"wrong")
        with self.assertRaises(ValidationError):
            authorize_restore(self.source,"example-password-2026",allow_damaged=True)

    def test_damaged_current_database_requires_explicit_offline_authorization(self):
        self.target.mkdir()
        (self.target / "attendance.db").write_bytes(b"damaged")
        with self.assertRaises(ValidationError):
            authorize_restore(self.target,"")
        authorize_restore(self.target,"",allow_damaged=True)

    def test_offline_password_reset_refuses_active_program_instances(self):
        from classroom_attendance.maintenance.reset_admin import reset_admin
        self.store.setup_admin('example-password-2026')
        with InstanceLease(self.source):
            with self.assertRaises(BusyError):
                reset_admin(self.source,confirmed=True)
        self.assertTrue(self.store.admin_exists())

    def recovery_window(self):
        root = tk.Tk()
        root.withdraw()
        def close():
            try:
                for timer in root.tk.call('after','info'):
                    root.after_cancel(timer)
                root.destroy()
            except tk.TclError:
                pass
        self.addCleanup(close)
        app = RecoveryApp(root)
        return root, app

    def wait_for_restore(self, root, app):
        deadline = time.monotonic() + 3
        while app.restore_running and time.monotonic() < deadline:
            root.update()
            threading.Event().wait(.005)
        self.assertFalse(app.restore_running)

    def test_recovery_ui_requires_verified_backup_and_confirmation(self):
        root, app = self.recovery_window()
        with patch('classroom_attendance.storage.recovery_app.restore_backup') as restore:
            app.restore()
            self.backup()
            app.checked(inspect_backup(self.archive))
            app.restore()
            restore.assert_not_called()
        self.assertFalse(app.restore_running)

    def test_recovery_ui_clears_password_and_returns_to_idle_after_success(self):
        self.backup()
        root, app = self.recovery_window()
        app.source.set(str(self.archive))
        app.checked(inspect_backup(self.archive))
        app.confirmed.set(True)
        app.password.set('example-password-2026')
        with patch('classroom_attendance.storage.recovery_app.restore_backup',return_value={'previous_snapshot':str(self.folder / 'previous'),'model_ready':True}), \
                patch('classroom_attendance.storage.recovery_app.messagebox.showinfo'):
            app.restore()
            self.assertEqual(app.password.get(),'')
            self.wait_for_restore(root,app)
        self.assertFalse(app.busy)
        self.assertIsNone(app.summary)
        self.assertIn('恢复完成',app.status.get())

    def test_recovery_ui_allows_retry_after_other_instance_blocks_it(self):
        self.backup()
        root, app = self.recovery_window()
        app.source.set(str(self.archive))
        app.checked(inspect_backup(self.archive))
        app.confirmed.set(True)
        with patch('classroom_attendance.storage.recovery_app.restore_backup',side_effect=BusyError('请关闭其他窗口')), \
                patch('classroom_attendance.storage.recovery_app.messagebox.showerror') as error:
            app.restore()
            self.wait_for_restore(root,app)
        self.assertFalse(app.busy)
        self.assertIsNotNone(app.summary)
        self.assertEqual(str(app.restore_button.cget('state')),'normal')
        error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
