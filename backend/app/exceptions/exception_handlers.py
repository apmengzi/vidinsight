# middlewares/exception_handler.py

import logging

from fastapi import Request
from fastapi import FastAPI

from app.enmus.exception import NoteErrorEnum
from app.exceptions.biz_exception import BizException
from app.exceptions.codes import ErrorCode, validate_registry
from app.exceptions.note import NoteError
from app.exceptions.provider import ProviderError
from app.utils.log_standard import log_event, request_id_var
from app.utils.logger import get_logger
from app.utils.response import ResponseWrapper as R
import traceback

logger = get_logger(__name__)


def _registry_problems() -> list[str]:
    """错误码注册表自检，只在异常时打 ERROR（避免每次请求都跑一遍）。"""
    problems = validate_registry()
    if problems:
        log_event(logger, "errorcode.registry_invalid", level=logging.ERROR,
                  problems=problems)
    return problems


def register_exception_handlers(app: FastAPI):
    _registry_problems()

    def _biz_response(request: Request, exc, err: str | None = None,
                      detail: dict | None = None):
        """统一错误响应：err/detail 透传 + 结构化事件日志（带 request_id）。"""
        rid = request_id_var.get()
        log_event(logger, "api.error", level=logging.ERROR,
                  err=err or getattr(exc, "err", None),
                  code=getattr(exc, "code", None),
                  error=getattr(exc, "message", str(exc)),
                  detail=detail or getattr(exc, "detail", None),
                  path=request.url.path, request_id=rid)
        return R.error(
            msg=getattr(exc, "message", str(exc)),
            code=getattr(exc, "code", 500000),
            err=err or getattr(exc, "err", None),
            detail=detail or getattr(exc, "detail", None),
        )

    @app.exception_handler(BizException)
    async def biz_exception_handler(request: Request, exc: BizException):
        return _biz_response(request, exc)

    @app.exception_handler(NoteError)
    async def note_exception_handler(request: Request, exc: NoteError):
        return _biz_response(request, exc)

    @app.exception_handler(ProviderError)
    async def provider_exception_handler(request: Request, exc: ProviderError):
        return _biz_response(request, exc)

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        spec = ErrorCode.INTERNAL
        log_event(logger, "api.error", level=logging.ERROR,
                  err=spec.err, code=spec.code, error=str(exc),
                  path=request.url.path,
                  request_id=request_id_var.get(),
                  exc_info=exc)
        return R.error(msg=spec.message, code=spec.code, err=spec.err,
                       detail={"path": request.url.path})
