from __future__ import annotations

import json
import logging
import threading
from typing import Any, Iterator

import redis

from familiar.core.settings import REDIS_URL

logger = logging.getLogger("familiar.run.redis_bus")

# 频道命名：agent:events:<thread_id>。前端/ dashboard 之后可订阅该频道，
# 把 agent 运行事件实时推到页面，等价于一条进程内的消息队列。
EVENT_PREFIX = "agent:events:"

_bus: "redis.Redis | None" = None
_bus_lock = threading.Lock()


def get_redis() -> "redis.Redis":
    """懒加载 Redis 客户端（单例）。

    decode_responses=True 让 publish/subscribe 直接拿到 str 而非 bytes。
    socket_connect_timeout 防止 Redis 不可用时长时间阻塞 agent 线程。
    """

    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=5)
    return _bus


def channel_for(thread_id: str) -> str:
    """返回某个 thread 的事件频道名。"""

    return f"{EVENT_PREFIX}{thread_id}"


def publish_event(thread_id: str, event: dict[str, Any]) -> None:
    """把一条 agent 运行事件发布到 Redis 频道。

    这是事件总线的“生产端”。Redis 不可用时只记日志、绝不抛异常，
    避免事件总线故障中断真正的 Agent 任务（事件本来就是前端展示用的）。
    """

    try:
        get_redis().publish(channel_for(thread_id), json.dumps(event, ensure_ascii=False))
    except Exception:
        logger.exception("发布 agent 事件到 Redis 失败 thread_id=%s", thread_id)


def subscribe_events(thread_id: str) -> "redis.client.PubSub":
    """订阅某个 thread 的事件频道，返回 PubSub 对象。

    调用方用 `for message in pubsub.listen():` 即可逐条消费。
    这是事件总线的“消费端”，供后续 dashboard SSE 桥接使用。
    """

    pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(channel_for(thread_id))
    return pubsub


def iter_events(thread_id: str) -> Iterator[dict[str, Any]]:
    """生成器：逐条产出某 thread 的实时事件（dict）。

    message["data"] 是 JSON 字符串，这里解析后产出。遇到 Redis 断开抛异常，
    由调用方决定重连或结束。
    """

    pubsub = subscribe_events(thread_id)
    try:
        for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                yield json.loads(message["data"])
            except (ValueError, TypeError):
                continue
    finally:
        pubsub.close()
