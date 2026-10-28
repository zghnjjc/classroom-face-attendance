"""首次使用查看/重置、只读启动、窗口交接失败与定时器回收。"""

import gc
import json
import sqlite3
import tkinter as tk
from contextlib import contextmanager
from unittest.mock import MagicMock, Mock, patch

import test_first_use
from classroom_attendance.ui.admin_app import AdminApp, AdminLogin
from classroom_attendance.core.app_startup import StartupController
from classroom_attendance.onboarding.first_use import FirstUseService, SETUP_KEY
from classroom_attendance.onboarding.first_use_ui import FirstUseWizard
from classroom_attendance.ui.launcher import InitialLauncher, Launcher


class PreparationEdges(test_first_use.SetupFixture):
    def complete(self):
        self.begin()
        self.service.record_camera('skipped', 0)
        self.service.record_roster('skipped')
        self.service.finish()

    def test_completed_prepare_uses_only_read_connection_and_preserves_metadata(self):
        self.complete()
        with self.store.connect() as connection:
            before = self.store._get(connection, SETUP_KEY)
        with patch.object(self.store, 'connect', wraps=self.store.connect) as connect:
            self.assertTrue(self.service.prepare()['completed'])
        self.assertTrue(all(not call.kwargs.get('write', False) for call in connect.call_args_list))
        with self.store.connect() as connection:
            self.assertEqual(self.store._get(connection, SETUP_KEY), before)

    def test_existing_user_without_progress_does_not_request_write_connection(self):
        self.store.setup_admin(test_first_use.PASSWORD)
        with patch.object(self.store, 'connect', wraps=self.store.connect) as connect:
            self.assertIsNone(self.service.prepare())
        self.assertTrue(all(not call.kwargs.get('write', False) for call in connect.call_args_list))

    def test_unchanged_incomplete_progress_is_read_only(self):
        self.begin()
        with patch.object(self.store, 'connect', wraps=self.store.connect) as connect:
            self.assertFalse(self.service.prepare()['completed'])
        self.assertTrue(all(not call.kwargs.get('write', False) for call in connect.call_args_list))

    def test_prepare_rechecks_progress_after_acquiring_write_transaction(self):
        original = self.store.connect
        completed = dict(version=1, completed=True, camera='skipped', roster='skipped',
                         directory=str(self.store.path.parent.resolve()), updated_at='synthetic-concurrent-value')
        @contextmanager
        def raced(write=False):
            if write:
                with original(write=True) as connection:
                    self.store._set(connection, SETUP_KEY, json.dumps(completed))
            with original(write=write) as connection:
                yield connection
        with patch.object(self.store, 'connect', side_effect=raced), patch.object(self.service, '_save') as save:
            self.assertEqual(self.service.prepare(), completed)
        save.assert_not_called()

    def test_explicit_manual_reset_still_restarts_completed_progress(self):
        self.complete()
        self.store.save_student('001', '合成学生')
        state = self.service.prepare(manual=True)
        self.assertFalse(state['completed'])
        self.assertEqual((state['camera'], state['roster']), ('pending', 'pending'))
        self.assertEqual(len(self.store.students()), 1)
        self.assertTrue(self.store.authenticate(test_first_use.PASSWORD))


