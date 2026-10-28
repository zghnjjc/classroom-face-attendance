# 架构说明 / Architecture

> 本文描述当前代码的模块分层、启动流程、主要数据流与并发模型，面向维护者。
> 具体业务规则见 [business-rules.md](design/business-rules.md)，数据与隐私见 [data-and-privacy.md](data-and-privacy.md)。

## 1. 目标与边界

- 目标：Windows 本地离线课堂签到与考勤工具；学生端与教师端独立进程，共享同一 SQLite 数据目录。
- 非目标：Web/移动端、云端同步、多账号权限体系、活体检测、安装包分发。
- 关键约束：Tk 主线程不能执行阻塞 I/O；相机读取可能挂起；SQLite 存在跨进程并发；人脸样本/模型/数据库含敏感信息。

## 2. 模块分层

代码集中在 `classroom_attendance/`，按功能组织。每个子目录是独立 Python 包；同一功能的业务文件和界面文件相邻，跨目录使用完整包名导入。完整文件与测试对照见 [模块导航](modules.md)。

| 目录 | 职责 |
| --- | --- |
| `ui/` | 启动页、教师/学生主窗口、共用 Tk 控件 |
| `onboarding/` | 首次使用向导、运行数据位置 |
| `attendance/` | 人工点名、筛选、更正展示与统计报表 |
| `courses/` | 课程模板、周期生成、课次时间 |
| `dashboard/` | 今日课堂聚合与课后核对 |
| `faces/` | 样本、模型训练、授权、删除和中断恢复 |
| `camera/` | 相机驱动进程、采集识别工作线程、诊断 |
| `storage/` | SQLite 数据访问、备份、恢复与迁移 |
| `maintenance/` | 现场维护、密码重置、发行包自检 |
| `core/` | 配置、启动预检、异常、任务调度、文件哈希与资源锁 |

根目录的 `app_entry.py` 在导入界面前处理参数和 `multiprocessing.freeze_support()`，然后分派到对应包。桌面快捷方式和角色子进程仍通过该入口启动。不要直接运行包内的界面文件。

`resources/` 保存 Haar 级联与空初始化模板。`core/app_config.py` 分别确定源码/冻结资源根目录、程序目录和运行数据目录；静态资源移动不改变用户数据位置，也不触发数据库迁移。`ClassroomAttendance.spec` 按白名单将静态资源打包进 `resources/`。

## 3. 启动流程

三个 GUI 入口（`classroom_attendance/ui/launcher.py`、`classroom_attendance/ui/student_app.py`、`classroom_attendance/ui/admin_app.py`）都会经过 `app_startup.launch_entry()`：

