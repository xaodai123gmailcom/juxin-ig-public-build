from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import BaseModel, Field


class ReviewDecision(BaseModel):
    review_status: Literal["reviewed", "insufficient_visible_data"]
    confidence: Literal["low", "medium", "high"]
    reason_codes: list[str] = Field(max_length=8)


class OpenAIProfileReviewer:
    """Optional second-pass review for accounts that already passed basic filters.

    The caller supplies only public, currently visible profile fields. Images, cookies,
    hidden metadata and unrelated post history are intentionally excluded.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("IGAC_OPENAI_MODEL", "gpt-5.6")

    def review(self, visible_profile: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("GPT 复核已开启，但尚未配置 OpenAI API Key")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("缺少 openai Python 依赖，无法执行 GPT 复核") from exc

        allowed = {
            key: visible_profile.get(key)
            for key in ("username", "display_name", "bio", "category", "external_links", "recent_visible_captions")
            if visible_profile.get(key) not in (None, "", [], {})
        }
        client = OpenAI(api_key=self.api_key, timeout=20.0, max_retries=0)
        response = client.responses.parse(
            model=self.model,
            store=False,
            input=[
                {
                    "role": "system",
                    "content": (
                        "Review only the supplied public Instagram profile fields. "
                        "Report whether the supplied visible fields contain enough readable public context for an optional manual review. "
                        "Do not classify demographic traits, business status, verified status, location, or any hidden/private attribute. "
                        "Return only short evidence-availability reason codes, not private details."
                    ),
                },
                {"role": "user", "content": json.dumps(allowed, ensure_ascii=False)},
            ],
            text_format=ReviewDecision,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("GPT 复核未返回可解析的结构化结果")
        return parsed.model_dump()
