import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
from app.utils.path_helper import get_runtime_path

load_dotenv()

# 默认 SQLite。桌面打包后不能写入安装目录，默认落到用户数据目录。
_default_db_path = get_runtime_path("vidinsight.db").replace("\\", "/")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_default_db_path}")

# SQLite 需要特定连接参数，其他数据库不需要
engine_args = {}
if DATABASE_URL.startswith("sqlite"):
    engine_args["connect_args"] = {"check_same_thread": False}

_pool_args = {}
if not DATABASE_URL.startswith("sqlite"):
    _pool_args = {
        "pool_size": int(os.getenv("DB_POOL_SIZE", "10")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "20")),
        "pool_pre_ping": True,
    }

engine = create_engine(
    DATABASE_URL,
    echo=os.getenv("SQLALCHEMY_ECHO", "false").lower() == "true",
    **engine_args,
    **_pool_args,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_engine():
    return engine


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
