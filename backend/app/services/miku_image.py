# -*- coding: utf-8 -*-
"""Miku AI 生图渠道客户端（miku.app.itstudio.club）。

协议实测（2026-11-10 抓取，纯 HTTP 跑通，无浏览器依赖）：
  登录   POST /api/v1/auth/login  {"email","password"} → {"data":{"token": JWT}}
         无验证码；JWT 有效期约 3 天 → 本地缓存、临期自动重登
  鉴权   Authorization: Bearer <token> + X-Miku-Device-ID / X-Device-ID
         （设备 ID 是任意 dev_<uuid>，本站只按它做设备标识，可自造）
  生图   POST /api/v1/images/generate
         {"model","prompt","input_images":[],"parameters":{quality,
          output_format,size,size_tier},"workspace_id":null}
         → {"data":{"task_id","estimated_cost","status":"pending"}}
  轮询   GET /api/v1/images/tasks/<task_id>
         pending → processing → completed；实测 GPT Image 2.6：
         1K 1280x720 约 45s，1.5K 1024x1536 约 38s
  取图   completed 时 result_url 为站内签名路径（?exp=&sig=），
         无需鉴权直接 GET 可下载

积分现实（如实记录，不做"无限"假设）：
  · 匿名 generate 返回 401 —— 必须登录
  · 按张计费：gpt-image-2.6-sunburst 1K=0.3、1.5K 竖版=0.35 credits/张；
    余额不足时任务不会创建（接口报错）
  · 每日签到 0.7~1.2 分（滑块验证码），积分 14 天过期——只能滚动补充，
    不存在一次性无限额度；调用方必须有失败回退路径

配置（环境变量优先，其次 config/miku.json；密钥绝不进源码/仓库）：
  MIKU_EMAIL / MIKU_PASSWORD / MIKU_BASE(默认上面站点) / MIKU_MODEL
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import uuid

import requests

# 本机常驻代理链（7895 中转）会对 miku 的 TLS 直接断连（实测 10054/SSLEOFError），
# 直连可达。实测（.tmp/miku_cdp/proxy_probe.py，2026-11-10）：
#   requests 2.32+ 里 proxies={http:None,https:None} 会被 env 代理回填（resolve_proxies），
#   照样走 7895 被掐断；唯一可靠做法是 Session.trust_env=False（不读任何 env 代理）。
# 故所有 miku 请求统一走 _rpost/_rget。NO_PROXY 仅为其他库兜底，不作数。
os.environ["NO_PROXY"] = (os.environ.get("NO_PROXY", "") + ",miku.app.itstudio.club").lstrip(",")


def _direct_session():
    s = requests.Session()
    s.trust_env = False  # 忽略 HTTP(S)_PROXY/netrc，强制直连
    return s


def _rpost(url, **kw):
    # POST 不自动重试（generate 有建任务副作用，重试可能双份扣费）；
    # 真遇连接重置由上层换模型/换渠道兜底。
    return _direct_session().post(url, **kw)


def _rget(url, **kw):
    # GET 幂等安全，对瞬时连接重置（本机代理链抖动/服务端限流窗口）退避重试两次。
    for attempt in range(3):
        try:
            return _direct_session().get(url, **kw)
        except requests.ConnectionError:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://miku.app.itstudio.club"
# 最省积分选择（2026-11-10 /models 实测价目）：0.15/张的有两家——
#   qwen-image-2.1-turbo：Qwen-Image 系，中文文字渲染强，正合一图流「文字逐字照抄」需求；
#   midjourney-8.2：同样 0.15 但中文排版不可靠，不选。
# 0.3 档的 gpt-image-2.6 / 0.35 的 nano-banana-pro 留给「想要更好看」时手动改配置。
DEFAULT_MODEL = "qwen-image-2.1-turbo"
# 一图流竖版：4:5 @1K = 1024x1280。注意 qwen/midjourney 只有 1K 档
# （price_by_size_tier 实测），发 1.5K 会被服务端判「no available channel」；
# 1024x1280 对一图流卡片足够，成本 0.15。想要更高清可把 model 与 1.5K 一起改。
PORTRAIT_SIZE = "1024x1280"
PORTRAIT_TIER = "1K"

_token_lock = threading.Lock()
_token_state = {"token": "", "exp_ts": 0.0, "device_id": ""}


class MikuError(RuntimeError):
    """渠道侧失败（鉴权/积分/任务失败等），message 保留服务端原文片段。"""


def _config_dir() -> str:
    from app.utils.path_helper import get_runtime_path
    return get_runtime_path("config")


def load_config() -> dict | None:
    """返回 {base,email,password,model,device_id}；未配置返回 None。

    环境变量 MIKU_EMAIL/MIKU_PASSWORD 优先，其次 config/miku.json
    （与 image_gen.json 同一套做法，凭据不进仓库）。
    """
    email = os.getenv("MIKU_EMAIL", "").strip()
    password = os.getenv("MIKU_PASSWORD", "").strip()
    base = os.getenv("MIKU_BASE", "").strip() or DEFAULT_BASE
    model = os.getenv("MIKU_MODEL", "").strip() or DEFAULT_MODEL
    if not (email and password):
        try:
            cfg_path = os.path.join(_config_dir(), "miku.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                email = (cfg.get("email") or "").strip() or email
                password = (cfg.get("password") or "").strip() or password
                base = (cfg.get("base") or "").strip() or base
                model = (cfg.get("model") or "").strip() or model
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[miku] 读取 config/miku.json 失败: {str(e)[:100]}")
    if not (email and password):
        return None
    return {"base": base.rstrip("/"), "email": email, "password": password, "model": model}


def is_configured() -> bool:
    return load_config() is not None


def _device_id(cfg: dict) -> str:
    """稳定设备 ID：存 config/miku_device_id.txt，首次随机生成。

    服务端用它做设备标识；固定一个比每次登录换新 ID 更贴近真实浏览器行为。
    """
    with _token_lock:
        if _token_state["device_id"]:
            return _token_state["device_id"]
    path = os.path.join(_config_dir(), "miku_device_id.txt")
    try:
        if os.path.exists(path):
            dev = open(path, "r", encoding="utf-8").read().strip()
            if dev:
                with _token_lock:
                    _token_state["device_id"] = dev
                return dev
    except Exception:
        pass
    dev = "dev_" + str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(dev)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[miku] 设备 ID 持久化失败（本次内存内使用）: {str(e)[:80]}")
    with _token_lock:
        _token_state["device_id"] = dev
    return dev


def _jwt_exp(token: str) -> float:
    """解 JWT exp（秒）；解不出按 20 分钟后过期处理（宁可提前重登）。"""
    try:
        pl = token.split(".")[1]
        pl += "=" * (-len(pl) % 4)
        return float(json.loads(base64.urlsafe_b64decode(pl)).get("exp", 0))
    except Exception:
        return time.time() + 1200


def _login(cfg: dict) -> str:
    dev = _device_id(cfg)
    resp = _rpost(
        f"{cfg['base']}/api/v1/auth/login",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/json",
            "X-Miku-Device-ID": dev, "X-Device-ID": dev,
        },
        # 中文不参与（email 是 ASCII），但仍显式 UTF-8，与既有渠道口径一致
        data=json.dumps({"email": cfg["email"], "password": cfg["password"]},
                        ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )
    if resp.status_code != 200:
        raise MikuError(f"miku 登录失败 HTTP {resp.status_code}: {resp.text[:160]}")
    j = resp.json()
    token = ((j.get("data") or {}).get("token")) or ""
    if not token:
        raise MikuError(f"miku 登录未返回 token: {str(j)[:160]}")
    with _token_lock:
        _token_state["token"] = token
        _token_state["exp_ts"] = _jwt_exp(token)
    logger.info("[miku] 登录成功，token 有效期至 %s",
                time.strftime("%m-%d %H:%M", time.localtime(_token_state["exp_ts"])))
    return token


def _token(cfg: dict, force: bool = False) -> str:
    with _token_lock:
        tok, exp = _token_state["token"], _token_state["exp_ts"]
    # 剩余不足 1 小时就重登（登录无验证码，成本极低）
    if not force and tok and exp - time.time() > 3600:
        return tok
    return _login(cfg)


def _headers(cfg: dict, token: str) -> dict:
    dev = _device_id(cfg)
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Miku-Device-ID": dev, "X-Device-ID": dev,
    }


def credit_balance(cfg: dict | None = None) -> float | None:
    """尽力获取当前积分余额；失败返回 None（调用方只做日志展示用）。"""
    cfg = cfg or load_config()
    if not cfg:
        return None
    try:
        r = _rget(f"{cfg['base']}/api/v1/user/credit-overview",
                         headers=_headers(cfg, _token(cfg)), timeout=20)
        if r.status_code == 200:
            return float(((r.json().get("data") or {}).get("current_balance")))
    except Exception:
        pass
    return None


# 模型通道会整体下线（qwen 实测「no available channel」）。按价格升序 + 健康度
# 选后备（/models/health 实测：1K 档 95~100% 成功率的都在此列）。
FALLBACK_MODEL_CHAIN = ["qwen-image-2.1-turbo", "gemini-3.1-flash-image-preview",
                        "gpt-image-2.5-sunburst", "gpt-image-2"]


def generate(prompt: str, *, size: str = PORTRAIT_SIZE, size_tier: str = PORTRAIT_TIER,
             model: str | None = None, wait_timeout_s: int = 240, poll_s: float = 4.0) -> dict:
    """文生图一次，返回 {url, task_id, cost, model}；失败抛 MikuError。

    url 是带签名的完整下载地址（可直接 GET，无需鉴权），调用方应立即落盘。
    首选渠道内最便宜模型；若该模型「无可用通道」，沿 FALLBACK_MODEL_CHAIN 换模型重试。
    """
    cfg = load_config()
    if not cfg:
        raise MikuError("未配置 miku 渠道（MIKU_EMAIL/MIKU_PASSWORD 或 config/miku.json）")
    wanted = model or cfg["model"]
    chain, seen = [], set()
    for mm in [wanted] + FALLBACK_MODEL_CHAIN:
        if mm not in seen:
            seen.add(mm); chain.append(mm)
    last_err: MikuError | None = None
    for i, mm in enumerate(chain):
        try:
            return _generate_on(cfg, prompt, size=size, size_tier=size_tier,
                                model=mm, wait_timeout_s=wait_timeout_s, poll_s=poll_s)
        except MikuError as e:
            last_err = e
            s = str(e)
            if "no available channel" in s or "不可用" in s:
                logger.warning("[miku] 模型 %s 无可用通道，换下一个（%d/%d）", mm, i + 1, len(chain))
                continue
            raise
    raise last_err


def _generate_on(cfg: dict, prompt: str, *, size: str, size_tier: str,
                 model: str, wait_timeout_s: int, poll_s: float) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "input_images": [],
        "parameters": {"quality": "auto", "output_format": "png",
                       "size": size, "size_tier": size_tier},
        "workspace_id": None,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")  # 中文 prompt 必须 ensure_ascii=False + utf-8（与灵栈渠道同坑）

    def _post_once():
        headers = _headers(cfg, _token(cfg))
        return _rpost(f"{cfg['base']}/api/v1/images/generate", headers=headers,
                             data=body, timeout=60)

    resp = _post_once()
    if resp.status_code == 401:  # token 中途失效：强制重登重试一次
        headers = _headers(cfg, _token(cfg, force=True))
        resp = _rpost(f"{cfg['base']}/api/v1/images/generate", headers=headers,
                             data=body, timeout=60)
    if resp.status_code != 200:
        raise MikuError(f"miku generate HTTP {resp.status_code}: {resp.text[:200]}")
    j = resp.json()
    if j.get("code") != 0:
        raise MikuError(f"miku generate code={j.get('code')}: {str(j)[:200]}")
    d = j.get("data") or {}
    task_id = d.get("task_id")
    cost = d.get("estimated_cost")
    if not task_id:
        raise MikuError(f"miku 未返回 task_id: {str(j)[:200]}")

    t0 = time.time()
    last: dict = {}
    while True:
        time.sleep(poll_s)
        st = _rget(f"{cfg['base']}/api/v1/images/tasks/{task_id}",
                          headers=_headers(cfg, _token(cfg)), timeout=30)
        if st.status_code != 200:
            raise MikuError(f"miku 任务查询 HTTP {st.status_code}: {st.text[:160]}")
        last = ((st.json() or {}).get("data") or {})
        status = last.get("status")
        if status == "completed":
            break
        if status in ("failed", "error", "canceled", "cancelled"):
            raise MikuError(f"miku 任务失败: {(last.get('error') or last.get('final_error_message') or status)[:200]}")
        if time.time() - t0 > wait_timeout_s:
            raise MikuError(f"miku 任务超时（{int(wait_timeout_s)}s，最后状态 {status}）")

    rel = last.get("result_url") or ""
    if not rel:
        raise MikuError(f"miku 任务完成但无 result_url: {str(last)[:200]}")
    logger.info("[miku] 生成完成 task=%s model=%s cost=%s 用时=%.1fs", task_id, model, cost, time.time() - t0)
    return {"url": f"{cfg['base']}{rel}", "task_id": task_id, "cost": cost, "model": model}


# ── 每日签到（把 0.7~1.2/天变成自维持供给）─────────────────────
# 协议复刻自 CheckIn chunk（2026-11-10 全部实测打通，纯 HTTP 一次通过拿 token）：
#   POST /check-in/captcha                → {id, background_image, template_image,
#                                           nonce, sign_key, issued_at, 宽高...}
#   POST /check-in/captcha/verify         → {id, sig:{v,nonce,ts,ph,mac}, traj,
#                                           offset_x, duration_ms, slider_max, pt, data:{...}}
#   sig: ph  = sha256hex("offset_x=..;duration_ms=..;slider_max=..;pt=..;traj=t,x,y|..")
#        mac = hmac_sha256hex(sign_key, "miku-ci-v1\n{id}\n{nonce}\n{issued_at}\n{ph}\n{UA}")
#              —— 连接符是真实换行（曾误读空格，是纯 HTTP 一直 400 的唯一根因）
#   POST /check-in                        → {mode:'lucky'|'fixed', captcha_token}
# 注意：一张验证码只有一次有效尝试（后续提交一律计失败）；offset 语义 = 条带拖动量
# （自然像素），服务端容差有限（<30px），检测偶失手 → 多张新码逐张一枪。

import hashlib
import hmac as _hmac
import io


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _hmac_sha256_hex(key: str, msg: str) -> str:
    return _hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def solve_slider(bg_bytes: bytes, tpl_bytes: bytes) -> dict:
    """检测滑块拼图缺口位置，返回 {strip_x, piece_x, score}。

    实测两种缺口形态：挖空近黑，或原图内容压成灰罩——共同不变量是
    「洞内显著比周围暗」。判据 = 环带均值 − 内区均值（亮度）：掩码腐蚀成
    内区、外扩成环带，滑窗取亮度降最大。两张存档真值对照下峰值一致收敛（±3px）。
    纯 numpy（本机 venv 无 cv2，不新增依赖）。
    """
    import numpy as np
    from PIL import Image

    bg = np.asarray(Image.open(io.BytesIO(bg_bytes)).convert("L"), dtype=np.float64)
    tpl = np.asarray(Image.open(io.BytesIO(tpl_bytes)).convert("RGBA"))
    alpha = tpl[:, :, 3] > 32
    ys, xs = np.where(alpha)
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    xt0, xt1 = int(xs.min()), int(xs.max()) + 1

    M = alpha[y0:y1, xt0:xt1]
    er = M.copy()  # 内区：掩码腐蚀 8 轮，避开描边
    for _ in range(8):
        e2 = er.copy()
        e2[1:, :] &= er[:-1, :]; e2[:-1, :] &= er[1:, :]
        e2[:, 1:] &= er[:, :-1]; e2[:, :-1] &= er[:, 1:]
        er = e2
    if not er.any():
        er = M
    dl = M.copy()  # 环带：外扩 6 轮后减内区
    for _ in range(6):
        t = dl.copy()
        t[1:, :] |= dl[:-1, :]; t[:-1, :] |= dl[1:, :]
        t[:, 1:] |= dl[:, :-1]; t[:, :-1] |= dl[:, 1:]
        dl = t
    ring = dl & ~er
    if not ring.any():
        ring = dl & ~M

    bgH, bgW = bg.shape
    strip_w = tpl.shape[1]
    best_score, best_x = -1e18, 0
    for x0 in range(0, bgW - strip_w + 1):
        win = bg[y0:y1, x0 + xt0:x0 + xt1]
        s = float(win[ring].mean() - win[er].mean())
        if s > best_score:
            best_score, best_x = s, x0
    logger.debug("[miku] slider lum-drop best=%.1f at strip_x=%d", best_score, best_x)
    # 实测语义已定：strip_x（窗口原点 = 洞左沿 - 透明内缩）即正确 offset，容差约 ±15px；
    # piece_x 仅留作参考，不再是候选语义。
    return {"strip_x": best_x, "piece_x": best_x + xt0, "score": best_score}


def solve_slider_v5(bg_bytes: bytes, tpl_bytes: bytes) -> dict:
    """v5「去饱和+局部亮度降」列信号：实测洞相对左右邻域主要不变量是饱和度骤降
    （scap 实测洞列 sat≈0.11 vs 邻列 0.26-0.35），亮度降作辅助项。
    返回 {'off': 提交用 offset, 'score': float}；单验证码只有一次尝试机会，
    检测偶发 ±40px 误差，由 checkin() 多张重试兜底。"""
    import io
    import numpy as np
    from PIL import Image
    bgrgb = np.asarray(Image.open(io.BytesIO(bg_bytes)).convert("RGB"), dtype=np.float64)
    tpl = np.asarray(Image.open(io.BytesIO(tpl_bytes)).convert("RGBA"))
    alpha = tpl[:, :, 3] > 32
    ys, xs = np.where(alpha)
    y0, y1, xt0, xt1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    ow = xt1 - xt0
    L = bgrgb.mean(axis=2)
    mx = bgrgb.max(axis=2); mn = bgrgb.min(axis=2)
    sat = (mx - mn) / (mx + 1.0)
    col_sat = sat[y0:y1, :].mean(axis=0)
    col_med = np.median(L[y0:y1, :], axis=0)
    H, W = L.shape
    scores = []
    for x0 in range(0, W - ow + 1):
        l0 = max(0, x0 - 30); r1 = min(W, x0 + ow + 30)
        ls, rs = col_sat[l0:x0], col_sat[x0 + ow:r1]
        lm, rm = col_med[l0:x0], col_med[x0 + ow:r1]
        if len(ls) + len(rs) < 20:
            scores.append(-9e9); continue
        nsat = np.concatenate([ls, rs]).mean()
        nmed = np.concatenate([lm, rm]).mean()
        wsat = col_sat[x0:x0 + ow].mean()
        wmed = col_med[x0:x0 + ow].mean()
        gate = (col_sat[x0:x0 + ow] < nsat - 0.06).mean()
        scores.append((nsat - wsat) + 0.05 * (nmed - wmed) - 0.30 * (1 - gate))
    r = np.array(scores); r[:15] = -9e9
    peak = int(np.argmax(r))
    return {"off": peak, "strip_x": peak, "score": float(r[peak])}


# 真实 UI 验证通过的坐标系（2026-11-10 实测，.tmp/miku_cdp/ui_checkin2.py 一次通过）：
# 页面 CSS 固定把背景显示宽钳到 300px（checkin chunk 的 E=300），据此推出全部量纲——
#   scale = 300 / bg_w（显示/自然像素比）；ut = 300 - tpl_w*scale（条带显示行程）；
#   slider_max = Z = slider.clientWidth - 46（滑轨行程，实测 356-46=310，不是 bg_w-tpl_w！）；
#   traj 的 dx/dy 是鼠标 CSS 像素（末点 ≈ P，即滑轨行程内位移）；
#   offset_x / data.trackList 的 x 是自然像素：offset = (P/Z)·ut/scale。
# 之前失败的原因正是把 traj 和 slider_max 也用自然像素，帧不自洽。
STAGE_W = 300
SLIDER_PAD = 46
SLIDER_TRAVEL = 310  # 356-46，桌面端 dialog 实测值；纯 HTTP 侧取同值保持帧自洽


def _verify_cap(cfg: dict, headers: dict, cap: dict, offset_x: int) -> str:
    """对给定验证码提交真实 UI 帧的 verify 载荷，成功返回 captcha_token。"""
    bg_w, bg_h = int(cap["background_image_width"]), int(cap["background_image_height"])
    tpl_w = int(cap["template_image_width"])
    # —— 按真实 UI 的帧构造（见上方注释）——
    scale = STAGE_W / max(1, bg_w)
    ut = STAGE_W - tpl_w * scale                      # 条带显示行程
    slider_max = SLIDER_TRAVEL                       # 滑轨行程（显示像素）
    mouse_max = offset_x * slider_max * scale / ut   # 落点对应的鼠标位移 P

    # 复刻前端行为（CheckIn chunk）：一条 0→P 的自然鼠标轨迹；
    # traj 用鼠标像素，trackList 用 dt() 映射后的自然像素，末点都精确收口。
    import random
    dur = random.randint(650, 950)
    n = random.randint(26, 38)
    events: list[tuple[int, int, int, int, str]] = []
    for i in range(n + 1):
        u = i / n
        e = 3 * u * u - 2 * u * u * u  # smoothstep
        mx = max(0, min(slider_max, round(mouse_max * e + (random.uniform(-2, 2) if 0 < i < n else 0))))
        my = round(random.uniform(-2, 2)) if 0 < i < n else 0
        tx = round(mx * ut / (slider_max * scale))   # dt(): 滑轨位置 → 自然像素
        typ = "DOWN" if i == 0 else ("UP" if i == n else "MOVE")
        events.append((round(dur * u), mx, my, tx, typ))
    events[-1] = (events[-1][0], int(mouse_max), events[-1][2], offset_x, "UP")

    traj = [[t, mx, my] for t, mx, my, _, _ in events]
    traj_str = "|".join(f"{t},{mx},{my}" for t, mx, my, _, _ in events)
    ph = _sha256_hex(f"offset_x={offset_x};duration_ms={dur};slider_max={slider_max};pt=mouse;traj={traj_str}")
    ua = headers["User-Agent"]
    # 注意：源码是 join(`\n`)（模板字面量内含真实换行）——用空格连接则 mac 永远错。
    # （曾把 minify 输出的换行显示成「2:」误读为空格，实测 400 的根因就是这个。）
    msg = "\n".join(["miku-ci-v1", str(cap["id"]), str(cap["nonce"]), str(cap["issued_at"]), ph, ua])
    sig = {"v": 1, "nonce": cap["nonce"], "ts": cap["issued_at"], "ph": ph,
           "mac": _hmac_sha256_hex(str(cap["sign_key"]), msg)}

    stop_time = int(time.time() * 1000)
    v = _rpost(
        f"{cfg['base']}/api/v1/check-in/captcha/verify", headers=headers,
        data=json.dumps({
            "id": cap["id"], "sig": sig, "traj": traj, "offset_x": offset_x,
            "duration_ms": dur, "slider_max": slider_max, "pt": "mouse",
            "data": {"bgImageWidth": bg_w, "bgImageHeight": bg_h,
                     "templateImageWidth": tpl_w, "templateImageHeight": int(cap["template_image_height"]),
                     "startTime": stop_time - dur, "stopTime": stop_time,
                     "startLeft": 0, "startTop": 0,
                     "trackList": [{"x": tx, "y": my, "t": t, "type": typ}
                                   for t, mx, my, tx, typ in events]},
        }, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )
    if v.status_code != 200:
        raise MikuError(f"签到验证失败 HTTP {v.status_code}: {v.text[:200]} (offset_x={offset_x})")
    token_c = (v.json().get("data") or {}).get("captcha_token") or ""
    if not token_c:
        raise MikuError(f"签到验证无 captcha_token: {v.text[:160]}")
    return token_c


def _checkin_once(cfg: dict, headers: dict, mode: str) -> dict:
    c = _rpost(f"{cfg['base']}/api/v1/check-in/captcha", headers=headers,
                      data=b"{}", timeout=30)
    if c.status_code != 200:
        raise MikuError(f"签到验证码 HTTP {c.status_code}: {c.text[:150]}")
    cap = (c.json().get("data") or {})
    bg_b = base64.b64decode(cap["background_image"].split(",", 1)[1])
    tpl_b = base64.b64decode(cap["template_image"].split(",", 1)[1])
    sol = solve_slider_v5(bg_b, tpl_b)
    offset_x = int(sol["off"])
    token_c = _verify_cap(cfg, headers, cap, offset_x)

    cl = _rpost(
        f"{cfg['base']}/api/v1/check-in", headers=headers,
        data=json.dumps({"mode": mode, "captcha_token": token_c}, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )
    if cl.status_code != 200:
        raise MikuError(f"签到领取失败 HTTP {cl.status_code}: {cl.text[:200]}")
    d = cl.json().get("data") or {}
    rec = d.get("record") or {}
    credits = rec.get("credits")
    logger.info("[miku] 签到成功 mode=%s credits=%s balance=%s", mode, credits, d.get("balance"))
    return {"credits": credits, "balance": d.get("balance"), "offset_x": offset_x}


def checkin(mode: str = "lucky", attempts: int = 8) -> dict:
    """自动完成一次每日签到，返回 {credits, balance}。mode: 'fixed'(固定) | 'lucky'(手气)。

    lucky 期望值更高（0.5~1.2 随机，均值≈0.85），默认 lucky。
    一张验证码只有一次尝试机会（实测后续提交一律计为失败），且检测偶发 ±40px 误差
    可能超出服务端容差——所以策略是「每张一枪、连开最多 attempts 张新码」。
    「今日已签到」是正常终态直接上抛，调度器明天再来。
    """
    cfg = load_config()
    if not cfg:
        raise MikuError("未配置 miku 渠道")
    headers = _headers(cfg, _token(cfg))
    last: MikuError | None = None
    for i in range(attempts):
        try:
            return _checkin_once(cfg, headers, mode)
        except MikuError as e:
            if "已签到" in str(e):
                raise
            last = e
            logger.warning("[miku] 签到第 %d 张失败，换新码重试: %s", i + 1, str(e)[:120])
    raise last
