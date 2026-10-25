import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


# 被替换前的真模块备份（加载完目标模块后还原，防会话级污染）
_STUBS_BACKUP: dict = {}


def _install_stubs():
    app_mod = types.ModuleType("app")
    gpt_pkg = types.ModuleType("app.gpt")
    models_pkg = types.ModuleType("app.models")

    base_mod = types.ModuleType("app.gpt.base")

    class _GPT:
        pass

    base_mod.GPT = _GPT

    prompt_builder_mod = types.ModuleType("app.gpt.prompt_builder")

    def _generate_base_prompt(**_kwargs):
        return "prompt"

    prompt_builder_mod.generate_base_prompt = _generate_base_prompt

    prompt_mod = types.ModuleType("app.gpt.prompt")
    prompt_mod.BASE_PROMPT = ""
    prompt_mod.AI_SUM = ""
    prompt_mod.SCREENSHOT = ""
    prompt_mod.LINK = ""
    prompt_mod.MERGE_PROMPT = "merge"

    utils_mod = types.ModuleType("app.gpt.utils")

    def _fix_markdown(text):
        return text

    utils_mod.fix_markdown = _fix_markdown

    request_chunker_mod = types.ModuleType("app.gpt.request_chunker")

    class _RequestChunker:
        def __init__(self, *_args, **_kwargs):
            pass

        def group_texts_by_budget(self, texts, _builder, **_kwargs):
            return [texts]

    request_chunker_mod.RequestChunker = _RequestChunker

    gpt_model_mod = types.ModuleType("app.models.gpt_model")

    class _GPTSource:
        pass

    gpt_model_mod.GPTSource = _GPTSource

    transcriber_model_mod = types.ModuleType("app.models.transcriber_model")

    class _TranscriptSegment:
        def __init__(self, **kwargs):
            self.start = kwargs.get("start", 0)
            self.end = kwargs.get("end", 0)
            self.text = kwargs.get("text", "")

    transcriber_model_mod.TranscriptSegment = _TranscriptSegment

    sys.modules.setdefault("app", app_mod)
    sys.modules.setdefault("app.gpt", gpt_pkg)
    sys.modules.setdefault("app.models", models_pkg)
    if "app.gpt.base" in sys.modules and "app.gpt.base" not in _STUBS_BACKUP:
        _STUBS_BACKUP["app.gpt.base"] = sys.modules["app.gpt.base"]
    sys.modules["app.gpt.base"] = base_mod
    if "app.gpt.prompt_builder" in sys.modules and "app.gpt.prompt_builder" not in _STUBS_BACKUP:
        _STUBS_BACKUP["app.gpt.prompt_builder"] = sys.modules["app.gpt.prompt_builder"]
    sys.modules["app.gpt.prompt_builder"] = prompt_builder_mod
    if "app.gpt.prompt" in sys.modules and "app.gpt.prompt" not in _STUBS_BACKUP:
        _STUBS_BACKUP["app.gpt.prompt"] = sys.modules["app.gpt.prompt"]
    sys.modules["app.gpt.prompt"] = prompt_mod
    if "app.gpt.utils" in sys.modules and "app.gpt.utils" not in _STUBS_BACKUP:
        _STUBS_BACKUP["app.gpt.utils"] = sys.modules["app.gpt.utils"]
    sys.modules["app.gpt.utils"] = utils_mod
    if "app.gpt.request_chunker" in sys.modules and "app.gpt.request_chunker" not in _STUBS_BACKUP:
        _STUBS_BACKUP["app.gpt.request_chunker"] = sys.modules["app.gpt.request_chunker"]
    sys.modules["app.gpt.request_chunker"] = request_chunker_mod
    if "app.models.gpt_model" in sys.modules and "app.models.gpt_model" not in _STUBS_BACKUP:
        _STUBS_BACKUP["app.models.gpt_model"] = sys.modules["app.models.gpt_model"]
    sys.modules["app.models.gpt_model"] = gpt_model_mod
    if "app.models.transcriber_model" in sys.modules and "app.models.transcriber_model" not in _STUBS_BACKUP:
        _STUBS_BACKUP["app.models.transcriber_model"] = sys.modules["app.models.transcriber_model"]
    sys.modules["app.models.transcriber_model"] = transcriber_model_mod


def _load_universal_gpt_class():
    _install_stubs()
    root = pathlib.Path(__file__).resolve().parents[1]
    module_path = root / "app" / "gpt" / "universal_gpt.py"
    spec = importlib.util.spec_from_file_location("universal_gpt", module_path)
    if spec is None or spec.loader is None:
        raise ImportError("universal_gpt module spec not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # ── 用完恢复真 app.* 模块（2026-10-18 根因修复）──
    # _install_stubs 把 app.gpt.* / app.models.* 换成空壳且不恢复，
    # 污染整个 pytest 会话——后续任何真实 import app.gpt.prompt_builder 的
    # 测试（如 test_00_prompt_dual_template）都会拿到假包。目标模块已经
    # 用 stubs 加载完毕，真模块此刻还原不影响本文件的测试。
    for _n in ("app.gpt.base", "app.gpt.prompt_builder", "app.gpt.prompt",
               "app.gpt.utils", "app.gpt.request_chunker",
               "app.models.gpt_model", "app.models.transcriber_model"):
        if _n in _STUBS_BACKUP:
            sys.modules[_n] = _STUBS_BACKUP.pop(_n)
        else:
            sys.modules.pop(_n, None)
    return module.UniversalGPT


UniversalGPT = _load_universal_gpt_class()


class _FailingCompletions:
    def create(self, **_kwargs):
        raise Exception("Error code: 524 - bad_response_status_code")


class _DummyChat:
    def __init__(self):
        self.completions = _FailingCompletions()


class _DummyModels:
    @staticmethod
    def list():
        return []


class _DummyClient:
    def __init__(self):
        self.chat = _DummyChat()
        self.models = _DummyModels()


class TestUniversalGPTCheckpoint(unittest.TestCase):
    def test_merge_524_error_persists_checkpoint(self):
        original_attempts = os.environ.get("OPENAI_RETRY_ATTEMPTS")
        os.environ["OPENAI_RETRY_ATTEMPTS"] = "1"
        gpt = UniversalGPT(_DummyClient(), model="mock-model")
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                gpt.checkpoint_dir = Path(tmp_dir)

                with self.assertRaises(Exception):
                    gpt._merge_partials(["part-a", "part-b"], "task-1", "sig-1")

                checkpoint_path = gpt._checkpoint_path("task-1")
                self.assertTrue(checkpoint_path.exists())
                payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["phase"], "merge")
                self.assertEqual(payload["partials"], ["part-a", "part-b"])
        finally:
            if original_attempts is None:
                os.environ.pop("OPENAI_RETRY_ATTEMPTS", None)
            else:
                os.environ["OPENAI_RETRY_ATTEMPTS"] = original_attempts


if __name__ == "__main__":
    unittest.main()
