"""知识库问答的 LangGraph 图。

拓扑**逐条对齐** `Multimodal_RAG/graph/workflow.py:193-214`，节点名也保持一致，
方便和那份代码对照：

    START → process_input
    process_input --only_image--> retriever_node
    process_input --有文本--> first_chatbot
    first_chatbot --tool_call--> search_context
    first_chatbot --无 tool_call--> END
    search_context --空--> retriever_node
    search_context --有内容--> second_chatbot
    second_chatbot → evaluate_node          ← 见下面第 1 条
    retriever_node → third_chatbot
    third_chatbot --纯图--> END
    third_chatbot --非纯图--> evaluate_node
    evaluate_node --score>=0.7--> END
    evaluate_node --score<0.7--> human_approval   ← interrupt_before
    human_approval --approve--> END
    human_approval --rejected--> fourth_chatbot
    fourth_chatbot --tool_call--> web_search_node
    fourth_chatbot --无 tool_call--> END
    web_search_node → fourth_chatbot

和原项目刻意不同的三处，都是"照搬会坏"的地方：

1. **`second_chatbot → evaluate_node` 这条边接回来了。** 原代码在 workflow.py:204
   注释掉了它，导致历史命中时答案直接结束，不过质检、不写记忆、拿不到引用。
   那是个 bug 不是设计。
2. **`first_chatbot` / `fourth_chatbot` 不靠模型"自愿"调工具。** 原文靠提示词要求
   模型必须调（workflow.py:76-85），但 `tools_condition` 在模型不调时直接走 END，
   等于模型可以绕开知识库自己答。这里改成让节点显式产出 tool_call：拓扑一点没变
   （条件边还在，`tools_condition` 还是那个判断），但不会出现"模型没听话所以跳过
   检索"这种随机结果。
3. **评分不再只有 ResponseRelevancy。** 见 `metrics.py` 的说明。

记忆：`search_context` 查的是**历史问答**（跨会话语义召回，`chat.recall_turns`），
`retriever_node` 查的是**知识库段落**（`service.search`）。这两个别搞混，它们是
两条独立的路，对应两个不同的数据集合。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from familiar.core.model import knowledge_model_name, make_chat_model
from familiar.env_utils import get_env
from familiar.knowledge import chat, metrics, service
from familiar.tools.web_search import web_search

logger = logging.getLogger("familiar.knowledge.graph")

# 历史检索为空时用的固定文案。路由函数按这个字符串判断走哪条路，
# 所以它只能出现在这里，不能在别处手写。
NO_HISTORY = "没有找到相关的历史上下文信息。"

# 送进生成提示词的命中段落上限。和检索的 top_k 分开：检索多取几条用来算
# support，真正喂给模型的可以少一点。
_DEFAULT_TOP_K = 5


class QAState(TypedDict, total=False):
    """图的状态。

    分四组：输入、检索、评分、产出。`messages` 用 `add_messages` 累加，
    其余字段都是覆盖写——图里没有需要累加的中间结果。
    """

    messages: Annotated[list[Any], add_messages]

    # 输入
    kb_id: str
    owner: str
    session_id: str
    question: str
    image_b64: str
    input_type: str  # has_text | only_image

    # 检索
    history_hits: list[dict[str, Any]]  # 历史问答（来自记忆索引）
    hits: list[dict[str, Any]]  # 知识库段落（带引用信息）

    # 评分
    score: float
    coverage: str
    relevancy: float | None
    support: float | None
    reason: str
    search_query: str

    # 多轮追问的上下文。focus 是改写器从上一轮回答里摘出的"被指代的那几段原文"
    # （可能是多段，用户说"第二点和第三点对比"时就是两段）；prev_answer 是兜底
    # 用的完整上一轮回答（见 first_chatbot 的说明）
    focus: list[str]
    prev_answer: str
    prev_question: str

    # 人工
    human_answer: str  # approve | rejected

    # 产出
    answer: str
    web_used: bool
    web_error: str


_SYSTEM_PROMPT = """你是知识库问答助手。你能使用的材料只有下面给出的两类，不许用它们之外的任何知识。

