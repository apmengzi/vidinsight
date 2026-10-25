# 贡献边界清单（T0 · 个人贡献自证）

> 用途：面试必问「这个项目哪些是你做的」。本文档逐模块给出**可验证的边界证据**，回答只主张自己真实做的部分。
> 证据口径：仓库 `D:\VidInsight\VidInsight`，初始导入 commit `21dee916`（团队课程交付「综合设计3」final source，2026-08-01），此后所有提交作者均为孟亚鑫（git: apmengzi，56+ commits）。
> 以下「初始后提交数」可用 `git log --oneline 21dee916..HEAD -- <路径> | wc -l` 复核。

## 一、边界总述（一句话版）

- **2026-08-01 之前**：团队课程交付（综合设计3）——RAG 基座（混合检索/BM25/Reranker）、ReAct 循环骨架、任务队列等由团队完成，组内分工见课程材料。
- **2026-08-01 之后至今**：全部迭代由孟亚鑫一人完成——小红书数据源（下载器+合集收割状态机）、Agent 工程标准化（错误码/结构化日志/轨迹/门禁）、桌面端收割链路（Tauri）、意图路由、评测体系（T1）等。

## 二、逐模块边界表

### A. 孟亚鑫主导/全新开发（初始导入后新增或重写，证据充分）

| 模块 | 路径 | 初始后提交 | 现行规模 | 说明 |
|---|---|---|---|---|
| 小红书下载器（图文+视频+合集展开+token 收割解析） | `backend/app/downloaders/xiaohongshu_downloader.py` | **11** | 487 行 | 含 `new Map([])` 毒化 JSON 解析的消毒修复、cookie 失效摘除重试 |
| 桌面端收割链路（Tauri 状态机 v2→v4.2：信号通道/登录墙/昵称验证/详情同源抓取） | `vidinsight-frontend/src-tauri/src/lib.rs` | **14** | 1089 行 | 解决 document.title 断通道、作者主页误抓、XHS 风控对策 |
| 聊天页合集流程 + 进度气泡 + 外链拦截 | `vidinsight-frontend/src/pages/ChatPage/index.tsx` | **12** | 1032 行 | |
| 工程标准化：错误码注册表 / JSON 结构化日志 / request_id 中间件 | `backend/app/exceptions/codes.py`、`backend/app/utils/log_standard.py` | 2 / 1 | 78 / 127 行 | 全新文件 |
| Agent 轨迹（T4：每请求 JSONL，token/耗时/工具事件） | `backend/app/services/agent_trace.py`、`backend/app/routers/trace.py` | 1 | 82 行 | 全新 |
| 发布门禁 + AgentOps 聚合 + 能力实验脚本 | `scripts/preflight.py`、`backend/tests/eval_agentops.py`、`backend/tests/exp_xhs_capability.py` | 新增 | — | |
| 意图路由（T7：规则→LLM→LRU 混合 Router） | `backend/app/services/intent_router.py` | 1 | 169 行 | |
| 检索评测（T1：40 条评测集 + 4 策略 × Recall/MRR/NDCG/延迟 + chunk 粒度对比） | `backend/tests/eval_retrieval.py`、`eval_queries.json`、`gen_eval_queries.py` | 新增 | 327 行 | 数字见 README「评测」小节 |

### B. 团队课程交付原样（初始导入后 0 提交——面试表述为「团队作品，我不主张个人所有权」）

| 模块 | 路径 | 现行规模 |
|---|---|---|
| 混合检索知识库（向量+BM25→RRF→Rerank 精排→来源标注） | `backend/app/services/knowledge_base.py` | 689 行 |
| BM25 索引（jieba） | `backend/app/services/bm25_index.py` | 187 行 |
| Reranker（BGE-Reranker-v2-M3 封装） | `backend/app/services/reranker.py` | 175 行 |
| 工具执行器（search_kb/get_source/list_notes/clarify） | `backend/app/services/agent_tools.py` | 254 行 |
| 任务串行执行器 | `backend/app/services/task_serial_executor.py` | 23 行 |
| 聊天服务入口 | `backend/app/services/chat_service.py` | 197 行 |
| 长转写分块（request_chunker） | `backend/app/gpt/request_chunker.py` | 161 行 |

### C. 团队交付 + 孟亚鑫增量（有改动，改动点可指认）

| 模块 | 路径 | 初始后提交 | 孟亚鑫的增量 |
|---|---|---|---|
| ReAct 循环 | `backend/app/services/agent.py` | 2（643 行） | 安全层合入（输入消毒）、意图分类埋点、**T4 轨迹全埋点**（每轮 LLM/工具/耗时/token） |
| GPT 客户端（含 _sanitize_input 22 组正则） | `backend/app/gpt/universal_gpt.py` | 1（345 行） | 详见该文件 git blame；安全正则层在课程交付中已存在，迭代期做过维护 |
| garak 安全扫描 | `backend/tests/garak_scan.py` | 1（310 行） | 扫描脚本维护（92% 防御率数字源自课程期扫描） |
| GPT 长文分块 | `backend/app/gpt/request_chunker.py` | 0（161 行） | 无改动（团队原样） |

## 三、诚实表述模板（面试口径）

- 问「RAG 是你做的吗」→「混合检索基座（BM25+向量+RRF+Reranker）是团队课程交付，我负责其上的**评测体系**——40 条人工标注评测集、4 策略对比、中文嵌入模型选型证据（默认英文模型 recall 0.008 vs bge-zh 0.42），以及后来的工程标准化层。」
- 问「Agent 循环是你写的吗」→「骨架来自课程交付；我做了意图分类路由、全链路轨迹埋点（每轮 thought/tool/耗时/token 落 JSONL）、取消机制，以及把整条链路的错误码/日志标准化。」
- 问「小红书数据源」→「从零到一全部我做的，包括对抗 XHS 签名墙/风控的完整战役（详见收割状态机与三层根因排查）。」

## 四、复核命令

```bash
cd D:\VidInsight\VidInsight
git log --oneline 21dee916..HEAD --stat -- backend/app/downloaders/xiaohongshu_downloader.py   # A 类示例
git log --oneline 21dee916..HEAD -- backend/app/services/knowledge_base.py                      # B 类应为空
git shortlog -sne --all                                                                          # 作者统计
```
