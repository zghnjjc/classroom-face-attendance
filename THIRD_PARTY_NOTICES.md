# Third-Party Notices / 第三方组件与许可声明

本项目（课堂签到 · Classroom Face Attendance）以 MIT License 发布，见仓库根目录 [LICENSE](LICENSE)。
本文件说明仓库中再分发的第三方数据文件，以及运行依赖的许可信息。

> 本文件是工程整理，不构成法律意见。正式发布前请维护者复核许可文本与版本号。

## 1. 仓库内再分发的数据文件：Haar 人脸检测级联

| 项目 | 说明 |
| --- | --- |
| 文件 | `haarcascade_frontalface_default.xml` |
| 文件大小 | 930,127 字节（约 930 KB） |
| SHA-256 | `0f7d4527844eb514d4a4948e822da90fbb16a34a0bbbbc6adc6498747a5aafb0` |
| 来源 | OpenCV 数据目录的上游文件 `opencv/data/haarcascades/haarcascade_frontalface_default.xml`；文件头注明由 Rainer Lienhart 创建（stump-based 24×24 AdaBoost frontal face detector） |
| 本机核对 | 与已安装的 `opencv-contrib-python 4.14.0.94` 包内 `cv2/data/haarcascade_frontalface_default.xml` 字节一致（SHA-256 相同） |
| 许可 | **Intel License Agreement**（BSD 风格；许可文本内嵌于文件头第 2–44 行） |

本仓库按原样再分发该文件，未修改其内容或删除许可头。该文件**不是** Apache-2.0；
请勿将其单独标注为 Apache-2.0。以下为文件头第 8–43 行的许可原文：

```text
  IMPORTANT: READ BEFORE DOWNLOADING, COPYING, INSTALLING OR USING.

  By downloading, copying, installing or using the software you agree to this license.
  If you do not agree to this license, do not download, install,
  copy or use the software.


                        Intel License Agreement
                For Open Source Computer Vision Library

 Copyright (C) 2000, Intel Corporation, all rights reserved.
 Third party copyrights are property of their respective owners.

 Redistribution and use in source and binary forms, with or without modification,
 are permitted provided that the following conditions are met:

   * Redistribution's of source code must retain the above copyright notice,
     this list of conditions and the following disclaimer.

   * Redistribution's in binary form must reproduce the above copyright notice,
     this list of conditions and the following disclaimer in the documentation
     and/or other materials provided with the distribution.

   * The name of Intel Corporation may not be used to endorse or promote products
     derived from this software without specific prior written permission.

 This software is provided by the copyright holders and contributors "as is" and
 any express or implied warranties, including, but not limited to, the implied
 warranties of merchantability and fitness for a particular purpose are disclaimed.
 In no event shall the Intel Corporation or contributors be liable for any direct,
 indirect, incidental, special, exemplary, or consequential damages
 (including, but not limited to, procurement of substitute goods or services;
 loss of use, data, or profits; or business interruption) however caused
 and on any theory of liability, whether in contract, strict liability,
 or tort (including negligence or otherwise) arising in any way out of
 the use of this software, even if advised of the possibility of such damage.
```

## 2. 运行依赖（不随仓库分发）

以下 Python 包由用户通过 `requirements-lock.txt` 安装，仓库不包含其 wheel、二进制或源码副本。
如需打包分发（如 PyInstaller 便携包、离线安装包），必须同时附带对应版本的许可文件。

| 组件 | 锁定版本 | 许可 | 说明 |
| --- | --- | --- | --- |
| opencv-contrib-python | 4.14.0.94 | Apache-2.0 | 提供 Haar 检测与 contrib face 模块（LBPH）；安装包内附 `LICENSE.txt` 与 `LICENSE-3RD-PARTY.txt` |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0（包元数据中的复合表达式） | 数组运算；安装包内附 `LICENSE.txt` 及 bundled licenses |
| openpyxl | 3.1.5 | MIT | 导出 `.xlsx` 考勤表 |
| et_xmlfile | 2.0.0 | MIT | openpyxl 的传递依赖 |

版本以锁文件 `requirements-lock.txt` 为准。升级依赖时请同步复核本文件的许可信息。

## 3. 由用户运行环境提供的组件（不随仓库分发）

| 组件 | 说明 |
| --- | --- |
| Python 3.11 | 由用户安装（python.org 发行版的许可为 PSF-2.0 等） |
| Tcl/Tk（Tkinter 8.6） | 随 Python 发行版提供 |
| SQLite | 使用 Python 标准库 `sqlite3`（SQLite 为公有领域） |
| Windows | 操作系统组件 |

## 4. 不包含在仓库中的运行数据

- `face_model.yml`、`model_manifest.json`：由部署者本机训练生成，包含人脸特征与学号标签，**不得提交或作为 Release 附件**。
- `dataset/`、`attendance.db`、`backups/`、`logs/`：由部署者本机生成，可能包含真实个人信息。
- 这些文件不属于第三方许可对象，而是隐私与合规问题；处理要求见 [docs/data-and-privacy.md](docs/data-and-privacy.md)。

## 5. 说明

- 本文件中的许可名称与版本依据当前安装包元数据与文件头整理；不同发行渠道或版本可能有差异。
- 再分发 Haar 级联时，请保留文件原样（含许可头），并保留本声明。
- 如对许可适用有疑问，请咨询专业法律人员。
