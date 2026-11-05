from app.db.engine import get_db
from app.db.models.models import Model
from app.db.models.providers import Provider

import json


def _caps_of(m) -> dict:
    """解析能力标记为完整结构（兼容老数据 {"image": true}）。"""
    from app.models.model_capabilities import ModelCapabilities
    try:
        raw = json.loads(m.capabilities) if m.capabilities else {}
    except Exception:
        raw = {}
    return ModelCapabilities.from_stored(raw).to_stored()


def get_model_by_provider_and_name(provider_id: int, model_name: str):
    db = next(get_db())
    try:
        model = db.query(Model).filter_by(provider_id=provider_id, model_name=model_name).first()
        if model:
            return {
                "id": model.id,
                "provider_id": model.provider_id,
                "model_name": model.model_name,
                "capabilities": _caps_of(model),
                "created_at": model.created_at,
            }
        return None
    finally:
        db.close()


def insert_model(provider_id: int, model_name: str):
    db = next(get_db())
    try:
        model = Model(provider_id=provider_id, model_name=model_name)
        db.add(model)
        db.commit()
        db.refresh(model)
        return {
            "id": model.id,
            "provider_id": model.provider_id,
            "model_name": model.model_name,
            "capabilities": _caps_of(model),
            "created_at": model.created_at,
        }
    finally:
        db.close()


def update_model_capabilities(model_id: int, capabilities: dict):
    """更新模型能力标记（视觉/视频输入等）。"""
    db = next(get_db())
    try:
        model = db.query(Model).filter_by(id=model_id).first()
        if not model:
            return None
        model.capabilities = json.dumps(capabilities or {}, ensure_ascii=False)
        db.commit()
        db.refresh(model)
        return {"id": model.id, "model_name": model.model_name,
                "capabilities": _caps_of(model)}
    finally:
        db.close()


def get_models_by_provider(provider_id: int):
    db = next(get_db())
    try:
        models = db.query(Model).filter_by(provider_id=provider_id).all()
        return [{"id": m.id, "model_name": m.model_name,
                 "capabilities": _caps_of(m)} for m in models]
    finally:
        db.close()


def delete_model(model_id: int):
    db = next(get_db())
    try:
        model = db.query(Model).filter_by(id=model_id).first()
        if model:
            db.delete(model)
            db.commit()
    finally:
        db.close()


def get_all_models():
    db = next(get_db())
    try:
        # 只查询启用状态供应商的模型
        models = db.query(Model).join(Provider, Model.provider_id == Provider.id).filter(Provider.enabled == 1).all()
        return [
            {"id": m.id, "provider_id": m.provider_id, "model_name": m.model_name,
             "capabilities": _caps_of(m)}
            for m in models
        ]
    finally:
        db.close()
