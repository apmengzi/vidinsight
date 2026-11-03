# HANDOFF — VidInsight 会话交接文档

> **给完全没有上下文的新会话看。**
> 最后更新：2026-11-02 10:45
> 恢复方式：新开会话第一句说「读 HANDOFF.md」，然后**先复述你理解的任务和进度，等我确认再动手**。

---

## ⚠️ 开工前必读：三个致命陷阱

### 陷阱 1：工作区目录不是项目目录

```
当前工作区：D:\CyberStomach\new_cyber_stomach_project   ← 旧仓库（BillNote 时代），别改这个
实际项目：  D:\CyberStomach\VidInsight                    ← 要改的是这里
```

两个是**不同的 git 仓库**（new_cyber `73e839f`，VidInsight `6a78623`）。
新会话如果按工作区找代码，会改到废弃的旧仓库。**所有代码操作都要显式 `cd /d/CyberStomach/VidInsight`。**

### 陷阱 2：`cargo build` 会做出坏的桌面端

**必须用 `pnpm tauri build`**（在 `vidinsight-frontend/` 下）。
`cargo build --release` 只编译 Rust，**不跑 `beforeBuildCommand`**，exe 里会打包旧前端资源。

判断是否真的成功了，要对比 chunk 名：

```bash
# exe 里的前端 chunk
python -c "import re; d=open('src-tauri/target/release/vidinsight.exe','rb').read(); print(sorted(set(n.decode() for n in re.findall(rb'MarkdownViewer-[A-Za-z0-9_-]+\.js', d))))"
# dist 里的
ls dist/assets/MarkdownViewer-*.js
# 两个必须一致，不一致就是没打包进去
```

### 陷阱 3：启动桌面端会抢焦点

```powershell
# 正确：最小化启动，不打断用户
Start-Process -FilePath '...\vidinsight.exe' -PassThru -WindowStyle Minimized
# 然后 ShowWindow($h, 7)  # SW_SHOWMINNOACTIVE
```

用户明确要求过：**不要弹前台窗口打断他**。脚本在 `/tmp/launch_bg.ps1`（会话级，可能已失效，逻辑见上）。

---

## 项目是什么

**VidInsight（灵悉）** — AI 视频笔记生成工具。贴 B 站/小红书/抖音/快手/YouTube 链接 → 下载 → 转写 → LLM 总结 → 结构化 Markdown 笔记。

- 后端：FastAPI，端口 8483，`backend/`
- 前端：React 19 + Vite + Tailwind，dev 3015，`vidinsight-frontend/`
- 桌面端：Tauri 2 + WebView2，`vidinsight-frontend/src-tauri/`
- 用户目标（原话）：**"到时候答辩肯定要实测 VidInsight，功能稳定性尤为重要……把深度调研 Codex、ZCode、DSH 的结果都用到 VidInsight 上"**

---

## 当前进度（2026-11-03）

### ⏱ 性能基准（A/B 实测，2026-11-03）

同一视频（BV1Wqp46GEiP 洗衣机 29min）、同 style（adaptive）、同 extras，
唯一变量是 `keyframe_extract` 开关：

| 配置 | 总耗时 | 笔记字数 | 关键画面 |
|---|---|---|---|
| **不开关键帧（默认）** | **35.4 秒** | 6481 | 无 |
| 开关键帧 | **217.0 秒** | 29027 | 30 帧 |

**关键帧让耗时变成 6.1 倍**。拆解：
- LLM 总结本身：35 秒（两次相同，不受关键帧影响）
- 关键帧部分：约 182 秒 = 下载 + 抽帧 + 视觉分析（最大头）+ 追加 22546 字

**这就是为什么默认关闭**：不需要画面的视频不该为 182 秒买单。

**LLM 总结分项基准**（同一份转写，不同 style，extras 一致）：

| style | 耗时 | LLM 输出字数 |
|---|---|---|
| knowledge | 20.0s | 4443 |
| tutorial | 19.5s | 4198 |
| adaptive（无 extras） | 22.9s | 4466 |
| adaptive（带 extras） | 31.7s | 7373 |

