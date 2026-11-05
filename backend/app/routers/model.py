from fastapi import APIRouter
from pydantic import BaseModel

from app.services.model import ModelService
from app.utils.response import ResponseWrapper as R
router = APIRouter()
modelService = ModelService()
class CreateModelRequest(BaseModel):
    provider_id: str
    model_name: str

# 返回体：模型信息
class ModelItem(BaseModel):
    id: int
    model_name: str
@router.get("/model_list")
def model_list():
    try:
        return R.success(modelService.get_all_models(True),msg="获取模型列表成功")
    except Exception as e:
        return R.error(e)
@router.get("/models/delete/{model_id}")
def delete_model(model_id: int):
    try:
        success = modelService.delete_model_by_id(model_id)
        if success:
            return R.success(msg="模型删除成功")
        else:
            return R.error("模型不存在或删除失败")
    except Exception as e:
        return R.error(f"删除模型失败: {e}")
@router.get("/model_list/{provider_id}")
def model_list(provider_id):

    return R.success(modelService.get_all_models_by_id(provider_id))


@router.post("/models")
def create_model(data: CreateModelRequest):
    success = ModelService.add_new_model(data.provider_id, data.model_name)
    if not success:
        return R.error("模型添加失败")
    return R.success(msg="模型添加成功")

@router.get("/model_enable/{provider_id}")
def get_enabled_models_by_provider(provider_id: str):
    try:
        models = modelService.get_enabled_models_by_provider(provider_id)
        return R.success(models, msg="获取启用模型成功")
    except Exception as e:
        return R.error(f"获取启用模型失败: {e}")


class CapabilityRequest(BaseModel):
    model_id: int
    capabilities: dict


@router.post("/model_capabilities")
def update_model_capabilities(data: CapabilityRequest):
    """更新模型能力标记（输入模态、上下文窗口、推理档位等）。

    结构参考 ZCode 开源版（zai-org/ZCode）的 completeModelPropertiesDataSchema。
    关键帧抽取等功能会先查这里，没勾读图的模型不启用画面分析。
    """
    try:
        from app.db.model_dao import update_model_capabilities as _upd
        from app.models.model_capabilities import ModelCapabilities
        caps = ModelCapabilities.from_stored(data.capabilities).to_stored()
        r = _upd(data.model_id, caps)
        if not r:
            return R.error("模型不存在")
        return R.success(r, msg="能力标记已保存")
    except Exception as e:
        return R.error(f"保存失败: {e}")


@router.get("/provider_templates")
def get_provider_templates():
    """供应商预设模板（结构参考 ZCode 的 templateRules）。

    前端「添加供应商」页按 group 分组展示，点选后自动填 baseUrl/apiFormat/模型列表。
    """
    try:
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / "data" / "provider_templates.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        return R.success(data.get("templates") or [])
    except Exception as e:
        return R.error(f"读取模板失败: {e}")


@router.get("/api_formats")
def get_api_formats():
    """API 协议格式枚举（照搬 ZCode ProviderApiFormatSelect 的「名称 + 路径」写法）。"""
    from app.models.model_capabilities import API_FORMAT_LABELS, API_FORMAT_PATHS
    return R.success([
        {"value": k, "label": API_FORMAT_LABELS[k], "path": API_FORMAT_PATHS[k]}
        for k in API_FORMAT_PATHS
    ])