# VidInsight v3 后端 E2E 实测报告（自适应结构）

- **日期**：2026-10-18 23:50 ~ 2026-10-19 00:05
- **被测代码**：`D:/CyberStomach/VidInsight` @ `d5eb19c`（feat(prompt): v3 结构自适应——LLM 按内容自行设计章节，adaptive 成新默认）
- **测试方式**：Bash + curl 直打运行中的后端（无 GUI 参与）
- **总判定**：**PARTIAL** —— 步骤 0/1/2/4 按断言通过；步骤 3（核心：单条生成自适应结构）被外部依赖阻断（收藏专辑被动捕获的 xsec_token 已过期 22.4 小时 + 小红书匿名风控 302），4 次尝试全部失败在下载阶段，LLM 环节未到达。

---

## 第 0 步：路由与进程摸底 ✅

### 命令与结果

```bash
grep -rn "board" D:/CyberStomach/VidInsight/backend/app/routers/
grep -rn "generate\|task" D:/CyberStomach/VidInsight/backend/app/routers/note.py
netstat -ano | findstr :8483
```

- 收藏专辑路由：`backend/app/routers/note.py:681` → `GET /xhs/board/{board_id}/notes`（另有 `POST /xhs/ingest_board_notes` @ note.py:639）
- 生成接口：`backend/app/routers/note.py:247` → `POST /generate_note`；`note.py:314` → `GET /task_status/{task_id}`
- 前端实际调用（`vidinsight-frontend/src/services/note.ts`）：
  - note.ts:230 `request.get('/xhs/board/${boardId}/notes')`
  - note.ts:20 `request.post('/generate_note', data)`
  - **baseURL = '/api'**（`src/utils/request.ts:33`）→ 所有请求需带 `/api` 前缀（第一发不带前缀 404 的原因）
- 端口：`netstat -ano | findstr :8483` 无输出 → **8483 无监听**；`tasklist` 无 vidinsight.exe → 无 GUI 侧车，直接源码启动

### 生成接口必填参数（`note.py:44-70` VideoRequest）

`video_url`、`platform`、`quality`、`model_name`、`provider_id` 必填；`style` 可空（v3 默认 adaptive）。

---

## 第 1 步：确保 8483 跑新代码 ✅

### 启动

8483 空闲，直接启动（无需杀进程，也未触碰任何 vidinsight.exe / WebView2）：

```bash
cd /d/CyberStomach/VidInsight/backend && /d/CyberStomach/VidInsight/.venv/Scripts/python.exe main.py
# main.py 底部：port = int(os.getenv("BACKEND_PORT", 8483))
```

轮询 `curl http://127.0.0.1:8483/docs`：try1=000 → **try2=200**（约 5~10 秒就绪）。

### 新代码生效证据

- 仓库 `git log --oneline -1` → `d5eb19c feat(prompt): v3 结构自适应——LLM 按内容自行设计章节，adaptive 成新默认`（工作树对后端 prompt 相关文件无未提交修改）
- 运行代码中：`backend/app/gpt/prompt.py:172` `ADAPTIVE_PROMPT = '''`；`backend/app/gpt/prompt_builder.py:40` `effective_style = style or 'adaptive'`（缺省即 adaptive）
- 风格接口：**后端不存在** `/get_note_styles` 之类端点（grep routers 无 styles 路由）。风格列表是前端常量 `vidinsight-frontend/src/constant/note.ts:19` → `{ label: '自适应结构', value: 'adaptive' }` **位于风格组第一位**（风格组 12-30 行，adaptive 在首，business 在第 29 行）。故「adaptive 在列表首位」以前端常量 + 后端 prompt_builder 源码为证，非 curl 接口证据。

---

## 第 2 步：收藏专辑链路 ✅

```bash
curl -s "http://127.0.0.1:8483/api/xhs/board/66e64e4e000000001c01b8ed/notes" -o doc/_e2e_board_v3.json -w "HTTP %{http_code}, %{size_download} bytes\n"
# → HTTP 200, 89356 bytes
```

解析（`doc/_e2e_board_v3.json` 已留档）：

| 断言 | 要求 | 实际 | 结果 |
|---|---|---|---|
| 笔记数 | ≥ 240 | **total=242**（len(notes)=242） | ✅ |
| 首条 title 非空 | 非空 | `什么时候用 Skill，什么时候用 RAG？` | ✅ |
| 首条 cover 非空 | 非空 | `https://sns-webpic-qc.xhscdn.com/202610180122/8af7dafe.../spectrum/1040g34o...` | ✅ |

首条 note_id=`6ac297130000000015003937`，带 xsec_token（xsec_source=pc_board_dom）。

---

## 第 3 步：单条生成走自适应结构（核心）❌ 被外部依赖阻断

### 尝试记录（共 4 次，全部失败在同一处）

