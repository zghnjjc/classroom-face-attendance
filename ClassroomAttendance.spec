# 仅收集程序和显式列出的资源，不包含任何运行数据库、样本、模型或日志。
from pathlib import Path
import sys
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH)
resource_data = [(str(root / 'resources' / name), 'resources') for name in
                 ('haarcascade_frontalface_default.xml', 'initial_data.json')]
resource_data += [(str(root / name), '.') for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md')]
for package in ('numpy', 'opencv-contrib-python', 'openpyxl', 'et_xmlfile', 'pyinstaller'):
    resource_data += copy_metadata(package)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    resource_data.append((str(python_license), 'licenses/python'))
for license_file in Path(sys.base_prefix).rglob('license.terms'):
    resource_data.append((str(license_file), 'licenses/' + license_file.parent.name))
a = Analysis(
    [str(root / 'app_entry.py')], pathex=[str(root)],
    binaries=[],
    datas=resource_data,
    hiddenimports=['classroom_attendance.ui.student_app', 'classroom_attendance.ui.admin_app', 'classroom_attendance.storage.recovery_app', 'classroom_attendance.maintenance.runtime_probe'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ClassroomAttendance',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ClassroomAttendance')
