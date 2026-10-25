# HARNESS —— VidInsight Agent 的分层边界设计

> **定位**：Agent 决定下一步做什么；Harness 负责规定它在什么边界里做、怎么记录、怎么验证、什么时候才算真的完成。
> 本文把项目里**已经存在**的机制组织成分层叙述（对标 pico 的 harness 分层），不引入新代码。面试追问「状态怎么管、断了怎么恢复、工具为什么不会乱调」时，按层作答。

## 分层总览

```
用户输入
   │
┌──▼──────────────────────────────────────────────────┐
│ ① Input Harness        _sanitize_input()            │  注入过滤·长度截断·意图分类
├─────────────────────────────────────────────────────┤
│ ② Context Harness      预检索注入·来源编号[n]·画像段 │  检索增强·反幻觉约束
├─────────────────────────────────────────────────────┤
│ ③ Tool Harness         工具白名单·JSON Schema·5轮上限 │  Agent 能做什么、做多少
├─────────────────────────────────────────────────────┤
│ ④ Runtime Harness      线程池执行器·协作式取消        │  任务怎么排队、怎么停下
├─────────────────────────────────────────────────────┤
│ ⑤ State Harness        status.json·checkpoint·DAO    │  断了怎么恢复
├─────────────────────────────────────────────────────┤
│ ⑥ Safety Harness       garak 扫描·伦理合规约束       │  红线与验证
└─────────────────────────────────────────────────────┘
```

## ① Input Harness（输入边界）

- **实现**：`agent.py::_sanitize_input()`（L450），22 组正则：ChatML/Llama 分隔符（`<|im_start|>`、`[INST]`）、中英文指令注入（"忽略以上指令"/"ignore all previous instructions"）、角色扮演劫持（"从现在开始你是"/DAN mode）、提示词提取（"输出 system prompt"）；超 4000 字符截断。
- **位置**：`chat()` 的 Step 0，意图分类之前——恶意载荷不进入任何下游环节。
- **意图分类**（search / task / chitchat）也是输入边界的一部分：决定走哪条受约束的路径，而不是把所有输入都丢给自由生成。

## ② Context Harness（上下文边界）

- **实现**：`knowledge_base.py` 预检索注入——提问前先检索统一知识库，把相关 chunk 注入 prompt；
- **来源编号引用 `[n]`**：每个被引用的内容都绑定来源（视频标题/时间戳），prompt 中硬性要求"无法找到依据时明确说不知道"，抑制幻觉。
- **上下文窗口管理**：对话历史截取 + 检索结果按相关度裁剪进 prompt（演进方向见「已知边界」）。

## ③ Tool Harness（能力边界）

- **实现**：`agent_tools.py`——只有 4 个工具（search_kb / get_source / list_notes / clarify），每个都有 JSON Schema 参数校验，白名单外的一律拒绝。
- **循环约束**：`agent.py::_react_loop()` 硬上限 5 轮，防止工具循环失控；每轮开头检查取消标志（见 ④）。
- **为什么不会乱调**：工具只能读知识库，没有写操作、没有 shell、没有网络出口——能力面天然收窄。

## ④ Runtime Harness（执行边界）

- **实现**：`task_serial_executor.py`（线程池，TASK_MAX_WORKERS=3）+ `task_cancel.py`（协作式取消）。
- **取消语义**（字节真题：耗时长用户等不及怎么停）：
  - API `POST /tasks/{id}/cancel` 设置取消标志 → 立即写 CANCELLED 终态；
  - 任务在**阶段边界**（下载/转写/每轮 LLM 调用之间）检查标志并抛 `TaskCancelled`；
  - **边界约束**：正在飞行中的单次网络/LLM 调用无法中断，只能等它返回——这是机制的延迟上界，主动承认而不是回避；
  - 取消后即使后续阶段抛出其他异常（实测：402 余额不足），也不会覆盖 CANCELLED 终态；
  - 排队中的任务在 executor 分发时直接跳过。
- **Chat Agent 的取消**：`_react_loop` 每轮开头检查；`POST /chat/cancel`。

## ⑤ State Harness（状态边界）

- **任务状态机**：`{task_id}.status.json`（PENDING → PARSING → DOWNLOADING → TRANSCRIBING → SUMMARIZING → SAVING → SUCCESS / FAILED / **CANCELLED**），前端 3s 轮询读文件而非内存——后端重启状态不丢。
- **断点恢复**：`universal_gpt.py` checkpoint 机制——长文转写分块总结时每个 chunk 完成即落盘 partials（`{task_id}_markdown.gpt.checkpoint.json`）；失败/取消后重试，已完成的 chunk 不重算（实测：取消任务留下的 checkpoint，重提任务直接续跑）。
- **前端状态持久化**：Zustand + IndexedDB（会话、任务卡片随会话隔离），路由切换/重启不丢。
- **僵尸状态自检**：应用重开时对"处理中"旧卡片查询后端真实状态，已不存在的任务标记 FAILED——状态永远向真实收敛。

## ⑥ Safety Harness（安全边界）

- **garak 漏洞扫描**：`backend/tests/garak_scan.py`（自研探针，prompt injection + jailbreak 两类 12 探针，92% 防御率）；多语言绕过是已知缺口。
- **伦理合规**：REACT_SYSTEM_PROMPT / CHITCHAT_SYSTEM_PROMPT 内置数据使用声明、有害内容拒答规则；隐私上 API key 只存本地 DB，日志不落敏感内容。
- **验证闭环**：输入消毒 → Agent 输出必须带 `[n]` 来源 → 来源可回链到知识库 chunk 的 citation（视频标题+时间戳）。

## 已知边界（主动交代）

1. Memory 是短期历史截断，无分层压缩/长期画像（演进：摘要压缩 + SQLite 画像注入）。
2. 无 ReAct 轨迹 JSONL 日志（演进：每轮写 thought/tool/耗时/token）。
3. 取消无法中断飞行中的调用——分布式部署需要把取消标志放共享存储。
4. Reranker 默认关闭（CPU 代价 10-18s/查询，实测数字见 README 评测小节）。
