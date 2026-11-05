"""关键画面抽取与分析服务。

三级筛选把成本压下来：
  1. ffmpeg 场景检测（零模型成本）→ 候选帧
  2. dHash 去重 + 方差过滤（零模型成本）→ 幸存帧
  3. 视觉模型判断「这帧有没有值得留存的信息」（只对幸存帧）

筛选上限 MAX_FRAMES 防止动作类视频（CS2）镜头狂动抽疯。
"""
import base64
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Literal, Optional

from PIL import Image

from app.utils.logger import get_logger

logger = get_logger(__name__)
_VISION_BASE_LOGGED = False

# 上限与阈值
# 40 帧：21 分钟空调视频 GT 实测 25 帧（79s 间隔）覆盖 92.5%、漏 8 帧；
# 40 帧把长视频间隔压到 ~32s，分析成本仅 +60%（6 路并发 30s 级）
MAX_FRAMES = int(os.getenv("KEYFRAME_MAX", "40"))
SCENE_THRESHOLD = os.getenv("KEYFRAME_SCENE_THRESHOLD", "0.4")
DHASH_HAMMING_MIN = 10
TIME_PROXIMITY_S = 8         # 与已保留帧间隔 < 8s 视为冗余          # 相邻帧 dHash 汉明距离 < 5 视为重复
VARIANCE_MIN = 120.0           # 帧方差低于此视为纯色/空镜
PROBE_SECONDS = 6              # 视觉模型单帧超时

FrameKind = Literal["table", "ingredients", "step", "aim_point", "chapter", "chart", "other"]


def _dhash(img: Image.Image, size: int = 8) -> str:
    """感知哈希：缩到 9x8 灰度，比较相邻像素亮度得 64-bit 指纹。"""
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = ""
    for row in range(size):
        for col in range(size):
            bits += "1" if px[row * (size + 1) + col] > px[row * (size + 1) + col + 1] else "0"
    return bits


def _hamming(a: str, b: str) -> int:
    return sum(c1 != c2 for c1, c2 in zip(a, b))


def _variance(img: Image.Image) -> float:
    g = img.convert("L").resize((64, 64))
    px = list(g.getdata())
    mean = sum(px) / len(px)
    return sum((p - mean) ** 2 for p in px) / len(px)


def _plan_times(duration: float, max_frames: int,
                chapters: list[dict] | None) -> list[float]:
    """规划抽帧时间点。

    有章节（B 站 view_points 自带 from_s/to_s）→ 把名额按各段时长比例分配，
    段内均匀取点；章节间的空隙（片头/片尾/无章节区间）同样参与分配，
    不留盲区。实测教训：只抽"章节起点+15s"会让章节中段成为盲区——
    空调视频 92.5% 覆盖率漏掉的 8 帧全部落在章节中段。
    无章节 → 全片等间隔。
    """
    segs: list[tuple[float, float]] = []
    for c in chapters or []:
        a, b = float(c.get("from_s", 0) or 0), float(c.get("to_s", 0) or 0)
        if b > a:
            segs.append((max(0.0, a), min(duration, b)))
    segs.sort()
    merged: list[list[float]] = []
    for a, b in segs:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])

    # 章节之间的空隙也作为采样段（片头/片尾/UP主没标章节的区间）
    full: list[tuple[float, float]] = []
    cursor = 0.0
    for a, b in merged:
        if a - cursor > 5:
            full.append((cursor, a))
        full.append((a, b))
        cursor = b
    if duration - cursor > 5:
        full.append((cursor, duration))
    if not full:
        full = [(0.0, duration)]

    total = sum(b - a for a, b in full) or duration

    # 名额上限受「最小间隔」约束（2026-11-03 修复）：
    # 78 秒短视频按 40 帧规划 → 间隔 1.9s，抽出后又被 8 秒时间邻近规则
    # 砍到 8 帧（40 抽 8 留），既浪费 ffmpeg 调用又让用户看到「0/8」这种
    # 莫名其妙的候选数。这里让抽帧间隔不低于去重阈值的一半，
    # 抽多少就用多少，不再「密抽→狠砍」。
    min_step = TIME_PROXIMITY_S / 2.0          # 4 秒
    effective_max = min(max_frames, max(1, int(duration / min_step)))
    max_frames = effective_max

    # 按时长比例分配名额，每段至少 1 帧
    budgets = [max(1, round((b - a) / total * max_frames)) for a, b in full]
    while sum(budgets) > max_frames:  # 四舍五入可能超名额，从最长段回收
        i = budgets.index(max(budgets))
        if budgets[i] <= 1:
            break
        budgets[i] -= 1

    times: list[float] = []
    for (a, b), n in zip(full, budgets):
        if len(times) >= max_frames:
            break
        step = (b - a) / n
        for i in range(n):
            t = a + step * (i + 0.5)
            if t < duration:
                times.append(t)
    times.sort()
    return times[:max_frames]


