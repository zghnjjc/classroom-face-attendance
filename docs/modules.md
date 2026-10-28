# 模块导航

想修改某个功能时，先找下面对应的目录。业务逻辑和配套界面尽量放在同一功能目录，测试统一放在 `tests/`，方便运行全套回归。

## 目录总览

```text
classroom-face-attendance/
├─ app_entry.py              # 统一启动入口，命令和桌面快捷方式继续使用它
├─ classroom_attendance/     # 应用代码
│  ├─ ui/                    # 启动页、教师端、学生端、共用控件
│  ├─ onboarding/            # 首次使用向导、数据位置选择
│  ├─ attendance/            # 人工点名、查询、更正、统计报表
│  ├─ courses/               # 课程模板、周期排课、课次时间
│  ├─ dashboard/             # 今日课堂、课后核对
│  ├─ faces/                 # 样本、训练、人脸授权与清理
│  ├─ camera/                # 摄像头驱动、采集识别、相机检测
│  ├─ storage/               # 数据库、数据迁移、备份与恢复
│  ├─ maintenance/           # 维护工具、密码重置、发行包自检
│  └─ core/                  # 配置、启动检查、任务、异常与锁
├─ resources/               # Haar 检测资源和空初始化模板
├─ tests/                   # 自动化回归测试
├─ tools/                   # 构建、桌面快捷方式等辅助脚本
│  └─ launchers/            # 可选的旧 BAT 启动脚本
├─ docs/                    # 使用说明、设计和维护文档
├─ requirements*.txt        # 运行依赖、锁定版本、构建依赖
└─ ClassroomAttendance.spec # PyInstaller 构建配置
```

根目录保留启动入口、依赖清单、构建配置、README、许可证及协作文件。运行数据由程序的数据位置设置管理，不属于源码目录结构。

## 按功能找代码

下表的文件名均相对于对应目录；测试名相对于 `tests/`。

| 要迭代的功能 | 代码目录及主要文件 | 先运行的测试 |
| --- | --- | --- |
| 启动页、教师或学生主窗口、共用控件 | [ui](../classroom_attendance/ui/)：`launcher.py`、`admin_app.py`、`student_app.py`、`ui_common.py` | `test_gui.py`、`test_ui_experience.py`、`test_login_edges.py` |
| 首次配置与数据位置 | [onboarding](../classroom_attendance/onboarding/)：`first_use.py`、`first_use_ui.py`、`storage_welcome.py` | `test_first_use.py`、`test_startup_paths.py` |
| 点名、考勤筛选、报表 | [attendance](../classroom_attendance/attendance/)：`manual_attendance.py`、`attendance_filters.py`、`attendance_reports.py`、`attendance_reports_ui.py` | `test_manual_attendance.py`、`test_attendance_filters.py`、`test_attendance_reports.py` |
| 课次时间与周期排课 | [courses](../classroom_attendance/courses/)：`session_times.py`、`course_schedule.py`、`course_schedule_ui.py` | `test_session_times.py`、`test_course_schedule.py` |
| 今日课堂与课后核对 | [dashboard](../classroom_attendance/dashboard/)：`teacher_dashboard.py`、`dashboard_ui.py` | `test_teacher_dashboard.py` |
| 人脸训练、授权、撤回与清理 | [faces](../classroom_attendance/faces/)：`train.py`、`face_lifecycle.py`、`face_lifecycle_ui.py`、`face_privacy.py`、`face_delete_recovery.py` | `test_face_lifecycle.py`、`test_freeze_fixes.py`、`test_system.py` |
| 相机连接、采集和识别 | [camera](../classroom_attendance/camera/)：`camera_device.py`、`camera_service.py`、`camera_diagnostics.py` | `test_regressions.py`、`test_maintenance_tools.py` |
| 数据库、备份、恢复和迁移 | [storage](../classroom_attendance/storage/)：`attendance_store.py`、`backup_service.py`、`automatic_backup.py`、`automatic_backup_ui.py`、`recovery_app.py`、`data_migration.py` | `test_business_rules.py`、`test_deployment.py`、`test_automatic_backup.py` |
| 现场维护与密码重置 | [maintenance](../classroom_attendance/maintenance/)：`maintenance_tools.py`、`maintenance_ui.py`、`reset_admin.py`、`runtime_probe.py` | `test_maintenance_tools.py`、`test_recovery.py` |
| 配置、启动检查、后台任务和锁 | [core](../classroom_attendance/core/)：`app_config.py`、`app_startup.py`、`task_runtime.py`、`app_errors.py`、`runtime_guard.py`、`resource_lock.py`、`file_integrity.py` | `test_package_layout.py`、`test_startup_paths.py`、`test_A_fixes.py` |

`storage/attendance_store.py` 目前仍集中实现数据库及部分考勤写入规则；`ui/admin_app.py` 负责教师主窗口组装。修改一个功能时，也可能需要同步这两个文件。本次整理归类文件并修正模块引用，保留现有业务边界。

## 开发约定

- 新增功能优先放进对应目录；同一功能的界面和业务文件可以相邻，公共基础能力放在 `core/`。
- 跨目录使用完整包名导入，例如 `from classroom_attendance.storage.attendance_store import AttendanceStore`；测试中的 `patch()` 目标也使用完整包名。
- 统一从项目根目录运行 `python app_entry.py`。教师、学生和恢复窗口分别使用 `--role admin`、`--role student`、`--role restore`，不要直接执行包内的界面文件。
- 确需运行维护命令时使用模块方式，例如 `python -m classroom_attendance.maintenance.reset_admin --help`。执行实际密码重置前仍须关闭窗口、选对数据目录并明确确认。
- `resources/` 只放程序静态资源。不要将人脸样本、训练模型、数据库或备份放进去；这些资料保存在独立运行数据目录。
- 涉及新增静态资源或动态导入时同步检查根目录的 `ClassroomAttendance.spec`，并重新验证 EXE。

针对某个功能测试时，在根目录执行，例如：

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -p test_course_schedule.py -v
```

完整测试仍用 `python -m unittest discover -s tests -v`。先把 `CLASSROOM_DATA_DIR` 指向独立的测试目录，运行方式与隔离说明见 [测试文档](testing.md)。
