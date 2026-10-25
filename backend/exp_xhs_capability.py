"""能力实验 v3：直接用 AppData 真实 cookie 测登录态 SSR 的上限。
  Q2 cookie 是否活着（登录态 SSR 标记）
  Q1b 合集页 SSR（带 cookie）：会不会多给 uid / token？
  Q4b 无 token 直接打详情 SSR（登录态）：能不能读到正文？（架构里说会墙，验证）
"""
import json
import re
import subprocess

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
COLL_URL = "https://www.xiaohongshu.com/collection/item/6a3a9c8a0ab4000000000001"

cfg = json.load(open(r"C:\Users\30369\AppData\Roaming\VidInsight\config\downloader.json",
                     encoding="utf-8"))
cookie = cfg["xiaohongshu"]
if isinstance(cookie, dict):
    cookie = cookie.get("cookie") or cookie.get("cookies") or ""
print(f"cookie_len={len(cookie)} web_session={'web_session' in cookie}")


def curl(url):
    cmd = ["curl", "-s", "-L", "--max-time", "30", "-A", UA,
           "-H", f"Cookie: {cookie.strip()}"]
    # Referer 伪装从站内跳转
    cmd += ["-H", "Referer: https://www.xiaohongshu.com/"]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, timeout=40)
    return r.stdout.decode("utf-8", "ignore")


def token_pairs(html):
    pairs = {}
    for m in re.finditer(r'"(?:note_?[iI]d|id)":"([0-9a-f]{24})"[^{}]{0,240}?"xsec_token":"([^"\\]+)"', html):
        pairs.setdefault(m.group(1), m.group(2))
    for m in re.finditer(r'"xsec_token":"([^"\\]+)"[^{}]{0,240}?"(?:note_?[iI]d|id)":"([0-9a-f]{24})"', html):
        pairs.setdefault(m.group(2), m.group(1))
    return pairs


# Q2: cookie 活性——登录态访问主站首页，SSR/重定向会有登录态痕迹
home = curl("https://www.xiaohongshu.com/")
alive_markers = [k for k in ('"loginUserId"', '"loginRed">', 'loginUser') if k in home]
wall = any(k in home for k in ("扫码登录", "登录/注册"))
print(f"[Q2] home_len={len(home)} alive_markers={alive_markers} wall_keywords={wall}")

# Q1b: 合集页带 cookie
html = curl(COLL_URL)
pairs = token_pairs(html)
uid_m = re.search(r'"userid":"([0-9a-f]{24})"', html) or re.search(
    r'"userId":"([0-9a-f]{24})"', html) or re.search(r'user/profile/([0-9a-f]{24})', html)
print(f"[Q1b] collection len={len(html)} pairs={len(pairs)} uid_found={uid_m.group(1) if uid_m else None}")

# Q4b: 无 token 打第一条详情（匿名清单里拿 id）
nid_m = re.search(r'"(?:note_?[iI]d|id)":"([0-9a-f]{24})"', html)
if nid_m:
    nid = nid_m.group(1)
    dhtml = curl(f"https://www.xiaohongshu.com/explore/{nid}")
    has_state = "__INITIAL_STATE__" in dhtml
    ok = has_state and f'"{nid}"' in dhtml
    desc_m = re.search(r'"desc":"(.{20,120}?)","', dhtml)
    print(f"[Q4b] tokenless+cookie detail: len={len(dhtml)} has_state={has_state} "
          f"note_in_map={ok} desc_snippet={desc_m.group(1)[:60] if desc_m else None!r}")
