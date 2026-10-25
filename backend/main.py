import os
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.staticfiles import StaticFiles
from dotenv import load_dotenv

from app.db.init_db import init_db
from app.db.provider_dao import seed_default_providers
from app.exceptions.exception_handlers import register_exception_handlers
# from app.db.model_dao import init_model_table
# from app.db.provider_dao import init_provider_table
from app.utils.logger import get_logger
from app import create_app
from app.services.transcriber_config_manager import TranscriberConfigManager
from app.utils.path_helper import get_runtime_path
from events import register_handler
from ffmpeg_helper import ensure_ffmpeg_or_raise

logger = get_logger(__name__)
load_dotenv()

# 读取 .env 中的路径
static_path = os.getenv('STATIC', '/static')
static_dir = os.getenv("STATIC_DIR", get_runtime_path("static"))
uploads_dir = os.getenv("UPLOAD_DIR", get_runtime_path("uploads"))
out_dir = os.getenv('OUT_DIR', os.path.join(static_dir, "screenshots"))

# 自动创建本地目录（static 和 static/screenshots）
if not os.path.exists(static_dir):
    os.makedirs(static_dir)
if not os.path.exists(uploads_dir):
    os.makedirs(uploads_dir)

if not os.path.exists(out_dir):
    os.makedirs(out_dir)


def warmup_knowledge_base() -> None:
    try:
        from app.services.knowledge_base import get_unified_knowledge_base

        stats = get_unified_knowledge_base().get_stats()
        logger.info(f"[startup warmup] 知识库预热完成: {stats}")
    except Exception as e:
        logger.warning(f"[startup warmup] 知识库预热失败（不影响启动）: {e}")


def warmup_transcriber() -> None:
    """启动期后台预热本地转写模型（2026-10-20）：generate_note 首单冷启动实测
    22.7s——NoteGenerator 构造链会完整加载 GPU whisper 模型。启动后在后台线程
    先做一遍，首单提交即热（毫秒级）。失败仅告警，绝不影响启动。

    参数必须与 NoteGenerator._init_transcriber()（app/services/note.py:341-345）
    完全一致，否则会命中不了 transcriber_provider 里的单例缓存、反而多加载一次。
    """
    try:
        cfg = TranscriberConfigManager().get_config()
        if cfg.get("transcriber_type") not in ("fast-whisper", "mlx-whisper"):
            logger.info("[startup warmup] 在线转写引擎，无需预热本地模型")
            return
        readiness = TranscriberConfigManager().is_model_ready()
        if not readiness.get("ready"):
            logger.info(
                f"[startup warmup] 转写模型未就绪，跳过预热"
                f"（{readiness.get('reason') or 'downloading'}）"
            )
            return
        t0 = time.time()
        from app.transcriber.transcriber_provider import get_transcriber

        get_transcriber(
            transcriber_type=cfg["transcriber_type"],
            model_size=cfg["whisper_model_size"],
            device=os.getenv("WHISPER_DEVICE", "cuda"),
        )
        logger.info(
            f"[startup warmup] 转写模型预热完成，耗时 {time.time() - t0:.1f}s（首单提交即热）"
        )
    except Exception as e:
        logger.warning(f"[startup warmup] 转写模型预热失败（不影响启动）: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动序列拆成 5 步、每步独立日志 + 异常时打明确的 [startup N/5 FAILED] 标记。
    # 目的：用户 docker logs 一眼能看出后端死在哪一步，避免「容器一直重启但看不出原因」。
    try:
        logger.info("[startup 1/5] register_handler() — 注册事件处理器")
        register_handler()

        logger.info("[startup 2/5] init_db() — 初始化 SQLite 数据库")
        init_db()

        logger.info("[startup 3/5] TranscriberConfigManager — 读取转写器配置")
        # 转写器不再在启动时强制初始化，而是在首次生成笔记时按需创建。
        # 如果配置了不可用的类型（如 mlx-whisper 未安装），会在使用时报错而非静默回退。
        _cfg = TranscriberConfigManager().get_config()
        logger.info(
            f"           当前转写器: type={_cfg['transcriber_type']}, "
            f"model_size={_cfg['whisper_model_size']}"
        )

        logger.info("[startup 4/5] seed_default_providers() — 初始化默认 LLM 供应商")
        seed_default_providers()

        logger.info("[startup 5/5] 启动完成，等待请求")
        threading.Thread(target=warmup_knowledge_base, daemon=True).start()
        threading.Thread(target=warmup_transcriber, daemon=True).start()
    except Exception:
        logger.exception("[startup FAILED] 后端启动期异常，详见堆栈；容器会退出并由 restart 策略决定是否重试")
        raise

    yield

app = create_app(lifespan=lifespan)

# 允许的源：本地 web 端 + Tauri 桌面端 + 浏览器扩展（chrome/edge/firefox）
# 用 regex 是因为 chrome-extension://<id> 的 id 在每次开发版加载时不固定
# Tauri 2 不同平台 webview origin 不一样，必须全列：
#   - macOS:   tauri://localhost  （自定义协议）
#   - Windows: https://tauri.localhost  （Edge WebView2）
#   - Linux:   http://tauri.localhost   （WebKitGTK）
# 漏掉哪个都会导致桌面端 fetch 返回 200 但 browser 因为 CORS 拒绝读响应，
# 表现为前端「连不上后端」但后端日志一片 200 OK。
CORS_ORIGIN_REGEX = (
    r"^chrome-extension://[a-z]+$"
    r"|^moz-extension://.+$"
    r"|^http://(localhost|127\.0\.0\.1)(:\d+)?$"
    r"|^tauri://localhost$"
    r"|^https?://tauri\.localhost$"
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)
register_exception_handlers(app)
# 请求上下文中间件：X-Request-Id 注入 + http.access 结构化访问日志
# （doc/ENGINEERING_STANDARD.md：用户报 bug 只给时间点，grep request_id 即可还原现场）
from app.utils.log_standard import register_request_middleware
register_request_middleware(app)
app.mount(static_path, StaticFiles(directory=static_dir), name="static")
app.mount("/uploads", StaticFiles(directory=uploads_dir), name="uploads")

# MCP server（SSE）：Agent 客户端（Cherry Studio/Claude Desktop 等）接
# http://<host>:8483/mcp/sse 后，对话里贴链接即可生成笔记/查进度/存凭证。
# 增量挂载独立子应用，不影响既有路由；MCP_ENABLED=0 可关闭。
if os.getenv("MCP_ENABLED", "1") != "0":
    try:
        from app.routers.mcp import mcp_asgi_app
        app.mount("/mcp", mcp_asgi_app(), name="mcp")
        logger.info("MCP server 已挂载：/mcp/sse")
    except Exception as e:  # noqa: BLE001 — MCP 不可用不阻塞主服务
        logger.warning(f"MCP server 挂载失败（忽略）：{e}")









if __name__ == "__main__":
    port = int(os.getenv("BACKEND_PORT", 8483))
    host = os.getenv("BACKEND_HOST", "0.0.0.0")
    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, reload=False)