材料有两类，可能只给其中一类：
- 「库内资料」：从用户上传的文档里检索到的原文，每条带编号 [1][2]。
- 「联网结果」：公开网络内容，没有编号。用户拒绝了上一版答案、系统改用联网重答时才会出现。

规则：
1. 先用一两句话直接回答，再补充必要的细节。不要复述问题，不要写"根据资料"这种开场白。
2. 每个来自库内资料的结论后面标出处编号，格式是 [1]、[2]，编号必须来自库内资料里已有的编号。来自联网结果的内容不要标编号，可以在句末注明（据网络资料）。
3. **优先用库内资料。库内资料不足时，只要下面给了联网结果，就用联网结果把答案补完整。** 不要因为库内没有就直接拒绝回答。
4. 两类材料都不足以回答时，才说没有找到，并说明还缺什么。不要编。
5. 带「图」标记的库内资料是从插图里识别出来的描述，不是原书文字，可能不精确。引用它时如果描述里出现"推测""可能"这类词，你要保留这种不确定的语气，不要改写成确定的事实。
6. 如果上面给了「上一轮回答」或「被指代的部分」，用户这次的"展开""继续""第三点""举个例子"指的是那段内容：以它为准来展开，可以补充库内资料，但不要整段复述已经说过的内容。如果给的是多段，按要求逐一展开或逐项对比，不要只答第一段。
7. 回答用中文。不要用 Markdown 标题，可以用短列表。"""

_HISTORY_ANSWER_PROMPT = """你要根据「过去的问答记录」回答用户这次的问题。

规则：
1. 先判断这些历史回答是否真的覆盖了用户这次问的内容。只是话题相近、实际答的不是
   同一件事时，明确说"之前的记录里没有直接相关的答案"，不要强行套用。
2. 用得上就直接复用其中的结论，并在句末注明（据历史问答），不要编造新的出处编号。
3. 历史回答可能已经过时或不准确，涉及具体数据时说清这是过去某次回答里的内容。
4. 回答用中文，不要用 Markdown 标题。"""

_REWRITE_PROMPT = """把用户最新的问题改写成一句独立、可检索的问题，并摘出它指代的那段原文。

规则：
1. 用下面的「上一轮回答」和更早的对话补全"它、那个、上面说的、这个方法"这类指代。
2. 保留专有名词、版本号、代码标识，不要换说法。
3. 不要回答问题，不要解释理由，不要加前缀。
4. 原问题本身已经独立完整时，query 原样返回，focus 留空字符串。
5. **如果用户在指代上一轮回答里的某段内容**（"第三点""上面那个方案""第二点和第三点
   对比一下""刚才那个结论"），把被指代的那几段**原文**按顺序放进 focus 数组：
   - 照抄原文，不要改写、不要概括、不要加自己的话
   - 最多 3 段，每段不超过 300 字；用户只点名一段就只放一段
   - 摘的是"用户要接着说的那几段"，不是整份回答；整份回答已经在提示词里了
   - 找不到明确指代时 focus 必须是空数组 []
