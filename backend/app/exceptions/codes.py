"""错误码注册表：全项目业务错误的唯一出处。

设计约定（doc/ENGINEERING_STANDARD.md）：
- err  = 机器可读字符串码（大写下划线），日志 grep / 前端透出 / 用户上报都认它
- code = 整数业务码，保持与历史 ResponseWrapper 兼容（前端已有按 code 判断的逻辑）
- message = 面向用户的默认文案；调用方可以覆盖 message 但不应绕过注册表编 err

整数段位划分（避免与既有码冲突）：
  0/1          通用成功失败（status_code.StatusCode）
  1001-1003    下载/转写/生成（StatusCode）
  2001-2002    参数类（StatusCode）
  2001xx       供应商域（enmus/exception.ProviderErrorEnum）
  3001xx       笔记域（enmus/exception.NoteErrorEnum）
  4001xx       小红书域
  4002xx       合集展开域
  500001       未分类内部错误（兜底）
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorSpec:
    err: str          # 机器可读错误码
    code: int         # 整数业务码
    message: str      # 默认用户文案


class ErrorCode:
    # ── 小红书域（4001xx）──
    XHS_PAGE_FETCH_FAILED = ErrorSpec(
        "XHS_PAGE_FETCH_FAILED", 400101, "小红书页面获取失败：链接可能已失效，请重新分享")
    XHS_NOTE_NOT_FOUND = ErrorSpec(
        "XHS_NOTE_NOT_FOUND", 400102,
        "未在页面数据中找到笔记内容：分享链接可能过期，或需要在设置页配置小红书 Cookie（网页版登录态）")
    XHS_COLLECTION_EMPTY = ErrorSpec(
        "XHS_COLLECTION_EMPTY", 400103,
        "未在合集页面解析到笔记清单：请确认已配置小红书 Cookie（网页版登录态）后重试")
    XHS_LOGIN_REQUIRED = ErrorSpec(
        "XHS_LOGIN_REQUIRED", 400104,
        "小红书登录态已失效（设备登录超限被踢下线）：请在 VidInsight 打开的小红书浏览窗口里重新扫码登录，或稍后再试")

    # ── 合集展开域（4002xx）──
    COLLECTION_PLATFORM_UNSUPPORTED = ErrorSpec(
        "COLLECTION_PLATFORM_UNSUPPORTED", 400201,
        "该平台的合集需要平台签名接口（a_bogus / __NS_sig3），目前不支持合集批量解析；可以逐条分享笔记链接解析。")
    COLLECTION_EXPAND_FAILED = ErrorSpec(
        "COLLECTION_EXPAND_FAILED", 400202, "合集展开失败")

    # ── LLM 供应商域（4003xx）──
    LLM_PROVIDER_TIMEOUT = ErrorSpec(
        "LLM_PROVIDER_TIMEOUT", 400301,
        "LLM 供应商响应超时或网关错误，请稍后重试")

    # ── 兜底（500001）──
    INTERNAL = ErrorSpec("INTERNAL", 500001, "系统内部错误")


_REGISTRY: dict[str, ErrorSpec] = {}
for _name, _spec in vars(ErrorCode).items():
    if isinstance(_spec, ErrorSpec):
        if _spec.err in _REGISTRY:
            raise RuntimeError(f"错误码重复注册: {_spec.err}")
        _REGISTRY[_spec.err] = _spec


def get_error_spec(err: str) -> ErrorSpec | None:
    """按 err 码查注册表；未注册返回 None（不抛错，方便渐进迁移）。"""
    return _REGISTRY.get(err)


def validate_registry() -> list[str]:
    """启动自检：整数码无冲突、err 无重复、文案非空。返回问题列表（空=通过）。"""
    problems: list[str] = []
    seen_codes: dict[int, str] = {}
    for err, spec in _REGISTRY.items():
        if not spec.message.strip():
            problems.append(f"{err}: 空文案")
        if spec.code in seen_codes:
            problems.append(f"{err}: 整数码 {spec.code} 与 {seen_codes[spec.code]} 冲突")
        seen_codes[spec.code] = err
    return problems
