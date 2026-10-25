"""WorkBuddy 本地免费模型池客户端（OpenAI chat/completions 兼容）。

端点：http://127.0.0.1:7864/v1（Bearer wb2api-local），本地 router 层统一管理
免费泳道路由，调用方只按模型名调用（deepseek-v4.1-flash / hy3 / hy4-preview）。

【硬性约定】该端点同时供一条 6 车并行的生产流水线使用，绝对不能互相干扰：
  1. 全局并发上限 2（进程内信号量），超出的请求排队，绝不并发打满
  2. 遇 429/503 一律指数退避重试：初始 5s，×2，上限 120s，加 ±10% 抖动；
     最多重试 5 次后放弃该请求并抛 WorkBuddyPoolExhausted 交给调用方降级
     （深夜 23:00-08:00 上游偶发 WAF 波是已知现象，会自动恢复）
  3. 请求超时：连接 10s，流式首字节 60s（httpx read），总时长按业务
  4. 直连不走代理（本地端点，绕开 ProxyConfigManager 注入与环境变量代理），
     且 openai SDK 自带重试关闭（max_retries=0），重试策略只由本模块持有，
     避免双重重试放大打到池子的流量

失败计数（429/503/其他异常/放弃/排队超时/连续失败）随每次失败打进应用日志，
便于观察池子健康；get_workbuddy_pool_stats() 供运维/预检读取。
"""
import os
import random
import threading
import time
import weakref
from typing import Any, Iterator, Optional

import httpx
from openai import OpenAI, RateLimitError, InternalServerError

from app.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:7864/v1"
DEFAULT_API_KEY = "wb2api-local"
WORKBUDDY_MODELS = ["deepseek-v4.1-flash", "hy3", "hy4-preview"]

MAX_RETRIES = int(os.getenv("WORKBUDDY_MAX_RETRIES", "5"))
BACKOFF_INITIAL_S = float(os.getenv("WORKBUDDY_BACKOFF_INITIAL", "5"))
BACKOFF_CAP_S = float(os.getenv("WORKBUDDY_BACKOFF_CAP", "120"))
CONNECT_TIMEOUT_S = float(os.getenv("WORKBUDDY_CONNECT_TIMEOUT", "10"))
FIRST_BYTE_TIMEOUT_S = float(os.getenv("WORKBUDDY_FIRST_BYTE_TIMEOUT", "60"))  # 模块级默认；实例化时会重新读取
QUEUE_TIMEOUT_S = float(os.getenv("WORKBUDDY_QUEUE_TIMEOUT", "600"))
GLOBAL_CONCURRENCY = int(os.getenv("WORKBUDDY_GLOBAL_CONCURRENCY", "2"))


class WorkBuddyPoolExhausted(RuntimeError):
    """退避重试预算耗尽，放弃该请求。调用方应降级（换模型/换供应商/稍后重试）。

    注意：str 里刻意不含 "429/503/timeout/rate limit/service unavailable" 等
    token，也不挂 status_code 属性——避免外层 UniversalGPT._is_retryable_error
    把它当成可重试错误再套一层重试，WorkBuddy 的重试预算只由本模块持有。
    """

    def __init__(self, attempts: int, waited_s: float):
        super().__init__(
            f"workbuddy_pool_exhausted: WorkBuddy 池退避重试 {attempts} 次后仍不可用，"
            f"已放弃该请求（累计等待 {waited_s:.0f}s）。深夜上游偶发 WAF 波会自动恢复，"
            f"可稍后重试或切换其他供应商"
        )
        self.attempts = attempts
        self.waited_s = waited_s


class WorkBuddyPoolBusy(RuntimeError):
    """排队超时：并发已满且等待超过 QUEUE_TIMEOUT。"""

    def __init__(self, waited_s: float):
        super().__init__(
            f"workbuddy_pool_busy: WorkBuddy 池并发已满（上限 {GLOBAL_CONCURRENCY}）"
            f"且排队 {waited_s:.0f}s 仍未获得名额，已放弃该请求"
        )
        self.waited_s = waited_s


def _backoff_delay(attempt: int) -> float:
    """第 attempt 次重试前的等待秒数：5s ×2 封顶 120s，±10% 抖动。"""
    base = min(BACKOFF_INITIAL_S * (2 ** attempt), BACKOFF_CAP_S)
    return base * random.uniform(0.9, 1.1)


