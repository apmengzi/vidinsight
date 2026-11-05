"""平台 Cookie 健康检查与风控冷却（稳定性分层防御，2026-10-02）。

背景实证：B 站风控(412)拦截时 yt-dlp 报错与"视频已下架"措辞相同，
Cookie（哪怕只是 buvid3 指纹 cookie）是防 412 的关键；SESSDATA 登录态更强。
本模块提供两件事：
  1. cookie 健康探测：区分 logged_in（SESSDATA 有效）/ anonymous（仅指纹
     cookie，防 412 够用）/ missing / error，给设置页与启动自检消费
  2. 平台级风控冷却：某平台被 412 拦截后进入全局冷却（默认 120s），
     冷却期内同平台新请求直接快速失败并告知等待时间——防止用户连点
     把限流越打越死（错误分类器触发，冷却自动过期）
"""
import threading
import time
from typing import Optional

import requests

from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Cookie 健康探测 ────────────────────────────────


def _cookie_dict(cookie_str: str) -> dict:
    d = {}
    for pair in (cookie_str or "").split("; "):
        if "=" in pair:
            k, v = pair.split("=", 1)
            d[k.strip()] = v
    return d


def check_bilibili_cookie() -> dict:
    """B 站 Cookie 健康探测。

    用官方 nav 接口（登录态自检的标准端点，轻量 GET）：
      - code=0 且 isLogin → logged_in（最强：SESSDATA 有效）
      - code=-101 → cookie 未登录，但若含 buvid3 等指纹 cookie仍能防 412
        → anonymous（够用，建议但不强制登录）
      - 其它 code / HTTP 异常 → error（可能风控或网络）
    """
    cookie = CookieConfigManager().get("bilibili")
    if not cookie:
        return {"platform": "bilibili", "status": "missing",
                "detail": "未配置 Cookie（裸请求易被 B 站 412 风控拦截）",
                "action": "到「设置 → Cookie」一键收集（打开 bilibili.com 即可，无需登录）"}

    ck = _cookie_dict(cookie)
    has_sessdata = "SESSDATA" in ck
    has_fingerprint = any(k in ck for k in ("buvid3", "b_nut", "b_lsid"))

    try:
        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            cookies={k: v for k, v in ck.items()},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                     "Referer": "https://www.bilibili.com/"},
            timeout=8,
        )
        data = resp.json()
        if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
            uname = data.get("data", {}).get("uname", "")
            return {"platform": "bilibili", "status": "logged_in",
                    "detail": f"登录态有效（{uname}）", "action": ""}
        # -101 未登录（指纹 cookie 仍防 412）；其它异常码视作风控/失效
        if data.get("code") == -101 and has_fingerprint:
            return {"platform": "bilibili", "status": "anonymous",
                    "detail": "指纹 Cookie 有效（可防风控限流），未配置登录态",
                    "action": "如需解析付费/会员内容，可一键收集登录态"}
        if has_fingerprint:
            return {"platform": "bilibili", "status": "anonymous",
                    "detail": f"nav 接口返回 code={data.get('code')}，指纹 Cookie 存在",
                    "action": "若反复失败请重新一键收集"}
        return {"platform": "bilibili", "status": "expired",
                "detail": f"Cookie 已失效（nav code={data.get('code')}）",
                "action": "请到「设置 → Cookie」重新一键收集"}
    except Exception as e:  # noqa: BLE001 —— 探测失败降级为 unknown，不阻塞业务
        logger.warning(f"B站 Cookie 健康探测异常: {e}")
        if has_sessdata or has_fingerprint:
            return {"platform": "bilibili", "status": "unknown",
                    "detail": f"探测请求失败（{str(e)[:60]}），Cookie 配置存在",
                    "action": ""}
        return {"platform": "bilibili", "status": "error",
                "detail": f"探测请求失败: {str(e)[:80]}",
                "action": "请检查网络后重试"}


