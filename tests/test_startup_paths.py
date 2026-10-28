"""用户目录不可写时仍能持久选择原数据，且不会静默新建替代数据库。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_config
import app_startup
from data_migration import LegacyDataFound


class StartupPathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='startup-paths-')
        self.addCleanup(self.temporary.cleanup)
        # Keep fault-injection paths consistent with resolved production paths.
        self.folder = Path(self.temporary.name).resolve()
        self.program = self.folder / 'program'
        self.user = self.folder / 'user'
        self.data = self.folder / 'existing-data'
        self.program.mkdir()
        self.data.mkdir()
        for context in (patch('app_config.PROGRAM_DIR', self.program),
                        patch('app_config.default_data_dir', return_value=self.user),
                        patch.dict(os.environ, {'CLASSROOM_DATA_DIR': ''})):
            context.start()
            self.addCleanup(context.stop)

    def preference(self, path, directory):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'directory': str(directory)}), encoding='utf-8')

    def test_user_directory_denied_saves_portable_preference_and_keeps_data_location(self):
        original = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path == self.user:
                raise PermissionError('synthetic denied user directory')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'mkdir', mkdir):
            app_config.save_data_location(self.data)
        self.assertEqual(app_config.configured_data_dir(), self.data)
        self.assertTrue((self.program / 'data_location.json').is_file())
        self.assertFalse((self.user / 'data_location.json').exists())
        self.assertFalse((self.data / 'attendance.db').exists())

    def test_portable_selection_overrides_old_user_preference_and_remains_editable(self):
        self.preference(self.user / 'data_location.json', self.folder / 'old-data')
        self.preference(self.program / 'data_location.json', self.data)
        self.assertEqual(app_config.configured_data_dir(), self.data)
        changed = self.folder / 'changed-data'
        app_config.save_data_location(changed)
        self.assertEqual(app_config.configured_data_dir(), changed)
        self.assertEqual(json.loads((self.user / 'data_location.json').read_text())['directory'], str(self.folder / 'old-data'))

    def test_environment_directory_still_overrides_both_preferences(self):
        self.preference(self.program / 'data_location.json', self.data)
        self.preference(self.user / 'data_location.json', self.folder / 'other-data')
        explicit = self.folder / 'explicit-data'
        with patch.dict(os.environ, {'CLASSROOM_DATA_DIR': str(explicit)}):
            self.assertEqual(app_config.configured_data_dir(), explicit)

    def test_denied_primary_file_replacement_also_uses_portable_preference(self):
        self.preference(self.user / 'data_location.json', self.folder / 'old-data')
        original = Path.replace

        def replace(path, target):
            if Path(target) == self.user / 'data_location.json':
                raise PermissionError('synthetic occupied preference')
            return original(path, target)

        with patch.object(Path, 'replace', replace):
            app_config.save_data_location(self.data)
        self.assertEqual(app_config.configured_data_dir(), self.data)
        self.assertFalse((self.user / 'data_location.tmp').exists())

    def test_both_locations_denied_does_not_report_saved_or_create_database(self):
        with patch.object(Path, 'mkdir', side_effect=PermissionError('synthetic denied')):
            with self.assertRaises(PermissionError):
                app_config.save_data_location(self.data)
        self.assertFalse((self.program / 'data_location.json').exists())
        self.assertFalse((self.data / 'attendance.db').exists())

    def test_invalid_portable_preference_does_not_silently_open_other_data(self):
        self.preference(self.user / 'data_location.json', self.data)
        (self.program / 'data_location.json').write_text('{invalid', encoding='utf-8')
        with self.assertRaises(ValueError):
            app_config.configured_data_dir()

    def test_legacy_detection_precedes_any_attempt_to_create_default_directory(self):
        (self.program / 'attendance.db').write_bytes(b'synthetic presence marker')
        with patch('app_startup.PROGRAM_DIR', self.program), \
                patch('app_startup.DATABASE_PATH', self.user / 'attendance.db'), \
                patch('app_startup.HAS_DATA_LOCATION_PREFERENCE', False), \
                patch('app_startup.DATA_DIRECTORY_ERROR', ''), \
                patch('app_startup.InstanceLease', side_effect=PermissionError()) as lease:
            with self.assertRaises(LegacyDataFound):
                app_startup.checked_store()
        lease.assert_not_called()
        self.assertFalse(self.user.exists())
