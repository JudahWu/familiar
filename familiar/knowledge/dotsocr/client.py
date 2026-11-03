"""dots_ocr 的 HTTP 客户端。

服务形态是 vLLM 起的 OpenAI 兼容端点（`/v1/chat/completions`），模型根目录
`/data/dots_deploy/DotsOCR`。所以不需要装 dots_ocr 的 python 包，也不需要 GPU
—— 只要一个地址。

两个实现细节是踩出来的：

1. **必须 `trust_env=False`。** 这台机器上有 `HTTP_PROXY`，httpx 默认会读它，
   访问 127.0.0.1 也会走代理，直接连不上。
2. **文本里要带 `<|img|><|imgpad|><|endofimg|>` 前缀。** 不带的话 vLLM 会在
   图片和指令之间插一个换行，模型输出的 JSON 会多出空行甚至解析失败。
"""

from __future__ import annotations

import base64
import logging
from io import BytesIO

import httpx
from PIL import Image

from familiar.env_utils import get_env
from familiar.knowledge.dotsocr.prompts import dict_promptmode_to_prompt

logger = logging.getLogger("familiar.knowledge.dotsocr")

# 图片和指令之间的分隔标记，见模块说明第 2 条
_IMG_TOKEN = "<|img|><|imgpad|><|endofimg|>"

DEFAULT_PROMPT_MODE = "prompt_layout_all_en"


class DotsOcrError(RuntimeError):
    """解析服务不可用或返回异常。调用方据此把文档标成失败。"""


def base_url() -> str:
    return get_env("DOTS_OCR_BASE_URL", "").strip().rstrip("/")


def model_name() -> str:
    return get_env("DOTS_OCR_MODEL", "model").strip() or "model"


def available() -> bool:
    return bool(base_url())


def _timeout() -> float:
    try:
        return float(get_env("DOTS_OCR_TIMEOUT", "300"))
    except ValueError:
        return 300.0


def _max_tokens() -> int:
    try:
        return int(get_env("DOTS_OCR_MAX_TOKENS", "8192"))
    except ValueError:
        return 8192


def parse_image(
    image: Image.Image,
    *,
    prompt_mode: str = DEFAULT_PROMPT_MODE,
) -> str:
    """把一页图发给模型，返回原始文本（正常情况下是一段布局 JSON）。

    不做任何解析和后处理 —— JSON 清洗、bbox 换算、转 Markdown 都在
    `layout_utils` / `format_transformer` 里，和原项目保持一致的分工。
    """

    if not available():
        raise DotsOcrError("未配置 DOTS_OCR_BASE_URL")

    prompt = dict_promptmode_to_prompt.get(prompt_mode)
    if not prompt:
        raise DotsOcrError(f"不支持的 prompt_mode：{prompt_mode}")

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    payload = {
        "model": model_name(),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": uri}},
                    {"type": "text", "text": f"{_IMG_TOKEN}{prompt}"},
                ],
            }
        ],
        "max_tokens": _max_tokens(),
        "temperature": 0.1,
    }
    try:
        with httpx.Client(timeout=_timeout(), trust_env=False) as client:
            response = client.post(f"{base_url()}/v1/chat/completions", json=payload)
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"])
    except httpx.HTTPStatusError as exc:
        raise DotsOcrError(f"解析服务返回 {exc.response.status_code}：{exc.response.text[:200]}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络/格式错误统一成 DotsOcrError
        raise DotsOcrError(f"解析服务调用失败：{type(exc).__name__}: {exc}") from exc


def health() -> dict[str, object]:
    """探活。给设置页和排查用，不进主链路。"""

    if not available():
        return {"available": False, "reason": "未配置 DOTS_OCR_BASE_URL"}
    try:
        with httpx.Client(timeout=8, trust_env=False) as client:
            response = client.get(f"{base_url()}/v1/models")
        response.raise_for_status()
        models = [item.get("id") for item in response.json().get("data", [])]
        return {"available": True, "models": models, "model": model_name()}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