extras 让字数 +65%、耗时 +38%。**extras 必须保留**——它保证标题格式、章节结构、
目录规范，是功能正确性的一部分，不能为了快而砍。

**各阶段耗时明细**（29min 视频，开关键帧）：

| 阶段 | 耗时 |
|---|---|
| 下载（1080p 分块并发） | 5s（命中缓存） |
| 音频 + GPU 转写 | 21s |
| 抽帧（6 路并发） | 23s（串行时 52s） |
| **视觉分析（6 路并发）** | **178s** |
| LLM 总结（带 extras） | 35s |

### 已完成 ✅

**1. 关键画面抽取（核心功能，端到端可用）**

管线：
```
下载(1080p上限+分块8并发) → 定点抽帧(40帧, 章节均匀) → dHash去重+方差过滤
→ 视觉逐帧分析(6路并发, 无预筛) → 内容级去重 → 图文混排 Markdown
```

关键文件：
- `backend/app/services/keyframe_service.py` — 抽帧/分析/去重/渲染
- `backend/app/services/note.py` — 集成点（搜 `keyframe_extract`）

实测数据：
- 真值覆盖率 **100%**（空调 106 真值帧、扫地机 45 真值帧、CS2 31 真值帧，三次独立验证）
- 15 个家电视频批量测试全部 SUCCESS，平均 22 帧/笔记
- 抽帧耗时 402s → **13-23s**（放弃全片场景检测改 `-ss` 跳取；再改 6 路并发）

**1b. 关键帧开关化 + 历史补做（2026-11-03）**

按用户要求改为**默认关闭**（视觉分析慢 6 倍）：
- `NoteForm.tsx` / `ChatPage` / `BoardPage` 三处默认值全部 `false`
- 勾选框绑定 `canReadImage`：模型没勾「图片」能力时 **disabled** 并提示去配置
- 后端 `POST /api/note/backfill_keyframes`：给已有笔记补做
- 前端「补做关键画面」按钮（笔记详情工具栏，仅当笔记无关键画面时出现）

补做实测：27 帧写入 25551 字的已有笔记，耗时约 2 分钟（复用视频缓存）。

**1c. 关键帧判定 prompt（三轮修复）**

三类场景都要认，否则漏帧：
- **游戏描点**：准星指向即信息，不要求有文字标注（CS2 从 1 帧 → 28 帧）
- **烹饪步骤**：操作动作即信息，不要求有配方表（食谱从 1 帧 → 8 帧）
- 加**复核机制**：判 false 但旁白在讲动作/位置时，用聚焦问题再问一次

**1d. 内容去重策略（四轮试错定稿）**

最终规则：**只合并表格内容相同的帧，画面类一律不合并**。

四轮教训（都写在代码注释里）：
- v1 时间窗 90s 同 kind 全合并 → CS2 27 帧砍到 3 帧
- v2 加地点关键词 → 40 帧砍到 5 帧
- v3 按道具名分组 → 39 帧砍到 16 帧，吞掉独立点位
- **根因**：dHash 实测这些帧画面距离最小 16（阈值 12），本来就不重复

**2. 下载提速 110 倍**

`backend/app/downloaders/bilibili_downloader.py`：
- 原来 `bv*` 拉 4K，311MB / 15 分钟 / 350KB/s
- 改成 `bv*[ext=mp4][height<=1080]` + `http_chunk_size=10MB` + `concurrent_fragment_downloads=8`
- 结果：84MB / **8 秒** / 10.7MB/s

**3. 模型配置页（参考 ZCode 开源版重构）**

- `vidinsight-frontend/src/components/Form/modelForm/ModelCapabilityDialog.tsx` — 能力弹窗
- `vidinsight-frontend/src/components/Form/modelForm/ProviderTemplatePicker.tsx` — 11 个预设模板
- `vidinsight-frontend/src/pages/SettingPage/ProviderPresetPage.tsx` — 预设路由页
- `backend/app/models/model_capabilities.py` — 能力 schema
- `backend/app/data/provider_templates.json` — 模板数据
- 新 API：`/api/provider_templates`、`/api/api_formats`、`/api/model_capabilities`