def extract_keyframes(video_path: str, out_dir: str, max_frames: int = MAX_FRAMES,
                      chapters: list[dict] | None = None) -> list[dict]:
    """定点抽帧 → dHash 去重 → 方差过滤。

    只用 -ss 跳取，不做全片场景检测。原因：场景检测要解码全片每一帧，
    实测 21 分钟 4K60 AV1 视频纯解码就要 4-6 分钟（AV1 是纯 CPU 软解），
    比后面所有视觉分析加起来还慢十倍。而定点跳取走关键帧定位，
    25 次抽取通常 10-20s。

    抽帧位置按 _plan_times 规划（章节内均匀，无章节等间隔）。
    返回 [{timestamp_s, path}]，按时间排序。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 获取时长
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", video_path],
            capture_output=True, text=True, timeout=30)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
    except Exception:
        duration = 600.0

    _times = _plan_times(duration, max_frames, chapters)

    uniform_dir = out / "uniform"
    uniform_dir.mkdir(exist_ok=True)
    uniform_frames = []

    # 并发抽帧（2026-11-03 优化）：串行 40 次 ffmpeg 启动实测 52s（4K AV1 视频），
    # 并发 6 路降到 23s。单进程 fps 全片解码更慢（4K AV1 超 5 分钟），已否决。
    def _grab(idx_t):
        _idx, _t = idx_t
        fp = uniform_dir / f"u_{_idx:04d}.jpg"
        # -ss 放 -i 前是快速定位（关键帧跳转），只解到目标帧，代价极低
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{_t:.2f}", "-i", video_path,
             "-frames:v", "1", "-q:v", "3", str(fp)],
            capture_output=True, timeout=60, check=False,
        )
        return (_t, fp) if fp.exists() else None

    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=6) as _ex:
        for _r in _ex.map(_grab, enumerate(_times, start=1)):
            if _r:
                uniform_frames.append(_r)
    uniform_frames.sort(key=lambda x: x[0])

    # ── 选择策略：全量定点帧去重 ──
    # 没有场景检测了，所有候选都是定点帧，直接全收 + 去重。
    # 间隔已经保证不重叠（interval >= 10s > TIME_PROXIMITY_S 的常见值），
    # 所以时间邻近过滤主要拦 dHash 撞车的相邻静帧。
    kept: list[dict] = []
    seen_hashes: list[str] = []

    def _try_keep(ts, fp):
        if len(kept) >= max_frames:
            return False
        # 时间间隔去重：与已保留帧间隔 < TIME_PROXIMITY_S 的跳过
        if any(abs(ts - k["timestamp_s"]) < TIME_PROXIMITY_S for k in kept):
            return False
        try:
            img = Image.open(fp)
        except Exception:
            return False
        h = _dhash(img)
        if any(_hamming(h, ph) < DHASH_HAMMING_MIN for ph in seen_hashes):
            return False
        if _variance(img) < VARIANCE_MIN:
            return False
        seen_hashes.append(h)
        kept.append({"timestamp_s": round(ts, 1), "path": str(fp)})
        return True

    for ts, fp in uniform_frames:
        _try_keep(ts, fp)

    # 按时间排序
    kept.sort(key=lambda x: x["timestamp_s"])

    # 清理未入选帧
    kept_paths = {k["path"] for k in kept}
    for _, fp in uniform_frames:
        if str(fp) not in kept_paths:
            fp.unlink(missing_ok=True)

    logger.info(
        f"关键帧: 定点抽 {len(uniform_frames)} 帧"
        f"（{'章节均匀' if chapters else '等间隔'}）"
        f" → 去重后 {len(kept)}（上限 {max_frames}，覆盖 0-{duration:.0f}s）")
    return kept


def _img_data_url(path: str, max_side: int = 768, quality: int = 70) -> str:
    """压缩并转 base64 data URL（控制单帧 token 成本）。"""
    img = Image.open(path).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        ratio = max_side / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    import io
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


ANALYZE_PROMPT = """你在分析视频的一帧画面。转录上下文（这帧前后的旁白）：{context}

判断这一帧是否包含**值得写入笔记的结构化信息**——即音频和字幕传达不了、
必须看画面才能获得的信息。

值得留存（valuable=true）：
- 参数对比表 / 数据表格 / 价格清单
- 食材配料清单（带克数份量）
- PPT / 幻灯片文字页（有大段文字、要点、图表）
- 原理图 / 结构剖面图 / 流程示意图（讲"为什么"和"怎么运作"的）
- 操作步骤图 / 界面截图（含具体数值、步骤编号、政策条款）
- 数据图表（柱状图、折线图、流程图、对比图）

