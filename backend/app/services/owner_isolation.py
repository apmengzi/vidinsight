# -*- coding: utf-8 -*-
"""匿名 owner 隔离：让公开部署的每个浏览器只看到自己的任务。

为什么需要：
    VidInsight 原本是单机桌面应用，note_results/ 目录下的任务属于唯一用户。
    部署成公开网页后，所有访问者共用同一份数据——任何人打开历史页都能看到
    别人生成的笔记。本模块给每个浏览器一个匿名身份，按身份过滤任务列表。

设计取舍：
    - **不做登录**：匿名 UUID 存浏览器 localStorage，首次访问自动生成。
      公开体验站没必要让用户注册，隔离的目标只是"别互相看见"。
    - **不迁移旧数据**：历史任务（owner 为空）归到 LEGACY_OWNER，只有
      管理员能看到。避免上线瞬间所有旧笔记消失。
    - **token 从请求头取**：前端每次请求带 X-VidInsight-Owner。取不到时
      退化为 LEGACY_OWNER（兼容桌面端 / 老客户端，行为与改造前一致）。

安全边界：
    owner token 是客户端自报的，可伪造。这不是身份认证，只是"防误看"。
    真要防恶意访问需要真正的登录体系，超出本次范围。
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Optional

from fastapi import Request

OWNER_HEADER = "x-vidinsight-owner"
LEGACY_OWNER = "legacy"
_OWNER_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# owner 索引文件：task_id -> owner。放 note_results 旁边，随产物目录一起持久化。
_INDEX_NAME = "_owners.json"
_lock = threading.Lock()


def _index_path(results_dir: str) -> str:
    return os.path.join(results_dir, _INDEX_NAME)


def _load_index(results_dir: str) -> dict:
    p = _index_path(results_dir)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_index(results_dir: str, data: dict) -> None:
    p = _index_path(results_dir)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def normalize_owner(raw: Optional[str]) -> str:
    """校验并规整 owner token；非法或缺失时归到 LEGACY_OWNER。"""
    if not raw:
        return LEGACY_OWNER
    raw = raw.strip()
    return raw if _OWNER_RE.match(raw) else LEGACY_OWNER


def owner_from_request(request: Request) -> str:
    return normalize_owner(request.headers.get(OWNER_HEADER))


def register_owner(results_dir: str, task_id: str, owner: str) -> None:
    """任务创建时登记归属。"""
    if not task_id:
        return
    with _lock:
        data = _load_index(results_dir)
        data[task_id] = owner
        _save_index(results_dir, data)


def get_owner(results_dir: str, task_id: str) -> str:
    """查任务归属；未登记视为 LEGACY_OWNER。"""
    with _lock:
        data = _load_index(results_dir)
    return data.get(task_id) or LEGACY_OWNER


def filter_tasks_by_owner(results_dir: str, task_ids: list[str], owner: str) -> set[str]:
    """返回该 owner 可见的 task_id 集合。"""
    with _lock:
        data = _load_index(results_dir)
    visible = set()
    for tid in task_ids:
        if (data.get(tid) or LEGACY_OWNER) == owner:
            visible.add(tid)
    return visible


def can_access(results_dir: str, task_id: str, owner: str) -> bool:
    """单个任务是否可被该 owner 访问。"""
    return get_owner(results_dir, task_id) == owner
