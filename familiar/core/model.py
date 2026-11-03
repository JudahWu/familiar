from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from familiar.env_utils import get_env, require_env

DEEPSEEK_V4_MAX_TOKENS = 25600
INTENT_MODEL_MAX_TOKENS = 200


def main_model_name() -> str:
    """当前主模型名。`get_env` 每次都会重读 `.env`，所以改配置不用改代码。"""

    return get_env("MAIN_MODEL", "deepseek-flash")


def knowledge_model_name() -> str:
    """知识库生成答案用的模型。

    `KNOWLEDGE_MODEL` 留空就跟随 `MAIN_MODEL`——这是默认行为，用户不用配第二遍。
    单独设置的价值在于：知识库可以换成更便宜或者能看图的模型，而不影响编码 Agent。
    """

    return get_env("KNOWLEDGE_MODEL", "").strip() or main_model_name()


def knowledge_judge_model_name() -> str:
    """知识库自评用的评委模型。

    刻意独立：评委和被评的模型是同一个，容易自说自话（自己写的答案自己给高分）。
    默认仍跟随主模型，但留了独立配置的口子。
    """

    return get_env("KNOWLEDGE_JUDGE_MODEL", "").strip() or main_model_name()


def _connection_for(model_name: str) -> dict[str, Any]:
    """按模型名挑连接配置。

    项目里能用的模型分属两家：DeepSeek（deepseek-*）和阿里 MaaS（qwen*）。
    两家的 key 和 base_url 完全不同，所以这里按名字前缀区分。

    这是个约定而不是配置项——因为模型名本身已经能表达供应商，再加一个
    `*_PROVIDER` 变量只会让两个字段有机会写歪（名字写 deepseek、provider 写
    alibaba，报错信息会非常难懂）。如果哪天要接第三家，再改成显式配置。
    """

    lowered = model_name.lower()
    if lowered.startswith("qwen"):
        return {
            "api_key": require_env("ALIBABA_API_KEY"),
            "base_url": require_env("ALIBABA_BASE_URL"),
            # qwen 走 OpenAI 兼容端点，不认 DeepSeek 的 thinking 参数
            "extra_body": {},
        }
    return {
        "api_key": require_env("DEEPSEEK_API_KEY"),
        "base_url": require_env("DEEPSEEK_BASE_URL"),
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def make_chat_model(
    model_name: str,
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: int = 120,
) -> BaseChatModel:
    """按模型名建一个非流式对话模型。

    知识库问答和自评都不需要流式：答案要先整段拿到才能算引用序号和自评分，
    边出边评没有意义。所以这里固定 `streaming=False`。
    """

    return init_chat_model(
        model=model_name,
        model_provider="openai",
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        streaming=False,
        **_connection_for(model_name),
    )


def make_main_model() -> BaseChatModel:
    """创建编码智能体使用的 DeepSeek 模型。

    目前只保留一个主模型，默认对齐 `deepseek-v4-pro`。
    这里不引入多模型 profile、fallback、路由器等生产级能力，
    保持“模型配置”和“Agent 编排”之间的简单关系。

    `thinking: disabled` 与 DeepSeek 的调用方式保持一致，
    避免模型输出额外思考内容影响工具调用和最终回复。

    这里使用 LangChain 1.x 推荐的 `init_chat_model` 统一初始化模型。
    DeepSeek 官方 API 兼容 OpenAI Chat Completions 协议，所以 provider 使用
    `openai`，并通过 `base_url` 指向 DeepSeek 服务地址。这样既保留当前
    `ChatOpenAI` 的稳定行为，又便于后续把模型供应商抽象成配置。
    """

    return init_chat_model(
        model=get_env("MAIN_MODEL", "deepseek-flash"),
        model_provider="openai",
        temperature=1.1,
        api_key=require_env("DEEPSEEK_API_KEY"),
        base_url=require_env("DEEPSEEK_BASE_URL"),
        max_tokens=DEEPSEEK_V4_MAX_TOKENS,
        streaming=True,
        extra_body={"thinking": {"type": "disabled"}},
    )


@lru_cache(maxsize=1)
def make_intent_model() -> BaseChatModel:
    """创建用户意图分类专用模型。

    这个模型和主 Agent 模型使用同一套 DeepSeek/OpenAI-compatible 连接配置，
    但参数目标完全不同：

    - 主 Agent 模型负责长上下文推理、工具调用和流式输出，所以 `streaming=True`，
      `max_tokens` 也比较大。
    - 意图分类模型只负责把用户输入归类为固定任务类型，不需要流式输出，也不应该
      产生长文本，因此使用 `streaming=False`、`temperature=0` 和较小的 `max_tokens`。

    单独提供公共函数的好处是：模型初始化逻辑集中在 `agent.core.model`，业务模块
    `task_intent.py` 只关心“如何分类”，不再自己拼装模型连接参数。
    """

    return init_chat_model(
        model=get_env("INTENT_MODEL", get_env("MAIN_MODEL", "deepseek-flash")),
        model_provider="openai",
        temperature=float(get_env("INTENT_MODEL_TEMPERATURE", "0")),
        api_key=require_env("DEEPSEEK_API_KEY"),
        base_url=require_env("DEEPSEEK_BASE_URL"),
        max_tokens=int(get_env("INTENT_MODEL_MAX_TOKENS", str(INTENT_MODEL_MAX_TOKENS))),
        streaming=False,
        extra_body={"thinking": {"type": "disabled"}},
    )