6. 只输出 JSON：{"query": "改写后的问题", "focus": ["被指代的原文段1", "..."]}"""

_PREV_ANSWER_MAX = 6000
_FOCUS_MAX = 300
_FOCUS_ITEMS = 3


# ---------------------------------------------------------------- 节点


def process_input(state: QAState) -> dict[str, Any]:
    """解析输入类型。纯图查询走稠密检索，没有文字可改写也没有文字可评估。"""

    question = (state.get("question") or "").strip()
    image = (state.get("image_b64") or "").strip()
    return {
        "input_type": "has_text" if question else "only_image" if image else "has_text",
        "question": question,
        "search_query": question,
    }


def first_chatbot(state: QAState) -> dict[str, Any]:
    """改写问题、摘出"被指代的那段"，并产出要调 `search_context` 的 tool_call。

    原项目这一步是把工具绑给模型、让模型自己决定调不调。这里保留"改写"这个
    实际有价值的部分，但 tool_call 由节点显式产出——理由见模块开头第 2 条。

    **为什么最后一条回答不截断。** 原来是每条历史都截 200 字，结果"你说第三点
    展开讲讲"这类追问必挂：一条有五点的答案远超 200 字，被截掉的恰好是列表项，
    改写器根本不知道第三点是什么，只能输出一句"展开讲讲第三点"去检索，必然搜空。

    现在的做法是让改写器看到完整的上一轮回答，并顺手把被指代的那段原文摘进
    `focus`。生成阶段只用 `focus`（200~300 token），不搬整份答案——塞全文既涨
    token，也容易被模型忽略（长 prompt 中段注意力差）。

    上一轮回答有 4096 token 的生成上限（`model.py:71`），所以整份传进来也不会
    顶到窗口；`_PREV_ANSWER_MAX` 只是防以后有人把上限调大的保险丝。
    """

    question = (state.get("question") or "").strip()
    query = question
    focus: list[str] = []
    prev_answer = ""
    prev_question = ""

    session_id = state.get("session_id") or ""
    history = chat.recent_turns(session_id, limit=chat.HISTORY_TURNS) if session_id else []
    if history:
        # 最后一条可能是助手轮，也可能是用户轮（上一轮刚问还没答完）。取最后一条
        # 助手轮，连同它前面那条用户轮，作为"上一轮问答"。
        last_index = max(
            (index for index, turn in enumerate(history) if turn.role == "assistant"),
            default=-1,
        )
        if last_index >= 0:
            prev_answer = history[last_index].content.strip()
            if last_index > 0 and history[last_index - 1].role == "user":
                prev_question = history[last_index - 1].content.strip()

    try:
        if history:
            lines = []
            for index, turn in enumerate(history):
                who = "用户" if turn.role == "user" else "助手"
                if index == last_index:
                    # 不截断：指代词的先行词几乎总在这条里，截断正好砍掉结构
                    lines.append(f"{who}（上一轮回答，完整）：{turn.content.strip()}")
                else:
                    lines.append(f"{who}：{turn.content[:200]}")
            lines.append(f"用户最新问题：{question}")

            raw = make_chat_model(
                knowledge_model_name(), temperature=0.0, max_tokens=600
            ).invoke(
                [
                    {"role": "system", "content": _REWRITE_PROMPT},
                    {"role": "user", "content": "\n\n".join(lines)},
                ]
            ).content
            data = _json_obj(str(raw))
            rewritten = str(data.get("query") or "").strip()
            # 改写是"让检索更准"，写坏了就用原句，别阻断问答
            if rewritten and len(rewritten) <= 200:
                query = rewritten
            focus = _focus_list(data.get("focus"))
    except Exception:  # noqa: BLE001
        logger.warning("问题改写失败，用原句检索", exc_info=True)

    logger.info(
        "历史检索用句：%r → %r（focus %d 段）", question[:40], query[:60], len(focus)
    )
    return {
        "focus": focus,
        "prev_answer": prev_answer[: _PREV_ANSWER_MAX],
        "prev_question": prev_question[:200],
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_context",
                        "args": {"query": query},
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                    }
                ],
            )
        ],
        "search_query": query,
    }


def search_context(state: QAState) -> dict[str, Any]:
    """检索**历史问答**（不是知识库），把结果塞进一条 ToolMessage。

    召回按 owner 过滤（跨会话但同一个人），不按 kb 过滤——跨库回忆正是它的用途。
    召回失败的三种情况（未配 embedding、Milvus 挂了、PG 里取不到）都退化成
    这条固定文案，让路由把流程交给知识库。
    """

    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    tool_call_id = ""
    query = state.get("search_query") or state.get("question") or ""
    if last is not None and getattr(last, "tool_calls", None):
        call = last.tool_calls[0]
        tool_call_id = str(call.get("id") or "")
        query = str((call.get("args") or {}).get("query") or query)

    turns = []
    try:
        turns = chat.recall_turns(query, owner=state.get("owner") or "", top_k=4)
    except Exception:  # noqa: BLE001 - 回忆是加分项，不能成为问答的必经路径
        logger.warning("历史回忆失败，按无历史处理", exc_info=True)

    # 相关性闸门。向量召回是"意思接近"，不是"答得上"——实测问 Python GIL 也能
    # 召回一堆 Flink 的问答。不过这一关就把流程交给知识库，否则知识库永远查不到。
    # RAG 项目靠 `evaluate_context` 做同一件事（tools.py:70-73）。
    if turns:
        coverage = metrics.context_coverage(
            state.get("question") or query, [turn.content for turn in turns]
        )
        if coverage == "none":
            logger.info("历史召回 %d 条但都不对题，转知识库", len(turns))
            turns = []

    content = _format_history_hits(turns) if turns else NO_HISTORY
    return {
        "messages": [ToolMessage(content=content, name="search_context", tool_call_id=tool_call_id)],
        "history_hits": [{"content": turn.content, "created_at": turn.created_at} for turn in turns],
    }


def second_chatbot(state: QAState) -> dict[str, Any]:
    """历史命中时直接基于历史回答。"""

    response = make_chat_model(knowledge_model_name()).invoke(
        [
            SystemMessage(content=_HISTORY_ANSWER_PROMPT),
            *state.get("messages", []),
            HumanMessage(content=state.get("question") or "（用户只上传了图片，没有文字）"),
        ]
    )
    return {"messages": [response], "answer": _text(response.content)}


def retriever_node(state: QAState) -> dict[str, Any]:
    """检索**知识库段落**。纯图走图搜图（只走稠密），文本走混合检索。"""

    kb_id = state.get("kb_id") or ""
    if not kb_id:
        return {"hits": []}

    if state.get("input_type") == "only_image":
        import base64

        try:
            image_bytes = base64.b64decode(_strip_data_url(state.get("image_b64") or ""))
        except Exception:  # noqa: BLE001
            logger.warning("查询图片解码失败", exc_info=True)
            return {"hits": []}
        hits = service.search(kb_id, "", top_k=_top_k(), image_bytes=image_bytes)
    else:
        hits = service.search(
            kb_id, state.get("search_query") or state.get("question") or "", top_k=_top_k()
        )

    logger.info("知识库命中 %d 条", len(hits))
    return {"hits": hits}


def third_chatbot(state: QAState) -> dict[str, Any]:
    """基于知识库命中生成答案。"""

    hits = state.get("hits") or []
    context = _format_context(hits)
    question = state.get("question") or ""

    sections: list[str] = []
    # 多轮追问的上下文。优先用改写器摘出的那段（短、准），没有就退回完整上一轮
    # 回答。两者都缺就不加这一段——首问不该背这个成本。
    focus = _focus_list(state.get("focus"))
    prev_answer = (state.get("prev_answer") or "").strip()
    if len(focus) == 1:
        sections.append(
            "上一轮回答里被指代的部分（用户这次就是在说这一段，请以它为准展开）：\n"
            f"{focus[0]}"
        )
    elif focus:
        blocks = "\n\n".join(f"【第 {index} 段】\n{text}" for index, text in enumerate(focus, start=1))
        sections.append(
            "上一轮回答里被指代的部分（用户点名了这 "
            f"{len(focus)} 段，请对照它们回答，该对比就逐项对比）：\n{blocks}"
        )
    elif prev_answer:
        prev_question = (state.get("prev_question") or "").strip()
        head = f"问：{prev_question}\n" if prev_question else ""
        sections.append(
            "上一轮问答（用户可能在引用它，只用来理解指代，不要复述里面的内容）：\n"
            f"{head}答：{prev_answer}"
        )

    sections.append(f"库内资料：\n{context}" if context else "库内资料：（没有检到相关内容）")
    if state.get("input_type") == "only_image":
        sections.append(
            "用户上传了一张图片作为查询。你看不到这张图，上面是与它最相似的库内资料"
            "（含插图描述）。请根据这些资料推断用户可能在问什么，并给出对应内容。"
            "措辞上用「与这张图最相似的库内资料显示……」或直接陈述内容，"
            "不要说「图中」「这张图里」——你没看过它。"
        )
    sections.append(f"问题：{question or '（用户只上传了图片，没有文字）'}")

    response = make_chat_model(knowledge_model_name()).invoke(
        [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content="\n\n".join(sections)),
        ]
    )
    return {"messages": [response], "answer": _text(response.content)}


def evaluate_node(state: QAState, config: RunnableConfig) -> dict[str, Any]:
    """三个指标合成一个判定，见 `metrics.py`。

    材料要把**历史回答**也算进去：走 `second_chatbot` 那条路时知识库根本没查，
    `hits` 是空的，只拿它当材料会判成"库里没料"——可那段历史回答本身就有依据。

    `config` 里的 callbacks 要显式往下传：这一段的评委调用原本一条都进不了
    这条 trace（实测整条记录停在上一个节点），显式传之后每次调用都有 span。
    """

    contexts = [str(hit.get("text") or "") for hit in (state.get("hits") or [])]
    contexts += [str(item.get("content") or "") for item in (state.get("history_hits") or [])]

    # 评分用**改写后的独立问题**，不是用户原话。"第二点展开讲讲"这种句子跟材料
    # 毫无词面交集，拿它去问"材料能不能回答这个问题"，评委只会答"不能"——
    # 实测整体判成 coverage=none，于是一个好答案被标成"资料不足"。
    # 首问两者相同，多轮追问才会有差别。
    question = (state.get("search_query") or "").strip() or (state.get("question") or "")

    verdict = metrics.evaluate(
        question,
        state.get("answer") or "",
        contexts,
        callbacks=(config or {}).get("callbacks"),
    )
    logger.info(
        "评分完成：主分=%.2f 覆盖=%s",
        verdict.score,
        verdict.coverage,
    )
    logger.info(
        "评分：主分=%.2f 覆盖=%s 切题=%s 材料=%s",
        verdict.score,
        verdict.coverage,
        verdict.relevancy,
        verdict.support,
    )
    return {
        "score": verdict.score,
        "coverage": verdict.coverage,
        "relevancy": verdict.relevancy,
        "support": verdict.support,
        "reason": verdict.reason,
    }


def human_approval(state: QAState) -> dict[str, Any]:
    """人工审批节点。

    真正的机制是 `interrupt_before`：图停在**进入这个节点之前**，等外部把
    `human_answer` 写进状态再继续。所以这个函数体本身不做事，只留一行日志，
    方便在运行记录里确认"确实是走到这儿才停的"。
    """

    logger.info("人工审批：human_answer=%s", state.get("human_answer") or "(空)")
    return {}


def fourth_chatbot(state: QAState) -> dict[str, Any]:
    """用户拒绝了低分答案，改走联网搜索。"""

    question = state.get("question") or ""
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "web_search",
                        "args": {"query": state.get("search_query") or question},
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                    }
                ],
            )
        ]
    }


def fourth_answer(state: QAState) -> dict[str, Any]:
    """拿到联网结果之后再生成一版答案。

    原项目把"发起搜索"和"基于结果作答"放在同一个节点里（靠 tools_condition
    循环回自己），这里拆成两个节点：`fourth_chatbot` 只负责发起搜索，
    这个节点只负责在拿到 ToolMessage 之后作答。条件边回指 `fourth_chatbot`
    的拓扑没有变，只是多了一层"已经有搜索结果就不再搜"的判断。
    """

    question = state.get("question") or ""
    # 取最后一条 web_search 的结果
    web_text = ""
    for message in reversed(state.get("messages") or []):
        if isinstance(message, ToolMessage) and message.name == "web_search":
            web_text = _text(message.content)
            break
    if not web_text or web_text.startswith("没有搜索到"):
        return {
            "answer": "",
            "web_error": "联网搜索没有返回结果",
            "web_used": False,
        }

    sections = [
        f"库内资料：\n{_format_context(state.get('hits') or [])}"
        if state.get("hits")
        else "库内资料：（没有检到相关内容）",
        f"联网结果（没有编号，来自这里的内容不要标 [n]）：\n{web_text}",
        f"问题：{question}",
        "用户认为上一版答案不够可靠，请结合联网结果重新回答，不要重复上一版的措辞。",
    ]
    response = make_chat_model(knowledge_model_name()).invoke(
        [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content="\n\n".join(sections)),
        ]
    )
    return {"messages": [response], "answer": _text(response.content), "web_used": True, "web_error": ""}


# ---------------------------------------------------------------- 路由


def route_only_image(state: QAState) -> str:
    """纯图不走历史检索——没有文字可改写，也没有历史可比。"""

    return "retriever_node" if state.get("input_type") == "only_image" else "first_chatbot"


def route_llm_or_retriever(state: QAState) -> str:
    """历史有内容就走历史，空了才去查知识库。判断依据是那条 ToolMessage。"""

    messages = state.get("messages") or []
    last = messages[-1] if messages else None
    content = _text(getattr(last, "content", "")) if last is not None else ""
    if not content or content.strip() == NO_HISTORY:
        return "retriever_node"
    return "second_chatbot"


def route_evaluate_node(state: QAState) -> str:
    """纯图查询跳过评估：答案描述的是"哪段资料和图相似"，不是对图片的转述，
    拿它跟问题算忠实度没有意义。"""

    return END if state.get("input_type") == "only_image" else "evaluate_node"


def route_human_node(state: QAState) -> str:
    """两个条件任一成立就交给用户裁决。

    1. 主分低于门槛：有料但答得可疑。
    2. **材料根本没覆盖问题**：这时候主分往往很高（模型很诚实地说了"没有找到"），
       只看分数会直接结束，用户拿不到"换联网"的入口。原项目只看分数
       （all_router.py:44），所以"库里没料"这种最常见的情况反而不触发人工。
       这是我们认为必须补的一处，`coverage` 这个维度不落到路由上就没有意义。
    """

    if (state.get("coverage") or "") == "none":
        return "human_approval"
    return END if float(state.get("score") or 0.0) >= threshold() else "human_approval"


def route_human_approval_node(state: QAState) -> str:
    """approve 收尾，其余一律走联网重答。

    默认值是 `rejected`（和原项目 my_state.py:21 一致），所以恢复时没写
    `human_answer` 就一定会去联网——前端必须让用户显式选择，不能"点继续就算通过"。
    """

    return END if (state.get("human_answer") or "") == "approve" else "fourth_chatbot"


def route_after_web_search(state: QAState) -> str:
    """搜到了就作答，没搜到就结束（避免在原地循环）。"""

    for message in reversed(state.get("messages") or []):
        if isinstance(message, ToolMessage) and message.name == "web_search":
            content = _text(message.content)
            return "fourth_answer" if content and not content.startswith("没有搜索到") else END
    return END


# ---------------------------------------------------------------- 组装


def _build():
    builder = StateGraph(QAState)

    builder.add_node("process_input", process_input)
    builder.add_node("first_chatbot", first_chatbot)
    builder.add_node("search_context", search_context)
    builder.add_node("second_chatbot", second_chatbot)
    builder.add_node("retriever_node", retriever_node)
    builder.add_node("third_chatbot", third_chatbot)
    builder.add_node("evaluate_node", evaluate_node)
    builder.add_node("human_approval", human_approval)
    builder.add_node("fourth_chatbot", fourth_chatbot)
    builder.add_node("web_search_node", ToolNode([web_search]))
    builder.add_node("fourth_answer", fourth_answer)

    builder.add_edge(START, "process_input")
    builder.add_conditional_edges(
        "process_input",
        route_only_image,
        {"retriever_node": "retriever_node", "first_chatbot": "first_chatbot"},
    )
    builder.add_conditional_edges(
        "first_chatbot", tools_condition, {"tools": "search_context", END: END}
    )
    builder.add_conditional_edges(
        "search_context",
        route_llm_or_retriever,
        {"retriever_node": "retriever_node", "second_chatbot": "second_chatbot"},
    )
    builder.add_edge("retriever_node", "third_chatbot")

    # 原项目这条边被注释掉了（workflow.py:204），见模块开头第 1 条
    builder.add_edge("second_chatbot", "evaluate_node")

    builder.add_conditional_edges(
        "third_chatbot", route_evaluate_node, {"evaluate_node": "evaluate_node", END: END}
    )
    builder.add_conditional_edges(
        "evaluate_node", route_human_node, {"human_approval": "human_approval", END: END}
    )
    builder.add_conditional_edges(
        "human_approval",
        route_human_approval_node,
        {"fourth_chatbot": "fourth_chatbot", END: END},
    )
    builder.add_conditional_edges(
        "fourth_chatbot", tools_condition, {"tools": "web_search_node", END: END}
    )
    builder.add_conditional_edges(
        "web_search_node",
        route_after_web_search,
        {"fourth_answer": "fourth_answer", END: END},
    )

    return builder.compile(
        checkpointer=_checkpointer(),
        interrupt_before=["human_approval"],
    )


_graph: Any = None


def get_graph() -> Any:
    """图是进程级单例：编译一次就够，checkpointer 本身也是长连的。"""

    global _graph
    if _graph is None:
        _graph = _build()
        logger.info("知识库问答图已编译")
    return _graph


def _checkpointer() -> Any:
    """复用主 Agent 的 checkpointer。

    延迟导入有两个原因：`app.core.graph` 会牵出整个 agent 运行时；而知识库接口
    在没配模型的环境里也该能导入（只是问答跑不起来）。
    """

    try:
        from familiar.core.graph import get_checkpointer as _shared

        return _shared()
    except Exception:  # noqa: BLE001 - 退回独立的一份，别让问答跟着 agent 起不来
        logger.warning("复用主 checkpointer 失败，知识库单独建一份", exc_info=True)
        from familiar.core.persistence import make_checkpointer
        from familiar.core.settings import CHECKPOINT_DB_PATH

        return make_checkpointer(CHECKPOINT_DB_PATH)


# ---------------------------------------------------------------- 工具


def threshold() -> float:
    try:
        return min(1.0, max(0.0, float(get_env("KNOWLEDGE_SCORE_THRESHOLD", "0.7"))))
    except ValueError:
        return 0.7


def _top_k() -> int:
    try:
        return max(1, int(get_env("KNOWLEDGE_TOP_K", str(_DEFAULT_TOP_K))))
    except ValueError:
        return _DEFAULT_TOP_K


def _text(content: Any) -> str:
    """把模型返回的 content 拍平成字符串（兼容多段 content block）。"""

    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content
        ).strip()
    return str(content or "").strip()


def _focus_list(value: Any) -> list[str]:
    """把 focus 规范成字符串列表。

    两种输入都要收：提示词现在要数组，但旧 checkpoint 里存的是字符串
    （改这一版之前跑的会话），而模型偶尔也会退回给字符串。收不合规的值不如
    统一收下来再截断。

    每段 300 字、最多 3 段：用户点名的段数实际很少超过三段，多了只会把生成
    提示词撑长，反而稀释库内资料的权重。
    """

    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return []
    cleaned = [item.strip()[: _FOCUS_MAX] for item in items if item and str(item).strip()]
    return cleaned[: _FOCUS_ITEMS]


def _json_obj(text: str) -> dict[str, Any]:
    """从模型输出里抠一个 JSON 对象。容忍代码块和前后废话。"""

    body = (text or "").strip()
    if body.startswith("```"):
        body = body.strip("`").split("\n", 1)[-1].strip()
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = body.find("{"), body.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(body[start : end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def _strip_data_url(value: str) -> str:
    return value.split(",", 1)[1] if value.startswith("data:") and "," in value else value


def _format_context(hits: list[dict[str, Any]]) -> str:
    """拼成带编号的资料块。编号就是答案里 [n] 的依据，也是前端引用的下标。"""

    blocks: list[str] = []
    for index, hit in enumerate(hits, start=1):
        source = hit.get("title") or hit.get("doc_title") or "未命名文档"
        page = hit.get("page") or 0
        kind = "图" if hit.get("category") == "image" else "文字"
        blocks.append(f"[{index}]《{source}》第 {page} 页（{kind}）\n{str(hit.get('text') or '').strip()}")
    return "\n\n".join(blocks)


def _format_history_hits(turns: list[chat.Turn]) -> str:
    blocks = []
    for index, turn in enumerate(turns, start=1):
        when = turn.created_at[:10] if turn.created_at else ""
        blocks.append(f"[历史 {index}] {when}\n{turn.content.strip()}")
    return "\n\n".join(blocks)


__all__ = ["QAState", "get_graph", "threshold", "NO_HISTORY"]
