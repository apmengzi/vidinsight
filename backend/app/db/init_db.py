from app.db.models.models import Model
from app.db.models.providers import Provider
from app.db.models.video_tasks import VideoTask
from app.db.engine import get_engine, Base


def _ensure_columns(engine):
    """轻量列迁移：create_all 只建新表，不会给已有表加列。

    幂等；失败不阻塞启动（功能侧读不到列时按默认值处理）。
    """
    import sqlalchemy as sa

    _want = {
        "models": [("capabilities", "TEXT")],
        "providers": [("api_format", "VARCHAR")],
    }
    try:
        insp = sa.inspect(engine)
        tables = set(insp.get_table_names())
        for table, cols_to_add in _want.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, coltype in cols_to_add:
                if col in existing:
                    continue
                with engine.connect() as conn:
                    conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                    conn.commit()
    except Exception:
        pass


def init_db():
    engine = get_engine()

    Base.metadata.create_all(bind=engine)
    _ensure_columns(engine)
