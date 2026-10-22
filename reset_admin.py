"""离线管理密码重置；须关闭全部程序，并明确确认后才操作指定数据目录。"""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

from attendance_store import AttendanceStore
from resource_lock import ResourceLease


def reset_admin(data_dir, *, confirmed=False):
    if not confirmed:
        raise ValueError("重置前必须明确确认并关闭全部程序。")
    directory=Path(data_dir).resolve()
    database=directory/"attendance.db"
    if not database.is_file():
        raise ValueError("指定目录没有 attendance.db，不会创建新库。")
    with ResourceLease(directory/".camera.lock"),ResourceLease(directory/".samples.lock"):
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


if __name__=="__main__":
    main()
