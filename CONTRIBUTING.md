# Contributing / 贡献指南

感谢你考虑为“课堂签到”做出贡献。这是一个本地优先的 Windows 桌面工具，项目规模较小，欢迎缺陷报告、文档改进、测试补充与小范围代码修改。

> 参与本项目即表示你同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)，并同意你的贡献以本项目的 MIT License 发布。

## 开发环境

- Windows 10 / 11（主要目标平台；Linux/macOS 未验证）
- Python **3.11**（开发环境在 3.11.9 验证）
- Tkinter（python.org 官方安装器自带）
- Git（可选）

在仓库根目录创建虚拟环境并安装固定依赖：

```powershell
py -3.11 -m venv .venv
./.venv/Scripts/python.exe -m pip install -r requirements-lock.txt
./.venv/Scripts/python.exe -m pip check
```

运行 GUI：

```powershell
./.venv/Scripts/python.exe GUI.py
```

## 运行测试

必须在**仓库根目录**运行：

```powershell
./.venv/Scripts/python.exe -m unittest discover -s tests -v
```

说明：

- 当前测试套件为 136 项 unittest；测试使用临时数据库、合成图片与模拟相机，不需要真实人脸、真实摄像头或生产数据库。
- 多数 GUI 测试会创建真实 Tk 根窗口，因此需要桌面图形会话；无头 CI 使用 Windows runner 或 Xvfb。
- 测试按项目根目录导入模块；不要在子目录中运行。
- 若修改了依赖版本，请同时更新 `requirements-lock.txt` 并说明原因。

## 代码风格

- 缩进：Python 4 个空格；文件编码 UTF-8（无 BOM）；文本换行 LF（`.bat` 为 CRLF，由 `.gitattributes` 保证）。
- 尽量遵循 PEP 8；项目现有代码风格偏紧凑，不要在无关修改中大规模重排/格式化整个文件。
- 优先使用标准库；新增第三方依赖需要先讨论（见下方 PR 要求）。
- 用户可见文案使用简体中文；错误提示应可读、可操作。
- 新增或修改数据层/业务规则时，请补充或更新 unittest；涉及 GUI 的修改尽量提供可测试的接口。
- 鼓励添加 docstring 和类型注解，尤其是数据层与公共函数。
- 不要把真实姓名、学号、人脸图像、数据库、模型、备份、日志、导出表格或本机绝对路径写入代码、测试夹具、注释或文档。

## 提交 Pull Request

1. **先开 Issue**（可选但推荐）：描述问题、复现步骤或改进动机，避免大改动方向不一致。
2. **保持改动聚焦**：一个 PR 解决一个问题；不要混入无关重构或格式化。
3. **说明验证方式**：在 PR 描述中写明你运行的命令与结果（例如 `python -m unittest discover -s tests -v` 通过），并说明是否手工验证了 GUI/相机相关改动。
4. **隐私检查**：确认 PR 不包含任何真实数据（见 [PULL_REQUEST_TEMPLATE](.github/PULL_REQUEST_TEMPLATE.md)）；测试与截图必须使用虚构数据，人脸需打码。
5. **新增依赖需先讨论**：说明必要性、许可是否兼容（MIT）、Windows 支持情况、安装体积与维护成本；未经讨论的依赖变更可能被拒绝。
6. **不降低安全/隐私基线**：不要移除参数化 SQL、导出的文本强制写入、日志脱敏、资源锁、密码哈希与限流等保护。
7. **同步文档**：行为变化时更新 README、CHANGELOG 或 docs/ 下的相关说明。
8. **保持 CI 通过**：工作流会对 Windows + Python 3.11 运行依赖安装、`pip check`、Tk 冒烟与全量 unittest。
9. **提交信息**：使用简洁的祈使句概括变更（例如 `Fix late threshold at session end`）；避免在提交信息中包含个人信息。
10. **不提交生成物**：不要提交 `.venv/`、`__pycache__/`、`attendance.db`、`dataset/`、`face_model.yml`、`model_manifest.json`、`backups/`、`logs/`、导出表格或压缩包；这些路径已在 `.gitignore` 中忽略。

## 数据与测试约定

- **绝不使用生产数据做开发或测试**。使用 `tempfile.TemporaryDirectory()`、合成图像与模拟相机。
- 如需复现某台真实相机的异常，请描述现象与脱敏后的错误类别，不要上传照片、视频或含个人信息的日志。
- 人脸样本、模型与数据库都是敏感数据；即使删除，也要考虑历史备份与 Git 历史的影响。

## 报告问题

- 缺陷：使用 [Bug report 模板](.github/ISSUE_TEMPLATE/bug_report.yml)。
- 功能建议：使用 [Feature request 模板](.github/ISSUE_TEMPLATE/feature_request.yml)。
- 安全漏洞：**不要**公开开 Issue，请按 [SECURITY.md](SECURITY.md) 私下报告。

## License

提交贡献即表示你同意以 MIT License 授权你的贡献（本项目不使用 CLA）。