**4. 前端关键帧开关**

之前**只有 API 能用，UI 开不了**。已补：
- `NoteForm.tsx` — schema + 默认值（**false**）+ 勾选框 + 视觉模型判断
- `ChatPage/index.tsx` — `AUTO_GEN_DEFAULTS.keyframe_extract = false`
- `BoardPage/index.tsx` — 批量生成传 `false`（十几条一起跑会很慢）
- `services/note.ts` — 接口加字段 + `backfillKeyframes()`

**5. 小红书关键帧支持（原先完全不可用）**

`XiaoHongShuDownloader.download_video` 是基类空实现（`pass`），关键帧功能在小红书上
直接崩在 `Path(None)`，报错信息是毫无意义的
`cannot access local variable 'video_path_str'`。

三级降级修复：
1. 笔记 API 直链（xsec_token 有效时最快）
2. **hellotik 兜底**（`hellotik.app/zh/rednote`，token 过期时的唯一出路）
3. 都失败 → 明确报错

顺带修了两个坑：
- **音频短路**：视频已在盘上时直接从 mp4 抽 mp3，不再调平台 API
  （小红书 token 过期时视频能靠 hellotik 拿到，但音频 API 会 400101 炸掉整个任务）
- **hellotik 平台映射**：`HELLOTIK_PAGES` + `CDN_ALLOW_BY_PLATFORM`

**7. 一图流（LLM 提炼 + 排版，2026-11-04 新增，桌面端已验收）**

需求：LLM 总结完视频后，根据整篇笔记生成一张「一图流」。

两条路，**默认走 A**：

**A. LLM 提炼 + PIL 排版（主路径，`POST /note/one_pager`）**
```
整篇笔记 → LLM 提炼 {title, subtitle, points[{head,body}], conclusion}
        → PIL 渲染 1080×1350（深色刊头 + 要点卡 + 结论条）
```
- 文字由代码画，**100% 准确**、秒级、无额度限制
- 提炼结果缓存到 `static/one_pagers/distill_<task_id>.json`，重复生成不再烧 token
- LLM 不可用 → 自动降级到本地抽取（`_parse`），功能不失效
- 关键文件：`app/utils/one_pager_distill.py`（提炼）、`app/utils/one_pager.py::_render_structured`（渲染）

**B. AI 排版（`POST /note/one_pager_image`，可选）**
同一份提炼结果 → 信息图提示词 → 文生图。更好看但可能写错字、约 23s、**每日 5 张**。

**灵栈渠道实测（api.lingzhanskill.com）**：
- 文生图 `POST /v1/images/generations`，模型 `gpt-image-2.5-flare`，约 23s，中文渲染准确
- 图生图 `POST /v1/images/edits`，**必须 multipart/form-data**
- 密钥在 `backend/config/image_gen.json`（已 gitignore），**绝不可硬编码**

**三个坑（都踩过）**：
1. **只支持 1024×1024**，传 1024x1536 报 422 `size_not_supported`。竖版靠 `_pad_to_portrait` 用 PIL 补画布
2. **中文 prompt 必须 UTF-8**。`curl -F "prompt=中文"` 会变 GBK 乱码（实测 `revised_prompt` 出现 `°ÑÍ¼ÖÐµÄ`），模型收到等于没指令
3. **灵能不自动抵扣**：网页端有余额，API 层免费额度用尽就是 429

**三个排版 bug（都实测踩过，已修 + 有测试锁住）**：
1. 结论条压页脚：`FOOTER_RESERVED` 62 → 96，改自下而上布局
2. 要点静默丢失（6 条只画 5 条）：测量基准与绘制基准不一致，改为共用 `CARD_TITLE_H` + 5 档自适应字号
3. 长标题压副标题：`head_h` 只算标题不算副标题行数

