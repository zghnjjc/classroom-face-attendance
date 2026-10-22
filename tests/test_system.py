import json
import sqlite3
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np
from openpyxl import load_workbook

from attendance_store import AttendanceStore, read_student_import
from camera_service import RecognitionStreak, quality_hint
from face_utils import load_classifier, load_recognizer, read_gray, save_gray
from resource_lock import ResourceLease
from train import model_health, preprocess, train_model


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="classroom-tests-")
        self.folder=Path(self.temporary.name)
        self.store=AttendanceStore(self.folder/"test.db",seed_path=None)
        self.store.save_student("001","测试甲","一班")
        self.store.save_student("002","测试乙","一班")
        self.store.save_student("003","测试丙","二班")
        self.now=datetime.now().replace(microsecond=0)

    def tearDown(self):
        self.temporary.cleanup()

    def make_session(self,course="课程一",group="一班"):
        return self.store.create_session(course,group,self.now-timedelta(minutes=5),self.now+timedelta(hours=1),10)

    def test_sessions_separate_same_day_and_preserve_first_time(self):
        first=self.make_session(); self.store.open_session(first,self.now)
        self.assertTrue(self.store.record_sign_in("001",first,self.now)["inserted"])
        self.assertTrue(self.store.record_sign_in("002",first,self.now)["inserted"])
        self.assertFalse(self.store.record_sign_in("001",first,self.now+timedelta(minutes=1))["inserted"])
        self.assertEqual(self.store.session_rows(first)[0]["signed_at"],self.now.isoformat())
        self.store.close_session(first)
        second=self.make_session("课程二"); self.store.open_session(second,self.now)
        self.assertTrue(self.store.record_sign_in("001",second,self.now)["inserted"])
        self.assertEqual(len([row for row in self.store.session_rows(first) if row["signed_at"]]),2)

    def test_window_membership_disabled_student_and_only_one_active(self):
        sid=self.make_session()
        with self.assertRaises(ValueError): self.store.record_sign_in("001",sid,self.now)
        self.store.open_session(sid,self.now)
        with self.assertRaises(ValueError): self.store.record_sign_in("003",sid,self.now)
        self.store.set_student_active("002",False)
        with self.assertRaises(ValueError): self.store.record_sign_in("002",sid,self.now)
        with self.assertRaises(ValueError): self.store.open_session(self.make_session("另一课次"),self.now)
        with self.assertRaises(ValueError): self.store.record_sign_in("001",sid,self.now+timedelta(hours=2))
        self.assertIsNone(self.store.active_session(self.now+timedelta(hours=2)))

    def test_late_leave_absence_and_audit(self):
        sid=self.make_session(); self.store.open_session(sid,self.now)
        result=self.store.record_sign_in("001",sid,self.now+timedelta(minutes=6))
        self.assertEqual(result["status"],"late")
        with self.assertRaises(ValueError): self.store.correct_attendance(sid,"002","leave","")
        self.store.correct_attendance(sid,"002","leave","已提交请假申请")
        self.assertEqual(self.store.session_rows(sid)[1]["status"],"leave")
        self.assertTrue(any("已提交请假申请" in row["detail"] for row in self.store.audit_rows(sid)))
        self.store.close_session(sid)
        third=self.make_session("下一课程"); self.store.open_session(third,self.now); self.store.close_session(third)
        self.assertTrue(all(row["status"]=="absent" for row in self.store.session_rows(third)))

    def test_roster_snapshot_survives_student_edits_and_new_students(self):
        sid=self.make_session()
        self.store.save_student("001","改名后的学生","二班")
        self.store.save_student("004","新学生","一班")
        rows=self.store.session_rows(sid)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]["name"],"测试甲")
        self.assertEqual(rows[0]["group_name"],"一班")

    def test_concurrent_checkins_are_atomic_and_audited_once(self):
        sid=self.make_session(); self.store.open_session(sid,self.now)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results=list(executor.map(lambda _:self.store.record_sign_in("001",sid,self.now),range(8)))
        self.assertEqual(sum(row["inserted"] for row in results),1)
        self.assertEqual(sum(row["action"]=="人脸签到" for row in self.store.audit_rows(sid)),1)

    def test_batch_import_is_atomic_and_duplicate_ids_are_rejected(self):
        with self.assertRaises(ValueError): self.store.import_students([("005","新同学","一班"),("bad-id","错误","一班")])
        self.assertEqual(len(self.store.students()),3)
        with self.assertRaises(ValueError): self.store.import_students([("005","甲","一班"),("005","乙","一班")])
        self.assertEqual(self.store.import_students([("00000005","新同学","三班")]),1)

    def test_admin_setup_hash_login_lockout_and_restart(self):
        self.assertFalse(self.store.admin_exists())
        with self.assertRaises(ValueError): self.store.setup_admin("short")
        self.store.setup_admin("Example-password-2026")
        with self.assertRaises(ValueError): self.store.setup_admin("Another-password")
        self.assertTrue(self.store.authenticate("Example-password-2026",self.now))
        for _ in range(5): self.assertFalse(self.store.authenticate("wrong",self.now))
        restarted=AttendanceStore(self.store.path,seed_path=None)
        with self.assertRaises(ValueError): restarted.authenticate("Example-password-2026",self.now)
        self.assertTrue(restarted.authenticate("Example-password-2026",self.now+timedelta(minutes=2)))
        with self.store.connect() as connection:
            auth=connection.execute("SELECT value FROM metadata WHERE key='admin_auth'").fetchone()[0]
        self.assertNotIn("Example-password-2026",auth)

    def test_export_has_session_context_text_ids_and_typed_dates(self):
        sid=self.make_session(); self.store.open_session(sid,self.now); self.store.record_sign_in("001",sid,self.now)
        output=self.folder/"中文考勤.xlsx"; self.store.export_excel(output,sid)
        book=load_workbook(output)
        try:
            sheet=book.active
            self.assertIn("课程一",sheet["A1"].value)
            self.assertEqual(sheet["B5"].value,"001")
            self.assertEqual(sheet["F5"].value,self.now)
            self.assertEqual(sheet.freeze_panes,"A5")
        finally: book.close()

    def test_backup_can_be_opened_as_consistent_database(self):
        sid=self.make_session(); self.store.open_session(sid,self.now); self.store.record_sign_in("001",sid,self.now)
        model=self.folder/"face_model.yml"; model.write_bytes(b"synthetic backup fixture")
        manifest=self.folder/"model_manifest.json"; manifest.write_text("{}",encoding="utf-8")
        dataset=self.folder/"dataset"; dataset.mkdir()
        save_gray(dataset/"001.0.jpg",np.random.default_rng(1).integers(50,200,size=(160,160),dtype=np.uint8))
        output=self.folder/"backup.zip"
        with patch("attendance_store.MODEL_PATH",model), patch("attendance_store.MODEL_MANIFEST_PATH",manifest), patch("attendance_store.DATASET_DIR",dataset):
            self.store.backup_archive(output)
        with zipfile.ZipFile(output) as archive:
            restored=self.folder/"restored.db"; restored.write_bytes(archive.read("attendance.db"))
            self.assertEqual(archive.read("face_model.yml"),model.read_bytes())
            self.assertEqual(archive.read("dataset/001.0.jpg"),(dataset/"001.0.jpg").read_bytes())
        with sqlite3.connect(restored) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0],"ok")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM checkins").fetchone()[0],1)
        connection.close()

    def test_csv_import_keeps_leading_zero_and_checks_headers(self):
        path=self.folder/"students.csv"; path.write_text("学号,姓名,班级\n00001,新学生,三班\n",encoding="utf-8-sig")
        self.assertEqual(read_student_import(path)[0][0],"00001")
        path.write_text("错误表头\n",encoding="utf-8")
        with self.assertRaises(ValueError): read_student_import(path)

    def test_existing_daily_database_migrates_once_without_loss(self):
        path=self.folder/"old.db"
        with sqlite3.connect(path) as connection:
            connection.executescript("""CREATE TABLE students(student_id TEXT PRIMARY KEY,name TEXT NOT NULL);
                INSERT INTO students VALUES('123','旧学生');
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO metadata VALUES('seed_imported','1');
                CREATE TABLE attendance(id INTEGER PRIMARY KEY,student_id TEXT,day TEXT,signed_at TEXT,UNIQUE(student_id,day));
                INSERT INTO attendance VALUES(1,'123','2022-11-22','2022-11-22T22:04:56');""")
        connection.close()
        migrated=AttendanceStore(path,seed_path=None)
        self.assertEqual(len(migrated.sessions()),1)
        sid=migrated.sessions()[0]["id"]
        self.assertEqual(migrated.session_rows(sid)[0]["signed_at"],"2022-11-22T22:04:56")
        self.assertEqual(len(AttendanceStore(path,seed_path=None).sessions()),1)
        self.assertEqual(migrated.students()[0]["group_name"],"未分班")


