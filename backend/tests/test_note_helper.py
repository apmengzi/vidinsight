import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "utils" / "note_helper.py"
spec = importlib.util.spec_from_file_location("note_helper", MODULE_PATH)
if spec is None or spec.loader is None:
    raise ImportError("note_helper module spec not found")
note_helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(note_helper)


class TestNoteHelper(unittest.TestCase):
    def test_prepend_source_link_adds_header_at_top(self):
        source_url = "https://www.bilibili.com/video/BV1xx411c7mD"
        markdown = "## 标题\n\n内容"

        result = note_helper.prepend_source_link(markdown, source_url)

        self.assertTrue(result.startswith(f"> 来源链接：{source_url}\n\n"))
        self.assertIn("## 标题", result)

    def test_prepend_source_link_does_not_duplicate_when_header_exists(self):
        source_url = "https://www.youtube.com/watch?v=abc123"
        markdown = f"> 来源链接：{source_url}\n\n## 标题\n\n内容"

        result = note_helper.prepend_source_link(markdown, source_url)

        self.assertEqual(result, markdown)

    def test_bili_plain_bv_uses_question_mark(self):
        result = note_helper.replace_content_markers(
            "Content-[05:01]", "BV185Yu68EKa", platform="bilibili"
        )

        self.assertIn("https://www.bilibili.com/video/BV185Yu68EKa?t=301", result)

    def test_bili_multi_part_uses_ampersand(self):
        result = note_helper.replace_content_markers(
            "Content-[05:01]", "BV185Yu68EKa_p2", platform="bilibili"
        )

        self.assertIn("https://www.bilibili.com/video/BV185Yu68EKa?p=2&t=301", result)

    def test_youtube_keeps_seconds_suffix(self):
        result = note_helper.replace_content_markers(
            "Content-[05:01]", "dQw4w9WgXcQ", platform="youtube"
        )

        self.assertIn("&t=301s", result)

    def test_douyin_no_time_param(self):
        result = note_helper.replace_content_markers(
            "Content-[05:01]", "7123456789012345678", platform="douyin"
        )

        self.assertIn("(https://www.douyin.com/video/7123456789012345678)", result)
        self.assertNotIn("t=", result)


if __name__ == "__main__":
    unittest.main()
