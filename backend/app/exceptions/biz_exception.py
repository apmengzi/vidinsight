# exceptions/biz_exception.py
from typing import Optional


class BizException(Exception):
    """业务异常：整数 code 兼容历史，err/detail 承载机器可读定位信息。

    - err    : 机器可读错误码（app/exceptions/codes.py 注册表），日志与用户上报都认它
    - detail : 结构化上下文（如 platform/url/note_id），进日志与响应，不进用户文案
    """

    def __init__(self, code: int, message: str = "业务异常",
                 err: Optional[str] = None,
                 detail: Optional[dict] = None):
        self.code = code
        self.message = message
        self.err = err
        self.detail = detail
