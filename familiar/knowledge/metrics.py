"""答案质量评分：算法对齐 RAGAS，但不依赖 ragas 包。

为什么不用 ragas：它 0.4.3 要求 `langchain-openai<=1.2`，会把本项目的
langchain-openai 从 1.7.2 拉到 1.1.9、openai 从 3.37 拉到 1.109。这个环境的
Agent 主链路（deepagents、ChatOpenAI、tool calling、structured output）都跑在
新版本上，为了一段评估代码降级整个产品不值得。所以这里按 RAGAS 的算法重写，
提示词和判定口径保持一致，输出结构也一致，将来要换回真包改动很小。

三个指标，各自回答一个独立的问题：

- `faithfulness`（主分，0.7 阈值看它）：答案里的每句话是否能在材料里找到依据。
  算法与 RAGAS `Faithfulness` 相同——先把回答拆成独立陈述，再逐条判断能否由
  材料推出，分数 = 成立条数 / 总条数。
- `relevancy`：答案是否在回答这个问题。算法与 RAGAS `ResponseRelevancy` 相同——
  让模型根据答案反推 N 个可能的问题，与原始问题算 embedding 余弦，取平均。
- `support`：问题与命中段落的向量相似度。**只作诊断，不参与判定。** 实测这个
  数在"同领域的无关问题"上（0.69）比真正相关的问题（0.56）还高，方向是反的，
  拿它当"有没有料"的判据会误判。留着是因为排查时需要它："明明有料为什么判
  none"这类问题，看这个数能快速分清是检索错了还是判断错了。

`coverage` 由模型判断（`context_coverage`）：材料读一遍就知道答不答得上，这个
主观判断恰好可靠，不像忠实度那样需要逐句核对。

主分刻意用 faithfulness 而不是 relevancy：低分要触发人工审批，判断标准应该是
"这段答案有没有依据"，而不是"它像不像在回答问题"。后者防的是跑题，前者防的是
编造，编造比跑题危险得多。
"""

from __future__ import annotations

import json
import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any

from familiar.core.model import knowledge_judge_model_name, make_chat_model

logger = logging.getLogger("familiar.knowledge.metrics")

# RAGAS 的默认 strictness。生成 1 个问题就够——每多一个多一次 embedding，
# 而分数几乎不变（RAG 项目也是 strictness=1，evaluate_self.py:103）。
STRICTNESS = 1

_STATEMENT_PROMPT = """把回答拆成若干条独立的陈述，用于后续的事实核查。

要求：
1. 每条陈述必须能脱离上下文单独读懂：把"它""这个""上述方法"换成具体名称。
2. 一句话里包含多个事实就拆成多条；纯连接词、过渡句不单独成条。
3. 只拆回答里**有实际信息**的句子，客套话、"没有找到"这类声明不算陈述。
4. 保持原意，不要补充回答里没有的信息，不要改写专有名词。
5. 输出 JSON 数组，元素是字符串，不要任何解释。回答里没有可核查的陈述时输出 []。

示例：
回答：「有界流有明确的起止时间，而无界流没有。两者在 Flink 里用不同的 API 处理。」
输出：["有界流有明确的起止时间", "无界流没有明确的起止时间", "有界流和无界流在 Flink 里用不同的 API 处理"]"""

_VERDICT_PROMPT = """判断一条陈述能否从给定材料中推断出来。

判定规则：
- 材料里有明确依据，或能由材料直接推出 → 1
- 材料里没有，或与材料矛盾，或只是模型自己的常识 → 0
- 陈述是对材料的主观评价、推测、比喻 → 0

只输出 JSON：{"verdict": 0 或 1, "reason": "一句话依据"}"""

