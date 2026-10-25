"""结构化日志标准（doc/ENGINEERING_STANDARD.md §1）。

约定：
- 文件日志 logs/app.log 一律 JSON Lines，字段：ts/level/logger/msg，
  事件用 log_event() 记录时额外携带 event + 业务字段 + request_id。
- 控制台保持人类可读（桌面端开发期看得到）。
- request_id 由请求中间件生成（透传 X-Request-Id 或自动 12 位 hex），
  ContextVar 全链路携带——用户报 bug 只要说时间点，grep request_id 就能
  拉出该请求的完整事件序列。
- event 命名：<域>.<动作>[.<结果>]，如 collection.expand.done / xhs.token.harvest。
"""
import logging
import time
import traceback
import uuid
from contextvars import ContextVar

# ── request_id 全链路上下文 ─────────────────────────────────
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# LogRecord 的标准属性，extra 里传这些键 logging 会直接 KeyError——禁止作为业务字段
RESERVED_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "asctime", "message",
})


def _sanitize_fields(fields: dict) -> dict:
    """业务字段与 LogRecord 标准属性撞名时，加 f_ 前缀而不是让整条日志炸掉。

    约定：调用方尽量别用 message/msg/exc_info 当字段名；这里只兜底。
    """
    out = {}
    for key, value in fields.items():
        out[f"f_{key}" if key in RESERVED_KEYS else key] = value
    return out


class JsonFormatter(logging.Formatter):
    """文件日志：一行一条 JSON，机器可 grep 可 jq。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        for key, value in record.__dict__.items():
            if key not in RESERVED_KEYS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = "".join(
                traceback.format_exception(*record.exc_info))[-2000:]
        import json
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    """控制台：人类可读；带 request_id 时追加尾缀方便对照。"""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        rid = request_id_var.get()
        if rid and f"[req:{rid}]" not in base:
            return f"{base} [req:{rid}]"
        return base


def log_event(logger: logging.Logger, event: str,
              level: int = logging.INFO, exc_info=None, **fields) -> None:
    """记录一个结构化事件。

    - fields 必须可 JSON 序列化（default=str 兜底）；异常堆栈走 exc_info 参数，
      不要塞进 fields（会与 LogRecord 标准属性撞名）。
    """
    logger.log(level, event, exc_info=exc_info,
               extra={"event": event, **_sanitize_fields(fields)})


# ── 请求中间件：request_id 生成 + 访问日志 ───────────────────
_ACCESS_SKIP_PREFIXES = ("/static", "/uploads", "/docs", "/openapi.json",
                         "/favicon", "/redoc")


def register_request_middleware(app) -> None:
    """挂到 FastAPI：响应头 X-Request-Id + http.access 事件日志。

    静态资源/文档路径不记访问日志（前端截图轮询、文档页会刷屏），
    但 request_id 仍然注入（同一 contextvar，异常处理也能拿到）。
    """
    from starlette.requests import Request

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            # 未被异常处理器接住的（如中间件外抛），兜底记一条再抛
            logging.getLogger("http").error(
                "request.crash", exc_info=True, extra={
                    "event": "request.crash", "method": request.method,
                    "path": request.url.path,
                    "ms": round((time.perf_counter() - start) * 1000, 1),
                    "error": repr(exc),
                })
            request_id_var.reset(token)
            raise
        request_id_var.reset(token)
        response.headers["X-Request-Id"] = rid
        path = request.url.path
        if not any(path.startswith(p) for p in _ACCESS_SKIP_PREFIXES):
            log_event(
                logging.getLogger("http"), "http.access",
                method=request.method, path=path,
                status=response.status_code,
                ms=round((time.perf_counter() - start) * 1000, 1),
            )
        return response
