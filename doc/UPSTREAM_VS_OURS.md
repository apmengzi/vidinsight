# UPSTREAM VS OURS —— 代码边界自证清单

> **目的**：本项目基于开源项目 BiliNote（v2.4.0）二次开发。本文档逐目录标注「上游 BiliNote / 本团队自研 / 团队修改的上游文件」，确保简历与面试**只主张自研部分**，并主动声明底座来源。
> **维护原则**：每项标注给出可复现的 git 证据（commit 或行数）。被面试官发现未声明的借用，比主动声明致命一百倍。

## 1. 基线声明

| 项 | 值 |
|---|---|
| 上游项目 | BiliNote（开源 AI 视频笔记工具） |
| 基线版本 | v2.4.0（上游 release/2.4.0） |
| 基线快照证据 | 旧仓库 merge commit `ddaa0ee`（Merge branch 'release/2.4.0'）；本仓库初始导入 `21dee91`（2026-08-01 综合设计3 交付源码） |
| 自研起始 | 团队课程期间（2026 夏）在 2.4.0 基础上新增 Agent 层，首个完整快照见旧仓库 `fcb3354`（2026-07-12 Initial import of Cyber Stomach project） |

**一句话结论**：下载器/转写器/笔记生成主管线/前端外壳来自上游 BiliNote v2.4.0；**AI Agent 问答层（ReAct 循环、工具调度、混合检索知识库、安全护栏）是本团队自研**。

## 2. 目录级标注

| 目录/模块 | 归属 | 说明 |
|---|---|---|
| `backend/app/downloaders/` | 上游（+团队修改 douyin/youtube） | 平台视频下载适配 |
| `backend/app/transcriber/` | 上游（+团队修改 whisper/provider） | 语音转写引擎 |
| `backend/app/gpt/`（note 生成主链路） | 上游（+团队修改 request_chunker 等） | LLM 笔记生成管线 |
| `backend/app/db/`、`backend/app/utils/` | 上游 | DAO、路径/响应工具 |
| `backend/app/services/{chat_service,note,provider,model,cookie_manager,task_serial_executor,vector_store,constant,proxy_config_manager,transcriber_config_manager,chat_tools}.py` | 上游文件（部分团队有修改，见 §4） | 上游原有服务层 |
| `backend/app/services/agent.py` | **本团队自研** | ReAct Agent 主体 |
| `backend/app/services/agent_tools.py` | **本团队自研** | 工具 schema 与执行器 |
| `backend/app/services/knowledge_base.py` | **本团队自研** | 统一知识库 + 混合检索 |
| `backend/app/services/bm25_index.py` | **本团队自研** | jieba 中文 BM25 引擎 |
| `backend/app/services/reranker.py` | **本团队自研** | BGE 交叉编码器精排 |
| `backend/tests/garak_scan.py` | **本团队自研** | LLM 漏洞扫描（prompt injection / jailbreak 探针） |
| `BillNote_frontend/`、`BillNote_extension/` | 上游外壳 + 团队功能修改 | React/Tauri 前端、MV3 插件 |

## 3. 自研文件明细（面试主张范围）

| 文件 | 行数 | 职责 | 证据 |
|---|---|---|---|
| `agent.py` | 593 | ReAct 循环（感知→决策→行动→反馈）、意图分类、`_sanitize_input()` 输入消毒（22 组正则 + 4000 字截断，见 L450）、来源编号引用 `[n]`、反幻觉约束 | 旧仓 `fcb3354` 新增；安全层增强 `827380f` |
| `agent_tools.py` | 254 | 4 个工具的 JSON Schema + ToolExecutor（search_kb / get_source / list_notes / clarify） | 旧仓 `fcb3354` 新增 |
| `knowledge_base.py` | 689 | 统一 Chroma collection、递归语义分块（标题→段落→句子→字符，250 字 + 80 重叠）、向量+BM25 → RRF(k=60) 融合、来源引用构建 | 旧仓 `fcb3354` 新增 |
| `bm25_index.py` | 187 | rank_bm25 + jieba 分词、持久化语料、增量增删 | 旧仓 `fcb3354` 新增 |
| `reranker.py` | 175 | BAAI/bge-reranker-v2-m3 交叉编码器精排（可开关，`RERANKER_ENABLED`） | 旧仓 `fcb3354` 新增 |
| `garak_scan.py` | 310 | 自建 garak 探针对 Agent 做注入/越狱扫描（12 探针、92% 防御率报告） | 旧仓根目录，本次移入 `backend/tests/` |

注：Agent 层开发为团队课程项目实践，采用「人主导设计 + AI 结对辅助编码」方式；架构设计、检索策略选型、安全规则由团队完成并负责。

## 4. 团队修改过的上游文件（诚实清单）

git 证据：`git diff --name-status ddaa0ee fcb3354 -- backend/`（旧仓库）

| 上游文件 | 团队改动 |
|---|---|
| `routers/chat.py` | 接入自研 Agent（chat_agent 入口） |
| `services/chat_service.py` | 桥接 Agent 循环与上游会话存储 |
| `routers/note.py`、`services/note.py`、`utils/note_helper.py` | 笔记完成后挂接知识库索引钩子 |
| `routers/config.py` | 转写器/模型配置面扩展 |
| `downloaders/youtube_downloader.py` | 音频格式回退链（bestaudio m4a→webm→best） |
| `downloaders/douyin_downloader.py` | 适配修复 |
| `transcriber/whisper.py`、`transcriber_provider.py` | 模型选择/设备检测修复 |
| `requirements.txt` | 新增 chromadb、rank_bm25、jieba、sentence-transformers 等 |
| `frontend/ChatPage 等前端` | 会话任务卡片隔离（`6e09c7a`）、批量粘贴解析（`035ec85`）、僵尸任务自检（`66ff4c1`） |
| `downloaders/bilibili_downloader.py` | Cookie 热更新（`4560727`）——本仓库个人 commit |

## 5. 本仓库（VidInsight）个人提交记录

2026-08-01 交付后，`21dee91..66ff4c1` 共 11 个 commit（作者 apmengzi）：Agent 安全层合入、B 站 Cookie 热更新、批量链接解析、任务卡片会话隔离、PyInstaller 打包修复、ffmpeg 检查 TTL 缓存（修复 0xC0000017 资源耗尽）等。

## 6. 面试话术红线

1. **只主张 §3 的自研范围**：ReAct Agent、混合检索知识库、BM25/Reranker、安全护栏。
2. 被问「项目怎么来的」：主动说「基于开源 BiliNote v2.4.0 做视频笔记底座，我们自研了 Agent 问答层」——这是加分项。
3. 不主张：上游的下载器/转写器核心算法、多语言支持、前端框架本身。
4. 上游 LICENSE（MIT）与出处已在仓库 `LICENSE` / `README.md` 保留。

## 7. 复现命令

```bash
# 上游基线快照（旧仓库）
git -C <旧仓库> ls-tree ddaa0ee --name-only backend/app/services/
# 团队新增/修改清单
git -C <旧仓库> diff --name-status ddaa0ee fcb3354 -- backend/
# 本仓库个人提交
git log --pretty=format:"%h %an %s" 21dee91..HEAD
```
