"""双模板测试（独立文件，避开 test_universal_gpt_content_format 的 sys.modules 污染）。"""

# 文件名前缀 00 保证字母序收集时排在 stubs 测试（test_universal_gpt_* 往
# sys.modules 装假 app 包且不恢复）之前——此时真 app 还在缓存，导入干净。
# ── 双模板：知识萃取默认 / 竞品营销可选（2026-10-18 定位转向）──
# 用户实锤：真实场景是收藏教程做知识资产（面试复习等），营销结构对技术
# 教程无意义。knowledge 是完整模板切换（非提示追加），格式契约保持。

def test_knowledge_style_swaps_template():
    from app.gpt.prompt_builder import generate_base_prompt
    k = generate_base_prompt('标题', 'seg', 'tag', _format=['link'], style='knowledge')
    assert '核心概念与术语' in k, 'knowledge 应切换到知识萃取主结构'
    assert '目标人群' not in k, 'knowledge 不应残留营销章节'
    assert '*Content-[mm:ss]' in k, '时间码跳转契约必须保留（前端依赖）'
    assert '分析模板' not in k, 'knowledge 是完整模板，不应再叠加 style 提示'


def test_marketing_template_unchanged():
    from app.gpt.prompt_builder import generate_base_prompt
    m = generate_base_prompt('标题', 'seg', 'tag', _format=['link'], style='business')
    assert '目标人群' in m, '营销模板主结构不能被破坏'
    assert '运营周报' in m, '营销 style 提示仍应生效'


def test_knowledge_is_first_style_option():
    from app.gpt.prompt_builder import note_styles
    assert note_styles[0]['value'] == 'adaptive', 'v3 起默认首选应为自适应结构'


# ── v3 自适应结构（2026-10-18）：结构由 LLM 按内容生成，护栏锁定 ──
def test_adaptive_is_default_when_style_missing():
    from app.gpt.prompt_builder import generate_base_prompt
    p = generate_base_prompt('标题', 'seg', 'tag', _format=['link'])
    assert '自行设计' in p, 'style 缺省应走自适应模板'
    assert '请按以下结构生成' not in p, '自适应不得预置固定章节结构（营销/知识模板才有）'
    assert '## 2. 核心概念与术语\n' not in p, '自适应不得预置知识萃取固定章节'
    assert '分析模板' not in p, '自适应是完整模板，不应叠加 style 提示'


def test_adaptive_explicit_same_as_default():
    from app.gpt.prompt_builder import generate_base_prompt
    a = generate_base_prompt('T', 'S', 'G', _format=['link'], style='adaptive')
    d = generate_base_prompt('T', 'S', 'G', _format=['link'])
    assert a == d, '显式 adaptive 与缺省行为必须一致'


def test_adaptive_keeps_format_contract():
    from app.gpt.prompt_builder import generate_base_prompt
    p = generate_base_prompt('标题', 'seg', 'tag', _format=['link', 'screenshot'])
    assert '*Content-[mm:ss]' in p, '自适应必须保留时间码契约（前端目录/跳转依赖）'
    assert '*Screenshot-[mm:ss]' in p, '截图格式注入仍应生效'
    assert '# 内容洞察报告' in p, '报告头契约必须保留'


def test_adaptive_guardrails_present():
    from app.gpt.prompt import ADAPTIVE_PROMPT
    assert '4-9 章' in ADAPTIVE_PROMPT, '章节数护栏必须在模板里'
    assert '首章固定' in ADAPTIVE_PROMPT and '末章固定' in ADAPTIVE_PROMPT, '首末章锚定护栏必须在'
    assert '第一步' in ADAPTIVE_PROMPT and '第二步' in ADAPTIVE_PROMPT, '两段式（判断→生成）必须在'


def test_marketing_still_selectable():
    from app.gpt.prompt_builder import generate_base_prompt
    m = generate_base_prompt('标题', 'seg', 'tag', _format=['link'], style='marketing')
    assert '产品/品牌卖点' in m, 'marketing 显式选择应回到营销 9 章模板'
    assert '分析模板' not in m, 'marketing 是完整模板，不叠加提示'
