"""Supported collection platform for the Instagram-only product."""
from __future__ import annotations

import json

from .errors import ValidationError


def collection_platform(settings: dict | None) -> str:
    if settings is not None and not isinstance(settings, dict):
        raise ValidationError("采集任务配置无效，请重新建立 Instagram 任务")
    platform = (settings or {}).get("platform", "instagram")
    if platform != "instagram":
        raise ValidationError("此版本仅支持 Instagram 采集")
    return "instagram"


def stored_task_settings(raw: str) -> dict:
    """Never interpret malformed/non-object stored JSON as legacy IG defaults."""
    try:
        settings = json.loads(raw)
    except (TypeError, ValueError):
        raise ValidationError("保存的任务配置无效，已停止执行") from None
    if not isinstance(settings, dict):
        raise ValidationError("保存的任务配置无效，已停止执行")
    collection_platform(settings)
    return settings
