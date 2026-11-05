from sqlalchemy import Column, Integer, String, DateTime, func, ForeignKey, Text

from app.db.engine import Base


class Model(Base):
    __tablename__ = "models"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider_id = Column(Integer, nullable=False)
    model_name = Column(String, nullable=False)
    # 模型能力标记，JSON 字符串，如 {"image": true, "video": false}
    # 存字符串不存 JSON 列：SQLite 加列无需迁移，老库自动兼容（读不到即空）
    capabilities = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())