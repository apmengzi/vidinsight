"""LLM 供应商错误归类：把上游网关/超时类异常翻译成注册表错误码。

背景（2026-09-14 E2E 实测）：tokenrhythm 网关 504 时，异常文本是整页
HTML，被原样写进任务状态与前端 toast——用户看到的是 HTML 而非人话。
"""
from app.exceptions.codes import ErrorSpec, ErrorCode


def classify_llm_error(exc: BaseException) -> ErrorSpec | None:
    """识别供应商网关/超时类错误；非此类返回 None（保持原样透传）。"""
    text = str(exc)
    lowered = text.lower()
    if ("504" in text or "gateway time-out" in lowered or "502" in text
            or "bad gateway" in lowered or "timed out" in lowered
            or "timeout" in lowered or "connection aborted" in lowered):
        return ErrorCode.LLM_PROVIDER_TIMEOUT
    return None
