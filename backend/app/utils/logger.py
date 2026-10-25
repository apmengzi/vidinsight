import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.utils.log_standard import HumanFormatter, JsonFormatter
from app.utils.path_helper import get_runtime_path

# 日志目录
LOG_DIR = Path(get_runtime_path("logs"))
LOG_DIR.mkdir(exist_ok=True)

# 文件：JSON Lines + 5MB×3 轮转（桌面端长期挂机不再无限膨胀）
file_handler = RotatingFileHandler(
    LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024,
    backupCount=3, encoding="utf-8")
file_handler.setFormatter(JsonFormatter())

# 控制台：人类可读
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(HumanFormatter(
    fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"))


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)
        logger.propagate = False
    return logger
