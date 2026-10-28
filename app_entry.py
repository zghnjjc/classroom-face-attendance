"""源码与发行版共用入口；先处理冻结子进程，再导入界面和模型依赖。"""

import os
import sys
from pathlib import Path


def role_command(role, data_dir):
    if role not in {"launcher", "student", "admin", "restore"}:
        raise ValueError("不支持的程序入口。")
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).resolve()))
    return command + ["--role", role, "--data-dir", str(Path(data_dir).resolve())]


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="课堂签到：离线单机考勤")
    parser.add_argument("--role", choices=("launcher", "student", "admin", "restore"), default="launcher")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--self-test", type=Path, metavar="REPORT_JSON")
    parser.add_argument("--reset-admin", action="store_true", help="关闭其他窗口后，在确认对话框中备份并重置管理密码")
    args = parser.parse_args(argv)
    if args.data_dir is not None:
        os.environ["CLASSROOM_DATA_DIR"] = str(args.data_dir.expanduser().resolve())
    if args.self_test:
        from classroom_attendance.maintenance.runtime_probe import run_self_test
        return run_self_test(args.self_test)
    if args.reset_admin:
        from classroom_attendance.maintenance.reset_admin import gui_main
        from classroom_attendance.core.app_config import DATA_DIR
        return gui_main(DATA_DIR)
    if args.role == "student":
        from classroom_attendance.ui.student_app import main as launch
    elif args.role == "admin":
        from classroom_attendance.ui.admin_app import main as launch
    elif args.role == "restore":
        from classroom_attendance.storage.recovery_app import main as launch
    else:
        from classroom_attendance.ui.launcher import main as launch
    launch()
    return 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