**⚠️ 相对路径坑**：后端返回 `/static/one_pagers/xxx.png` 是相对路径，桌面端会拼成
`tauri.localhost` 导致 404（破图）。前端必须过 `resolveAssetUrl()`（`utils/image-url.ts`）。

### 待办 ⏳

**6. 离线回看原片（2026-11-03 新增，桌面端已验收）**

需求：「像刷小红书一样」点开笔记就能看原片，断网也能看（飞机上学习）。

实现：
- `backend/events/handlers.py` — 转写完成后视频**不再删除**，改归档到
  `note_results/{task_id}/video.mp4`，用 `ffmpeg -c copy -movflags +faststart`
  重封装（moov 前置才能边下边播）；`MAX_ARCHIVE_BYTES` 默认 20GB，超限按最旧淘汰
  （只删视频，笔记和关键帧图保留）
- `backend/app/routers/note.py`
  - `GET|HEAD /api/task_video/{task_id}` — FileResponse，Starlette 自带 Range
  - `POST /api/note/save_video` — 老笔记就地补存原片（幂等）
- `vidinsight-frontend/src/pages/HomePage/components/NoteVideoPlayer.tsx`
  - 有归档 → 一行「播放原片 / 已归档到本地，离线可看」，点击展开 `<video>`
  - 无归档 → 一行「保存原片」，点击后台下载，轮询探测直到出现
- 开关：`NoteForm` 的「保存原片」勾选、`ChatPage` 的「存原片」按钮

**⚠️ HEAD 路由坑（踩过）**：只注册 `@router.get` 时 FastAPI 对 HEAD 直接 **405**，
前端 HEAD 探测永远失败 → 播放器根本不渲染。必须
`@router.api_route(..., methods=["GET", "HEAD"])`；前端探测也已改成 Range GET 兜底。

**⚠️ owner 隔离坑（2026-11-03 踩到，网页版必读）**：`X-VidInsight-Owner` 是 axios
拦截器统一注入的，**裸 `fetch` 不会带**。播放器用 fetch 探测 → 后端判成别人的笔记
→ 404 → 视频明明归档了也显示「保存原片」，点保存又永远卡在「正在保存」。
修法两处：`utils/request.ts` 导出 `getOwnerId()`，播放器所有 fetch 带上；
`<video src>` 无法设请求头，后端 `task_video` 额外接受 `?owner=` query。
验证：`?owner=<正确>` 200 / `?owner=<错误>` 404 / 不带 404。

**⚠️ 网页版深链白屏（2026-11-03 踩到）**：`vite.config.ts` 的 `base` 原是 `'./'`，
BrowserRouter 下打开 `/history/<id>` 时 `./assets/x.js` 被解析成
`/history/assets/x.js` → nginx `try_files` 回落 index.html → MIME `text/html`
→ 模块脚本被拒 → 白屏。改成按 mode 区分：`tauri` 用 `'./'`（file:// 必需），
其余用 `'/'`。nginx 同时加 `location /assets/ { try_files $uri =404; }`，
让缺失资源直接 404 而不是被 index.html 掩盖。

**⚠️ 历史列表永远为空（2026-11-03 修）**：`HistoryPage` 在 `tasks` 为空时提前
return「暂无分析记录」，`NoteHistory`（唯一拉 `/tasks/list` 的地方）根本没挂载 →
服务器补全永远跑不到 → 换浏览器/清缓存后历史全空。修法：把补全上移到
`HistoryPage` 自身，并加 `hydrating` 加载态。

**⚠️ 桌面端 owner 隔离要关掉（2026-11-03）**：`start_gpu.py` 和 Tauri
`spawn_backend_sidecar` 都 `setdefault("OWNER_ISOLATION", "0")`。桌面端是单人本地
应用，开着隔离会让老笔记（owner=`legacy`）对浏览器随机 UUID 不可见，播放原片
被判越权 404。**网页端走 Docker，不经过这两个入口，隔离仍默认开启**——
验证口径：网页端无 owner 404 / 正确 owner 200；桌面端无 owner 200。

