"""项目路径与样本命名规则，启动位置不影响文件访问。"""

from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CLASSROOM_DATA_DIR", BASE_DIR)).resolve()
DATASET_DIR = DATA_DIR / "dataset"
MODEL_PATH = DATA_DIR / "face_model.yml"
CASCADE_PATH = BASE_DIR / "haarcascade_frontalface_default.xml"
DATABASE_PATH = DATA_DIR / "attendance.db"
MODEL_MANIFEST_PATH = DATA_DIR / "model_manifest.json"
CAMERA_LOCK_PATH = DATA_DIR / ".camera.lock"
DATA_LOCK_PATH = DATA_DIR / ".samples.lock"
LOG_DIR = DATA_DIR / "logs"
DB_READ_TIMEOUT = 2.0
DB_WRITE_TIMEOUT = 8.0
TASK_TIMEOUT = 30.0
CAMERA_READ_TIMEOUT = 2.0
PASSWORD_ITERATIONS = 600_000
LEGACY_PASSWORD_ITERATIONS = 260_000
SEED_PATH = BASE_DIR / "initial_data.json"
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