**烹饪/手工类视频的特殊判断（重要，别漏）**：
做菜、烘焙、手工、实验类视频里，**操作过程画面本身就是信息**——
即使没有文字标注，只要画面能看出"这一步在做什么"（倒面糊、包锡纸、
进烤箱、切块、称料），这一帧就是操作步骤（kind=step），必须 valuable=true。
典型场景：食谱的备料/搅拌/成型/加热/出炉各步骤、手工的组装顺序、实验的操作。
判断依据是「手/器具正在做什么动作 + 旁白在讲这一步」，不是「有没有配方表」。
注意：**成品摆拍图**（只有成品没有操作过程）才算 valuable=false。

**游戏类视频的特殊判断（重要，别漏）**：
游戏第一人称/第三人称画面里，**准星指向的位置本身就是信息**——
即使画面上没有任何文字标注、箭头、图表，只要旁白在讲"站在这里、对着这个点、
往这个方向扔"，这一帧就是描点/站位教学（kind=aim_point），必须 valuable=true。
典型场景：CS2/瓦罗兰特 投掷物点位、射击游戏架枪位、MOBA 蹲草点、我的世界建筑坐标。
判断依据是「准星/视角指向的画面元素 + 旁白在讲位置」，不是「有没有文字标注」。

**⚠️ 只描述你在这张图里真看到的东西（防幻觉，最重要）**：
旁白会连续讲一整段教学内容，但**每张截图只对应其中一个瞬间**。
禁止把旁白里提到的、或上一张图看到的内容，套用到当前这张图上。

判定 aim_point 前，**必须先在心里回答这三个问题**（答案只写进 description）：
1. 画面里有没有**正在瞄准的投掷物**（手雷/烟雾弹/闪光弹的投掷姿势）？
2. 画面里**准星是不是正对着某个可辨认的参照物**（墙角/屋檐/电线/牌子）？
3. 这是**丢之前**（准备动作）还是**丢之后**（烟雾已散/只剩枪）？

三个问题的判定：
- 1 和 2 都是「有」+ 3 是「丢之前」 → **aim_point，valuable=true**
- 3 是「丢之后」（道具已出手、烟雾已扩散、只剩持枪视角）→ **valuable=false**
  （这是效果展示，不是描点教学；即使旁白还在讲点位也不行）
- 1 或 2 任一是「没有」→ **valuable=false**（可能是普通行走/架枪/对枪画面）

注意：**"手上拿着枪"不等于"在瞄准投掷点位"**。持枪看某个方向是普通游戏画面，
只有「手持投掷物 + 准星对参照物 + 未出手」才是描点教学。

不值得留存（valuable=false）：
- 人物说话、人脸特写、主播出镜（画中画小窗不算主体，别因为它判 false）
- 纯产品外观展示（只有外形没有信息）
- 纯风景、纯场景、空镜
- 只有一行字幕的画面
- 视频章节进度条本身（章节单独用 API 拿）
- 纯游戏对战画面（没有在讲具体位置/点位，只是打斗过程）
- **纯黑背景 + 大字幕的过渡卡/片头片尾**（"起因是…""于是…""一键三连"这类，
  画面主体只有文字没有游戏画面/图表，属于剪辑转场，不是信息）
- **道具已丢完的效果展示**（烟雾已扩散、只剩持枪视角、没有瞄准动作）

重要：判断依据是**画面主体**（占屏幕 80% 的区域）。如果主体是表格/清单/PPT/原理图，
即使画面下方有视频章节进度条，也要按主体判定为 table/ingredients/chart/step，
不要因为看到进度条就判成 chapter。只有画面主体本身就是章节列表时才算 chapter。
同理，游戏画面右上角有主播摄像头小窗，也**不影响**主体判定。

只回答 JSON，不要任何其他文字：
{{"valuable": true/false, "kind": "table|ingredients|step|aim_point|chapter|chart|other", "title": "一句话说明这帧在讲什么", "table": {{"headers": ["列1", ...], "rows": [["单元格", ...], ...]}} 或 null, "description": "画面内容完整描述", "confidence": 0.0到1.0}}

规则：
- 看不清的文字留空或不写，绝对不要猜、不要补全画面里没有的内容
- 表格只在画面真的存在表格/清单类结构时才给
- 界面截图里若含价格、政策条款、步骤编号等可读信息，按内容归到 step 或 table，
  不要因为"不是标准表格"就笼统归成 other
- 没有结构化信息的画面一律 valuable=false

**⚠️ 表格必须逐格读取真实内容（防编造，第二重要）**：
提取 table 时，每个单元格必须是你**真的从画面里读到的文字/数字**。
以下情况 table 必须给 null，而不是编一个占位表格：
- 表格被大标题/水印/遮挡物盖住超过一半，看不清具体内容
- 只能看清表头看不清数据行
- 只能看清"型号1/型号2/价格/导航"这种笼统分类，读不到具体型号名和数值

