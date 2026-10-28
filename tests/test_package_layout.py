"""Moved modules must import and find resources without depending on the working directory."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class PackageLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='package-layout-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.data = self.directory / 'isolated-data'
        self.environment = dict(os.environ, CLASSROOM_DATA_DIR=str(self.data),
                                PYTHONPATH=str(PROJECT), PYTHONUTF8='1')

    def run_python(self, *arguments):
        result = subprocess.run([sys.executable, *arguments], cwd=self.directory,
                                env=self.environment, capture_output=True,
                                text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_entry_help_works_outside_project_without_creating_data(self):
        output = self.run_python(str(PROJECT / 'app_entry.py'), '--help')
        self.assertIn('--role', output)
        self.assertIn('--data-dir', output)
        self.assertFalse(self.data.exists())

    def test_all_feature_modules_import_without_opening_windows_or_creating_data(self):
        output = self.run_python('-c', '''
import importlib
import pkgutil
import tkinter as tk
import classroom_attendance
modules = list(pkgutil.walk_packages(classroom_attendance.__path__, classroom_attendance.__name__ + '.'))
for module in modules:
    importlib.import_module(module.name)
assert tk._default_root is None, 'Import opened a window'
print(len(modules))
''')
        self.assertGreater(int(output.strip()), 10)
        self.assertFalse(self.data.exists())

    def test_resources_and_data_paths_are_independent_of_working_directory(self):
        output = self.run_python('-c', '''
import json
from classroom_attendance.core.app_config import PROGRAM_DIR, CASCADE_PATH, SEED_PATH, DATA_DIR, DATABASE_PATH
from classroom_attendance.faces.face_utils import load_classifier
assert not load_classifier().empty()
assert json.loads(SEED_PATH.read_text(encoding='utf-8'))['students'] == []
print(json.dumps({'program': str(PROGRAM_DIR), 'cascade': str(CASCADE_PATH),
                  'seed': str(SEED_PATH), 'data': str(DATA_DIR), 'database': str(DATABASE_PATH)}))
''')
        paths = json.loads(output)
        self.assertEqual(Path(paths['program']), PROJECT)
        self.assertEqual(Path(paths['cascade']).parent, PROJECT / 'resources')
        self.assertEqual(Path(paths['seed']).parent, PROJECT / 'resources')
        self.assertEqual(Path(paths['data']), self.data)
        self.assertEqual(Path(paths['database']), self.data / 'attendance.db')
        self.assertFalse(self.data.exists())


if __name__ == '__main__':
    unittest.main()
