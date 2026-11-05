"""模型能力与选项规格（schema 参考 ZCode 开源版 zai-org/ZCode）。

设计取舍（对应 ZCode 的 complete/sparse 两层）：
    - 完整版 ModelCapabilities：执行前校验，字段齐全
    - 稀疏版只在需要覆盖时才写（这里用 dict 存 JSON，读时补默认值）

为什么把能力挂在模型上而不是供应商上：
    同一个供应商下不同模型能力差异巨大（GLM-5.3 支持读图，glm-5.3-flash 不一定），
    ZCode 的 providerSiteRules 甚至要按 baseUrl 区分，说明这个维度不能简化。

为什么 API 格式挂在供应商上：
    Anthropic Messages / Chat Completions / Responses 是协议层差异，
    同一供应商所有模型共用一套端点（ZCode 的 provider.api.type 也是这个层级）。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ── API 协议格式（ZCode: providerApiTypeDataSchema）────────────────────────
ApiFormat = Literal["anthropic-messages", "openai-chat-completions", "openai-responses"]

# UI 展示用：名称 + 端点路径（照搬 ZCode ProviderApiFormatSelect 的写法，
# 用户看到 /chat/completions 就知道该填什么，显著降低配错概率）
API_FORMAT_PATHS: dict[str, str] = {
    "anthropic-messages": "/v1/messages",
    "openai-chat-completions": "/chat/completions",
    "openai-responses": "/responses",
}

API_FORMAT_LABELS: dict[str, str] = {
    "anthropic-messages": "Anthropic Messages",
    "openai-chat-completions": "Chat Completions",
    "openai-responses": "Responses",
}


class ModelInputFormat(BaseModel):
    """输入模态（ZCode: completeModelInputFormatDataSchema）。"""

    supportsText: bool = True
    supportsImage: bool = False
    supportsVideo: bool = False
    supportsAudio: bool = False
    supportsPdf: bool = False


class ModelCapabilities(BaseModel):
    """模型能力全集（ZCode: completeModelPropertiesDataSchema + optionSpecs）。

    字段名沿用 ZCode 的 supports* 命名，语义已被上游验证，避免自己造词。
    """

    # —— 属性 ——
    contextWindow: Optional[int] = None
    maxOutputTokens: Optional[int] = None
    inputFormat: ModelInputFormat = Field(default_factory=ModelInputFormat)
    supportsToolCall: bool = False
    supportsJsonSchemaOutput: bool = False       # 结构化输出
    supportsNativeWebSearch: bool = False        # 原生联网搜索
    supportsMidConversationSystem: bool = False  # 对话中系统消息

    # —— 推理档位 ——
    # 按语义强度从低到高排列（ZCode 明确要求有序，首项是最低公开档位）
    reasoningLevels: list[str] = Field(default_factory=list)
    # 档位 → 请求体字段的映射方式。ZCode 用受限 CEL DSL，这里只覆盖实际
    # 用到的 4 种形态，够用且不用引 870 行 parser（见调研结论第 3 条）
    reasoningMap: Optional[str] = None

    # —— 向后兼容：老数据只有 {"image": true} ——
    @classmethod
    def from_stored(cls, raw: dict | None) -> "ModelCapabilities":
        """把库里的 JSON 解析成完整能力。兼容历史数据。"""
        if not raw:
            return cls()
        data = dict(raw)
        # 老格式 {"image": bool} → inputFormat.supportsImage
        if "image" in data and "inputFormat" not in data:
            data["inputFormat"] = {"supportsText": True, "supportsImage": bool(data.pop("image"))}
        else:
            data.pop("image", None)
        try:
            return cls(**data)
        except Exception:
            return cls()

    def to_stored(self) -> dict:
        return self.model_dump(exclude_none=True)


# 推理档位映射的四种形态（覆盖 GLM / DeepSeek / GPT-5 / Claude 实际用法）
REASONING_MAP_KINDS: dict[str, str] = {
    "reasoning_effort": "请求体 reasoning_effort = 档位值",
    "thinking_toggle": "请求体 thinking.type = enabled/disabled",
    "output_config_effort": "请求体 output_config.effort = 档位值",
    "enable_thinking": "请求体 enable_thinking = 布尔",
}