def check_xhs_cookie() -> dict:
    """小红书 Cookie 登录态预检（2026-10-19）。

    为什么不用签名 API：/api/sns/web/v1/* 需要 x-s/x-t 签名，无签名一律
    406 `{"code":-1}` 或 500 `create invoker failed`（本次实测，真实与伪造
    cookie 返回完全一致）—— 不可用。

    判别方式（2026-11-05 重写，修误报）：

    **不能只看 `/explore` 的 `"loggedIn"` 标志**。实测发现：真实有效的
    cookie（web_session/id_token/a1 齐全）访问 `/explore` 仍返回
    `"loggedIn":false` —— 这个字段反映的是「SSR 渲染时的登录态快照」，
    对带 cookie 的普通 HTTP 请求不可靠，导致用户明明登录着却被报「已过期」。

    改用 `/user/profile/me` 的**状态码**判别（实测三态清晰）：
      - 真实有效 cookie → **404**（路由不存在，但请求被放行）
      - 伪造 web_session → **500**（服务端解析会话失败）
      - 无 cookie        → **302** 跳 /explore 或 /login

    同时保留 `/explore` 的 loggedIn 作为**加分项**：任一信号为真即判有效，
    两个都为假才判失效——宁可少报错，不要误伤用户。
    """
    cookie = CookieConfigManager().get("xiaohongshu")
    if not cookie:
        return {"platform": "xiaohongshu", "status": "missing",
                "detail": "未配置小红书 Cookie",
                "action": "请打开收藏专辑捕获窗口完成登录（窗口关闭会自动回写登录态）"}

    has_session = "web_session=" in cookie
    if not has_session:
        return {"platform": "xiaohongshu", "status": "expired",
                "detail": "Cookie 中没有 web_session（未登录或登录态未回写）",
                "action": "请打开捕获窗口重新登录，关闭窗口后登录态会自动保存"}

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        "Referer": "https://www.xiaohongshu.com/",
        "Cookie": cookie,
    }

    # 主判据：/user/profile/me 的状态码
    me_status = None
    try:
        me = requests.get("https://www.xiaohongshu.com/user/profile/me",
                          headers=headers, timeout=10, allow_redirects=False)
        me_status = me.status_code
    except Exception as e:  # noqa: BLE001
        logger.warning(f"小红书 /user/profile/me 探测异常: {e}")

    # 辅助判据：/explore 的 loggedIn（可能为 false，仅作加分）
    explore_logged_in = False
    try:
        resp = requests.get("https://www.xiaohongshu.com/explore",
                            headers=headers, timeout=10)
        explore_logged_in = '"loggedIn":true' in resp.text
    except Exception as e:  # noqa: BLE001
        logger.warning(f"小红书 /explore 探测异常: {e}")

    # 404 = 已登录（路由不存在但请求放行）；200/500/302 = 视情况
    if me_status == 404 or explore_logged_in:
        return {"platform": "xiaohongshu", "status": "logged_in",
                "detail": "登录态有效", "action": ""}

    if me_status == 500:
        return {"platform": "xiaohongshu", "status": "expired",
                "detail": "登录态已失效（服务端拒绝该会话）",
                "action": "登录态已过期，请打开捕获窗口重新登录"}

    if me_status is None:
        # 两个探测都失败：网络问题，不能判用户登录态失效
        return {"platform": "xiaohongshu", "status": "unknown",
                "detail": "探测请求失败，无法确认登录态（Cookie 配置存在）",
                "action": ""}

    return {"platform": "xiaohongshu", "status": "expired",
            "detail": f"登录态已失效（profile 返回 {me_status}）",
            "action": "登录态已过期，请打开捕获窗口重新登录"}


def check_platform_cookie(platform: str) -> dict:
    """统一入口：按平台分派健康探测（后续平台在此注册）。"""
    if platform == "bilibili":
        return check_bilibili_cookie()
    if platform in ("xiaohongshu", "xhs"):
        return check_xhs_cookie()
    return {"platform": platform, "status": "unsupported",
            "detail": "该平台暂无健康探测通道", "action": ""}


# ── 平台级风控冷却 ────────────────────────────────

_COOLDOWN_SECONDS = 120  # 412 后默认冷却 2 分钟（B 站风控实测几十秒~几分钟自愈）

_cooldowns: dict[str, float] = {}
_lock = threading.Lock()


def mark_cooldown(platform: str, seconds: float = _COOLDOWN_SECONDS) -> float:
    """标记平台进入风控冷却，返回到期时间戳。由错误分类器在 412 时调用。"""
    with _lock:
        until = time.time() + seconds
        prev = _cooldowns.get(platform, 0)
        _cooldowns[platform] = max(until, prev)
        logger.warning(f"[风控冷却] {platform} 进入冷却 {seconds:.0f}s（至 {time.strftime('%H:%M:%S', time.localtime(_cooldowns[platform]))}）")
        return _cooldowns[platform]


def cooldown_remaining(platform: str) -> float:
    """平台冷却剩余秒数（0 = 不在冷却）。提交/预览前检查，避免连环触发。"""
    with _lock:
        until = _cooldowns.get(platform, 0)
        remain = until - time.time()
        return max(0.0, remain)


def clear_cooldown(platform: str) -> None:
    """成功请求后解除冷却（一次成功即证明风控已缓解）。"""
    with _lock:
        _cooldowns.pop(platform, None)
