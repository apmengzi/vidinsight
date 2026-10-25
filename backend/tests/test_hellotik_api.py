# -*- coding: utf-8 -*-
"""hellotik_api 协议层单测（离线，不碰网络）。

fixture 来自 2026-10-24 真实抓包（out/hellotik_protocol.json，笔记
「字节一面新鲜出炉十连问面经」），断言解密结果与当次抓包一致。
"""
import json
from pathlib import Path

import pytest

from app.services.hellotik_api import (
    decrypt_parse_response,
    normalize_note,
    split_title,
    deobfuscate,
)

FIXTURE = Path(r"D:\CyberStomach\VidInsight\out\hellotik_protocol.json")

pytestmark = pytest.mark.skipif(not FIXTURE.exists(), reason="抓包 fixture 不存在")


@pytest.fixture()
def parse_resp():
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return json.loads(next(e for e in d["api"] if e["url"] == "/api/parse")["respBody"])


def test_decrypt_real_capture(parse_resp):
    data = decrypt_parse_response(parse_resp)
    assert data["type"] == "video"
    assert data["title"].startswith("字节一面新鲜出炉十连问面经")
    assert "|||" in data["title"]
    assert data["url"].startswith("https://sns-video-hw.xhscdn.com/stream/")
    assert data["pics"], "应有封面图"
    # 超高清轨道存在（video_fullinfo）
    quals = [v for vs in data.get("videos", []) for v in vs.get("video_fullinfo", [])]
    assert any(q.get("type") == "超高清" for q in quals)


def test_split_title():
    t, desc = split_title("标题甲 |||正文乙 #话题")
    assert t == "标题甲"
    assert desc.startswith("正文乙")
    t2, d2 = split_title("没有分隔符")
    assert t2 == "没有分隔符" and d2 == ""


def test_normalize_note_fields():
    note = normalize_note({
        "title": "T |||D", "type": "video",
        "url": "https://sns-video-hw.xhscdn.com/x.mp4",
        "pics": ["https://sns-img-hw.xhscdn.com/a.jpg"],
        "cover": "https://sns-img-hw.xhscdn.com/c.jpg",
        "videos": [{"url": "u0", "video_fullinfo": [
            {"url": "u1", "size": 1, "type": "正常"}, {"url": "u2", "size": 2, "type": "超高清"}]}],
    })
    assert note["title"] == "T" and note["desc"] == "D"
    assert note["note_type"] == "video"
    assert note["video_url"].endswith("x.mp4")
    assert len(note["video_qualities"]) == 2
    assert note["cover"].endswith("c.jpg")

    img_note = normalize_note({"title": "I |||DDD", "pics": ["p1", "p2"]})
    assert img_note["note_type"] == "images"
    assert img_note["cover"] == "p1"


def test_deobfuscate_known_prefix():
    # 混淆层各自可逆：拿真实 data 的去混淆结果必须是合法 CBC 密文长度（16 倍数）
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    resp = json.loads(next(e for e in d["api"] if e["url"] == "/api/parse")["respBody"])
    ct = deobfuscate(resp["data"])
    iv = deobfuscate(resp["key"])
    assert len(ct) % 16 == 0
    assert len(iv) == 16
