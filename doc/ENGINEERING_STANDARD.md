# VidInsight 工程标准化（T11）

> 目标：**出 bug 立马能定位，发布前问题必须拦住。**
> 发布后解决问题的成本是发布前的几十倍——本规范就是成本防线。
> 本文档是对 `doc/UPSTREAM_VS_OURS.md`（边界自证）的工程纪律补充。

## 1. 日志标准（backend）

**文件日志 `logs/app.log` = JSON Lines**（一行一条 JSON，可 grep / 可 jq / 可导入分析工具），5MB×3 自动轮转。控制台保持人类可读。

### 1.1 怎么记

```python
from app.utils.log_standard import log_event
from app.utils.logger import get_logger

logger = get_logger(__name__)

log_event(logger, "collection.expand.done", platform="xiaohongshu", total=8)
log_event(logger, "collection.expand.fail", level=logging.ERROR,
          err=spec.err, error=str(e), exc_info=e)   # 堆栈走 exc_info 参数
```

### 1.2 命名与字段约定

- **event**：`<域>.<动作>[.<结果>]`，如 `collection.expand.done`、`xhs.token.harvest`、`api.error`、`http.access`
- **字段**：扁平、可 JSON 序列化。禁止用 `message`/`msg`/`exc_info` 当字段名（与 LogRecord 标准属性撞名会 KeyError；`log_event` 已自动加 `f_` 前缀兜底）
- **request_id**：请求中间件自动注入每条日志，不需要手写

### 1.3 报障定位流程（用户报 bug → 现场）

1. 用户说**时间点 + 现象**（toast 上的错误文案和 `[ERR码]`）
2. `grep "ERR码" app.log` → 定位到 `api.error` 事件行 → 拿到 `request_id`
3. `grep "<request_id>" app.log` → 还原该请求全链路（http.access → 中间事件 → 失败）

## 2. 错误码标准（backend + frontend）

**唯一出处：`backend/app/exceptions/codes.py` 注册表**。新增错误必须注册，禁止散写字符串。

- `err`：机器可读（`XHS_TOKEN_MISSING` 这类大写下划线码）——日志、上报、自动化都认它
- `code`：整数业务码（与历史 ResponseWrapper 兼容），段位见 codes.py 头注释
- `detail`：结构化上下文（url/note_id/platform…），进日志和响应，不进用户文案

用法：

```python
from app.exceptions.codes import ErrorCode
from app.exceptions.biz_exception import BizException

spec = ErrorCode.XHS_PAGE_FETCH_FAILED
raise BizException(spec.code, spec.message, err=spec.err, detail={"url": url})
```

前端 `utils/request.ts` 拦截器统一在 toast 上附加 `[ERR码]`，用户截图即可对码。

## 3. 测试与发布门禁

### 3.1 纪律

- **改完必跑**：`python scripts/preflight.py --skip-cargo`（日常迭代）
- **合入 main / 出包部署前必跑**：`python scripts/preflight.py`（全量）
- 新功能必须带测试；修 bug 必须先写复现测试再修（回归保护）

### 3.2 preflight 内容

| 检查 | 命令 | 拦截什么 |
|---|---|---|
| backend.pytest | `python -m pytest -q`（范围锁定 tests/，见 pytest.ini） | 业务逻辑回归 |
| frontend.tsc | `pnpm exec tsc --noEmit` | 前端类型错误 |
| tauri.cargo_check | `cargo check` | Rust 侧编译错误 |
| env.ffmpeg / env.port_8483_free / env.dotenv | — | 打包环境缺件、端口孤儿、配置缺失 |

### 3.3 已被门禁拦住过的真实案例

- `dist_build/`（PyInstaller 产物）混入 pytest 收集 → python311.dll 冲突，全量收集 24 errors → `pytest.ini testpaths=tests` 修复
- `log_event` 字段撞 LogRecord 标准属性 → KeyError 吞掉真实异常 → `_sanitize_fields` 兜底 + 测试锁定
- `is_local_target("/x")` 在 Windows 误判（缺盘符不算绝对路径）→ 补 POSIX 根路径分支 + 既有测试转绿

## 4. 发布 checklist（桌面版）

1. `python scripts/preflight.py` 全绿
2. `git status` 干净（构建产物 dist_build/ 不入库）
3. PyInstaller 重打 backend（`_internal` 与 exe 必须同版本整体部署）
4. `pnpm tauri build`；MSI/NSIS 失败但 exe 成功时，便携部署走 exe 直拷（既有惯例）
5. 部署到 `D:\VidInsight` 后：冷启动 → 看启动横幅无报错 → `logs/app.log` 里 `startup 5/5` → 发一条真实链接 E2E

## 5. 打包环境标准（2026-09-14 numpy 双重导入事故后确立）

**事故**：Anaconda base 环境打包的 backend 启动即崩（`ImportError: cannot load module more than once per process`）。
**根因链**：Anaconda base 塞满科学全家桶（IPython/astropy/dask/numba…）→ PyInstaller 分析图爆炸到 20,187 模块（正常 11,826）→ 同时收进 `numpy._core.*`(2.x) 与 `numpy.core.*`(1.x) 两套 numpy → 冻结后双重初始化崩溃。
**教训**：打包产物 = 环境的函数；「能跑 pytest 的环境」不等于「能打包的环境」。

规则：
1. **打包必须用项目 `.venv`**（Python 3.12，uv 托管 CPython；`backend/build.bat` 强制校验）。禁止用 Anaconda base 打包
2. `.venv` 丢失时用 `uv` 的 CPython 3.12 重建（`py -3.11` 的注册项指向已失效路径，勿信）
3. **构建配方以 `backend/build.bat` 为唯一权威**（命令行参数式，动态生成 spec）；仓库里的 `VidInsightBackend.spec` 是过时副本，两套配方产物不同（91MB vs 137MB PYZ）
4. **部署前必须先在产物目录原地冒烟**（启动 exe → 探活 8483），通过了才准拷贝到 `D:\VidInsight`
5. 同一 venv 上**禁止并发跑两个 pip**（会互相锁构建目录，表现为永久卡死）；pip 卡住先查进程数

