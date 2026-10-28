# 可选的旧启动脚本

这些 BAT 从源码根目录移到这里，供已有使用习惯兼容。脚本会先定位项目根目录，再调用根目录的 `app_entry.py`；需要先在根目录准备 `.venv`。

| 文件 | 用途 |
| --- | --- |
| `start.bat` | 启动入口 |
| `admin.bat` | 教师管理 |
| `student.bat` | 学生签到 |
| `restore.bat` | 备份恢复 |
| `reset_admin.bat` | 打开密码重置确认窗口 |

日常建议使用 [项目首页](../../README.md) 的统一启动命令，或通过 `tools/create_shortcuts.ps1` 生成桌面快捷方式。Windows 仍可能对 BAT 或未签名 EXE 显示来源提示；移动脚本不会改变签名或安全区域判定。

这组脚本不加入 EXE 发行包。恢复和密码重置仍由程序自己的确认与数据保护流程处理。