反面案例（绝对禁止）：输出 headers=["型号","型号2","型号3"] 配 rows=[["价格","价格","价格"],["导航","导航","导航"]]
—— 这不是提取，这是把表头当内容复制，等于编造。

正确做法：读不清就 table=null，在 description 里写"表格被标题遮挡，内容不可读"。

**⚠️ 章节过渡页/大标题页一律 valuable=false**：
画面上有一个巨大的章节标题（如「Part 5 产品推荐」「第三部分」）盖在背景内容上，
且标题占据画面中心 30% 以上面积 —— 这是剪辑转场，不是信息页。
即使背景里隐约有表格，只要被大标题遮挡看不清，就判 valuable=false。

**⚠️ 游戏画面里禁止编造空间方位**：
说"面向 B 门/对着 A 大"之前，必须能从画面里**辨认出那个具体地标**（门/通道/牌子的形状）。
辨认不出就说"朝某个方向"，不要编一个具体地名。旁白提到"B 门"不代表画面里就是 B 门。"""


# 视觉模型配置：默认走 WorkBuddy 反代的 deepseek-v4.1-flash
# （本地 VidInsight 默认模型，已实测支持图像输入；base_url 对应反代 7864）
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "http://127.0.0.1:7864/v1")
VISION_API_KEY = os.getenv("VISION_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "deepseek-v4.1-flash")

# 备用视觉端（2026-11-03）：主端（本机反代）挂掉时自动降级到这里。
# Systalyze 直连可访问、不依赖任何本机进程，代价是单帧约 45s（主端 2-5s）。
# 不配则保持单端行为（向后兼容）。
VISION_FALLBACK_BASE = os.getenv("VISION_FALLBACK_BASE", "")
VISION_FALLBACK_KEY = os.getenv("VISION_FALLBACK_KEY", "")
VISION_FALLBACK_MODEL = os.getenv("VISION_FALLBACK_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash-1017")


def analyze_frame(image_path: str, context: str, provider: dict = None, model_name: str = None) -> Optional[dict]:
    """调视觉模型分析单帧。返回 FrameRecord dict 或 None（无价值/失败）。

    provider/model_name 不传时用 VISION_* 环境变量（默认 GLM-5.3-Flash）。
    """
    import httpx
    data_url = _img_data_url(image_path)
    prompt = ANALYZE_PROMPT.format(context=context[:1500])
    if provider and model_name:
        base = (provider.get("base_url") or "").rstrip("/")
        key = provider.get("api_key") or ""
        model = model_name
    else:
        base = VISION_BASE_URL
        key = VISION_API_KEY
        model = VISION_MODEL
    if not key:
        key = "wb2api-local"  # WorkBuddy 反代的默认 key（本地反代不校验）
    # 诊断（只打一次）：容器部署时 base_url 极易配错——127.0.0.1 在容器里
    # 指向容器自己，访问不到宿主机上的反代
    global _VISION_BASE_LOGGED
    if not _VISION_BASE_LOGGED:
        _VISION_BASE_LOGGED = True
        logger.info(f"[keyframe] 视觉模型 base={base} model={model}")

    # 主备双端（2026-11-03）：主端通常是本机 workbuddy 反代（快，2-5s/帧），
    # 但它依赖本机开机；备用端是云端 Systalyze（慢，45s/帧，但不依赖本机）。
    # 主端连不上（Connection refused / 超时）立即切备用，不浪费重试次数。
    endpoints = [(base, key, model)]
    if VISION_FALLBACK_BASE and VISION_FALLBACK_BASE != base:
        endpoints.append((VISION_FALLBACK_BASE, VISION_FALLBACK_KEY,
                          VISION_FALLBACK_MODEL))

    raw = ""
    for ep_i, (ep_base, ep_key, ep_model) in enumerate(endpoints):
        for _attempt in range(3):
            try:
                r = httpx.post(
                    f"{ep_base}/chat/completions",
                    headers={"Authorization": f"Bearer {ep_key}"},
                    json={
                        "model": ep_model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }],
                        # 4000 是给思考模型留的余量：Systalyze 的
                        # deepseek-v4.1-flash 不认 thinking:disabled，
                        # 会先写 1200+ 字 reasoning，预算太小 content 为空
                        "max_tokens": 4000,
                        "temperature": 0.1,
                        # 支持该参数的模型会关掉思考（workbuddy 反代有效）
                        "thinking": {"type": "disabled"},
                    },
                    timeout=PROBE_SECONDS * 10 if ep_i == 0 else 90,
                )
                _resp = r.json()
                _msg = _resp.get("choices", [{}])[0].get("message", {})
                # 思考模型的正文可能在 content 或 reasoning_content 里
                raw = _msg.get("content") or ""
                if not raw.strip():
                    raw = _msg.get("reasoning_content") or ""
                if raw.strip():
                    break
                logger.warning(f"视觉模型返回空（ep{ep_i} attempt {_attempt + 1}, "
                               f"finish={_resp.get('choices',[{}])[0].get('finish_reason')}）")
            except Exception as e:
                logger.warning(f"视觉模型调用失败（ep{ep_i} attempt {_attempt + 1}）: {e}")
                # 连接类错误（主端挂了）不必重试 3 次，直接切备用端。
                # 注意 Windows 的报错是中文「由于目标计算机积极拒绝」，
                # 只匹配英文 refused 会漏（实测踩过），两种都认。
                if ep_i == 0 and any(k in str(e).lower() for k in (
                        "refused", "connection", "unreachable", "timed out",
                        "拒绝", "无法连接", "连接失败", "10061", "10054")):
                    logger.info(f"[keyframe] 主端不可用，切备用端 {VISION_FALLBACK_MODEL}")
                    break
            if _attempt < 2:
                time.sleep(2 * (_attempt + 1))
        if raw.strip():
            break
    if not raw.strip():
        return None

    # 解析 JSON（容忍 ```json 包裹）
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None

    # 复核机制：视觉模型对同一帧的判定不稳定（实测 CS2 描点帧换个措辞
    # 就从 false 变 true）。判 false 但旁白明显在讲"位置/方向/对准"时，
    # 用更聚焦的问题再问一次，避免把教学帧漏掉。
    if not d.get("valuable") and _looks_positional(context):
        d = _recheck_positional(image_path, context, base, key, model) or d

    if not d.get("valuable") or d.get("confidence", 0) < 0.5:
        return None

    # 表格编造校验（2026-11-03，代码层兜底不靠模型自觉）：
    # 实测扫地机视频 07:31 那帧是「Part 5 产品推荐」大标题过渡页，
    # 模型看不清却编出 {"型号","型号2"...} × {"价格","导航"...} 的占位表格，
    # 等于把表头当数据复制。这里检测「单元格高度重复」和「疑似占位词」，
    # 命中就丢掉表格（保留这一帧的图，只是不渲染假表）。
    tbl = d.get("table")
    if isinstance(tbl, dict) and tbl.get("headers") and tbl.get("rows"):
        if _looks_fabricated_table(tbl):
            logger.warning(f"[keyframe] 疑似编造表格，已丢弃: {image_path[-24:]}")
            d["table"] = None

    return {
        "timestamp_s": 0.0,  # 调用方回填
        "image_path": image_path,
        "kind": d.get("kind", "other"),
        "title": d.get("title", ""),
        "extracted": d.get("table"),
        "raw_description": d.get("description", ""),
        "confidence": d.get("confidence", 0.5),
    }


# 旁白里出现这些词，说明这一帧可能在讲"站在哪/朝哪看/怎么扔"
_POSITIONAL_HINTS = (
    # 游戏点位
    "这里", "这个点", "这个位置", "对准", "瞄准", "对着", "往这", "朝这",
    "站在", "站位", "点位", "扔到", "丢到", "跳上", "卡住", "贴着",
    "准星", "视角", "方向", "角度", "参照",
    # 烹饪/手工操作步骤（"倒进去""搅拌""烤""包上"这类动作词）
    "倒入", "搅拌", "打发", "揉", "切", "铺", "包上", "刷", "挤", "筛",
    "烤", "蒸", "煮", "煎", "炸", "冷藏", "静置", "预热", "倒入模具",
    "材料", "配料", "克", "毫升", "勺",
)


def _looks_positional(context: str) -> bool:
    """粗筛：旁白是否在讲位置/方向（避免对每帧都触发复核，浪费调用）。"""
    if not context:
        return False
    return any(h in context for h in _POSITIONAL_HINTS)


# 表格「占位词」黑名单：这些词出现在**数据行**里说明模型在复制表头/编造
_PLACEHOLDER_CELLS = {
    "价格", "型号", "型号2", "型号3", "型号4", "型号5",
    "参数", "导航", "避障", "吸力", "拖地", "续航", "噪音",
    "列1", "列2", "列3", "单元格", "值", "数据", "项目",
    "a", "b", "c", "x", "y", "1", "2", "3",
}


def _looks_fabricated_table(tbl: dict) -> bool:
    """检测「编造/占位」表格（2026-11-03，代码层兜底）。

    实测扫地机视频 07:31 是「Part 5 产品推荐」大标题过渡页，模型看不清
    却输出 headers=["型号","型号2","型号3"...] rows=[["价格","价格"...],["导航"...]]。
    这种表把表头词当数据填，等于编造。

    判据（任一命中即视为编造）：
    1. 数据行里出现 ≥3 个占位词
    2. 某一行所有单元格文字完全相同（"价格,价格,价格"）
    3. 表头里出现「型号2/型号3」这类带序号的占位命名
    4. 数据行数 ≤ 1 且列数 ≥ 4（读不全的表）
    """
    headers = [str(h).strip().lower() for h in (tbl.get("headers") or [])]
    rows = tbl.get("rows") or []
    if not headers or not rows:
        return False

    # ③ 表头带序号占位命名
    seq_placeholder = sum(
        1 for h in headers
        if re.fullmatch(r"(型号|列|参数|项目)\s*\d+", h)
    )
    if seq_placeholder >= 2:
        return True

    # ①② 数据行占位词 / 整行重复
    placeholder_hits = 0
    for row in rows:
        cells = [str(c).strip().lower() for c in (row or [])]
        if len(cells) >= 3 and len(set(cells)) == 1:
            return True          # 整行一模一样
        for c in cells:
            if c in _PLACEHOLDER_CELLS:
                placeholder_hits += 1
    if placeholder_hits >= 3:
        return True

    # ④ 表很大但只读到一行
    if len(rows) <= 1 and len(headers) >= 4:
        return True

    return False


RECHECK_PROMPT = """旁白在讲操作步骤或位置：「{context}」