验收（桌面端，真实 UI + CDP 检查）：
- 已归档笔记：`readyState=4, 1920x1080, 458s, currentTime 递增, error=null`
- 老笔记点「保存原片」→ 197MB 落盘 → 界面自动变「播放原片」→ 4K/1265s 正常播放

**7. 网页版默认模型自动降级（2026-11-03）**

`backend/app/services/note.py`：
- `_is_connection_failure()` — 连接类失败判定，中英文都认
  （Windows 报「由于目标计算机积极拒绝，无法连接」，不含 `refused`）
- `_get_fallback_gpt()` — 主模型连接失败时换备用供应商（Systalyze 优先，
  其次任一已配 Key 的其它供应商），重跑总结
- 排除 401/403/invalid api key/model not found：换模型没用，别浪费一次调用
- 单测 6/6 通过（3 类连接错误 → True，3 类认证/参数错误 → False）

前端 `ChatPage` 的 `PREFERRED` 顺序：Systalyze 的
`deepseek-ai/DeepSeek-V4.1-Flash-1017` 第一，workbuddy 的 `deepseek-v4.1-flash` 第二。

### 待办 ⏳

- [ ] 暗色主题下预设页图标对比度
- [ ] 预设页「一键启用 N 个模型」实测（代码已写，没点过）
- [ ] `api_format` 真正参与请求构造（只存了字段，仍走 OpenAI 兼容路径）
- [ ] 视觉分析速度：178s 是大头，8 路并发会因反代限流变慢，需要更强的反代或本地模型

### 已验证通过 ✅

- CS2 描点（BV1rYp16oEfe）— 29 帧
- CS2 道具（BV13Bv6BpEjm）— 40 帧，每个道具一帧
- 食谱步骤（小红书空气炸锅布丁）— 8 帧，覆盖备料/熬糖/搅拌/出炉
- 家电参数表（洗衣机 29min）— 30 帧，表格去重正常
- 家电冰箱（24min）— 33 帧
- 连续 5 视频稳定性测试 — **5/5 SUCCESS**
- 历史笔记补做关键画面 — 27 帧写入成功，约 2 分钟

---

## 关键路径与命令

```bash
# 后端（源码模式，非打包）
cd /d/CyberStomach/VidInsight/backend
OWNER_ISOLATION=0 TASK_MAX_WORKERS=2 ../.venv/Scripts/python.exe start_gpu.py

# 前端构建 + 桌面端打包（必须这条）
cd /d/CyberStomach/VidInsight/vidinsight-frontend
pnpm tauri build

# 类型检查
npx tsc --noEmit -p tsconfig.json

# 桌面端 exe
vidinsight-frontend/src-tauri/target/release/vidinsight.exe
```

**端口**：后端 8483，前端 dev 3015，**CDP 调试 9333**（`tauri.conf.json` 的 `additionalBrowserArgs` 里开了 `--remote-debugging-port=9333`）

---

## 验证 UI 的正确方式（不碰用户桌面）

**不要开 localhost 网页端**——用户明确禁止过。用 CDP 读桌面端真实 DOM：

```python
# 模板脚本：D:/tmp_kf_gt/cdp_measure2.py（会话级，逻辑如下）
import asyncio, json, urllib.request, websockets
targets = json.load(urllib.request.urlopen('http://127.0.0.1:9333/json/list'))
page = next(t for t in targets if t.get('type') == 'page')
# 连 page['webSocketDebuggerUrl']，发 Runtime.evaluate / Page.captureScreenshot
```

截图用 `Page.captureScreenshot`（不需要窗口在前台）。

---

## 已否决的方案（别再试）

