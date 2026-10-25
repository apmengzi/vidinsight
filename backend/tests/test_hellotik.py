"""hellotik 兜底通道单元测试。

锁住三件事：
  1. 直链必须来自可信 CDN——注入脚本不可信，后端必须挡住任意地址
  2. 失败契约：永远返回 dict，靠 success 判断，不抛异常给调用方
  3. URL 规整：带参长链裁到 BV 主体，b23.tv 保持原样
"""
import pytest

from app.services.hellotik import (
    build_extract_script,
    build_prefill_script,
    detect_platform,
    is_trusted_direct_url,
    normalize_bilibili_url,
    parse_result_payload,
)

BILI_CDN = "https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/99/91/x.mp4?e=sig&deadline=1"
XHS_CDN = "https://sns-video-hw.xhscdn.com/stream/79/110/258/01eab63e1b73beef4f037001a0d7e5e742_258.mp4"


def test_normalize_strips_query():
    assert (
        normalize_bilibili_url("https://www.bilibili.com/video/BV1GJ411x7h7?p=2&spm_id_from=333")
        == "https://www.bilibili.com/video/BV1GJ411x7h7"
    )


def test_normalize_keeps_short_link():
    raw = "https://b23.tv/abc123"
    assert normalize_bilibili_url(raw) == raw


def test_normalize_leaves_non_bv_untouched():
    raw = "https://www.bilibili.com/bangumi/play/ep123"
    assert normalize_bilibili_url(raw) == raw


def test_trusted_cdn_whitelist():
    assert is_trusted_direct_url(BILI_CDN) is True
    assert is_trusted_direct_url("https://i1.hdslb.com/bfs/a.jpg") is True
    # 小红书（2026-10-04 用真实分享链接实测可用，此前误判为不支持）
    assert is_trusted_direct_url(XHS_CDN) is True
    assert is_trusted_direct_url("https://sns-img-hw.xhscdn.com/spectrum/abc") is True
    # 恶意/非白名单地址必须被拒
    assert is_trusted_direct_url("https://evil.example.com/x.mp4") is False
    assert is_trusted_direct_url("https://www.youtube.com/watch?v=x") is False
    assert is_trusted_direct_url("javascript:alert(1)") is False
    assert is_trusted_direct_url("") is False


def test_detect_platform():
    assert detect_platform(BILI_CDN) == "bilibili"
    assert detect_platform(XHS_CDN) == "xiaohongshu"
    assert detect_platform("https://evil.example.com/a.mp4") is None


def test_parse_xhs_success():
    """小红书直链：平台要回填 xiaohongshu，不能一律写 bilibili。"""
    out = parse_result_payload(
        {"direct_url": XHS_CDN, "title": "how to | Jev模型保姆级教程"},
        source_url="https://xhslink.cn/o/8evVX10JFUu",
    )
    assert out["success"] is True
    assert out["platform"] == "xiaohongshu"
    assert out["direct_url"] == XHS_CDN


def test_parse_success():
    out = parse_result_payload(
        {"direct_url": BILI_CDN, "title": "【官方 MV】Never Gonna Give You Up - Rick Astley"},
        source_url="https://www.bilibili.com/video/BV1GJ411x7h7",
    )
    assert out["success"] is True
    assert out["engine"] == "hellotik_webview"
    assert "Never Gonna Give You Up" in out["title"]
    assert out["direct_url"] == BILI_CDN


def test_parse_rejects_untrusted_host():
    out = parse_result_payload({"direct_url": "https://evil.example.com/a.mp4"})
    assert out["success"] is False
    assert out["reason"] == "untrusted_host"


def test_parse_no_direct_url():
    out = parse_result_payload({"direct_url": ""})
    assert out["success"] is False
    assert out["reason"] == "no_direct_url"


def test_parse_strips_site_branding_title():
    """结果页若抓到站名，不能当成视频标题。"""
    out = parse_result_payload({"direct_url": BILI_CDN, "title": "HelloTik.app"})
    assert out["success"] is True
    assert out["title"] is None


def test_parse_bad_payload():
    for bad in (None, "string", 42, []):
        out = parse_result_payload(bad)
        assert out["success"] is False
        assert "reason" in out


def test_scripts_are_escaped_and_self_contained():
    evil = "https://x.com/'; alert(1); //"
    js = build_prefill_script(evil)
    assert "\\'" in js  # 单引号被转义，注入脚本不会被提前闭合
    assert "alert(1)" in js  # 内容仍在（作为字符串字面量）
    assert js.startswith("(function(){")
    # 提取脚本只读 DOM，不做任何导航
    ex = build_extract_script()
    assert "querySelector('video')" in ex
    assert "location" not in ex
