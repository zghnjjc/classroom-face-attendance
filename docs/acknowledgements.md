# 许可证与第三方致谢

- 本项目代码以 **MIT License** 发布，见 [LICENSE](../LICENSE)。
  版权行为 `Copyright (c) 2026 Classroom Face Attendance contributors`。
- 第三方组件与数据文件许可见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，其中包含仓库再分发的 Haar 级联 `haarcascade_frontalface_default.xml` 的 Intel License Agreement 原文、来源与 SHA-256。
- 运行依赖：`opencv-contrib-python 4.14.0.94`（Apache-2.0）、`numpy 2.4.6`（BSD-3-Clause 等复合许可）、`openpyxl 3.1.5`（MIT）、`et_xmlfile 2.0.0`（MIT）；Python、Tcl/Tk 与 SQLite 由用户运行环境提供，不随仓库分发。
- 设计思路参考（未复制代码或模型）：
  - [SmartAttend](https://github.com/mitraboga/SmartAttend/)：角色分工、课程课次与考勤时段。
  - [Face Recognition Attendance Management System](https://github.com/swapnil3104/Face-Recognition-Attendance-Management-System)：学生资料管理与考勤导出流程。
  - [Python SQLite 官方文档](https://docs.python.org/3.11/library/sqlite3.html)：事务、连接与在线备份。
  - [OpenCV LBPH 官方文档](https://docs.opencv.org/4.13.0/df/d25/classcv_1_1face_1_1LBPHFaceRecognizer.html)：灰度输入、训练与距离阈值。
- 感谢 OpenCV、NumPy、openpyxl 及其贡献者；Haar 级联由 Rainer Lienhart 创建，来自 OpenCV 数据目录。
