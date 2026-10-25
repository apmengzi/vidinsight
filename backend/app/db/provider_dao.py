import json
import os
import sys
from app.db.models.models import Model
from app.db.models.providers import Provider
from app.utils.logger import get_logger
from app.db.engine import get_engine, Base, get_db

logger = get_logger(__name__)


def get_builtin_providers_path():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(__file__)
    return os.path.join(base_path, 'builtin_providers.json')


def seed_default_providers():
    db = next(get_db())
    try:
        json_path = get_builtin_providers_path()
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                providers = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read builtin_providers.json: {e}")
            return

        # 补缺式种子：只添加内置清单里数据库还没有的供应商，绝不覆盖
        # 用户已有的行（保留用户填的 key / 改过的 base_url）。
        existing_ids = {p.id for p in db.query(Provider).all()}
        added = []
        for p in providers:
            if p['id'] in existing_ids:
                continue
            db.add(Provider(
                id=p['id'],
                name=p['name'],
                api_key=p['api_key'],
                base_url=p['base_url'],
                logo=p['logo'],
                type=p['type'],
                enabled=p.get('enabled', 1)
            ))
            added.append(p['id'])
        db.commit()

        # 补缺模型：对**已存在**的供应商，只追加内置清单里 DB 还没有的默认模型，
        # 保留用户已选/改过的；这样 workbuddy 等内置池扩了模型后，老机器自动跟随。
        for p in providers:
            if p['id'] not in existing_ids:
                # 新供应商：把 default_models 全插进去
                for model_name in p.get('default_models', []):
                    db.add(Model(provider_id=p['id'], model_name=model_name))
                continue
            existing_models = {m.model_name for m in
                              db.query(Model).filter_by(provider_id=p['id']).all()}
            for model_name in p.get('default_models', []):
                if model_name not in existing_models:
                    db.add(Model(provider_id=p['id'], model_name=model_name))
                    logger.info(f"Seeded missing model: {p['id']}/{model_name}")
        db.commit()

        if added:
            logger.info(f"Seeded missing builtin providers: {added}")
        else:
            logger.info("All builtin providers already present, nothing to seed.")
    except Exception as e:
        logger.error(f"Failed to seed default providers: {e}")
    finally:
        db.close()


def insert_provider(id: str, name: str, api_key: str, base_url: str, logo: str, type_: str, enabled: int = 1):
    db = next(get_db())
    try:
        provider = Provider(id=id, name=name, api_key=api_key, base_url=base_url, logo=logo, type=type_, enabled=enabled)
        db.add(provider)
        db.commit()
        logger.info(f"Provider inserted successfully. id: {id}, name: {name}, type: {type_}")
        return id
    except Exception as e:
        logger.error(f"Failed to insert provider: {e}")
    finally:
        db.close()


def get_enabled_providers():
    db = next(get_db())
    try:
        return db.query(Provider).filter_by(enabled=1).all()
    finally:
        db.close()


def get_provider_by_name(name: str):
    db = next(get_db())
    try:
        return db.query(Provider).filter_by(name=name).first()
    finally:
        db.close()


def get_provider_by_id(id: str):
    db = next(get_db())
    try:
        return db.query(Provider).filter_by(id=id).first()
    finally:
        db.close()


def get_all_providers():
    db = next(get_db())
    try:
        return db.query(Provider).all()
    finally:
        db.close()


def update_provider(id: str, **kwargs):
    db = next(get_db())
    try:
        provider = db.query(Provider).filter_by(id=id).first()
        if not provider:
            logger.warning(f"Provider {id} not found for update.")
            return

        for key, value in kwargs.items():
            if hasattr(provider, key):
                setattr(provider, key, value)

        db.commit()
        logger.info(f"Provider updated successfully. id: {id}, updated_fields: {list(kwargs.keys())}")
    except Exception as e:
        logger.error(f"Failed to update provider: {e}")
    finally:
        db.close()


def delete_provider(id: str):
    db = next(get_db())
    try:
        provider = db.query(Provider).filter_by(id=id).first()
        if provider:
            db.delete(provider)
            db.commit()
            logger.info(f"Provider deleted successfully. id: {id}")
    except Exception as e:
        logger.error(f"Failed to delete provider: {e}")
    finally:
        db.close()