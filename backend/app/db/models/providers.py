from sqlalchemy import Column, String, Integer, DateTime, func
from sqlalchemy.orm import declarative_base

from app.db.engine import Base


class Provider(Base):
    __tablename__ = "providers"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    logo = Column(String, nullable=False)
    type = Column(String, nullable=False)
    api_key = Column(String, nullable=False)
    base_url = Column(String, nullable=False)
    # API 协议格式（参考 ZCode provider.api.type）：
    # anthropic-messages / openai-chat-completions / openai-responses
    # 存在供应商级而非模型级——同一供应商所有模型共用一套端点协议
    api_format = Column(String, nullable=True)
    enabled = Column(Integer, default=1)
    created_at = Column(DateTime, server_default=func.now())