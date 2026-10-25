"""hellotik 兜底解析通道（B 站专用）。

它是什么：
    hellotik.app 是一个免费在线解析站。真人用法 = 打开网页 → 粘贴视频链接 →
    点「解析视频」→ 结果页出现可播放的 <video src>，那就是**平台官方 CDN 的
    无水印直链**。本模块把这套真人动作搬进 WebView 窗口里执行，把直链取回来
    交给我们的下载器用。

为什么要它：
    B 站风控会对 yt-dlp 返回 412，且伪装成"视频不存在/已下架"（实证：
    连 Rick Astley 这种 100% 存在的视频都被误报）。我们自己的主通道是
    yt-dlp + Cookie，兜底一 = WebView 读 __INITIAL_STATE__。hellotik 是
    兜底二：它的服务器替我们去取直链，风控压力不在我们和用户 IP 上。

实测边界（2026-10-04，务必记住，别再重测）：
    B站     ✅ 两次均成功，拿到 upos-sz-mirrorcos.bilivideo.com 直链，
              脱离其页面后 HTTP 206 / video/mp4 可正常下载
    YouTube ✗ 真实视频（dQw4w9WgXcQ）两次均"解析失败"
    抖音     ✗ 失败（且 yt-dlp 自身也要 cookie）
    小红书   ✗ 失败
    → 结论：它只覆盖 B 站，与我们的痛点正好重合，所以**只做 B 站兜底**，
      不宣传"全平台"。

用法：
    from app.services.hellotik import build_prefill_script, parse_result_payload
    前端/注入脚本拿到页面数据后 POST /api/parse/hellotik，后端校验并落库。
"""
from __future__ import annotations

import re
from typing import Any, Optional

HELLOTIK_HOME = "https://www.hellotik.app/zh"

# 平台官方 CDN 域名白名单——只接受这些域的直链，防止注入脚本被页面带偏。
# 2026-10-04/08 实测：B站(bilivideo/hdslb) 与 小红书(xhscdn) 均通过并端到端跑通；
# YouTube 三次实测均"解析失败"(status 403)，不支持。
# 抖音：用户 2026-10-08 反馈"实测可以"，但我这边只有自造 ID 可测（均 403，
# 不能据此判不支持——同样的错在小红书上犯过一次）。故**通道放开、按钮显示**，
# 待用户用真实链接实测确认后回填结论。
_CDN_PATTERNS = (
    # B 站：直链带 deadline 签名，会过期
    "bilivideo.com",
    "hdslb.com",
    "biliapi.com",
    "akamaized.net",
    # 小红书：sns-video-*.xhscdn.com / sns-img-*.xhscdn.com，直链无签名，较稳定
    "xhscdn.com",
    "xiaohongshu.com",
    # 抖音（待用户实测确认）
    "douyinvod.com",
    "douyin.com",
    "amemv.com",
    "byteimg.com",
)

# CDN → 平台归属，用于回填 platform 字段
def detect_platform(direct_url: str) -> Optional[str]:
    """按直链域名判定平台，认不出就返回 None（调用方按未知处理）。"""
    u = (direct_url or "").lower()
    if "xhscdn.com" in u or "xiaohongshu.com" in u:
        return "xiaohongshu"
    if any(p in u for p in ("douyinvod.com", "amemv.com")):
        return "douyin"
    if any(p in u for p in ("bilivideo.com", "hdslb.com", "biliapi.com", "akamaized.net")):
        return "bilibili"
    return None

_MAX_TITLE_LEN = 300


def normalize_bilibili_url(url: str) -> Optional[str]:
    """规整成 hellotik 最认的形态（去掉 query/trailing slash）。

    用户常贴带 ?p=2 / &spm_id_from= 的长链接，hellotik 对带参长串更敏感，
    统一裁到 BV 主体可提高命中率。b23.tv 短链保持原样（它需要服务端跳转，
    规整反而会丢信息）。
    """
    if not url:
        return None
    url = url.strip()
    if "b23.tv" in url:
        return url
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if m:
        return f"https://www.bilibili.com/video/{m.group(1)}"
    return url


def is_trusted_direct_url(url: str) -> bool:
    """直链是否来自可信 CDN（防止页面注入任意地址）。"""
    if not url or not url.startswith(("http://", "https://")):
        return False
    return any(p in url for p in _CDN_PATTERNS)