1. 创建 Tk 根窗口与 `StartupController`（先显示“正在检查数据”页面）；
2. 在后台线程构造 `AttendanceStore`：先只读拒绝未知版本，再创建/升级数据库结构、执行一次性迁移与种子导入，并恢复中断的人脸删除；
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
- **课次**：创建时保存名单快照；人脸入口开启前检查模型覆盖；人工点名入口记录原因后可无模型开课；两种入口共用时段及唯一开放课次规则。
- **模板与周期课次**：`classroom_attendance/courses/course_schedule.py` 规范化模板、展开星期和周间隔、检查全机时段冲突，通过 `CourseScheduleStore` 复用数据层连接和课次写入；`classroom_attendance/courses/course_schedule_ui.py` 提供编辑、选课名单和日期预览。模板带修订号存于现有 metadata，生成确认复核预览令牌，整批名单快照与操作记录在同一事务保存。
- **今日课堂与课后核对**：`classroom_attendance/dashboard/teacher_dashboard.py` 复用统一考勤 SQL 聚合首页数据，并通过考勤快照与操作序号复核课后确认；`classroom_attendance/dashboard/dashboard_ui.py` 提供首页与核对窗口，读取和提交均走后台流程。核对标记与最近备份时间存于现有 metadata；标记随程序中的考勤或名单变化失效，随备份恢复。历史课次导航使用精确日期和课次目标，沿用查询与导出条件。
- **人工点名**：`classroom_attendance/attendance/manual_attendance.py` 提供独立教师弹窗，复用更正计划和写入函数；预览绑定所选快照、当前结果、证据、最近更正与登记内容，确认时在同一写事务复核，整批结果与逐人历史、汇总一起提交。
- **考勤查询/导出**：`attendance_records()` 统一推导状态；导出使用 `openpyxl`，强制字符串单元格文本类型、学号文本格式与日期格式。
- **学期统计报表**：`classroom_attendance/attendance/attendance_reports.py` 复用统一考勤 SQL，在显式读事务中选取已结束课次及名单快照，生成学生/课程汇总与矩阵；`classroom_attendance/attendance/attendance_reports_ui.py` 在后台查询并保留页面快照供五表 Excel 导出。修改筛选禁用旧快照导出；取消检查与临时文件原子替换保护已有文件，无需增加数据库表。
- **首次使用**：`classroom_attendance/onboarding/storage_welcome.py` 确认并保存位置，`GUI.InitialLauncher` 与教师登录按新用户/续办/已有用户路由；`classroom_attendance/onboarding/first_use.py` 在 metadata 保存版本化进度并原子提交预览后的名单，`classroom_attendance/onboarding/first_use_ui.py` 管理登录后的相机、名单、完成检查。`classroom_attendance/camera/camera_diagnostics.py` 只传递内存 PNG，复用相机进程超时及资源锁，实际释放后才推进或关闭。
- **首次使用复核与切换**：`prepare()` 先读取，需创建/重置/迁移时再取得写锁并复核；入口向登录传递已知进度。设置页重新检查保留完成状态，确认“重新开始”才显式重置。`ui_common.switch_view()` 在下一页成功构造后销毁旧页，失败则清理新增控件/定时器并恢复原页与窗口回调。向导仅记录尚未执行的 after 令牌，执行后移除。
- **现场维护**：`classroom_attendance/maintenance/maintenance_ui.py` 提供独立检测窗口、空间查询、超时设置与诊断导出；`classroom_attendance/maintenance/maintenance_tools.py` 在 metadata 保存两项时限、在写文件前检查目标卷、将环境和计数白名单重建为 JSON/ZIP，不复制原始日志或用户字段。检测复用 `CameraPreviewWorker`；采集/签到直接使用有界 `ProcessCapture`，释放结束后才发 stopped。重试由用户显式触发，采集保留本次累计数，签到重新复核课次/模型。
- **备份**：SQLite 在线备份 API 生成一致性数据库 → 与模型、清单、样本打包 ZIP → 原子替换目标文件。
- **自动备份**：`classroom_attendance/storage/automatic_backup.py` 在现有 metadata 保存配置/重试状态，使用数据目录锁跨窗口去重、样本锁保证一致性，复用完整 ZIP；归属索引原子提交后按份数清理哈希一致的旧文件。`classroom_attendance/storage/automatic_backup_ui.py` 以队列与单工作线程调度，Tk 只展示结果；底层真实结束前不允许启动重复任务，关闭时协作取消。换数据目录后暂停，教师端以外没有常驻服务。
- **删除人脸**：`face_privacy.delete_face_data()` 在相机/数据锁内先持久化原样本/模型恢复清单，移动样本后在暂存目录生成新模型，记录新哈希再安装；审计和删除提交标记在同一数据库事务保存。`classroom_attendance/faces/face_delete_recovery.py` 在启动时恢复未提交原件，或完成已提交删除的暂存清理；路径、哈希或旧清单不明时保留资料并报错，恢复前停止采集/模型使用/训练/新备份。
- **密码**：首次设置、登录（含剩余次数/锁定倒计时）、修改、离线重置；PBKDF2-HMAC-SHA256 600,000 次，旧摘要成功登录后升级。

## 6. 数据模型

