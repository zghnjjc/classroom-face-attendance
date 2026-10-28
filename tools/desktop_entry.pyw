"""Desktop shortcut entry for the installed Python and this project's virtual environment."""

import argparse
import ctypes
from pathlib import Path
import subprocess


def main(argv=None):
    parser = argparse.ArgumentParser(description='Classroom Attendance desktop entry')
    parser.add_argument('--role', choices=('launcher', 'admin', 'student'), default='launcher')
    args = parser.parse_args(argv)
    project = Path(__file__).resolve().parent.parent
    runtime = project / '.venv' / 'Scripts' / 'pythonw.exe'
    entry = project / 'app_entry.py'
    try:
        if not runtime.is_file() or not entry.is_file():
            raise FileNotFoundError('找不到项目虚拟环境或 app_entry.py，请在完整项目目录中重新生成快捷方式。')
        subprocess.Popen([str(runtime), str(entry), '--role', args.role], cwd=project)
    except OSError as error:
        ctypes.windll.user32.MessageBoxW(None, str(error), '课堂签到 · 启动失败', 0x10)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
