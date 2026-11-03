"""DeepSeek 网页搜索（`POST /v1/tools/search`）。

用它替掉智谱搜索，理由很实际：项目本来就要 `DEEPSEEK_API_KEY`，同一把钥匙顺手就能
搜网页；而智谱是另一套 key、另一个 SDK，多一个依赖就多一处会失效的地方。

接口要点（都是实测/文档确认的）：
- 与对话模型共用同一套 Bearer 鉴权和 `DEEPSEEK_BASE_URL`；
- **`need_content` 必须显式传 true**，否则 `content` 返回空串，只剩标题和 URL，等于没用；
- 计费按"成功请求次数"，无结果或失败不计费，所以 count 开大点不心疼；
- 408 / 429 / 5xx 值得重试，401（key 无效）和 402（余额不足）重试没意义。

这里只做搜索，不做网页抓取。项目已有 `fetch_url` 负责读单个页面，两件事分开，
调用方想抓原文时用那个。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from familiar.env_utils import get_env, require_env

logger = logging.getLogger("familiar.run.deepseek_search")

DEFAULT_BASE_URL = "https://api.deepseek.com"
SEARCH_PATH = "/v1/tools/search"

_TIMEOUT = 30.0
_MAX_ATTEMPTS = 3
# 单条正文上限。一次搜索返回 5 条整页正文轻松上万字，塞进知识库问答的提示词里
# 会把库内资料挤没，所以按条截断；要全文请用 fetch_url。
_BODY_LIMIT = 1200
# 拼给模型的总长度上限，比单条上限更重要——5 条各 1200 字也有 6000 字了。
_TOTAL_LIMIT = 4000

# 值得重试的状态码：限流/超时/服务端抖动。401、402 重试多少次都一样。
_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})

_HINTS = {
    400: "query 或 count 超出允许范围（query 1~128 字符，count 1~50）",
    401: "DEEPSEEK_API_KEY 缺失或无效",
    402: "DeepSeek 账号余额不足",
    429: "触发 QPS 限流，稍后重试",
}


class DeepSeekSearchError(RuntimeError):
    """搜索失败。message 是给用户看的，不要塞堆栈。"""


@dataclass
class SearchHit:
    title: str
    url: str
    body: str
    score: float = 0.0


def _endpoint() -> str:
    """拼出完整 URL。

    `DEEPSEEK_BASE_URL` 有两种常见写法：`https://api.deepseek.com` 和带 `/v1` 的
    OpenAI 风格地址。先去掉 `/v1` 再拼路径，否则会变成 `/v1/v1/tools/search`。
    """

    base = (get_env("DEEPSEEK_BASE_URL", "") or DEFAULT_BASE_URL).strip().rstrip("/")
    base = base.removesuffix("/v1").rstrip("/") or DEFAULT_BASE_URL
    return base + SEARCH_PATH


def _pick_body(item: dict) -> str:
    """正文优先，其次长摘要，最后短摘要。三者都可能为空。"""

    for key in ("content", "summary", "snippet"):
        value = (item.get(key) or "").strip()
        if value:
            return value
    return ""


def search_web(query: str, *, count: int = 5, need_content: bool = True) -> list[SearchHit]:
    """搜索网页。返回按相关度排序的结果；失败抛 `DeepSeekSearchError`。"""

    text = (query or "").strip()
    if not text:
        raise DeepSeekSearchError("搜索词不能为空")
    if len(text) > 128:
        # 接口硬限制 128 字符，超了直接 400。截断比报错好——用户的问题是整句时很常见。
        text = text[:128]

    import httpx

    payload = {"query": text, "count": max(1, min(50, count)), "need_content": need_content}
    headers = {
        "Authorization": f"Bearer {require_env('DEEPSEEK_API_KEY')}",
        "Content-Type": "application/json",
    }

    last_error = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = httpx.post(_endpoint(), json=payload, headers=headers, timeout=_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 网络异常统一转成可读错误
            last_error = f"网络请求失败：{exc}"
            logger.warning("DeepSeek 搜索网络异常（第 %d 次）：%s", attempt, exc)
            if attempt < _MAX_ATTEMPTS:
                continue
            raise DeepSeekSearchError(last_error) from exc

        if response.status_code == 200:
            data = response.json()
            results = data.get("results")
            if not isinstance(results, list):
                raise DeepSeekSearchError("响应格式异常：缺少 results 数组")
            hits = [
                SearchHit(
                    title=(item.get("title") or "").strip(),
                    url=(item.get("url") or "").strip(),
                    body=_pick_body(item),
                    score=float(item.get("rerank_score") or 0.0),
                )
                for item in results
                if isinstance(item, dict)
            ]
            hits.sort(key=lambda hit: hit.score, reverse=True)
            return hits

        hint = _HINTS.get(response.status_code, "")
        last_error = f"HTTP {response.status_code}{'：' + hint if hint else ''}"
        if response.status_code not in _RETRYABLE:
            raise DeepSeekSearchError(last_error)
        logger.warning("DeepSeek 搜索可重试错误（第 %d 次）：%s", attempt, last_error)

    raise DeepSeekSearchError(last_error or "搜索失败")


def format_for_context(hits: list[SearchHit], *, max_chars: int = _TOTAL_LIMIT) -> str:
    """把结果拼成给模型读的段落。

    带上标题和 URL：模型引用时能说清来源，用户也能自己点开核对。正文按条截断，
    整体再截一次——上限是给提示词留预算，不是给接口省流量。
    """

    blocks: list[str] = []
    used = 0
    for hit in hits:
        body = hit.body[:_BODY_LIMIT]
        if len(hit.body) > _BODY_LIMIT:
            body += "……"
        block = f"《{hit.title or hit.url}》{hit.url}\n{body}"
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)
