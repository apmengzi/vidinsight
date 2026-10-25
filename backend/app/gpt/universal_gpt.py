from app.gpt.base import GPT
from app.gpt.prompt_builder import generate_base_prompt
from app.models.gpt_model import GPTSource
import os
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.gpt.prompt import BASE_PROMPT, AI_SUM, SCREENSHOT, LINK, MERGE_PROMPT
from app.gpt.utils import fix_markdown
from app.gpt.request_chunker import RequestChunker
from app.models.transcriber_model import TranscriptSegment
from datetime import timedelta
from typing import List
from app.utils.path_helper import get_runtime_path
from app.services.task_cancel import TaskCancelled, check_cancelled


class UniversalGPT(GPT):
    def __init__(self, client, model: str, temperature: float = 0.7):
        self.client = client
        self.model = model
        self.temperature = temperature
        self.screenshot = False
        self.link = False
        self.max_request_bytes = int(os.getenv("OPENAI_MAX_REQUEST_BYTES", str(45 * 1024 * 1024)))
        self.checkpoint_dir = Path(os.getenv("NOTE_OUTPUT_DIR", get_runtime_path("note_results")))
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        # 初始化时缓存重试配置，避免每次请求重复读取环境变量
        self._max_retry_attempts = max(1, int(os.getenv("OPENAI_RETRY_ATTEMPTS", "3")))
        self._retry_base_backoff = float(os.getenv("OPENAI_RETRY_BACKOFF_SECONDS", "1.5"))

    def _format_time(self, seconds: float) -> str:
        return str(timedelta(seconds=int(seconds)))[2:]

    def _build_segment_text(self, segments: List[TranscriptSegment]) -> str:
        return "\n".join(
            f"{self._format_time(seg.start)} - {seg.text.strip()}"
            for seg in segments
        )

    def ensure_segments_type(self, segments) -> List[TranscriptSegment]:
        return [TranscriptSegment(**seg) if isinstance(seg, dict) else seg for seg in segments]

    def create_messages(self, segments: List[TranscriptSegment], **kwargs):

        content_text = generate_base_prompt(
            title=kwargs.get('title'),
            segment_text=self._build_segment_text(segments),
            tags=kwargs.get('tags'),
            _format=kwargs.get('_format'),
            style=kwargs.get('style'),
            extras=kwargs.get('extras'),
        )

        video_img_urls = kwargs.get('video_img_urls', [])

        content: list[dict] | str
        if video_img_urls:
            # 有截图时走 OpenAI 多模态 content 数组（text + image_url）
            content = [{"type": "text", "text": content_text}]
            for url in video_img_urls:
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": url,
                        "detail": "auto"
                    }
                })
        else:
            # 纯文本场景退回 string content：DeepSeek deepseek-chat 等非多模态模型
            # 不识别 [{"type":"text",...}] 数组形态，会返回 invalid_request_error
            # （issue #282）。OpenAI 规范本身也允许 content 为 string。
            content = content_text

        messages = [{
            "role": "user",
            "content": content
        }]

        return messages

    def list_models(self):
        return self.client.models.list()

    def _estimate_messages_bytes(self, messages: list) -> int:
        import json
        return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))

    def _build_merge_messages(self, partials: list) -> list:
        merge_text = MERGE_PROMPT + "\n\n" + "\n\n---\n\n".join(partials)
        # 合并阶段没有图片，直接用 string content 兼容非多模态模型（issue #282）
        return [{
            "role": "user",
            "content": merge_text
        }]

    def _checkpoint_path(self, checkpoint_key: str) -> Path:
        safe_key = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in checkpoint_key)
        return self.checkpoint_dir / f"{safe_key}.gpt.checkpoint.json"

    def _build_source_signature(self, source: GPTSource) -> str:
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_request_bytes": self.max_request_bytes,
            "title": source.title,
            "tags": source.tags,
            "format": source._format,
            "style": source.style,
            "extras": source.extras,
            "video_img_urls": source.video_img_urls or [],
            "segments": [
                {
                    "start": getattr(seg, "start", None),
                    "end": getattr(seg, "end", None),
                    "text": getattr(seg, "text", "")
                }
                for seg in source.segment
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_checkpoint(self, checkpoint_key: str, source_signature: str) -> dict | None:
        path = self._checkpoint_path(checkpoint_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("source_signature") != source_signature:
                path.unlink(missing_ok=True)
                return None
            return data
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def _save_checkpoint(self, checkpoint_key: str, source_signature: str, partials: list, phase: str) -> None:
        path = self._checkpoint_path(checkpoint_key)
        data = {
            "version": 1,
            "source_signature": source_signature,
            "phase": phase,
            "partials": partials,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _clear_checkpoint(self, checkpoint_key: str) -> None:
        self._checkpoint_path(checkpoint_key).unlink(missing_ok=True)

    @staticmethod
    def _is_insufficient_quota_error(exc: Exception) -> bool:
        raw = str(exc)
        return (
            "insufficient_user_quota" in raw
            or "预扣费额度失败" in raw
            or "insufficient quota" in raw.lower()
        )

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        raw = str(exc).lower()
        retryable_tokens = (
            "error code: 524",
            "bad_response_status_code",
            "timed out",
            "timeout",
            "rate limit",
            "error code: 429",
            "error code: 500",
            "error code: 502",
            "error code: 503",
            "error code: 504",
            "apiconnectionerror",
            "connection error",
            "service unavailable",
        )
        if any(token in raw for token in retryable_tokens):
            return True

        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return status in {408, 409, 429, 500, 502, 503, 504, 524}

    @staticmethod
    def _is_temperature_unsupported_error(exc: Exception) -> bool:
        """OpenAI o1/o3/gpt-5 系列等新模型不接受自定义 temperature，
        只允许默认值 1，传 0.7 会报 `'temperature' does not support 0.7 ...`。"""
        raw = str(exc).lower()
        return "temperature" in raw and (
            "does not support" in raw
            or "unsupported_value" in raw
            or "only the default" in raw
        )

    def _thinking_kwargs(self) -> dict:
        """思考强度控制（2026-10-12 实测确定，别再猜参数名）。

        产品诉求（用户明确要求）：**总结任务不需要深度思考**，思考链会实打实
        消耗输出预算——实测 deepseek-v4.1-flash 在分析题上思考 10794 字、
        正文 0 字（finish=length），报告直接生成失败。

        ═══════════════════════════════════════════════════════════
        ⚠️ 两个场景要分清，最优解相反（曾把两者混为一谈，导致误判）：
        ───────────────────────────────────────────────────────────
        【场景 A · 运行时】本文件服务的场景：VidInsight 内跑的任务
          （视频总结 / 配图提示词 / Agent 问答）
          特征：任务**轻量**（把已有字幕整理成固定结构），不需要深度推理
          策略：**思考一律关掉**（能关就关，只有档位的设最低档）
          理由：思考链纯粹浪费预算与时间，实测关掉后 10.7s 出 2302 字报告

        【场景 B · 开发时】用模型写代码（不经过本文件，是人在 IDE/CLI 里用）
          特征：长链路推理（理解需求→拆方案→多文件改动→调 bug）
          策略：**全力以赴，要最聪明最正确**，不省思考
          注意：评价场景 B 的结论**不能拿场景 A 的条件去测**（曾用小预算
                测 effort=max 得出"会翻车"，实为预算不足所致，属误判）
        ═══════════════════════════════════════════════════════════

        策略（仅场景 A）：能开关的关掉，只有档位的设最低档。
        实测（同一难题，预算都给 4000）：
          deepseek 裸跑                      → 思考 10794 字，正文 0 字 ❌
          deepseek + thinking{type:disabled} → 思考 0 字，正文 3046 字，6.8s ✅
          glm-5.3（裸跑已无思考输出）          → 正文 2514 字，17.6s ✅

        参数名踩坑（都试过，别重试）：
          ✗ reasoning_effort=low/none/minimal 对 deepseek 无效（思考照旧上万字）
          ✗ enable_thinking=False / chat_template_kwargs / thinking_budget=0 无效
          ✓ thinking={"type":"disabled"} 唯一生效

        环境变量 VIDINSIGHT_THINKING_OFF=0 可关闭本行为。
        """
        if os.getenv("VIDINSIGHT_THINKING_OFF", "1") != "1":
            return {}
        return {"extra_body": {"thinking": {"type": "disabled"}}}

    def _do_create(self, messages: list):
        """单次调用，按"思考尽量关掉"的诉求做参数降级：

        1. thinking{type:disabled} —— 实测对 deepseek 生效（唯一有效写法）
        2. 若该参数不被接受 → reasoning_effort=low（对 GLM 等档位型模型生效）
        3. 仍不接受 → 普通调用（宁可多花思考预算，也不能因可选参数让任务失败）

        实测补充（2026-10-12，位置很关键）：
          顶层 reasoning_effort 会**真的生效**——glm-5.3 + effort=max 实测
            112 秒、思考 12636 字、正文 0 字（比不开还差，等于自毁）
          放进 extra_body 的 reasoning_effort **被忽略**（13.8s 正常返回）
        所以第 2 级降级用**顶层**写法且只设 low（绝不设 max——实测会翻车）。
        """
        attempts = [
            {**self._thinking_kwargs(), "temperature": self.temperature},
            {"reasoning_effort": "low", "temperature": self.temperature},
            {"temperature": self.temperature},
        ]
        last_exc: Exception | None = None
        for i, extra in enumerate(attempts):
            try:
                return self.client.chat.completions.create(
                    model=self.model, messages=messages, **extra,
                )
            except Exception as exc:
                last_exc = exc
                if self._is_temperature_unsupported_error(exc):
                    print(f"[universal_gpt] {self.model} 不支持自定义 temperature，去掉后重试")
                    return self.client.chat.completions.create(
                        model=self.model, messages=messages,
                        **{k: v for k, v in extra.items() if k != "temperature"},
                    )
                if self._is_unsupported_param_error(exc) and i < len(attempts) - 1:
                    print(f"[universal_gpt] {self.model} 不接受第 {i + 1} 组思考参数，降级重试")
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("chat completion failed without exception")

    @staticmethod
    def _is_unsupported_param_error(exc: Exception) -> bool:
        """识别"不认识某个请求参数"类错误，用于参数降级重试。"""
        raw = str(exc).lower()
        return any(k in raw for k in (
            "unknown", "unsupported", "invalid_request", "unexpected",
            "not supported", "unrecognized", "extra fields", "unrecognized request",
        ))

    @staticmethod
    def _extract_content(response) -> str:
        """安全提取正文：空/None 一律返回空串（调用方据此判空并重试）。"""
        try:
            return (response.choices[0].message.content or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _finish_reason(response) -> str:
        """取 finish_reason 用于日志定位（length = 输出预算被耗尽）。"""
        try:
            return str(response.choices[0].finish_reason or "")
        except Exception:
            return ""

    def _chat_completion_create(self, messages: list):
        last_exc = None
        for attempt in range(self._max_retry_attempts):
            try:
                return self._do_create(messages)
            except Exception as exc:
                last_exc = exc
                if attempt == self._max_retry_attempts - 1 or not self._is_retryable_error(exc):
                    raise
                sleep_seconds = self._retry_base_backoff * (2 ** attempt)
                time.sleep(sleep_seconds)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("chat completion failed without exception")

    def _merge_partials(self, partials: list, checkpoint_key: str | None, source_signature: str | None,
                        on_progress=None) -> str:
        def build_messages(texts, *_args, **_kwargs):
            return self._build_merge_messages(texts)

        merge_chunker = RequestChunker(
            lambda *_args, **_kwargs: [],
            self.max_request_bytes,
            self._estimate_messages_bytes
        )

        current_partials = list(partials)
        # 合并阶段占总进度的 90-100：按"轮次内已完成组数 / 本轮总组数"推进
        merge_last_report = 0.0

        def _report_merge(pct: float) -> None:
            nonlocal merge_last_report
            if on_progress is None:
                return
            now = time.monotonic()
            if now - merge_last_report < 1.0 and pct < 100:
                return
            merge_last_report = now
            try:
                on_progress(pct)
            except TaskCancelled:
                raise  # 取消必须继续向上传播，不能被进度上报吞掉
            except Exception:
                pass

        while len(current_partials) > 1:
            groups = merge_chunker.group_texts_by_budget(current_partials, build_messages)
            new_partials = []
            for group_idx, group in enumerate(groups):
                # 合并阶段同样是 LLM 轮次边界，同样响应取消
                check_cancelled(checkpoint_key)
                messages = build_messages(group)
                try:
                    response = self._chat_completion_create(messages)
                except Exception as exc:
                    if checkpoint_key and source_signature:
                        self._save_checkpoint(checkpoint_key, source_signature, current_partials, "merge")
                    raise

                # 合并阶段同样防空正文：空内容参与后续合并会污染最终报告
                content = self._extract_content(response)
                if not content:
                    logger.warning("合并阶段模型返回空正文，重试一次")
                    content = self._extract_content(self._chat_completion_create(messages))
                    if not content:
                        raise Exception(
                            "合并阶段模型连续返回空内容（思考链可能失控占满输出预算）。"
                            "建议更换模型后重试"
                        )

                new_partials.append(content)
                _report_merge(90.0 + (group_idx + 1) / max(1, len(groups)) * 10.0)

                if checkpoint_key and source_signature:
                    remaining_partials = []
                    for remaining_group in groups[group_idx + 1:]:
                        remaining_partials.extend(remaining_group)
                    resumable_partials = new_partials + remaining_partials
                    self._save_checkpoint(checkpoint_key, source_signature, resumable_partials, "merge")

            current_partials = new_partials

        return current_partials[0]

    def summarize(self, source: GPTSource) -> str:
        self.screenshot = source.screenshot
        self.link = source.link
        source.segment = self.ensure_segments_type(source.segment)
        checkpoint_key = source.checkpoint_key
        source_signature = self._build_source_signature(source) if checkpoint_key else None

        def message_builder(segments, image_urls, **kwargs):
            return self.create_messages(segments, video_img_urls=image_urls, **kwargs)

        chunker = RequestChunker(message_builder, self.max_request_bytes, self._estimate_messages_bytes)

        try:
            chunks = chunker.chunk(
                source.segment,
                source.video_img_urls or [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                style=source.style,
                extras=source.extras
            )
        except ValueError:
            chunks = chunker.chunk(
                source.segment,
                [],
                title=source.title,
                tags=source.tags,
                _format=source._format,
                style=source.style,
                extras=source.extras
            )

        partials = []
        if checkpoint_key and source_signature:
            checkpoint = self._load_checkpoint(checkpoint_key, source_signature)
            if checkpoint and isinstance(checkpoint.get("partials"), list):
                partials = checkpoint["partials"]

        if len(partials) > len(chunks):
            partials = []

        # 阶段内进度：分块总结占 0-90，合并阶段占 90-100。
        # 分块循环是同步阻塞的，节流用墙钟时间，避免每块都写一次状态文件。
        total_chunks = max(1, len(chunks))
        on_progress = getattr(source, "on_progress", None)
        last_report = 0.0

        def _report(pct: float, force: bool = False) -> None:
            nonlocal last_report
            if on_progress is None:
                return
            now = time.monotonic()
            if not force and now - last_report < 1.0:
                return
            last_report = now
            try:
                on_progress(pct)
            except TaskCancelled:
                raise  # 取消必须继续向上传播，不能被进度上报吞掉
            except Exception:
                pass  # 进度上报失败不影响生成

        def _chunk_pct(done: int) -> float:
            return min(done, total_chunks) / total_chunks * 90.0

        # 断点续跑时从已完成块起算，不重复报 0
        _report(_chunk_pct(len(partials)), force=True)

        for chunk in chunks[len(partials):]:
            # LLM 轮次边界取消检查（checkpoint 已保存 partials，取消后可断点续跑）
            check_cancelled(checkpoint_key)
            messages = self.create_messages(
                chunk.segments,
                title=source.title,
                tags=source.tags,
                video_img_urls=chunk.image_urls,
                _format=source._format,
                style=source.style,
                extras=source.extras
            )
            try:
                response = self._chat_completion_create(messages)
            except Exception as exc:
                if checkpoint_key and source_signature:
                    self._save_checkpoint(checkpoint_key, source_signature, partials, "summarize")
                raise

            content = self._extract_content(response)
            if not content:
                # 空正文防护：某些模型（实测 deepseek-v4.1-flash 在开放式分析题上）
                # 思考链会失控膨胀，把 token 预算耗在 reasoning 上，finish_reason=length
                # 而正文为 0 字。若直接存空串，最终会生成**残缺/空白报告**且任务仍显示
                # 成功——这是答辩现场的真实风险。这里重试一次（换次采样常能恢复），
                # 仍空则抛出可读错误，让上层走失败契约而不是产出空报告。
                logger.warning(
                    f"模型返回空正文（疑似思考链失控，finish_reason="
                    f"{self._finish_reason(response)}），重试一次"
                )
                retry_resp = self._chat_completion_create(messages)
                content = self._extract_content(retry_resp)
                if not content:
                    raise Exception(
                        "模型连续两次返回空内容（思考链可能失控占满输出预算）。"
                        "建议在「设置」中更换模型后重试（实测 GLM-5.3 / GPT 系列更稳）"
                    )
                response = retry_resp

            partials.append(content)
            # 块级进度同样节流（1s），但最后一块必须落盘，否则前端会停在 90% 前
            _report(_chunk_pct(len(partials)), force=(len(partials) >= total_chunks))
            if checkpoint_key and source_signature:
                self._save_checkpoint(checkpoint_key, source_signature, partials, "summarize")

        if len(partials) == 1:
            _report(100.0, force=True)
            if checkpoint_key:
                self._clear_checkpoint(checkpoint_key)
            return partials[0]
        merged = self._merge_partials(partials, checkpoint_key, source_signature, on_progress=on_progress)
        _report(100.0, force=True)
        if checkpoint_key:
            self._clear_checkpoint(checkpoint_key)
        return merged
