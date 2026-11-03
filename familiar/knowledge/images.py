"""从 PDF 里抽插图。

两条路，自动降级：

1. **内嵌位图**（始终可用）：PyMuPDF 的 `page.get_images()` 拿 PDF 里嵌的位图。
   技术书的操作截图基本都是这种。实测 Flink 第二章 18 页抽出 30 张，抽出来的
   确实是书里的插图。

2. **版面裁剪**（配了 OCR_BASE_URL 才走）：让版面识别模型找出图片区域，再用
   PyMuPDF 把该区域渲染成 PNG。这条能拿到**矢量**的流程图、架构图——那些是
   页面上的绘制指令，不是位图，第一条路拿不到。

   没配 OCR 端点、或调用失败，就自动跳过这条，退回第一条。

两条路的结果按内容 md5 去重：同一张图可能既被位图抽到、又被版面裁到。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import pymupdf

from familiar.knowledge import ocr

logger = logging.getLogger("familiar.knowledge.images")

# 小于这个尺寸的多半是图标、装饰线条，不是插图
_MIN_SIDE = 120

# 渲染版面图与裁剪用的 DPI。150 足够看清图里的文字，又不至于太大。
_RENDER_DPI = 150


@dataclass
class ExtractedImage:
    page: int
    index: int
    data: bytes
    ext: str = "png"
    source: str = "embedded"  # embedded | layout


def extract_images(pdf_bytes: bytes, *, use_ocr: bool = True) -> list[ExtractedImage]:
    """抽出一篇 PDF 里的所有插图。

    参数 `use_ocr` 关掉后就只走内嵌位图那条路。
    """

    images: list[ExtractedImage] = []
    seen: set[str] = set()

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        images.extend(_embedded_images(doc, seen))
        if use_ocr and ocr.available():
            images.extend(_layout_images(doc, seen))
        else:
            if use_ocr:
                logger.info("未配置 OCR_BASE_URL，跳过版面裁剪（只抽内嵌位图）")

    for position, image in enumerate(images, start=1):
        image.index = position
    return images


def _embedded_images(doc: pymupdf.Document, seen: set[str]) -> list[ExtractedImage]:
    """PDF 内嵌的位图。"""

    found: list[ExtractedImage] = []
    for page_no, page in enumerate(doc, start=1):
        try:
            xrefs = page.get_images(full=True)
        except Exception:  # noqa: BLE001 - 个别页可能读不出来
            continue
        for xref in xrefs:
            try:
                info = doc.extract_image(xref[0])
            except Exception:  # noqa: BLE001
                continue
            width = int(info.get("width") or 0)
            height = int(info.get("height") or 0)
            if width < _MIN_SIDE or height < _MIN_SIDE:
                continue
            data = info["image"]
            digest = hashlib.md5(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            found.append(
                ExtractedImage(
                    page=page_no,
                    index=0,
                    data=data,
                    ext=info.get("ext") or "png",
                    source="embedded",
                )
            )
    return found


def _layout_images(doc: pymupdf.Document, seen: set[str]) -> list[ExtractedImage]:
    """按版面识别结果裁出矢量插图。

    某一页识别失败不影响其他页——单页降级为"这页没有图"，继续往下走。
    """

    found: list[ExtractedImage] = []
    for page_no, page in enumerate(doc, start=1):
        try:
            pix = page.get_pixmap(dpi=_RENDER_DPI)
            boxes = ocr.find_picture_boxes(pix.tobytes("png"))
        except ocr.OcrError as exc:
            logger.warning("第 %d 页版面识别失败，跳过该页：%s", page_no, exc)
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("第 %d 页渲染失败，跳过该页：%s", page_no, exc)
            continue

        for x1, y1, x2, y2 in boxes:
            try:
                clip = pymupdf.Rect(
                    x1 / 1000 * page.rect.width,
                    y1 / 1000 * page.rect.height,
                    x2 / 1000 * page.rect.width,
                    y2 / 1000 * page.rect.height,
                )
                sub = page.get_pixmap(clip=clip, dpi=_RENDER_DPI)
                data = sub.tobytes("png")
            except Exception:  # noqa: BLE001
                continue
            if sub.width < _MIN_SIDE or sub.height < _MIN_SIDE:
                continue
            digest = hashlib.md5(data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            found.append(
                ExtractedImage(page=page_no, index=0, data=data, ext="png", source="layout")
            )
    return found
