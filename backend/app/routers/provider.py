from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel

from app.exceptions.provider import ProviderError
from app.models.model_config import ModelConfig
from app.services.model import ModelService
from app.utils.response import ResponseWrapper as R
from app.services.provider import ProviderService

router = APIRouter()

#  新增 type 字段
class ProviderRequest(BaseModel):
    name: str
    api_key: str
    base_url: str
    logo: Optional[str] = None
    type: str

class TestRequest(BaseModel):
    id: str
    # 可选：指定用哪个 model 跑连通性测试；不传则用该 provider 在 DB 里的第一个模型
    model: Optional[str] = None
class ProviderUpdateRequest(BaseModel):
    id: str
    name: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    logo: Optional[str] = None
    type: Optional[str] = None
    enabled:Optional[int] = None

@router.post("/add_provider")
def add_provider(data: ProviderRequest):
    try:
        res = ProviderService.add_provider(
            name=data.name,
            api_key=data.api_key,
            base_url=data.base_url,
            logo=data.logo,
            type_=data.type
        )
        return R.success(msg='添加模型供应商成功',data=res)
    except Exception as e:
        return R.error(msg=e)

@router.get("/get_all_providers")
def get_all_providers():
    try:
        res = ProviderService.get_all_providers_safe()
        return R.success(data=res)
    except Exception as e:
        return R.error(msg=e)

@router.get("/get_provider_by_id/{id}")
def get_provider_by_id(id: str):
    try:
        res = ProviderService.get_provider_by_id_safe(id)
        return R.success(data=res)
    except Exception as e:
        return R.error(msg=e)
#
# @router.get("/get_provider_by_name/{name}")
# def get_provider_by_name(name: str):
#     try:
#         res = ProviderService.get_provider_by_name(name)
#         return R.success(data=res)
#     except Exception as e:
#         return R.error(msg=e)


@router.post("/update_provider")
def update_provider(data: ProviderUpdateRequest):
    try:
        if all(
            field is None
            for field in [data.name, data.api_key, data.base_url, data.logo, data.type,data.enabled]
        ):
            return R.error(msg='请至少填写一个参数')

        updated_provider =ProviderService.update_provider(
            id=data.id,
            data=dict(data)
        )
        if updated_provider:
            return R.success(msg='更新模型供应商成功', data=updated_provider)
        else:
            return R.error(msg='更新模型供应商失败')
    except Exception as e:
        print(e)
        return R.error(msg=str(e))

@router.post('/connect_test')
def gpt_connect_test(data: TestRequest):
    ModelService().connect_test(data.id, model=data.model)
    return R.success(msg='连接成功')


class RemoteModelsRequest(BaseModel):
    api_key: str
    base_url: str

@router.post('/remote_models')
def list_remote_models(data: RemoteModelsRequest):
    """真实拉取供应商的可用模型列表（OpenAI 兼容 /v1/models）。

    首启引导用：用户填完 API Key 后点「检测可用模型」，下拉条直接选，
    不用猜模型名。注意两个已知边界（与 connect_test 同源）：
      - 部分网关不实现 /v1/models → 返回明确失败提示，回退手填
      - 代理网关可能返回「全量模型清单」而非 key 可用清单——是参考不是保证
    """
    try:
        from app.gpt.provider.OpenAI_compatible_provider import OpenAICompatibleProvider
        client = OpenAICompatibleProvider(data.api_key, data.base_url).get_client
        models = sorted(m.id for m in client.models.list() if getattr(m, 'id', None))
        return R.success({"models": models, "count": len(models)})
    except Exception as e:
        return R.error(msg=f"拉取模型列表失败：{e}")

