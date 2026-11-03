"""版面识别：找出一页里有哪些图。

只做一件事——把页面渲染图发给一个能看图的 OpenAI 兼容端点，让它返回图片区域
的坐标。**不需要移植 dots_ocr 整个库**：那套 1000 多行代码里我们真正用到的
只有"bbox + category"，其余（版面后处理、markdown 转换、输出清洗）都不需要。

坐标用**归一化值（0-1000）**，不用像素。原因是服务端可能对图片做 resize
（vLLM 的多模态预处理就会），像素坐标回去对不上；归一化坐标与实际尺寸无关，
换算时只要乘回我们自己渲染出来的尺寸即可。

端点没配（OCR_BASE_URL 为空）时，调用方会自动降级为只抽 PDF 内嵌位图。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from familiar.core.settings import OCR_BASE_URL, OCR_MODEL
from familiar.env_utils import get_env

logger = logging.getLogger("familiar.knowledge.ocr")

# 要求模型只找图片/图表区域。给了明确的类别列表，减少误报。
_PROMPT = (
    "Please find all pictures, figures, diagrams, and charts in this page of a technical book.\n"
    "For each one, output its bounding box.\n"
    "Requirements:\n"
    "1. Output a single JSON array, nothing else.\n"
    "2. Each element: {\"bbox\": [x1, y1, x2, y2]}\n"
    "3. Coordinates are normalized to 0-1000 relative to the image width and height "
    "(0 is the left/top edge, 1000 is the right/bottom edge).\n"
    "4. Skip decorative elements, icons, and small logos. Only include real figures, "
    "charts, screenshots, and diagrams.\n"
    "5. If there are none, output an empty array []."
)

# 有些模型会在 JSON 外面裹 markdown 代码块
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class OcrError(RuntimeError):
    """版面识别失败。调用方据此降级。"""


def available() -> bool:
    return bool(OCR_BASE_URL.strip())


def find_picture_boxes(image_bytes: bytes, *, timeout: int = 120) -> list[tuple[float, float, float, float]]:
    """返回图片区域的归一化 bbox 列表。

    失败抛 OcrError。返回空列表表示这一页没有图（这是正常结果，不是错误）。
    """

    if not available():
        raise OcrError("未配置 OCR_BASE_URL，无法做版面识别")

    import base64

    import httpx

    uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": OCR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": uri}},
                ],
            }
        ],
        "max_tokens": 1024,
        "temperature": 0.0,
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{OCR_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {get_env('OCR_API_KEY', '').strip()}"},
                json=payload,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        raise OcrError(f"版面识别调用失败：{type(exc).__name__}: {exc}") from exc

    return _parse_boxes(content)


def _parse_boxes(content: str) -> list[tuple[float, float, float, float]]:
    """从模型输出里把 bbox 列表抠出来。容错处理得比较宽——模型的输出格式
    不太稳定，但只要能拿到四个数就用。"""

    text = (content or "").strip()
    match = _FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()

    boxes: list[tuple[float, float, float, float]] = []
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        # 有些模型会输出 {"boxes": [...]} 或者在 JSON 前后带话
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            logger.warning("版面识别返回的不是合法 JSON：%s", text[:120])
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            logger.warning("版面识别 JSON 解析失败：%s", text[:120])
            return []

    if isinstance(data, dict):
        data = data.get("boxes") or data.get("bboxes") or data.get("items") or []
    if not isinstance(data, list):
        return []

    for item in data:
        box = item.get("bbox") if isinstance(item, dict) else item
        if not isinstance(box, (list, tuple)) or len(box) < 4:
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in box[:4])
        except (TypeError, ValueError):
            continue
        # 归一化坐标应该在 0-1000；超出范围的多半是模型给了像素坐标，
        # 直接丢弃比乱裁一通好。
        if not (0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000):
            logger.debug("忽略异常 bbox：%s", box)
            continue
        boxes.append((x1, y1, x2, y2))
    return boxes