class RecognitionTests(unittest.TestCase):
    def test_temporal_confirmation_and_identity_switch(self):
        streak=RecognitionStreak(required=3,minimum_seconds=1)
        for index in range(10): self.assertFalse(streak.update("001" if index%2 else "002",index*.2))
        streak.update(None,2.9)
        self.assertFalse(streak.update("001",3))
        self.assertFalse(streak.update("001",3.1))
        self.assertTrue(streak.update("001",4.2))
        self.assertFalse(streak.update(None,4.3))

    def test_quality_gates(self):
        self.assertTrue(quality_hint(np.zeros((160,160),dtype=np.uint8)))
        self.assertTrue(quality_hint(np.full((160,160),128,dtype=np.uint8)))
        self.assertTrue(quality_hint(np.full((50,50),128,dtype=np.uint8)))
        self.assertEqual(quality_hint(np.random.default_rng(2).integers(60,190,size=(160,160),dtype=np.uint8)),"")

    def test_training_roundtrip_and_stale_model_rejection(self):
        with tempfile.TemporaryDirectory(prefix="model-test-") as temporary:
            folder=Path(temporary)/"中文目录"; folder.mkdir()
            student_id="0012345678901234567890"
            sample=folder/f"{student_id}.0.jpg"
            save_gray(sample,np.random.default_rng(1).integers(50,200,size=(160,160),dtype=np.uint8))
            model=folder/"model.yml"; train_model(folder,model,{student_id})
            recognizer=load_recognizer(model)
            prediction,distance=recognizer.predict(preprocess(read_gray(sample)))
            self.assertEqual(recognizer.getLabelInfo(prediction),student_id)
            self.assertLess(distance,1)
            self.assertTrue(model_health({student_id},model,folder)[0])
            sample.touch()
            # 保证文件时间不同，即使文件系统时间粒度较粗。
            import os
            os.utime(sample,ns=(sample.stat().st_atime_ns,sample.stat().st_mtime_ns+1000000000))
            self.assertFalse(model_health({student_id},model,folder)[0])

    def test_resource_lock_is_exclusive_and_releases(self):
        with tempfile.TemporaryDirectory(prefix="lease-test-") as temporary:
            path=Path(temporary)/"camera.lock"
            with ResourceLease(path):
                with self.assertRaises(RuntimeError):
                    with ResourceLease(path): pass
            with ResourceLease(path): pass

    def test_detector_loads_under_chinese_project_path(self):
        self.assertFalse(load_classifier().empty())


if __name__=="__main__": unittest.main()