class _PoolStats:
    """失败计数（线程安全）。每次失败随日志打出，便于观察池子健康。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.requests = 0
        self.retries = 0
        self.e429 = 0
        self.e503 = 0
        self.other_errors = 0
        self.gave_up = 0
        self.queue_timeouts = 0
        self.consecutive_failures = 0
        self.last_failure_ts: Optional[float] = None
        self.last_success_ts: Optional[float] = None

    def snapshot(self) -> dict:
        with self._lock:
            return dict(
                requests=self.requests, retries=self.retries,
                e429=self.e429, e503=self.e503, other_errors=self.other_errors,
                gave_up=self.gave_up, queue_timeouts=self.queue_timeouts,
                consecutive_failures=self.consecutive_failures,
                last_failure_ts=self.last_failure_ts,
                last_success_ts=self.last_success_ts,
            )

    def inc_request(self):
        with self._lock:
            self.requests += 1

    def record_retry(self, kind: str, attempt: int, delay: float, detail: str):
        with self._lock:
            self.retries += 1
            if kind == "e429":
                self.e429 += 1
            elif kind == "e503":
                self.e503 += 1
            self.consecutive_failures += 1
            self.last_failure_ts = time.time()
        logger.error(
            f"[WorkBuddy] {detail} → 退避重试 {attempt + 1}/{MAX_RETRIES}，"
            f"等待 {delay:.1f}s | 池健康: {self.snapshot()}"
        )

    def record_other_error(self, detail: str):
        with self._lock:
            self.other_errors += 1
            self.consecutive_failures += 1
            self.last_failure_ts = time.time()
        logger.error(f"[WorkBuddy] {detail} | 池健康: {self.snapshot()}")

    def record_gave_up(self, attempts: int, waited_s: float):
        with self._lock:
            self.gave_up += 1
        logger.error(
            f"[WorkBuddy] 退避重试 {attempts} 次耗尽，放弃该请求（累计等待 "
            f"{waited_s:.0f}s）| 池健康: {self.snapshot()}"
        )

    def record_queue_timeout(self, waited_s: float):
        with self._lock:
            self.queue_timeouts += 1
        logger.error(f"[WorkBuddy] 排队 {waited_s:.0f}s 超时放弃 | 池健康: {self.snapshot()}")

    def record_success(self):
        with self._lock:
            recovered = self.consecutive_failures > 0
            self.consecutive_failures = 0
            self.last_success_ts = time.time()
        if recovered:
            logger.warning(f"[WorkBuddy] 池子恢复（此前有连续失败）| 池健康: {self.snapshot()}")


class _ReleasingStream:
    """包装上游 stream：迭代期间持有并发名额，耗尽/关闭/异常时归还。"""

    def __init__(self, inner: Any, sem: threading.Semaphore):
        self._inner = inner
        self._sem = sem
        self._released = False

    def _release_once(self):
        if not self._released:
            self._released = True
            self._sem.release()

    def __iter__(self) -> Iterator:
        return self

    def __next__(self):
        try:
            return next(self._inner)
        except StopIteration:
            self._release_once()
            raise
        except Exception:
            self._release_once()
            raise

    def close(self):
        try:
            close = getattr(self._inner, "close", None)
            if close:
                close()
        finally:
            self._release_once()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __del__(self):
        # 调用方半路 break/异常退出且未 close 时，靠 GC 兜底归还并发名额
        self._release_once()

    def __getattr__(self, name):  # response/usage 等属性透传
        return getattr(self._inner, name)


class _Completions:
    def __init__(self, owner: "WorkBuddyClient"):
        self._owner = owner

    def create(self, *, model: str, messages: list, **kwargs) -> Any:
        return self._owner._create(model=model, messages=messages, **kwargs)


class _Chat:
    def __init__(self, owner: "WorkBuddyClient"):
        self.completions = _Completions(owner)


class WorkBuddyClient:
    """OpenAI 兼容客户端包装器：暴露与 openai.OpenAI 相同的
    `chat.completions.create(...)` / `models.list()` 调用面，
    在内部施加并发闸门与退避重试。工厂对 type=workbuddy 的供应商返回本类。"""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 inner: Optional[Any] = None):
        # 环境变量优先（运维开关/演练注入），其次供应商 DB 配置，最后内置默认
        env_key = os.getenv("WORKBUDDY_API_KEY")
        env_url = os.getenv("WORKBUDDY_BASE_URL")
        # 首字节超时实例化时读取：思考型模型夜间首字可达 2 分钟，运维/测试
        # 需要能在不重启解释器的情况下调整（模块级常量 import 即固化，改不动）
        self._first_byte_timeout = float(os.getenv("WORKBUDDY_FIRST_BYTE_TIMEOUT", str(FIRST_BYTE_TIMEOUT_S)))
        self._api_key = env_key or (api_key or "").strip() or DEFAULT_API_KEY
        self._base_url = env_url or (base_url or "").strip() or DEFAULT_BASE_URL
        self.stats = _PoolStats()
        self._sem = threading.Semaphore(GLOBAL_CONCURRENCY)
        if inner is not None:  # 测试注入
            self._inner = inner
        else:
            # 本地端点：trust_env=False 直连（不吃全局代理）；SDK 重试关闭，
            # 退避策略只由本模块持有
            timeout = httpx.Timeout(
                connect=CONNECT_TIMEOUT_S,
                read=self._first_byte_timeout,  # 流式首字节 / 非流式 TTFB
                write=30.0,
                pool=10.0,
            )
            self._inner = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                max_retries=0,
                http_client=httpx.Client(timeout=timeout, trust_env=False),
            )
        self.chat = _Chat(self)
        self.models = self._inner.models
        _CLIENTS.append(weakref.ref(self))
        logger.info(
            f"[WorkBuddy] 客户端就绪 base_url={self._base_url} "
            f"并发上限={GLOBAL_CONCURRENCY} 退避={BACKOFF_INITIAL_S}s×2 封顶"
            f"{BACKOFF_CAP_S}s 重试≤{MAX_RETRIES} "
            f"超时=连接{CONNECT_TIMEOUT_S}s/首字节{self._first_byte_timeout}s"
        )

    # 业务层可按需放宽 read（总时长按业务设）；connect/首字节默认值见模块头
    @property
    def timeout(self):
        return self._inner.timeout

    @timeout.setter
    def timeout(self, value):
        self._inner.timeout = value

    def _create(self, *, model: str, messages: list, **kwargs) -> Any:
        stream = bool(kwargs.get("stream"))
        self.stats.inc_request()
        attempt = 0
        waited = 0.0
        while True:
            if not self._sem.acquire(timeout=QUEUE_TIMEOUT_S):
                self.stats.record_queue_timeout(QUEUE_TIMEOUT_S)
                raise WorkBuddyPoolBusy(QUEUE_TIMEOUT_S)
            try:
                resp = self._inner.chat.completions.create(
                    model=model, messages=messages, **kwargs)
            except (RateLimitError, InternalServerError) as e:
                self._sem.release()
                status = getattr(e, "status_code", 0) or 0
                # 硬性约定：只对 429/503 退避重试；其余状态（如 500）不重试，
                # 计数后原样抛出，由调用方按既有降级逻辑处理
                if status not in (429, 503):
                    self.stats.record_other_error(f"上游 {status}（不在重试白名单）")
                    raise
                kind = "e429" if status == 429 else "e503"
                if attempt < MAX_RETRIES:
                    delay = _backoff_delay(attempt)
                    self.stats.record_retry(kind, attempt, delay, f"上游 {status}")
                    waited += delay
                    attempt += 1
                    time.sleep(delay)
                    continue
                self.stats.record_gave_up(attempt + 1, waited)
                raise WorkBuddyPoolExhausted(attempt + 1, waited) from e
            except Exception as e:
                self._sem.release()
                self.stats.record_other_error(f"请求异常: {type(e).__name__}: {str(e)[:200]}")
                raise
            # 成功：
            if stream:
                # 名额随流的生命周期持有（生成中也是并发占用），耗尽时归还
                return _ReleasingStream(resp, self._sem)
            self._sem.release()
            self.stats.record_success()
            return resp


def get_workbuddy_pool_stats() -> dict:
    """读取进程内 WorkBuddy 池健康计数（无存活客户端实例时返回零值快照）。"""
    for ref in reversed(_CLIENTS):
        client = ref()
        if client is not None:
            return client.stats.snapshot()
    return _PoolStats().snapshot()


# 存活客户端登记（weakref，防泄漏）；get_workbuddy_pool_stats 取最近一个
_CLIENTS: list = []


def is_workbuddy_provider(provider_type: str, base_url: str = "") -> bool:
    """工厂判据：type=workbuddy，或 base_url 指向本地 7864 端点。"""
    if (provider_type or "").strip().lower() == "workbuddy":
        return True
    return "127.0.0.1:7864" in (base_url or "")


def workbuddy_enabled() -> bool:
    return os.getenv("WORKBUDDY_ENABLED", "1") != "0"
