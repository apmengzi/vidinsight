import os

from app.gpt.base import GPT
from app.gpt.provider.OpenAI_compatible_provider import OpenAICompatibleProvider
from app.gpt.universal_gpt import UniversalGPT
from app.gpt.workbuddy_provider import WorkBuddyClient, is_workbuddy_provider, workbuddy_enabled
from app.models.model_config import ModelConfig


class GPTFactory:
    @staticmethod
    def from_config(config: ModelConfig) -> GPT:
        if is_workbuddy_provider(config.provider, config.base_url):
            if not workbuddy_enabled():
                raise ValueError(
                    "WorkBuddy 免费模型池已被 WORKBUDDY_ENABLED=0 关闭，"
                    "请改用其他模型供应商"
                )
            # 并发闸门 / 退避重试 / 超时策略都收在 WorkBuddyClient 内
            client = WorkBuddyClient(api_key=config.api_key, base_url=config.base_url)
        else:
            client = OpenAICompatibleProvider(api_key=config.api_key, base_url=config.base_url).get_client
        return UniversalGPT(client=client, model=config.model_name)