_QUESTION_PROMPT = """根据一段回答，反推出它可能在回答什么问题。

要求：
1. 问题是这句话**最可能**在回答的那个问题，用中文，一句话。
2. 不要照抄回答里的句子，要写成疑问句。
3. 如果这段回答是在说"没有找到/无法回答/资料不足"，把 noncommittal 标为 1。
4. 只输出 JSON：{"question": "...", "noncommittal": 0}"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _judge_model(
    callbacks: Any,
    *,
    temperature: float = 0.0,
    max_tokens: int = 256,
) -> Any:
    """建评委模型，并把调用方给的 callbacks 显式挂上。

    **为什么不用 contextvar 的隐式传递。** 依赖"节点上下文自动继承"时，实测
    在 `evaluate_node` 里全部丢失：整条 trace 只记到上一个节点，评分阶段一次
    调用都不出现（而这正是最贵的一段，一次问答要打 6~10 次）。显式传 callbacks
    后每个调用都有自己的 span，父子关系也对。
    """

    model = make_chat_model(
        knowledge_judge_model_name(), temperature=temperature, max_tokens=max_tokens
    )
    handlers = getattr(callbacks, "handlers", callbacks) if callbacks else None
    if handlers:
        return model.with_config(callbacks=handlers)
    return model


def _submit(pool: ThreadPoolExecutor, fn: Any, *args: Any) -> Any:
    """把当前上下文复制进工作线程再执行。

    Langfuse / LangChain 的 callback 与 span 上下文存在 contextvars 里，而
    ThreadPoolExecutor 的线程不继承调用方的上下文。不复制的话，评分这几路
    模型调用在 trace 里**一条都看不到**——实测整条问答只记到 third_chatbot，
    evaluate_node 之后全丢，而那恰恰是最贵的部分（faithfulness 还要按 claim
    再扇出一层并发）。

    每个任务单独 `copy_context()`：同一个 Context 不能被多个线程同时进入。
    """

    context = copy_context()
    return pool.submit(context.run, fn, *args)


@dataclass
class Verdict:
    """一次评分的完整结果。"""

    # 主分：忠实度。0 表示答案全是编的，1 表示每句都有依据
    score: float
    # 覆盖度：材料够不够。full / partial / none / unknown（没算出来）
    coverage: str
    # 切题度：答案像不像在回答问题
    relevancy: float | None = None
    # 材料相关度：问题与命中段落的最大相似度
    support: float | None = None
    reason: str = ""
    # 中间结果，排查"为什么给了这个分"时用
    details: dict[str, object] = field(default_factory=dict)


def support_floor() -> float:
    """兼容旧调用点。support 已不参与判定，这个函数只剩文档意义。"""

    from familiar.env_utils import get_env

    try:
        return float(get_env("KNOWLEDGE_SUPPORT_FLOOR", "0.35"))
    except ValueError:
        return 0.35


# ---------------------------------------------------------------- 主入口


_COVERAGE_PROMPT = """判断给定的材料能否回答用户的问题。只回答覆盖程度，不评价答案。

三档：
- full：材料里有直接回答这个问题所需的内容，读完就能答。
- partial：材料只覆盖问题的一部分，或者只是背景相关、缺关键信息。
- none：材料与这个问题无关，或者根本不涉及这个问题。

判定要求：
1. 看的是**材料里有没有答案**，不是材料的主话题是否沾边。同一个技术领域但答不上
   这个问题，就是 none。