这一帧画面里，能否看出**正在做什么具体动作/指向什么位置**？
比如手上在倒面糊/搅拌/包锡纸/切块，或准星对准某个物体、视角朝某个方向。

如果有 → 这一帧是操作步骤或点位教学，回答 valuable=true：
  - 烹饪/手工/实验的操作动作 → kind="step"
  - 游戏/器械的指向性站位 → kind="aim_point"
  title 用一句话说清「这一步在做什么」。
如果画面完全没有动作也没有指向性（纯成品摆拍/纯风景/纯打斗）→ valuable=false。

只回答 JSON：
{{"valuable": true/false, "kind": "step|aim_point|other", "title": "一句话", "table": null, "description": "画面内容", "confidence": 0.0-1.0}}"""


def _recheck_positional(image_path: str, context: str, base: str, key: str, model: str):
    """复核：只问「有没有指向性」，比完整分析更聚焦，减少漏判。"""
    import httpx
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": RECHECK_PROMPT.format(context=context[-400:])},
                    {"type": "image_url", "image_url": {"url": _img_data_url(image_path)}},
                ]}],
                "max_tokens": 700,
                "temperature": 0.1,
                "thinking": {"type": "disabled"},
            },
            timeout=PROBE_SECONDS * 10,
        )
        msg = r.json().get("choices", [{}])[0].get("message", {})
        raw = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        mm = re.search(r"\{.*\}", raw, re.S)
        if not mm:
            return None
        d = json.loads(mm.group(0))
        if d.get("valuable") and d.get("confidence", 0) >= 0.5:
            logger.info(f"复核救回一帧: {image_path[-20:]}")
            return d
    except Exception as e:
        logger.debug(f"复核失败（忽略）: {e}")
    return None


SCREEN_PROMPT = """这帧画面里是否有【无法用语音传达】的结构化信息？

