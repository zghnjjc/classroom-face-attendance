"""离线管理密码重置；须关闭全部程序，并明确确认后才操作指定数据目录。"""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from attendance_store import AttendanceStore
from resource_lock import ResourceLease
from runtime_guard import MaintenanceLease


def reset_admin(data_dir, *, confirmed=False):
    if not confirmed:
        raise ValueError("重置前必须明确确认并关闭全部程序。")
    directory=Path(data_dir).resolve()
    database=directory/"attendance.db"
    if not database.is_file():
        raise ValueError("指定目录没有 attendance.db，不会创建新库。")
    with MaintenanceLease(directory),ResourceLease(directory/".camera.lock"),ResourceLease(directory/".samples.lock"):
        store=AttendanceStore(database,seed_path=None)
        backup=directory/("password_reset_"+datetime.now().strftime("%Y%m%d_%H%M%S_%f")+".db")
        try:
            with store.connect() as source:
                target=sqlite3.connect(backup)
                try: source.backup(target)
                finally: target.close()
            with store.connect(write=True) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM metadata WHERE key='admin_auth'")
                store._audit(connection,"离线重置管理密码",detail="持有本机文件权限的操作者确认重置")
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
        return backup


def main():
    parser=argparse.ArgumentParser(description="关闭所有签到程序后，备份并离线重置管理密码")
    parser.add_argument("--data-dir",type=Path,required=True)
    parser.add_argument("--confirm-reset",action="store_true")
    args=parser.parse_args()
    backup=reset_admin(args.data_dir,confirmed=args.confirm_reset)
    print("管理口令已重置；下次打开教师端需重新设置。")
    print("旧库备份包含敏感数据，请妥善加密保存："+str(backup))


def gui_main(data_dir):
    import tkinter as tk
    from tkinter import messagebox
    from app_errors import user_message
    root = tk.Tk()
    root.withdraw()
    try:
        if not messagebox.askyesno('离线重置管理密码',
                '此操作依赖本机文件权限，不验证原密码。请先关闭全部签到窗口。\n将先保存数据库备份，再清除管理密码；下次进入教师端需重新设置。\n确认对当前数据目录执行重置？\n' + str(data_dir), parent=root):
            return 0
        backup = reset_admin(data_dir, confirmed=True)
        messagebox.showinfo('重置完成','请重新打开教师端设置管理密码。原数据库备份含敏感数据，保存在：\n' + str(backup),parent=root)
        return 0
    except Exception as error:
        messagebox.showerror('重置未完成',user_message(error),parent=root)
        return 1
    finally:
        root.destroy()


if __name__=="__main__":
    main()