| # | 笔记 | 链接形式 | task_id | 结果 |
|---|---|---|---|---|
| 1 | 上号领皮肤（title 最短，5 字，按任务要求首选） | explore+token | `372aca65-60dc-49bd-be06-d5adf50bc112` | FAILED 400101 |
| 2 | 面试经验分享（次短，6 字） | explore+token | `3ed3ce7e-e4ff-4fdd-8533-5c596efc7a55` | 曾到 DOWNLOADING，FAILED 400101 |
| 3 | 什么时候用 Skill，什么时候用 RAG？（捕获序号新鲜度最高的首条） | explore+token | `d55337dc-74a2-4b55-a405-53d30236aecf` | FAILED 400101 |
| 4 | 同 #3 | 裸链接（无 token，BoardPage 前端支持的另一分支） | `90e15bed-cb06-4181-8aff-bfa1f2eec938` | FAILED 400101 |

统一错误：`{"code":500,"msg":"(400101, '页面已返回但未找到该笔记数据（链接可能已失效）')"}`

后端日志栈（task 3ed3ce7e）：

```
File "app/services/note.py", line 207, in generate → _download_media
File "app/downloaders/xiaohongshu_downloader.py", line 131, in download → _get_note
File "app/downloaders/xiaohongshu_downloader.py", line 535, in _get_note → raise BizException(400101 ...)
reason=note_not_in_initial_state（页面有 __INITIAL_STATE__ 但目标笔记不在状态树）
```

### 根因定性（三项独立证据）

1. **token 过期**：`backend/board_notes.json`（BoardNoteStore 磁盘档）中该专辑 242 条捕获于 **22.4~22.6 小时前**——小红书 xsec_token 时效远短于此。
2. **cookie 双路均失败**：后端已配置 XHS cookie（`/api/get_downloader_cookie/xiaohongshu` 返回非空 a1/xsecappid 等），日志中 8 次 `xhs.detail.retry_anonymous`（带 cookie 失败→摘 cookie 匿名重试也失败）。
3. **匿名直拉被风控**：`curl -s https://www.xiaohongshu.com/explore/6ac297130000000015003937`（浏览器 UA）→ **HTTP 302**（339 字节重定向壳，无 `__INITIAL_STATE__`）。

### 四项结构断言 — 未执行（未到达 LLM 环节）

- ① `# 内容洞察报告：` ② `*Content-[` ≥3 次 ③ 不含 `## 9. 风险与不可照搬内容` / `## 8. 学习价值小结` ④ `## ` 章节数 4-12
- **生成章节标题列表：无**（LLM 从未被调用到；如实报告，不编造）

---

## 第 4 步：批量总结链路 ✅（按任务字面断言）

前端逻辑（`vidinsight-frontend/src/pages/BoardPage/index.tsx:212-239` `handleSubmit`）：勾选笔记**循环**调 `generateNote`（即 `POST /generate_note`），style='adaptive'、format=['link','summary']、quality='medium'、grid_size=[3,3]；URL 带 xsec_token。

照此提交 2 条（含第 3 步实际使用的那条）：

```text
6ac297130000000015003937 → HTTP 200 {"code":0,"msg":"success","data":{"task_id":"d91c13cf-7f0c-4e2f-b314-a385a653073c"}}
6ac26f460000000018004ab0 → HTTP 200 {"code":0,"msg":"success","data":{"task_id":"9ea56e69-c6e2-47e5-952a-03067ef86bcf"}}
```

| 断言 | 要求 | 实际 | 结果 |
|---|---|---|---|
| 任务创建成功 | 返回 task id / accepted | 两条均 HTTP 200 + code:0 + task_id | ✅ |
| 至少 1 条到 PARSING 或更后 | 时间盒 3 分钟内 | **两条均到达**：状态文件 `stage_times` 记录 `PARSING(0.8s) → DOWNLOADING(0.6~0.7s)` | ✅ |

两条最终同样 FAILED 400101（token 过期同根因）；任务要求只到 PARSING+ 即可，故本步按字面通过。状态文件原文（`backend/note_results/d91c13cf-….status.json`）：

```json
{"status":"FAILED","started_at":1792339439.31,"elapsed_s":1.5,
 "stage_times":{"PARSING":0.8,"DOWNLOADING":0.7},
 "message":"(400101, '页面已返回但未找到该笔记数据（链接可能已失效）')"}
```

---

## 问题与遗留

1. **【阻断第 3 步】收藏专辑 token 过期**：需在 VidInsight 桌面端重新打开被动捕获窗口翻一遍收藏，刷新 `backend/board_notes.json`（xsec_token 时效 <1 天，捕获后应尽快消费），然后重跑第 3 步四断言。
2. **后端实例仍在运行**：本次测试启动的 8483 实例（源码 uvicorn，后台任务）未停，可留作后续复测；不需要时直接结束该 python 进程即可（非 vidinsight.exe，允许杀）。
3. **小观察**：`/api/task_status` 失败时 HTTP 层返回 200、body 内 `code:500`（`{"detail":"Not Found"}` 式 404 与之同理均带前缀问题）——前端轮询若只看 HTTP 状态码会误判，建议统一（本次用 body 内 code 判定，不影响结论）。
4. 风格列表无后端接口（纯前端常量），“adaptive 首位”断言以 `constant/note.ts:19` 为证；若希望后端可校验，可考虑补一个只读端点。

## 留档文件

- `doc/_e2e_board_v3.json` — 专辑 242 条笔记原始响应
- `doc/E2E实测报告-v3.md` — 本报告