class LoginAndWizardEdges(test_first_use.SetupFixture):
    def setUp(self):
        super().setUp()
        self.root = tk.Tk()
        self.bad_callbacks = []
        self.messagebox = patch('classroom_attendance.ui.ui_common.messagebox.showerror').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.cleanup_root)

    def cleanup_root(self):
        try:
            self.root.update_idletasks()
            for timer in self.root.tk.call('after', 'info'):
                self.root.after_cancel(timer)
            self.root.destroy()
        except tk.TclError:
            pass
        gc.collect()

    def complete(self):
        self.begin()
        self.service.record_camera('skipped', 0)
        self.service.record_roster('skipped')
        self.service.finish()

    def raw_progress(self):
        with self.store.connect() as connection:
            return self.store._get(connection, SETUP_KEY)

    def wizard(self, manual=False, on_finish=None):
        return FirstUseWizard(self.root, self.store, on_finish or (lambda target: None),
                              async_io=False, manual=manual, worker_factory=test_first_use.FakePreview)

    def fail_partial(self, root, *args, **kwargs):
        partial = tk.Frame(root)
        partial.pack()
        root.after(1, lambda: self.bad_callbacks.append(True))
        root.protocol('WM_DELETE_WINDOW', lambda: None)
        root.bind('<Return>', lambda event: None)
        root.title('synthetic-partial-destination')
        raise RuntimeError('synthetic-construction-failure')

    def test_completed_review_then_close_does_not_restart_next_launch(self):
        self.complete()
        before = self.raw_progress()
        wizard = self.wizard(manual=True)
        self.assertTrue(wizard.reviewing)
        self.assertEqual(wizard.page, 'finish')
        self.assertEqual(self.raw_progress(), before)
        wizard.close()
        self.assertEqual(self.raw_progress(), before)
        self.root = tk.Tk()
        gate = InitialLauncher(self.root, self.store, async_io=False)
        self.assertIsInstance(gate.application, Launcher)

    def test_legacy_user_review_does_not_create_incomplete_progress(self):
        self.store.setup_admin(test_first_use.PASSWORD)
        wizard = self.wizard(manual=True)
        self.assertTrue(wizard.reviewing)
        self.assertIsNone(self.raw_progress())
        wizard.close()
        self.assertIsNone(self.service.prepare())

    def test_dismissing_restart_confirmation_preserves_completed_progress(self):
        self.complete()
        before = self.raw_progress()
        wizard = self.wizard(manual=True)
        with patch('classroom_attendance.onboarding.first_use_ui.messagebox.askyesno', return_value=False):
            wizard.restart()
        self.assertTrue(wizard.reviewing)
        self.assertEqual(self.raw_progress(), before)

    def test_confirmed_restart_creates_resumable_steps_and_keeps_password(self):
        self.complete()
        wizard = self.wizard(manual=True)
        with patch('classroom_attendance.onboarding.first_use_ui.messagebox.askyesno', return_value=True):
            wizard.restart()
        self.assertFalse(wizard.reviewing)
        self.assertEqual(wizard.page, 'camera')
        wizard.close()
        self.assertFalse(self.service.prepare()['completed'])
        self.assertTrue(self.store.authenticate(test_first_use.PASSWORD))

    def test_leaving_review_via_finish_does_not_write_progress(self):
        self.complete()
        before = self.raw_progress()
        targets = []
        wizard = self.wizard(manual=True, on_finish=targets.append)
        with patch.object(wizard.service, 'finish') as finish:
            wizard.finish('home')
        finish.assert_not_called()
        self.assertEqual(targets, ['home'])
        self.assertEqual(self.raw_progress(), before)

    def test_executed_timers_are_removed_and_pending_timers_remain_cancellable(self):
        self.begin()
        wizard = self.wizard()
        seen = []
        for unused in range(600):
            wizard.schedule(0, lambda: seen.append(True))
        self.root.update()
        self.assertEqual(len(seen), 600)
        self.assertEqual(len(wizard.after_ids), 0)
        token = wizard.schedule(60000, lambda: None)
        self.assertEqual(wizard.after_ids, {token})
        wizard.clear_timers()
        self.assertFalse(wizard.after_ids)
        self.assertNotIn(token, self.root.tk.call('after', 'info'))

    def test_launcher_passes_known_progress_to_login_without_second_prepare(self):
        original = FirstUseService.prepare
        with patch('classroom_attendance.onboarding.first_use.FirstUseService.prepare', autospec=True, side_effect=original) as prepare:
            gate = InitialLauncher(self.root, self.store, async_io=False)
        self.assertIsInstance(gate.application, AdminLogin)
        self.assertEqual(prepare.call_count, 1)

    def test_completed_login_page_renders_while_another_connection_has_write_lock(self):
        self.complete()
        writer = sqlite3.connect(self.store.path)
        try:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE metadata SET value=value WHERE key='first_use_v1'")
            login = AdminLogin(self.root, self.store, async_io=False, onboarding=True)
            self.assertTrue(login.frame.winfo_exists())
            self.assertTrue(login.setup_progress['completed'])
            self.assertTrue(login.auth_status['configured'])
        finally:
            writer.rollback()
            writer.close()

    def test_management_construction_failure_restores_login_and_retry_succeeds(self):
        self.store.setup_admin(test_first_use.PASSWORD)
        login = AdminLogin(self.root, self.store, async_io=False)
        children = set(self.root.winfo_children())
        return_binding = self.root.bind('<Return>')
        close_binding = self.root.protocol('WM_DELETE_WINDOW')
        login.password.set(test_first_use.PASSWORD)
        with patch('classroom_attendance.ui.admin_app.AdminApp', side_effect=self.fail_partial):
            login.submit()
        self.root.update()
        self.assertFalse(login.login_complete)
        self.assertTrue(login.frame.winfo_ismapped())
        self.assertTrue(login.error.get())
        self.assertEqual(set(self.root.winfo_children()), children)
        self.assertEqual(self.root.bind('<Return>'), return_binding)
        self.assertEqual(self.root.protocol('WM_DELETE_WINDOW'), close_binding)
        self.assertEqual(self.bad_callbacks, [])
        login.password.set(test_first_use.PASSWORD)
        login.submit()
        self.assertTrue(login.login_complete)
        self.assertIsInstance(login.application, AdminApp)

    def test_new_password_persists_but_failed_wizard_can_retry_via_login(self):
        login = AdminLogin(self.root, self.store, async_io=False, onboarding=True)
        login.password.set(test_first_use.PASSWORD)
        login.confirm.set(test_first_use.PASSWORD)
        with patch('classroom_attendance.ui.admin_app.FirstUseWizard', side_effect=self.fail_partial):
            login.submit()
        self.root.update()
        self.assertFalse(login.login_complete)
        self.assertFalse(login.is_setup)
        self.assertTrue(login.frame.winfo_ismapped())
        self.assertTrue(login.error.get())
        self.assertTrue(self.store.authenticate(test_first_use.PASSWORD))
        login.password.set(test_first_use.PASSWORD)
        login.submit()
        self.assertTrue(login.login_complete)
        self.assertIsInstance(login.application, FirstUseWizard)

    def test_wizard_finish_failure_keeps_completed_checklist_and_retry_is_idempotent(self):
        self.begin()
        self.service.record_camera('skipped', 0)
        self.service.record_roster('skipped')
        ready = []
        def complete(target):
            if not ready:
                ready.append(False)
                self.fail_partial(self.root)
            ready.append(True)
            page = tk.Frame(self.root)
            page.pack()
        wizard = self.wizard(on_finish=complete)
        wizard.finish('home')
        self.root.update()
        self.assertFalse(wizard.closed)
        self.assertTrue(wizard.frame.winfo_ismapped())
        self.assertTrue(wizard.reviewing)
        self.assertTrue(wizard.message.get())
        self.assertTrue(self.service.prepare()['completed'])
        self.assertEqual(self.bad_callbacks, [])
        wizard.finish('home')
        self.assertTrue(wizard.closed)
        self.assertEqual(ready, [False, True])
        self.assertEqual(sum(row['action'] == '完成首次使用向导' for row in self.store.audit_rows()), 1)

    def test_initial_launcher_construction_failure_keeps_retry_page(self):
        with patch('classroom_attendance.ui.admin_app.AdminLogin', side_effect=self.fail_partial):
            gate = InitialLauncher(self.root, self.store, async_io=False)
        self.root.update()
        self.assertFalse(gate.closed)
        self.assertTrue(gate.frame.winfo_ismapped())
        self.assertTrue(gate.message.get())
        self.assertEqual(self.bad_callbacks, [])
        gate.prepare()
        self.assertTrue(gate.closed)
        self.assertIsInstance(gate.application, AdminLogin)

    def test_startup_destination_failure_keeps_retry_and_releases_lease(self):
        lease = MagicMock()
        self.store._instance_lease = lease
        def successful(root, store):
            frame = tk.Frame(root)
            frame.pack()
            return frame
        factory = Mock(side_effect=[RuntimeError('synthetic-destination'), None])
        boot = StartupController(self.root, factory, store_factory=lambda: self.store, async_io=False)
        self.assertFalse(boot.closed)
        self.assertTrue(boot.frame.winfo_exists())
        self.assertEqual(boot.retry.cget('state'), 'normal')
        lease.__exit__.assert_called_once()
        boot.factory = successful
        boot.probe()
        self.assertTrue(boot.closed)

    def test_manual_wizard_construction_failure_removes_modal_and_partial_timers(self):
        self.store.setup_admin(test_first_use.PASSWORD)
        admin = AdminApp(self.root, self.store, authenticated=True, async_io=False)
        children = set(self.root.winfo_children())
        with patch('classroom_attendance.ui.admin_app.FirstUseWizard', side_effect=self.fail_partial):
            admin.open_setup()
        self.root.update()
        self.assertEqual(set(self.root.winfo_children()), children)
        self.assertIsNone(self.root.grab_current())
        self.assertEqual(self.bad_callbacks, [])
        self.messagebox.assert_called_once()
