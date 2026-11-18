# 课堂签到 · Classroom Face Attendance

[![tests](https://github.com/zghnjjc/classroom-face-attendance/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/zghnjjc/classroom-face-attendance/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/release/zghnjjc/classroom-face-attendance?label=release)](https://github.com/zghnjjc/classroom-face-attendance/releases)
[![license](https://img.shields.io/github/license/zghnjjc/classroom-face-attendance)](LICENSE)

课堂签到是一款在 **Windows 本机离线运行**的考勤工具。教师可导入学生名单、按课程排课、人工点名、核对考勤并导出 Excel。老师开启课次后，已获授权的学生可在同一台电脑上逐人刷脸签到。名单、人脸资料和考勤记录均保存在本机。

[下载 Windows 便携包](https://github.com/zghnjjc/classroom-face-attendance/releases/latest) · [使用说明](docs/user-guide.md) · [版本记录](CHANGELOG.md)

> **开发预览**。当前公开版本为 `v0.2.0`，适合学习、演示和功能体验。识别效果和长期稳定性仍待真实课堂验证，暂不建议用于正式考勤。

## 怎么运行

### Windows 便携包

适用于 Windows 10 / 11 x64，无需安装 Python。

1. 下载 `ClassroomAttendance-0.2.0-win-x64.zip` 和同名 `.sha256` 文件。[v0.2.0 发行页](https://github.com/zghnjjc/classroom-face-attendance/releases/tag/v0.2.0)
2. 在 PowerShell 执行 `Get-FileHash .\ClassroomAttendance-0.2.0-win-x64.zip -Algorithm SHA256`，将结果与 `.sha256` 文件核对。
3. 核对一致后解压整个 ZIP，保留 `_internal/` 文件夹，双击 `ClassroomAttendance.exe`。发行包未签名，Windows 可能提示“未知发布者”。

首次使用会引导你确认数据位置、设置管理密码、测试摄像头和准备名单。可以先用虚构学生体验人工点名。跳过摄像头测试后，维护一份测试名单，创建当前时段的课次，在「人工点名」开课并登记，再结束课次、核对和导出。

如需体验刷脸，请使用本人或明确同意参与测试者的资料。先登记人脸授权，再采集样本并更新识别模型。教师在签到时段内开启课次后，进入学生端刷脸签到。项目不附带学生名单、人脸样本、训练好的模型或考勤数据库。

### 从源码运行

在 Windows 10 / 11 安装 Python 3.11，然后下载并解压源码。在包含 `app_entry.py` 的目录打开 PowerShell，依次执行下面三行。安装依赖时需要联网。

```powershell
py -3.11 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe app_entry.py
```

如果找不到 `py`，可用 `python --version` 确认解释器为 Python 3.11，再把第一行的 `py -3.11` 换成 `python`。以后启动只需执行最后一行。[启动与构建说明](docs/deployment.md)

## 主要功能

- **名单与排课**支持 CSV / Excel 导入预览。可创建单次课次，或使用课程模板按周、隔周批量排课，设置开放签到、正式上课、迟到和关闭时间。
- **签到与点名**支持学生逐人刷脸签到，教师也可以在没有摄像头或模型时人工开课、批量登记。
- **课后核对与报表**支持从「今日课堂」查看待核对课次，更正记录并保留更正原因。可按日期、课程或班级汇总并导出五表 Excel。
- **人脸授权与清理**支持记录告知版本、授权期限和撤回历史，提供到期提醒与受控清理。备份恢复后，人脸授权需逐人复核。
- **备份与维护**支持校验和恢复完整备份，可启用教师端运行期间的每日自动备份。维护页提供独立摄像头检测。

技术栈为 Python 3.11、OpenCV、Tkinter、SQLite 和 openpyxl。

## 使用前需要知道

- **识别结果需要人工核对。** 光线、角度、遮挡和设备都会影响效果，程序也无法可靠识别照片或视频代刷。
- **单机运行。** 同一时刻只开放一个签到课次。当前验证环境为 Windows 10 / 11 和 Python 3.11。
- **保护本地资料。** 使用人脸功能前应取得实际知情同意。请妥善保管本地样本和备份，勿在公开仓库或 Issue 上传真人资料、数据库、模型或备份。

## 按需查阅

不必先读完所有文档。遇到具体问题时，再打开对应说明：

| 想了解什么 | 去哪里看 |
| --- | --- |
| 想改某个功能，代码和测试在哪里 | [模块导航](docs/modules.md) |
| 详细操作、常见问题 | [使用说明](docs/user-guide.md) |
| 数据保存在哪里，怎样备份或清理 | [数据与隐私](docs/data-and-privacy.md) |
| 摄像头故障排查、人脸授权与清理 | [维护说明](docs/maintenance.md) · [人脸资料管理](docs/face-lifecycle.md) |
| 怎样参与开发或运行测试 | [贡献指南](CONTRIBUTING.md) · [测试说明](docs/testing.md) |
| 程序如何组织，考勤如何计算 | [架构说明](docs/architecture.md) · [考勤规则](docs/design/business-rules.md) |

遇到问题可在 [Issues](https://github.com/zghnjjc/classroom-face-attendance/issues) 描述操作步骤和错误提示，截图请先遮住个人信息。安全问题请按 [安全策略](SECURITY.md) 反馈。

代码采用 [MIT 许可证](LICENSE)。第三方组件说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，设计参考见 [致谢](docs/acknowledgements.md)。

应用代码集中在 `classroom_attendance/`，按考勤、排课、人脸、摄像头、存储等功能分目录；静态资源放在 `resources/`，辅助脚本放在 `tools/`。根目录保留 `app_entry.py` 作为统一启动入口。
