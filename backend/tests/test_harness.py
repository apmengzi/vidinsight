"""
VidInsight Harness 单元测试。

覆盖：
1. produce 工具的结构化结果（验证 gate：success / reason）
2. 受限工具标记（产品红线）
3. 工具注册表完整性（schema 与 handler 一一对应）
4. harness 工具选择白名单（场景隔离）
5. 平台识别正确性
"""

import json

import pytest

from app.services import harness, produce_tools


# ── 1. 验证 gate：结构化结果 ────────────────────────

def test_validate_video_url_ok():
    out = json.loads(produce_tools.validate_video_url(
        {"url": "https://www.bilibili.com/video/BV1xx411c7mD"},
    ))
    assert out["success"] is True
    assert out["platform"] == "bilibili"
    assert out["supported"] is True


def test_validate_video_url_unknown():
    out = json.loads(produce_tools.validate_video_url({"url": "https://example.com/x"}))
    assert out["success"] is False
    assert "无法识别平台" in out["reason"]
    assert "supported_platforms" in out  # 失败也要给可行动信息


def test_validate_video_url_empty():
    out = json.loads(produce_tools.validate_video_url({"url": ""}))
    assert out["success"] is False
    assert out["reason"] == "链接为空"


def test_list_supported_platforms():
    out = json.loads(produce_tools.list_supported_platforms({}))
    assert out["success"] is True
    assert out["total"] >= 5
    ids = {p["id"] for p in out["platforms"]}
    assert {"bilibili", "youtube", "douyin", "xiaohongshu"} <= ids


def test_ingest_board_rejects_non_xhs():
    out = json.loads(produce_tools.ingest_board(
        {"board_url": "https://www.bilibili.com/video/BV1"},
    ))
    assert out["success"] is False
    assert "不是小红书链接" in out["reason"]


def test_ingest_board_parses_board_id():
    """无法访问网络时会失败，但 board_id 解析逻辑必须走到（reason 不应是解析失败）。"""
    out = json.loads(produce_tools.ingest_board(
        {"board_url": "https://www.xiaohongshu.com/board/66e64e4e000000001c01b8ed"},
    ))
    # 要么成功，要么因网络/登录态失败；绝不能是 "无法从链接解析 board_id"
    assert out.get("reason") != "无法从链接解析 board_id", out
    assert "success" in out


# ── 2. 受限工具标记（产品红线）───────────────────────

def test_restricted_tools_marked():
    assert produce_tools.RESTRICTED_TOOLS == {
        "generate_note", "ingest_board", "parse_xhs_note", "parse_video_link"}


def test_restricted_helper():
    assert harness._tool_is_restricted("generate_note") is True
    assert harness._tool_is_restricted("ingest_board") is True
    assert harness._tool_is_restricted("search_kb") is False
    assert harness._tool_is_restricted("validate_video_url") is False


# ── 3. 注册表完整性 ──────────────────────────────────

def test_schema_handler_parity():
    """每个 schema 里的工具都必须有对应 handler，反之亦然。"""
    schema_names = {t["function"]["name"] for t in produce_tools.PRODUCE_TOOLS}
    handler_names = set(produce_tools.PRODUCE_HANDLERS.keys())
    assert schema_names == handler_names, (
        f"schema≠handler: 仅schema={schema_names-handler_names} "
        f"仅handler={handler_names-schema_names}"
    )


def test_harness_tools_count():
    # 4 consume (search_kb/get_source/list_notes/clarify)
    # + 6 produce (validate_video_url/list_supported_platforms/generate_note/
    #              ingest_board/parse_xhs_note/parse_video_link)
    names = {t["function"]["name"] for t in harness.HARNESS_TOOLS}
    assert len(names) == 10, names
    consume = {"search_kb", "get_source", "list_notes", "clarify"}
    produce = set(produce_tools.PRODUCE_HANDLERS.keys())
    assert (consume | produce) == names


def test_all_tools_have_required_schema_fields():
    for t in harness.HARNESS_TOOLS:
        assert t["type"] == "function", t
        fn = t["function"]
        assert "name" in fn and "description" in fn, fn
        assert "parameters" in fn, fn


# ── 4. 工具选择白名单（场景隔离）──────────────────────

class _FakeHarness(harness.VidInsightHarness):
    """不连 LLM，只测工具选择逻辑。"""

    def __init__(self):  # noqa: D107
        pass  # 跳过 __init__ 的 LLM/知识库初始化


def test_select_tools_consume_only():
    h = _FakeHarness()
    names = {t["function"]["name"] for t in h._select_tools(["consume"])}
    assert names == {"search_kb", "get_source", "list_notes", "clarify"}, names


def test_select_tools_produce_only():
    h = _FakeHarness()
    names = {t["function"]["name"] for t in h._select_tools(["produce"])}
    assert names == set(produce_tools.PRODUCE_HANDLERS.keys()), names


def test_select_tools_none_means_all():
    h = _FakeHarness()
    names = {t["function"]["name"] for t in h._select_tools(None)}
    assert len(names) == 10, names


# ── 5. 平台识别 ──────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.bilibili.com/video/BV1xx", "bilibili"),
    ("https://b23.tv/abc123", "bilibili"),
    ("https://www.youtube.com/watch?v=xyz", "youtube"),
    ("https://youtu.be/xyz", "youtube"),
    ("https://www.douyin.com/video/123", "douyin"),
    ("https://www.xiaohongshu.com/board/abc", "xiaohongshu"),
    ("https://www.kuaishou.com/f/x", "kuaishou"),
])
def test_detect_platform(url, expected):
    assert produce_tools.detect_platform(url) == expected


def test_detect_platform_unknown():
    assert produce_tools.detect_platform("https://example.com") is None


# ── 6. 验证 gate 解析器 ──────────────────────────────

def test_result_ok_true():
    assert harness.VidInsightHarness._result_ok('{"success": true}') is True


def test_result_ok_false():
    assert harness.VidInsightHarness._result_ok('{"success": false, "reason": "x"}') is False


def test_result_ok_non_json_error():
    assert harness.VidInsightHarness._result_ok('{"error": "boom"}') is False


def test_result_ok_non_json_plain():
    assert harness.VidInsightHarness._result_ok('plain text result') is True