只有以下情况回答 YES：
- 参数对比表（多行多列的数据表格）
- 食材/配料清单（带克数或份量）
- 屏幕上的 PPT / 幻灯片文字页（有大段文字或图表）
- 描点/站位示意（游戏准星位置、操作图示）
- 数据图表（柱状图、折线图、流程图）

以下一律回答 NO：
- 人物说话、人脸特写
- 纯产品外观展示
- 纯风景、纯场景
- 只有一行字幕的画面
- 视频章节进度条（这个单独用 API 拿）

注意：如果画面下方有视频章节进度条，那不算信息——看**画面主体**（占屏幕 80% 的区域）。
主体是表格就 YES，主体是人在说话就 NO。

只回答一个词：YES 或 NO"""


def _screen_frame(image_path: str) -> tuple[str, bool]:
    """快速筛选：只问 YES/NO，输出 5 token，约 1.7s/帧。"""
    import httpx
    try:
        data_url = _img_data_url(image_path, max_side=640, quality=60)
        r = httpx.post(
            f"{VISION_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {VISION_API_KEY or 'wb2api-local'}"},
            json={
                "model": VISION_MODEL,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": SCREEN_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]}],
                "max_tokens": 5,
                "temperature": 0,
                "thinking": {"type": "disabled"},
            },
            timeout=30,
        )
        ans = r.json()["choices"][0]["message"].get("content", "").strip().upper()
        return image_path, ans.startswith("YES")
    except Exception as e:
        logger.warning(f"筛选失败 {image_path[-24:]}: {e}")
        return image_path, True  # 失败时保守放行，交给完整分析判断


def screen_frames(frames: list[dict], workers: int = 6) -> list[dict]:
    """并发快速筛选，只保留 YES 帧。约 2-4s/帧（6 路并发）。"""
    from concurrent.futures import ThreadPoolExecutor
    if not frames:
        return []
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for path, keep in ex.map(_screen_frame, [f["path"] for f in frames]):
            results[path] = keep
    kept = [f for f in frames if results.get(f["path"], True)]
    logger.info(f"快速筛选: {len(frames)} 帧 → {len(kept)} 帧有信息")
    return kept


def analyze_keyframes(frames: list[dict], provider: dict = None, model_name: str = None,
                      transcript_hint: str = "", workers: int = 6,
                      screen_first: bool = False, on_progress=None) -> list[dict]:
    """并发逐帧分析，有价值的留存。

    默认不做 YES/NO 预筛。实测预筛不可靠：同一张原理图重跑一次 YES 一次 NO，
    把召回押在最脆弱的一环上必然漏帧。而单帧完整分析只要 2-5s，25 帧 6 路
    并发约 20s，比"筛选 53s + 分析 16s"还快——预筛既慢又损质量。

    screen_first=True 仅保留给需要极限省 token 的场景。
    on_progress(done, total) 每完成一帧回调一次，供前端展示百分比。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if screen_first:
        frames = screen_frames(frames, workers=workers)
        if not frames:
            logger.info("视觉分析: 无值得留存的画面")
            return []

    def _one(f):
        d = analyze_frame(f["path"], transcript_hint, provider, model_name)
        if d:
            d["timestamp_s"] = f["timestamp_s"]
        return d

    results = []
    total = len(frames)
    done = 0
    # as_completed 而不是 ex.map：map 按提交顺序阻塞等待，慢帧会卡住整批
    # （实测 24 帧：map 70.6s vs as_completed 52.8s，快 25%）
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_one, f) for f in frames]
        for fu in as_completed(futures):
            try:
                d = fu.result()
            except Exception as e:
                logger.warning(f"单帧分析异常（跳过）: {str(e)[:80]}")
                d = None
            if d:
                results.append(d)
            done += 1
            if on_progress:
                try:
                    on_progress(done, total)
                except Exception:
                    pass
    logger.info(f"视觉分析: {len(frames)} 帧 → 留存 {len(results)}")
    return results


