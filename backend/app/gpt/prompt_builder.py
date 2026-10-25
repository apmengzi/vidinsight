from app.gpt.prompt import BASE_PROMPT, KNOWLEDGE_PROMPT, ADAPTIVE_PROMPT

note_formats = [
    {'label': '目录', 'value': 'toc'},
    {'label': '内容定位', 'value': 'link'},
    {'label': '关键画面', 'value': 'screenshot'},
    {'label': '洞察总结', 'value': 'summary'},
]

# 2026-10-18 v3 结构自适应：默认改为「自适应结构」——LLM 根据视频内容自行
# 设计章节（4-9 章、首末章锚定、格式契约锁定），适配任意主题/课题/视频类型。
# knowledge（知识萃取 8 章）与 marketing（竞品营销 9 章）保留为固定模板可选；
# 其余营销细分风格仍叠加在营销模板上（它们本身就是营销场景）。
note_styles = [
    {'label': '自适应结构', 'value': 'adaptive'},
    {'label': '知识萃取', 'value': 'knowledge'},
    {'label': '竞品营销', 'value': 'marketing'},
    {'label': '竞品速览', 'value': 'minimal'},
    {'label': '深度洞察', 'value': 'detailed'},
    {'label': '市场研究', 'value': 'academic'},
    {'label': '脚本拆解', 'value': 'tutorial'},
    {'label': '小红书种草分析', 'value': 'xiaohongshu'},
    {'label': '用户情绪洞察', 'value': 'life_journal'},
    {'label': '选题清单', 'value': 'task_oriented'},
    {'label': '运营周报', 'value': 'business'},
    {'label': '会议复盘', 'value': 'meeting_minutes'},
]

# 完整模板切换表：这三种是整模板（不叠加 style 提示），其余叠加在营销模板上
_FULL_TEMPLATES = {
    'adaptive': ADAPTIVE_PROMPT,
    'knowledge': KNOWLEDGE_PROMPT,
    'marketing': BASE_PROMPT,
}


def generate_base_prompt(title, segment_text, tags, _format=None, style=None, extras=None):
    # style 缺省 → 自适应结构（v3 新默认）；knowledge/marketing 为固定模板；
    # 营销细分风格（minimal/business/...）保持原行为：营销模板 + 提示叠加
    effective_style = style or 'adaptive'
    template = _FULL_TEMPLATES.get(effective_style)
    if template is not None:
        prompt = template.format(
            video_title=title,
            segment_text=segment_text,
            tags=tags,
        )
        is_full_template = True
    else:
        prompt = BASE_PROMPT.format(
            video_title=title,
            segment_text=segment_text,
            tags=tags,
        )
        is_full_template = False

    if _format:
        prompt += "\n" + "\n".join([get_format_function(f) for f in _format])

    if style and not is_full_template:
        prompt += "\n" + get_style_format(style)

    if extras:
        prompt += f"\n{extras}"
    return prompt


def get_format_function(format_type):
    format_map = {
        'toc': get_toc_format,
        'link': get_link_format,
        'screenshot': get_screenshot_format,
        'summary': get_summary_format,
    }
    return format_map.get(format_type, lambda: '')()


def get_style_format(style):
    style_map = {
        'minimal': '分析模板：竞品速览。只保留视频主题、核心卖点、主要痛点和 3 条运营建议，适合快速扫素材。',
        'detailed': '分析模板：深度洞察。尽量完整拆解卖点、人群、痛点、证据、脚本结构、话术和风险，适合市场研究。',
        'academic': '分析模板：市场研究。强调客观描述、证据链、竞品定位和用户需求归纳，语气正式克制。',
        'tutorial': '分析模板：脚本拆解。重点还原开场钩子、段落推进、证明方式、转化话术和结尾行动号召。',
        'xiaohongshu': '分析模板：小红书种草分析。重点分析种草场景、情绪价值、关键词、标题套路和种草风险。',
        'life_journal': '分析模板：用户情绪洞察。重点提炼用户焦虑、渴望、顾虑、情绪触发点和评论区可验证方向。',
        'task_oriented': '分析模板：选题清单。输出不少于 5 个可参考选题，每个选题附目标人群、切入角度和注意事项。',
        'business': '分析模板：运营周报。按“本周发现、竞品卖点、用户痛点、可参考选题、下周行动”组织内容。',
        'meeting_minutes': '分析模板：会议复盘。整理为团队会议可讨论的议题、结论、待验证问题和负责人建议。',
    }
    return style_map.get(style, '')


def get_toc_format():
    return '''
10. **目录**：不要手写目录。前端会根据最终 `##` / `###` 标题统一生成一个普通文本目录。
'''


def get_link_format():
    return '''
11. **内容定位**：为每个主要章节标题追加对应的起始时间，格式为 `*Content-[mm:ss]`。
示例：`## 5. 脚本结构 *Content-[01:23]`
'''


def get_screenshot_format():
    return '''
12. **关键画面**：结合截图网格和转录内容，在产品展示、价格活动、使用演示或对比画面处插入截图提示。
- 格式：`*Screenshot-[mm:ss]`
'''


def get_summary_format():
    return '''
13. **洞察总结**：在报告末尾加入 `## 洞察总结`，总结最值得运营团队带走的 3-5 条发现。
'''
