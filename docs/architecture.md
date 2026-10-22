# 架构说明 / Architecture

> 本文描述当前代码的模块分层、启动流程、主要数据流与并发模型，面向维护者。
> 具体业务规则见 [business-rules.md](design/business-rules.md)，数据与隐私见 [data-and-privacy.md](data-and-privacy.md)。

## 1. 目标与边界

- 目标：Windows 本地离线课堂签到与考勤工具；学生端与教师端独立进程，共享同一 SQLite 数据目录。
- 非目标：Web/移动端、云端同步、多账号权限体系、活体检测、安装包分发。
- 关键约束：Tk 主线程不能执行阻塞 I/O；相机读取可能挂起；SQLite 存在跨进程并发；人脸样本/模型/数据库含敏感信息。

## 2. 模块分层

```text
入口层        GUI.py（角色选择器）
              student_app.py（学生端）
              admin_app.py（教师端）
              reset_admin.py（离线密码重置，CLI）
              train.py（命令行训练入口）

应用/运行时    app_startup.py   启动预检、中文重试、错误兜底
              task_runtime.py  可取消/带超时的后台任务、FlowHost/IO/checkpoint
              app_errors.py    领域异常、中文用户文案、脱敏日志
              ui_common.py     Tk 主题、通用控件、相机预览

业务/服务层    attendance_store.py   学生、课次、名单、签到、证据、审计、导入导出、备份
              attendance_filters.py  查询时间范围与学期边界
              camera_service.py      采集/识别工作线程、连续确认、画质门槛
              face_privacy.py        经口令复核的样本删除与模型重建
              train.py               训练、模型清单、完整性校验

设备/IO 层     camera_device.py   独立相机驱动进程、读超时、释放
              face_utils.py      OpenCV 图像/模型/级联读写（中文路径兼容）
              resource_lock.py   跨进程相机/样本资源锁

配置           app_config.py      路径、阈值、超时、密码迭代次数
```

## 3. 启动流程

三个 GUI 入口（`GUI.py`、`student_app.py`、`admin_app.py`）都会经过 `app_startup.launch_entry()`：

1. 创建 Tk 根窗口与 `StartupController`（先显示“正在检查数据”页面）；
2. 在后台线程构造 `AttendanceStore`：创建/升级数据库结构、执行一次性迁移与种子导入、校验运行数据组合；
3. 若数据库忙/损坏/权限错误，显示中文提示与“重试/退出”，主线程保持响应；
4. 成功后将 store 交给目标窗口（入口选择器、学生端或教师端），销毁启动页。

教师端在展示管理页面前需通过登录/首次设置密码；学生端不显示管理功能，只轮询当前课次。

启动守卫细节：若数据库文件不存在但模型或样本仍存在，`checked_store()` 会报缺少数据库，避免在“残缺数据”上继续运行。

## 4. 学生端签到数据流

1. 学生端每 2 秒轮询 `active_session()`；发现开放课次后展示课程、班级、时段与已到/应到人数。
2. 用户点击“开始识别”时再次强制刷新课次并校验模型；通过后创建 `CameraWorker(mode="sign")` 工作线程。
3. 工作线程获取相机锁（`.camera.lock`），在数据锁（`.samples.lock`）内校验并加载模型，随后启动相机读取（默认独立驱动进程）。
4. 每帧：灰度化 → Haar 检测（要求恰好一张人脸）→ 画质检查（尺寸/亮度/清晰度）→ 缩放 160×160 + 直方图均衡 → LBPH 预测。
5. 距离 < 60 且身份在本课次应到名单且启用 → 作为候选；`RecognitionStreak` 要求同一身份连续 8 次且 ≥1 秒才确认。
6. 确认后调用 `record_sign_in()`：在一个 `BEGIN IMMEDIATE` 事务中检查课次状态/时间、名单与启用状态，写入签到与首次人脸证据（唯一约束去重），写操作记录。
7. 结果通过线程安全队列回到 Tk 主线程；学生端显示脱敏姓名/学号、状态与时间；重复签到只提示已有记录。
8. 识别循环约每 0.75 秒复核课次、启用名单与模型哈希；课次结束或模型变化时停止；工作线程退出时释放相机进程/设备与资源锁。

## 5. 教师端主要流程

- **名单**：数据层校验学号（1–24 位 ASCII 数字）、姓名与班级长度；导入 CSV/XLSX 先整体校验再单事务写入，重复学号整批拒绝。
- **采集**：`CameraWorker(mode="capture")` 在相机锁 + 数据锁内运行，按 `<学号>.<序号>.jpg` 追加样本（不覆盖旧编号），默认目标 20 张，间隔 ≥0.4 秒，画质不合格不保存。
- **训练**：`train.train_model()` 读取样本 → 预处理 → LBPH 训练 → 写临时模型 → 计算样本签名与模型 SHA-256 → 原子替换模型与清单；训练期间样本变化会失败重试。
- **课次**：创建时保存名单快照；开启前检查模型覆盖；开启/结束/取消/名单调整均通过数据层事务与操作记录。
- **考勤查询/导出**：`attendance_records()` 统一推导状态；导出使用 `openpyxl`，强制字符串单元格文本类型、学号文本格式与日期格式。
- **备份**：SQLite 在线备份 API 生成一致性数据库 → 与模型、清单、样本打包 ZIP → 原子替换目标文件。
- **删除人脸**：`face_privacy.delete_face_data()` 在相机/数据锁内移动样本到临时目录 → 重建或删除模型 → 写审计 → 清理临时目录；失败时回滚模型与样本。
- **密码**：首次设置、登录（含剩余次数/锁定倒计时）、修改、离线重置；PBKDF2-HMAC-SHA256 600,000 次，旧摘要成功登录后升级。