def dedup_analyzed(frames: list[dict]) -> list[dict]:
    """内容级去重：合并"同一张表/同一幅图"的多帧。

    dHash 去重只在抽帧层拦得住完全相同的画面；同一张参数表停留 3 分钟，
    期间高亮行变化、鼠标移动，dHash 距离被拉开，就漏进来了（实测 14 帧
    里 4 帧是同一张 2000 元内参数表）。这一层在分析之后做：

    1. 表格内容哈希相同 → 重复（不管时间差多少）
    2. 标题归一后相似度 > 0.78 且时间差 < 240s → 同屏微变，重复
       （时间窗防误杀：2000 元内表和 2500 元以上表标题也相似，但内容不同，
        内容哈希能兜住；title 相似+内容都没有的两帧近距离才算同屏）

    合并时保留信息更全的一帧（extracted.rows 多者优先，其次标题长者）。
    """
    def _norm_cell(x):
        return re.sub(r"[\s，,。]", "", str(x))

    def _table_of(f):
        t = f.get("extracted") or {}
        if not t.get("headers"):
            return None
        return ([_norm_cell(x) for x in t["headers"]],
                [[_norm_cell(c) for c in r] for r in (t.get("rows") or [])])

    def _same_table(a, b):
        """表头相同且一方的行是另一方的子集 → 同一张表（一帧可能只露了部分行）。"""
        if not a or not b:
            return False
        ha, ra = a
        hb, rb = b
        if ha != hb or not ra or not rb:
            return False
        short, long_ = (ra, rb) if len(ra) <= len(rb) else (rb, ra)
        hit = sum(1 for r in short if r in long_)
        return hit >= max(1, int(len(short) * 0.8))

    # 内容去重（2026-11-02 四轮实测后的最终结论）
    #
    # 前三版都失败了：
    #   v1 90s 时间窗同 kind 全合并 → 27 帧砍到 3 帧
    #   v2 加地点关键词 → 40 帧砍到 5 帧
    #   v3 按道具名分组 → 39 帧砍到 16 帧，且吞掉「凹槽雷」「车位灭火烟」
    #      「石板火」「喷泉闪」「台下闪」等独立点位
    #
    # 根因（dHash 实测）：这类教学视频 40 帧两两画面距离最小 16，**没有一对
    # 低于 12**——它们本来就是不同画面。用标题/时间窗猜"是不是同一段"必然误杀。
    #
    # 最终规则：**画面类（table/chart/step/aim_point 等）一律不合并**。
    # 只合并「表格内容相同」的帧（同一张参数表被抽到多次，那是真重复）。
    # 宁可多留几帧，也不能漏点位——用户明确要求「全」优先于「精简」。

    kept: list[dict] = []
    kept_tables = []
    for f in frames:
        tab = _table_of(f)
        dup_idx = -1
        # 只有表格才判重：表头相同 + 行内容 80% 重合 = 同一张表
        for i, ktab in enumerate(kept_tables):
            if _same_table(tab, ktab):
                dup_idx = i
                break
        if dup_idx >= 0:
            k = kept[dup_idx]
            # 留信息更全的：表格行数多者优先，其次标题更长的
            n_new = len((f.get("extracted") or {}).get("rows") or [])
            n_old = len((k.get("extracted") or {}).get("rows") or [])
            if n_new > n_old or (n_new == n_old and len(f.get("title") or "") > len(k.get("title") or "")):
                kept[dup_idx] = f
                kept_tables[dup_idx] = tab or kept_tables[dup_idx]
            continue
        kept.append(f)
        kept_tables.append(tab)
    if len(kept) < len(frames):
        logger.info(f"内容去重: {len(frames)} → {len(kept)}（同表多帧已合并）")
    # 合并时用晚帧替换早帧会破坏时间序，这里统一恢复
    kept.sort(key=lambda x: x["timestamp_s"])
    return kept