2. 材料是历史问答时同理：以前的回答答的不是这个问题，就是 none，哪怕主题相近。
3. 只输出 JSON：{"coverage": "full|partial|none", "reason": "一句话依据"}"""


def context_coverage(question: str, texts: list[str], callbacks: Any = None) -> str:
    """材料对问题的覆盖程度：full / partial / none。

    这是 `support` 想干但干不好的事。实测 DashScope 多模态 embedding 的相似度
    在"同一领域的无关问题"上也能到 0.69，比真正相关的问题（0.56）还高——
    拿它当"有没有料"的判据，方向是反的。所以这一维只能交给模型判断。

    模型判断是主观的，但这个问题主观判断恰好可靠：人读一遍材料就知道答不答得上，
    不像"忠实度"那样需要逐句核对。两个节点都复用它：
    - `search_context`：历史召回结果过不了这一关就当没召回；
    - `evaluate_node`：判定材料够不够，决定要不要提示"资料不足"。
    """

    material = [text for text in (texts or []) if (text or "").strip()]
    if not material:
        return "none"

    joined = "\n\n".join(f"[材料 {index}]\n{text}" for index, text in enumerate(material, start=1))
    try:
        model = _judge_model(callbacks, max_tokens=200)
        raw = model.invoke(
            [
                {"role": "system", "content": _COVERAGE_PROMPT},
                {"role": "user", "content": f"{joined}\n\n[问题]\n{question}"},
            ]
        ).content
        data = _loads(str(raw))
        verdict = str(data.get("coverage") if isinstance(data, dict) else "").strip().lower()
        return verdict if verdict in {"full", "partial", "none"} else "partial"
    except Exception:  # noqa: BLE001 - 判不出来时保守按 partial，不要误报"没料"
        logger.warning("材料覆盖度判断失败，按 partial 处理", exc_info=True)
        return "partial"


def evaluate(
    question: str,
    answer: str,
    contexts: list[str],
    *,
    allow_llm: bool = True,
    callbacks: Any = None,
) -> Verdict:
    """算三个维度，合并成一个判定。

    `allow_llm=False` 时只算 support（纯 embedding），用于不需要质检的场景。
    任何一步失败都不抛错：评不出来就给 `coverage="unknown"`、`score` 退回 0.5，
    由调用方决定是否让用户确认——评分是附加能力，不该阻断答案返回。
    """

    texts = [text for text in (contexts or []) if (text or "").strip()]
    support = _safe_support(question, texts)

    if not allow_llm or not texts:
        # 没材料时忠实度无从谈起，也不能因为"答案诚实"就判它不合格
        return Verdict(
            score=0.5,
            coverage="none" if not texts else "unknown",
            support=support,
            reason="没有材料，未做忠实度评估",
        )

    # 三个维度并发跑。用 _submit 把上下文复制进工作线程，否则评委调用会掉出
    # 整条 trace（contextvars 不自动跨线程）。
    with ThreadPoolExecutor(max_workers=3) as pool:
        faithful_future = _submit(pool, _safe_faithfulness, answer, texts, callbacks)
        relevant_future = _submit(pool, _safe_relevancy, question, answer, callbacks)
        coverage_future = _submit(pool, context_coverage, question, texts, callbacks)
        score, statements = faithful_future.result()
        relevancy = relevant_future.result()
        coverage = coverage_future.result()

    return Verdict(
        score=round(score, 4),
        coverage=coverage,
        relevancy=None if relevancy is None else round(relevancy, 4),
        support=None if support is None else round(support, 4),
        reason=_reason(coverage, score, relevancy),
        details={"statements": statements[:8]},
    )


def _reason(coverage: str, score: float, relevancy: float | None) -> str:
    if coverage == "none":
        return "检索到的材料与问题关系不大，答案缺少依据"
    if score < 0.5:
        return "答案里有相当一部分内容在材料中找不到依据"
    if relevancy is not None and relevancy < 0.5:
        return "答案与问题关联较弱，可能答非所问"
    if coverage == "partial":
        return "材料只覆盖了问题的一部分，答案可能不完整"
    return "答案有据可依"


# ---------------------------------------------------------------- 忠实度


def _safe_faithfulness(
    answer: str, contexts: list[str], callbacks: Any = None
) -> tuple[float, list[str]]:
    try:
        return faithfulness(answer, contexts, callbacks=callbacks)
    except Exception:  # noqa: BLE001
        logger.warning("忠实度评估失败，主分退回 0.5", exc_info=True)
        return 0.5, []


def faithfulness(
    answer: str, contexts: list[str], *, callbacks: Any = None
) -> tuple[float, list[str]]:
    """RAGAS Faithfulness 的实现：拆陈述 → 逐条判定 → 成立比例。

    返回 (分数, 陈述列表)。没有可核查的陈述时返回 (1.0, [])——"我当前的知识库里
    没有找到"这类回答不该被判成不忠实，那不是编造。这个空分母的处理和 RAGAS
    一致（分母扣掉 noncommittal 的部分）。
    """

    statements = _statements(answer, callbacks)
    if not statements:
        return 1.0, []

    material = "\n\n".join(f"[材料 {index}]\n{text}" for index, text in enumerate(contexts, start=1))
    model = _judge_model(callbacks)

    def _judge(statement: str) -> int:
        try:
            raw = model.invoke(
                [
                    {"role": "system", "content": _VERDICT_PROMPT},
                    {"role": "user", "content": f"{material}\n\n[待判定陈述]\n{statement}"},
                ]
            ).content
            data = _loads(str(raw))
            verdict = data.get("verdict")
            # 模型有时给字符串 "1"，有时给布尔
            if isinstance(verdict, str):
                verdict = verdict.strip()
            return 1 if str(verdict).strip() in {"1", "true", "True"} else 0
        except Exception:  # noqa: BLE001 - 单条判不出来就按"无依据"算，宁可严一点
            logger.debug("单条陈述判定失败：%s", statement[:40], exc_info=True)
            return 0

    # 逐条判定并发跑，同样用 _submit 带上上下文。一个答案通常 3~8 条陈述，
    # 串行会多花 2~4 秒。
    with ThreadPoolExecutor(max_workers=min(6, len(statements))) as pool:
        futures = [_submit(pool, _judge, statement) for statement in statements]
        verdicts = [future.result() for future in futures]
    return (sum(verdicts) / len(verdicts) if verdicts else 1.0), statements


def _statements(answer: str, callbacks: Any = None) -> list[str]:
    text = (answer or "").strip()
    if not text:
        return []
    model = _judge_model(callbacks, max_tokens=800)
    raw = model.invoke(
        [
            {"role": "system", "content": _STATEMENT_PROMPT},
            {"role": "user", "content": text},
        ]
    ).content
    data = _loads(str(raw))
    if isinstance(data, dict):
        data = data.get("statements") or data.get("items") or []
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


# ---------------------------------------------------------------- 切题度


def _safe_relevancy(
    question: str, answer: str, callbacks: Any = None
) -> float | None:
    try:
        return response_relevancy(question, answer, callbacks=callbacks)
    except Exception:  # noqa: BLE001
        logger.warning("切题度评估失败，本次不计该维", exc_info=True)
        return None


def response_relevancy(
    question: str,
    answer: str,
    *,
    strictness: int = STRICTNESS,
    callbacks: Any = None,
) -> float:
    """RAGAS ResponseRelevancy 的实现：反推问题 → 与原始问题算余弦 → 取平均。

    **这个指标完全看不到材料**，这正是它的局限。它只回答"这段文字像不像在回答
    这个问题"，所以编造得越流畅分越高。修这个缺陷靠 `support`，不靠调它的权重。
    """

    text = (answer or "").strip()
    if not text:
        return 0.0

    model = _judge_model(callbacks, temperature=0.3)
    generated: list[str] = []
    noncommittal = 0
    for _ in range(max(1, strictness)):
        raw = model.invoke(
            [
                {"role": "system", "content": _QUESTION_PROMPT},
                {"role": "user", "content": text},
            ]
        ).content
        data = _loads(str(raw))
        if not isinstance(data, dict):
            continue
        candidate = str(data.get("question") or "").strip()
        if candidate:
            generated.append(candidate)
        if str(data.get("noncommittal", "0")).strip() in {"1", "true", "True"}:
            noncommittal += 1

    if not generated:
        return 0.0
    # 全部是"没有找到/答不了"这类回避性回答，按 RAGAS 的口径记 0
    if noncommittal >= len(generated):
        return 0.0

    from familiar.knowledge import embeddings

    vectors = embeddings.embed_texts([question] + generated)
    if len(vectors) != len(generated) + 1:
        return 0.0
    base = vectors[0]
    sims = [_cosine(base, vector) for vector in vectors[1:]]
    # 负相似度对这个指标没有意义，压到 0；RAGAS 也做了截断
    return max(0.0, sum(max(0.0, sim) for sim in sims) / len(sims))


# ---------------------------------------------------------------- 材料相关度


def _safe_support(question: str, texts: list[str]) -> float | None:
    try:
        return support(question, texts)
    except Exception:  # noqa: BLE001
        logger.warning("材料相关度计算失败", exc_info=True)
        return None


def support(question: str, texts: list[str]) -> float | None:
    """问题与命中段落的最大相似度。不调 LLM，只打一次 embedding。

    取最大值而不是平均值：一段材料答对了就说明库里有料，其他几段沾边与否
    不影响这个判断。用平均值的话，命中里混进两条无关结果就会把分数拉低，
    反而误判成"没料"。
    """

    if not (question or "").strip() or not texts:
        return None

    from familiar.knowledge import embeddings

    # 提一个问题 + 若干段材料，一次批量调用
    vectors = embeddings.embed_texts([question, *texts])
    if len(vectors) != len(texts) + 1:
        return None
    base = vectors[0]
    return max(_cosine(base, vector) for vector in vectors[1:])


# ---------------------------------------------------------------- 工具


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _loads(text: str) -> object:
    """从模型输出里抠 JSON。模型的输出格式不稳定，这里容忍代码块和前后废话。"""

    body = (text or "").strip()
    match = _FENCE_RE.search(body)
    if match:
        body = match.group(1).strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = body.find(opener), body.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(body[start : end + 1])
            except json.JSONDecodeError:
                continue
    return {}
