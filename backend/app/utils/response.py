from typing import Optional

from fastapi.responses import JSONResponse
from app.utils.status_code import StatusCode
from pydantic import BaseModel


class ResponseWrapper:
    @staticmethod
    def success(data=None, msg="success", code=0):
        return JSONResponse(content={
            "code": code,
            "msg": msg,
            "data": data
        })

    @staticmethod
    def error(msg="error", code=500, data=None,
              err: Optional[str] = None, detail: Optional[dict] = None):
        """错误响应。err/detail 仅在提供时输出（保持对旧调用方与旧前端的兼容）。"""
        payload = {
            "code": code,
            "msg": str(msg),
            "data": data
        }
        if err:
            payload["err"] = err
        if detail:
            payload["detail"] = detail
        return JSONResponse(content=payload)
