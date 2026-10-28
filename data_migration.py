"""显式复制旧数据到新目录；原目录不删除、不就地迁移数据库。"""

import tempfile
from pathlib import Path

from app_errors import ValidationError
from backup_service import SnapshotSource, create_backup, inspect_backup, restore_backup
from resource_lock import ResourceLease
from runtime_guard import MaintenanceLease


class LegacyDataFound(ValidationError):
    def __init__(self, source):
        self.source = Path(source)
        super().__init__("程序目录检测到旧版运行数据，尚未创建新数据库。请关闭旧版窗口后迁移，或退出并指定原数据目录。")


def migrate_legacy(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("迁移源目录与目标目录不能相同或互相包含。")
    if not (source / "attendance.db").is_file():
        raise ValueError("旧数据目录没有 attendance.db，不会创建新库。")
    if any((target / name).exists() for name in ("attendance.db", "dataset", "face_model.yml", "model_manifest.json")):
        raise ValueError("目标目录已有运行数据，请改用备份恢复流程，避免覆盖。")
    target.parent.mkdir(parents=True, exist_ok=True)
    with MaintenanceLease(source), ResourceLease(source / ".camera.lock"), ResourceLease(source / ".samples.lock"):
        with tempfile.TemporaryDirectory(prefix="classroom-migration-", dir=target.parent) as temporary:
            archive = Path(temporary) / "migration.zip"
            create_backup(SnapshotSource(source / "attendance.db"), archive,
                          model=source / "face_model.yml", manifest=source / "model_manifest.json", dataset=source / "dataset")
            summary = inspect_backup(archive)
            # 授权在维护锁内再次检查，防止预检后目标目录被另一个窗口初始化。
            def ensure_empty(directory):
                if any((directory / name).exists() for name in ("attendance.db", "dataset", "face_model.yml", "model_manifest.json")):
                    raise ValueError("目标目录已出现数据，迁移已停止。")
            return restore_backup(archive, target, expected_sha256=summary["archive_sha256"], authorize=ensure_empty)
