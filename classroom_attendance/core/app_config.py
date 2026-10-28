"""项目路径与样本命名规则，启动位置不影响文件访问。"""

from pathlib import Path
import json
import os
import sys

# Source root, or PyInstaller resource root in a frozen bundle.
BASE_DIR = Path(__file__).resolve().parents[2]
RESOURCE_DIR = BASE_DIR / "resources"
PROGRAM_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE_DIR
APP_VERSION = "0.2.0-preview10"


def default_data_dir():
    """程序资源与运行数据分离；Windows 使用当前用户的本地应用数据目录。"""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ClassroomFaceAttendance"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "ClassroomFaceAttendance"


def data_location_preferences():
    """程序目录的显式配置优先；供用户目录不可写的源码或便携运行使用。"""
    return (PROGRAM_DIR / 'data_location.json', default_data_dir() / 'data_location.json')


def existing_data_location_preference():
    try:
        return next((path for path in data_location_preferences() if path.exists()), None)
    except OSError as error:
        raise ValueError('数据位置配置无法读取，请在启动页重新选择数据目录。') from error


def configured_data_dir():
    override = os.environ.get("CLASSROOM_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    preference = existing_data_location_preference()
    if preference is not None:
        try:
            if preference.stat().st_size > 65536:
                raise ValueError()
            value = json.loads(preference.read_text(encoding="utf-8"))
            directory = value.get("directory") if isinstance(value, dict) else None
            if not isinstance(directory, str) or not Path(directory).is_absolute():
                raise ValueError()
            return Path(directory).resolve()
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("数据位置配置无法读取，请在启动页重新选择数据目录。") from error
    return default_data_dir().resolve()


def save_data_location(directory):
    directory = Path(directory).expanduser().resolve()
    try:
        selected = existing_data_location_preference()
    except ValueError:
        selected = None
    portable, user = data_location_preferences()
    targets = list(dict.fromkeys((selected or user, portable)))
    for index, preference in enumerate(targets):
        temporary = preference.with_suffix('.tmp')
        try:
            preference.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"directory": str(directory)}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(preference)
            return
        except PermissionError:
            if index == len(targets) - 1:
                raise
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


DATA_DIRECTORY_ERROR = ""
HAS_DATA_LOCATION_PREFERENCE = False
try:
    HAS_DATA_LOCATION_PREFERENCE = existing_data_location_preference() is not None
    DATA_DIR = configured_data_dir()
except ValueError as error:
    DATA_DIR = default_data_dir().resolve()
    DATA_DIRECTORY_ERROR = str(error)
DATASET_DIR = DATA_DIR / "dataset"
MODEL_PATH = DATA_DIR / "face_model.yml"
CASCADE_PATH = RESOURCE_DIR / "haarcascade_frontalface_default.xml"
DATABASE_PATH = DATA_DIR / "attendance.db"
MODEL_MANIFEST_PATH = DATA_DIR / "model_manifest.json"
CAMERA_LOCK_PATH = DATA_DIR / ".camera.lock"
DATA_LOCK_PATH = DATA_DIR / ".samples.lock"
LOG_DIR = DATA_DIR / "logs"
DB_READ_TIMEOUT = 2.0
DB_WRITE_TIMEOUT = 8.0
TASK_TIMEOUT = 30.0
BACKUP_TIMEOUT = 300.0
RESTORE_CHECK_TIMEOUT = 300.0
CAMERA_READ_TIMEOUT = 2.0
CAPTURE_TARGET = 20
PASSWORD_ITERATIONS = 600_000
LEGACY_PASSWORD_ITERATIONS = 260_000
SEED_PATH = RESOURCE_DIR / "initial_data.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp"}


def sample_student_id(path: Path) -> str:
    """兼容旧的 name.id.index.jpg 和新的 id.index.jpg。"""
    parts = path.stem.split(".")
    if len(parts) < 2 or not parts[-2].isdigit() or not parts[-1].isdigit():
        raise ValueError(f"样本文件名不符合 学号.序号.jpg 格式：{path.name}")
    return parts[-2]


def sample_paths(directory: Path = DATASET_DIR) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