| 方案 | 为什么否决 |
|---|---|
| **全片场景检测抽帧**（`select=gt(scene)`） | 21 分钟 4K60 AV1 要解码 3.8 万帧，纯 CPU 软解 4-6 分钟。降帧率/缩分辨率都没用，`select` 必须逐帧算分。改 `-ss` 定点跳取后 402s→13.4s |
| **YES/NO 预筛再分析** | 同一张原理图重跑一次 YES 一次 NO，不可靠；且单帧完整分析只要 2-5s，预筛反而更慢（69s vs 52s）。已删 |
| **图片 `object-cover` + `maxHeight:500`** | 裁掉宽表格右侧，表格读不全 |
| **图片固定 `maxWidth: 900px`** | 窗口宽度变化时图片不变，窄窗口溢出、宽窗口偏小 |
| **图片 `max-w-[88%]` / `max-w-full`** | 相对父级算，父级被内容撑到 1334px 时跟着撑，约束无效 |
| **在 `.markdown-body` 上改宽度** | 不是根因。真正撑宽的是 Radix ScrollArea 内部的 `display:table` 包裹层 |
| **`OWNER_HEADER` 隔离照旧** | 桌面端 owner 是 localStorage 随机 UUID，curl/脚本提交的任务归 `legacy`，桌面端看不见。已加 `OWNER_ISOLATION=0` 开关 |
| **`cargo build --release` 打包桌面端** | 见陷阱 2，会做出连 localhost 的坏 exe |

---

## 踩过的坑（血泪）

1. **Radix ScrollArea 内容宽度失控**（修了 6 次才对）
   - 现象：图片右侧被切，只有最大化正常
   - 根因：Radix Viewport 内部包一层 `display:table; min-width:100%`，宽度由内容决定
   - 修复：`src/index.css` 末尾加 `[data-slot='scroll-area-viewport'] > div { display:block !important; min-width:0 !important; width:100% !important; }`

2. **桌面端启动会杀死源码后端**
   - 根因：`src-tauri/src/lib.rs` 的 `kill_port_orphans(8483)` 无差别杀端口占户
   - 修复：加 `backend_already_healthy()`，端口上有健康后端就复用，不拉 sidecar

3. **数据目录分裂**
   - 打包版写 `%APPDATA%\VidInsight\`，源码版写 `D:\CyberStomach\VidInsight\`
   - 切后端会导致收藏/笔记"消失"。修复：把 APPDATA 的 `board_notes.json` 和 `static/covers/` 合并到项目目录

4. **WorkBuddy 反代并发上限**
   - 3 任务 × 6 路视觉 = 18 路同时打，超时率约 15%
   - 修复：`analyze_frame` 加 3 次重试；任务并发降到 2

5. **Windows Python 默认 gbk**
   - 写中文 JSON 用 `open()` 不指定 `encoding='utf-8'` 会崩
   - 所有脚本都要显式指定

6. **`keyframe_extract` 参数链断了三次**
   - 缺 `VideoRequest` 字段 → 缺 `background_tasks.add_task` 位置参数 → 缺 `_download_media` 参数
   - 诊断方法：看日志 `[keyframe] 开关=False`

---

## ZCode 开源调研结论

仓库：`github.com/zai-org/ZCode`（Apache-2.0，8410 stars），本地克隆 `F:\research\ZCode`

**照搬了**：能力 schema 字段命名（`supportsImage`/`contextWindow`/`reasoningLevels`）、API 格式三值枚举 + 「名称（端点路径）」展示、预设模板结构（`templateId/apiFormat/baseUrl/models`）

**有意没抄**：870 行受限 CEL DSL（我们只需 4 种映射形态）、Coding Plan 购买/额度逻辑、58 条站点特例规则

---

## 下一步建议（按优先级）

1. **先跑通一次完整验证**：桌面端点「内容分析」→ 贴 B 站链接 → 确认笔记末尾有 `## 关键画面信息` 且图片显示正常
2. CS2 描点 + 食谱清单两个场景实测（用户最初提的三个例子只验证了电商参数表）
3. 答辩前稳定性：连续跑 5 个视频不崩

---

## 相关文档

- `VidInsight关键帧进度.md`（项目根目录）— 关键帧管线细节，更新到 11-01 22:54
- `E:\pet\VidInsight-批量抽帧测试进度.md` — 15 视频批量测试数据，更新到 11-02 00:36
- `E:\pet\VidInsight-关键帧方案.md` — 最初的方案设计
- `TASKS.md`（同目录）— 待办池