## 6. 数据模型

```text
students(student_id PK, name, group_name, active)
metadata(key PK, value)                 -- admin_auth、camera_index、semester_dates、schema_version、seed_imported
sessions(id PK, course_name, group_name, starts_at, late_at, ends_at,
         state, created_at, cancel_reason, cancelled_at)
session_students(session_id, student_id, name, group_name, included,
                 PK(session_id, student_id))
checkins(session_id, student_id, status, signed_at, method, note,
         PK(session_id, student_id))
face_evidence(session_id, student_id, signed_at, method, recorded_at, origin,
              PK(session_id, student_id))  -- 触发器禁止 UPDATE/DELETE
audit_log(id PK, created_at, action, session_id, student_id, detail,
          actor_label, actor_kind, before_json, after_json)
attendance(id PK, student_id, day, signed_at, UNIQUE(student_id, day))  -- 旧版兼容
```

- 名单快照保存创建时的姓名/班级；`included` 支持“移出但保留记录”。
- `checkins` 保存当前有效结果；`face_evidence` 保存首次不可变证据；两者分离使人工更正不破坏原始证据。
- `schema_version=3`；启动时执行旧库迁移（旧“按天签到”记录转为“历史签到”课次；能给存在的刷脸时间补证据，但不反推已被覆盖的旧时间）。
- 使用 WAL、外键、唯一约束、索引与 `busy_timeout`；关键写事务使用 `BEGIN IMMEDIATE`。

## 7. 并发与超时模型

- **Tk 主线程**只做界面更新；业务 I/O 通过 `task_runtime.IO` 交给后台线程，`FlowRunner` 轮询结果并支持取消/超时（默认 30 秒）。
- **数据库**：读连接超时 2 秒、写连接 8 秒；连接级 `busy_timeout` 与业务层重试提示。
- **相机**：独立驱动进程（`spawn`）+ 2 秒读取超时；主进程可终止挂起进程并释放设备。
- **资源锁**：`resource_lock.ResourceLease` 使用 Windows `msvcrt.locking` / POSIX `flock` 非阻塞锁；进程退出由操作系统释放，不删除锁文件。
- **取消**：`TaskControl` + `checkpoint()` 在长任务/Db 写入/文件任务中检查取消与截止时间；取消后不提交迟到写入。
- **跨端联动**：学生端每 2 秒、教师端每 3 秒刷新；课次状态、模型版本与考勤变化会被下一轮轮询发现。

## 8. 错误处理与日志

- `app_errors` 定义业务异常（校验、繁忙、相机、取消、超时）并映射为中文用户提示；未知异常不直接展示底层文本。
- 日志只保留事件、异常类型与调用链的文件/行号/函数名；不写异常值、源代码、局部变量、姓名、学号、口令或图像。
- 日志写入 `logs/classroom.log`，1 MB 轮转、保留 3 份；日志目录不可写时不影响错误展示与退出。
- 启动页、管理端与学生端均有错误兜底与重试/恢复路径（数据库忙、相机异常、后台任务失败等）。

## 9. 文件写入与原子性

- 模型、模型清单、导出 Excel、备份 ZIP、样本图片均采用“临时文件 + 原子替换”。
- 训练与删除人脸在失败时回滚旧模型/清单/样本。
- 数据库写操作依赖事务与 `checkpoint()`，避免取消后提交。
- 备份使用 SQLite 在线备份接口，保证数据库副本一致性。

## 10. 可测试性设计

- 数据层不依赖 UI，可直接用临时数据库测试。
- 相机通过 `worker_factory` / `camera_device` 可替换为假对象；GUI 测试可注入 store 与假 worker。
- 时间规则多通过显式参数或可控时钟验证。
- 启动流程与后台任务支持同步模式（`async_io=False`），便于单元测试。
- `tests/` 覆盖数据层、GUI 状态机、故障恢复、并发锁、模型完整性与隐私操作。

## 11. 已知设计取舍与限制

- UI 层仍较重（`admin_app.py` 集中了页面、校验与流程），未拆出完整 Service/ViewModel 层；新功能应优先把规则放进数据层。
- 单机单摄像头、同一时刻一个开放课次；无多账号/网络同步。
- 数据库、样本、模型与备份默认未加密；安全边界依赖操作系统账户与部署者流程。
- 人脸识别为 LBPH，无活体检测；识别结果只作为辅助考勤记录。
- 自动结束课次依赖运行中程序的查询触发，而不是独立后台服务。
- 配置阈值目前集中在 `app_config.py` 与少量模块常量中，未来可进一步集中。

## 12. 扩展建议

- 新增考勤规则：优先改 `attendance_store.py` 的事务/状态推导，再补 `tests/test_business_rules.py`。
- 新增导出字段：同时更新 `export_excel()`、`export_attendance()` 与导出测试（保持文本强制写入）。
- 新增设备/平台支持：抽象 `camera_device` 与字体/启动器；补充跨平台 CI（如 Linux + Xvfb）后再声明支持。
- 新增隐私操作：必须经过密码复核、原子回滚与审计；不得破坏 `face_evidence` 的不可变语义。
- 引入新依赖前评估许可、体积、Windows 支持与离线可用性，并更新 `THIRD_PARTY_NOTICES.md`。

---

相关文档：[业务规则](design/business-rules.md) · [数据与隐私](data-and-privacy.md) · [测试说明](testing.md)
