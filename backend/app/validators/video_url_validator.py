from pydantic import AnyUrl, validator, BaseModel, field_validator
import re
from urllib.parse import urlparse

SUPPORTED_PLATFORMS = {
    "bilibili": r"(https?://)?(www\.)?bilibili\.com/video/[a-zA-Z0-9]+",
    "youtube": r"(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w\-]+",
    "douyin": "douyin",
    "kuaishou": "kuaishou",
    "xiaohongshu": r"(https?://)?(www\.)?xiaohongshu\.com/(explore|discovery/item|collection/item)/[0-9a-f]{24}",
}

# 小红书短链域名（App 分享两种形态都可能出现：xhslink.com / xhslink.cn，两域均实测存活）
XHS_SHORT_HOSTS = {"xhslink.cn", "xhslink.com"}


def is_supported_video_url(url: str) -> bool:
    parsed = urlparse(url)

    # 检查是否为Bilibili的短链接
    if parsed.netloc == "b23.tv":
        return True

    # 小红书短链（App 分享）
    if parsed.netloc in XHS_SHORT_HOSTS:
        return True

    for name, pattern in SUPPORTED_PLATFORMS.items():
        if pattern in ["douyin", "kuaishou"]:
            if pattern in url:
                return True
        else:
            if re.match(pattern, url):
                return True
    return False


class VideoRequest(BaseModel):
    url: AnyUrl
    platform: str

    @field_validator("url")
    def validate_video_url(cls, v):
        if not is_supported_video_url(str(v)):
            raise ValueError("暂不支持该视频平台或链接格式无效")
        return v