def build_prefill_script(url: str, *, auto_parse: bool = True) -> str:
    """生成注入 JS：像真人一样填框 + 点「解析视频」。

    两步都是标准 DOM 操作（填 input → 点 submit 按钮），不碰它的加密协议，
    因此不受它换协议 hash（2026w12 → 下周）的影响。
    """
    safe_url = (
        str(url)
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "")
        .replace("\r", "")
    )
    auto = "true" if auto_parse else "false"
    return (
        "(function(){try{"
        f"var u='{safe_url}';"
        "var i=document.querySelector('input[type=text],input:not([type])');"
        "if(!i)return 'NO_INPUT';"
        "var set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
        "set.call(i,u);"
        "i.dispatchEvent(new Event('input',{bubbles:true}));"
        f"if({auto}){{"
        "var b=document.querySelector('button[type=submit]')||"
        "[].slice.call(document.querySelectorAll('button')).filter(function(x){"
        "return (x.textContent||'').indexOf('解析')>=0;})[0];"
        "if(b){b.click();return 'CLICKED';}"
        "return 'NO_BUTTON';}"
        "return 'FILLED';"
        "}catch(e){return 'ERR:'+e.message;}})()"
    )


def build_extract_script() -> str:
    """生成注入 JS：从结果页读标题 + <video src> 直链 + 封面。

    只读 DOM 里已经渲染出来的东西——这正是真人在页面上看到的内容。
    标题必须在 <video> 所在的结果卡里找：直接取 h1/h2/h3 会命中页头的
    站名 "HelloTik.app"（2026-10-04 实测踩到），那不是视频标题。
    """
    return (
        "(function(){try{"
        "var v=document.querySelector('video');"
        "var src=(v&&(v.src||(v.querySelector('source')||{}).src))||'';"
        "var title='',cover='';"
        "if(v){"
        # 从 video 向上找到包含它的结果卡，再在卡内找标题/封面——避免命中页头
        "var card=v.closest('div[class]');"
        "for(var k=0;k<6&&card;k++){"
        "var t=card.querySelector('h1,h2,h3,h4');"
        "if(t&&(t.textContent||'').trim()){title=t.textContent.trim();break;}"
        "card=card.parentElement;}"
        "var img=card?card.querySelector('img'):null;"
        "if(img&&img.src&&img.src.indexOf('data:')!==0){cover=img.src;}"
        "}"
        "return JSON.stringify({direct_url:src,title:title,cover:cover});"
        "}catch(e){return JSON.stringify({error:e.message});}})()"
    )


def parse_result_payload(data: Any, *, source_url: str = "") -> dict:
    """校验并规整注入脚本回传的结果。失败契约：success=False + reason。

    与 universal_parser 同款契约——调用方永远拿 200，靠 success 判断。
    """
    if not isinstance(data, dict):
        return {"success": False, "reason": "payload_not_dict", "hint": "兜底通道回传数据格式异常"}

    if data.get("error"):
        return {"success": False, "reason": "inject_error", "hint": str(data["error"])[:120]}

    direct_url = str(data.get("direct_url") or "").strip()
    if not direct_url:
        return {
            "success": False,
            "reason": "no_direct_url",
            "hint": "hellotik 未能解析出视频直链（该平台可能不受支持或链接失效）",
        }

    if not is_trusted_direct_url(direct_url):
        return {
            "success": False,
            "reason": "untrusted_host",
            "hint": "解析结果不是 B 站官方 CDN 地址，已拒绝使用",
        }

    title = str(data.get("title") or "").strip()
    # 结果页若在页面头部命中站名，不能当成视频标题。
    # 站名有多种写法（"HelloTik.app" / "Hello Tik .app"），比较前先去掉空格、
    # 统一小写，避免写死字符串漏判。
    _squashed = re.sub(r"\s+", "", title).lower()
    if not title or _squashed in ("hellotik.app", "hellotik") or title == "免费解析去水印工具":
        title = ""
    if title:
        title = title[:_MAX_TITLE_LEN]

    cover = str(data.get("cover") or "").strip()
    if cover and not cover.startswith(("http://", "https://")):
        cover = ""

    return {
        "success": True,
        "platform": detect_platform(direct_url) or "bilibili",
        "engine": "hellotik_webview",
        "title": title or None,
        "direct_url": direct_url,
        "cover": cover or None,
        "source_url": source_url,
    }