```text
students(student_id PK, name, group_name, active)
metadata(key PK, value)                 -- admin_auth、camera_index、semester_dates、schema_version、seed_imported
sessions(id PK, course_name, group_name, opens_at, starts_at, late_at, ends_at,
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
- `schema_version=5`；schema 4 引入 `opens_at`，旧课次回填原 `starts_at`，正式上课与原迟到/关闭值保留。schema 5 新增 `face_notices`、`face_authorizations`、`face_cleanup_jobs`，不为旧资料推定同意。备份只读验证 schema 3/4/5；旧备份恢复后首次打开升级，所有恢复写入 `face_restore_review_after` 水位，恢复的授权必须逐人重新核验登记。
- 告知/授权/撤回以追加记录保存，触发器禁止 UPDATE/DELETE；最新告知与最新授权事件共同决定资格。签到在写事务同时检查业务时间与当前时间。合并模型含受限身份时整体阻止使用，采集每次保存前复核，应用训练只选有效授权的启用学生。
- 受控清理在数据库先提交 `prepared` 意图与完整哈希清单，再以有界暂存名移动并删除文件，最后原子标记 `completed` 与写完成审计。中断保留原计划且暂停人脸/新备份，可密码复核后续办；变化、链接或非法路径停止处理，不删除确认范围外文件。清理不改变名单、考勤和授权历史。
- `classroom_attendance/courses/session_times.py` 统一四时间顺序与 24 小时校验；`starts_at` 表示正式上课及统计归属，开课和签到使用 `[opens_at, ends_at)`，`late_at` 该秒起计迟到。周期冲突使用整个开放窗口，新模板保存四时分与日偏移，兼容旧宽限模板。
- 使用 WAL、外键、唯一约束、索引与 `busy_timeout`；关键写事务使用 `BEGIN IMMEDIATE`。

## 7. 并发与超时模型

- **Tk 主线程**只做界面更新；业务 I/O 通过 `task_runtime.IO` 交给后台线程，`FlowRunner` 轮询结果并支持取消/超时（默认 30 秒）。
- **独立时限**：教师端训练/删除重建默认 180 秒，Excel/诊断导出默认 120 秒；`maintenance_timeouts_v1` 分别保存 30–3600 秒，下次任务生效。原生训练/文件保存采取协作取消，检查点前不宣称结束，备份仍使用独立 300 秒。
- **数据库**：读连接超时 2 秒、写连接 8 秒；连接级 `busy_timeout` 与业务层重试提示。
- **相机**：独立驱动进程（`spawn`）+ 2 秒读取超时；主进程可终止挂起进程并释放设备。
- **资源锁**：`resource_lock.ResourceLease` 使用 Windows `msvcrt.locking` / POSIX `flock` 非阻塞锁；进程退出由操作系统释放，不删除锁文件。
- **取消**：`TaskControl` + `checkpoint()` 在长任务/Db 写入/文件任务中检查取消与截止时间；取消后不提交迟到写入。FlowRunner 在底层线程返回前保留注册和同名去重，超时异常在底层退出后送回流程，显式取消丢弃迟到界面回调。
- **跨端联动**：学生端每 2 秒、教师端每 3 秒刷新；课次状态、模型版本与考勤变化会被下一轮轮询发现。

## 8. 错误处理与日志

- `app_errors` 定义业务异常（校验、繁忙、相机、取消、超时）并映射为中文用户提示；未知异常不直接展示底层文本。
- 日志只保留事件、异常类型与调用链的文件/行号/函数名；不写异常值、源代码、局部变量、姓名、学号、口令或图像。
- 日志写入 `logs/classroom.log`，1 MB 轮转、保留 3 份；日志目录不可写时不影响错误展示与退出。
- 启动页、管理端与学生端均有错误兜底与重试/恢复路径（数据库忙、相机异常、后台任务失败等）。
- 人脸表单在本窗口内展示校验/任务失败并保留输入；通用消息框以当前模态窗口为父窗口，任务轮询在回调退出后继续排期。

## 9. 文件写入与原子性

- 模型、模型清单、导出 Excel、备份 ZIP、样本图片均采用“临时文件 + 原子替换”。
- 训练与删除人脸在失败时回滚旧模型/清单/样本。
- 数据库写操作依赖事务与 `checkpoint()`，避免取消后提交。
- 备份使用 SQLite 在线备份接口，保证数据库副本一致性。
- 新备份包含完整文件清单与哈希；恢复先在私有临时目录检查，维护锁拒绝存活的其他新版实例，再移动旧数据、安装新数据。恢复日志区分未提交与已提交阶段，启动时回退未提交阶段；原始快照与可读原库的 ZIP 都保留。
- 数据默认位于 Windows 用户数据目录，程序目录只保存资源；目录偏好、命令行与环境变量在导入业务模块前解析。发行包使用同一 EXE 的角色参数，冻结子进程在导入 Tk/OpenCV 前分派。

## 10. 可测试性设计

- 数据层不依赖 UI，可直接用临时数据库测试。
- 相机通过 `worker_factory` / `camera_device` 可替换为假对象；GUI 测试可注入 store 与假 worker。
- 时间规则多通过显式参数或可控时钟验证。
- 启动流程与后台任务支持同步模式（`async_io=False`），便于单元测试。
- `tests/` 覆盖数据层、GUI 状态机、故障恢复、并发锁、模型完整性与隐私操作。

## 11. 已知设计取舍与限制

- UI 层仍较重（`classroom_attendance/ui/admin_app.py` 集中了页面、校验与流程），未拆出完整 Service/ViewModel 层；新功能应优先把规则放进数据层。
- 单机单摄像头、同一时刻一个开放课次；无多账号/网络同步。
- 数据库、样本、模型与备份默认未加密；安全边界依赖操作系统账户与部署者流程。
- 人脸识别为 LBPH，无活体检测；识别结果只作为辅助考勤记录。
- 自动结束课次依赖运行中程序的查询触发，而不是独立后台服务。
- 配置阈值目前集中在 `classroom_attendance/core/app_config.py` 与少量模块常量中，未来可进一步集中。

## 12. 扩展建议

- 新增考勤规则：优先改 `classroom_attendance/storage/attendance_store.py` 的事务/状态推导，再补 `tests/test_business_rules.py`。
- 新增导出字段：同时更新 `export_excel()`、`export_attendance()` 与导出测试（保持文本强制写入）。
- 新增设备/平台支持：抽象 `camera_device` 与字体/启动器；补充跨平台 CI（如 Linux + Xvfb）后再声明支持。
- 新增隐私操作：必须经过密码复核、原子回滚与审计；不得破坏 `face_evidence` 的不可变语义。
- 引入新依赖前评估许可、体积、Windows 支持与离线可用性，并更新 `THIRD_PARTY_NOTICES.md`。

---

相关文档：[业务规则](design/business-rules.md) · [数据与隐私](data-and-privacy.md) · [测试说明](testing.md)