def get_bili_chapters(video_url: str, retries: int = 3) -> list[dict]:
    """B 站原生章节（view_points），零成本——不需要识别。
    返回 [{title, from_s, to_s}]；无章节或非 B 站返回 []。

    带登录 cookie（downloader 同源）+ 重试：B 站 API 对匿名请求偶发
    断连/超时（实测 RemoteDisconnected），章节缺失会退化为等间隔抽帧。
    """
    m = re.search(r"(BV[0-9A-Za-z]{10})", video_url)
    if not m:
        return []
    bvid = m.group(1)
    import httpx
    try:
        from app.services.cookie_manager import CookieConfigManager
        raw_cookie = CookieConfigManager().get("bilibili") or ""
    except Exception:
        raw_cookie = ""
    _headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.bilibili.com"}
    if raw_cookie:
        _headers["Cookie"] = raw_cookie
    try:
        view = None
        for i in range(retries):
            try:
                view = httpx.get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
                                 headers=_headers, timeout=12).json()
                break
            except Exception as e:
                if i == retries - 1:
                    raise
                logger.warning(f"B站 view API 第{i+1}次失败: {e}")
                time.sleep(1.5 * (i + 1))
        aid = view.get("data", {}).get("aid")
        cid = (view.get("data", {}).get("pages") or [{}])[0].get("cid")
        if not aid or not cid:
            return []
        player = None
        for i in range(retries):
            try:
                player = httpx.get(f"https://api.bilibili.com/x/player/v2?aid={aid}&cid={cid}",
                                   headers=_headers, timeout=12).json()
                break
            except Exception as e:
                if i == retries - 1:
                    raise
                logger.warning(f"B站 player API 第{i+1}次失败: {e}")
                time.sleep(1.5 * (i + 1))
        points = (player.get("data", {}).get("view_points") or [])
        return [{"title": p.get("content", ""), "from_s": p.get("from", 0), "to_s": p.get("to", 0)}
                for p in points]
    except Exception as e:
        logger.warning(f"B站章节获取失败: {e}")
        return []


def frames_to_markdown(frames: list[dict], url_prefix: str = "frames") -> str:
    """把留存帧渲染成图文混排 Markdown（表格 + 原图 + 时间戳）。"""
    if not frames:
        return ""
    out = ["\n## 关键画面信息\n"]
    for f in frames:
        mm, ss = int(f["timestamp_s"] // 60), int(f["timestamp_s"] % 60)
        ts = f"{mm:02d}:{ss:02d}"
        out.append(f"\n### {f.get('title') or '画面'} *Content-[{ts}]*\n")
        # 帧文件实际落在 <task_dir>/uniform|scene/ 下，只取文件名会丢子目录导致破图
        p = Path(f["image_path"])
        img_rel = f"{p.parent.name}/{p.name}"
        out.append(f"![{f.get('title') or '画面'}]({url_prefix}/{img_rel})\n")
        tbl = f.get("extracted")
        if tbl and tbl.get("headers") and tbl.get("rows"):
            headers = [str(h) for h in tbl["headers"]]
            n = len(headers)
            out.append("| " + " | ".join(headers) + " |")
            out.append("|" + "---|" * n)
            for row in tbl["rows"]:
                # 模型偶尔漏列：短行补空、长行截断，保证每行列数=表头数
                cells = [str(c) for c in (row or [])][:n]
                cells += [""] * (n - len(cells))
                out.append("| " + " | ".join(cells) + " |")
            out.append("")
        if f.get("raw_description"):
            out.append(f"> {f['raw_description'][:300]}\n")
    return "\n".join(out)
