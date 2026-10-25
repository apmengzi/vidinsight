import re
from typing import Optional
import requests


def extract_video_id(url: str, platform: str) -> Optional[str]:
    """
    从视频链接中提取视频 ID

    :param url: 视频链接
    :param platform: 平台名（bilibili / youtube / douyin）
    :return: 提取到的视频 ID 或 None
    """
    if platform == "bilibili":
        # 如果是短链接，则解析真实链接
        if "b23.tv" in url:
            resolved_url = resolve_bilibili_short_url(url)
            if resolved_url:
                url = resolved_url

        # 匹配 BV号（如 BV1vc411b7Wa）
        match = re.search(r"BV([0-9A-Za-z]+)", url)
        return f"BV{match.group(1)}" if match else None

    elif platform == "youtube":
        # 匹配 v=xxxxx 或 youtu.be/xxxxx，ID 长度通常为 11
        match = re.search(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})", url)
        return match.group(1) if match else None

    elif platform == "douyin":
        # 匹配 douyin.com/video/1234567890123456789
        match = re.search(r"/video/(\d+)", url)
        return match.group(1) if match else None

    elif platform == "xiaohongshu":
        # 短链先解析为真实链接（分享 token 保留在查询参数里）
        # 小红书同时维护 xhslink.com / xhslink.cn 两个短链域（均实测存活）
        if "xhslink.cn" in url or "xhslink.com" in url:
            resolved = resolve_xiaohongshu_short_url(url)
            if resolved:
                url = resolved
        match = re.search(r"/(?:explore|discovery/item|board/\w+)/([0-9a-f]{24})", url)
        if match:
            return match.group(1)
        # 合集页链接：返回合集 id（下载器会给出明确提示）
        match = re.search(r"/collection/item/([0-9a-f]{24})", url)
        return match.group(1) if match else None

    return None


def resolve_bilibili_short_url(short_url: str) -> Optional[str]:
    """
    解析哔哩哔哩短链接以获取真实视频链接

    :param short_url: Bilibili短链接（如"https://b23.tv/xxxxxx"）
    :return: 真实的视频链接或None
    """
    try:
        response = requests.head(short_url, allow_redirects=True)
        return response.url
    except requests.RequestException as e:
        print(f"Error resolving short URL: {e}")
        return None


def resolve_xiaohongshu_short_url(short_url: str) -> Optional[str]:
    """
    解析小红书短链（xhslink.cn）为真实笔记链接。

    :param short_url: xhslink.cn 短链
    :return: 真实链接（含 xsec_token），失败返回 None
    """
    try:
        resp = requests.head(
            short_url, allow_redirects=False, timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        location = resp.headers.get("Location", "")
        return location or None
    except Exception:
        return None
