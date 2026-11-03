import { useEffect } from "react";
import {  useQueryClient } from "@tanstack/react-query";

import { agentsApi } from "./api";
import { agentThreadKeys } from "./queries";
import { dropPendingPrompts } from "./pendingPrompts";
import type {QueryClient} from "@tanstack/react-query";
import type {
  AgentStatus,
  AgentThread,
  Chunk,
  Message,
  TextChunk,
  TodoItem,
} from "./types";

interface StreamPart {
  event?: string;
  data?: unknown;
}

interface ThreadMetaPayload {
  id?: string;
  status?: AgentStatus;
  branch?: string;
  pr?: AgentThread["pr"];
  updatedAt?: number;
  // 上下文用量环的数据；后端取不到用量时为 null。
  usage?: { used: number; total: number } | null;
}

interface TextDeltaPayload {
  message_id?: string;
  run_id?: string;
  content?: string;
  mode?: "append" | "replace";
}

interface UserMessagePayload {
  message_id?: string;
  run_id?: string;
  thread_id?: string;
  content?: string;
  timestamp?: string;
}

interface TodoDeltaPayload {
  message_id?: string;
  run_id?: string;
  todos?: Array<TodoItem>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function updateThread(
  threadId: string,
  queryClient: QueryClient,
  updater: (thread: AgentThread) => AgentThread,
) {
  const key = agentThreadKeys.detail(threadId);
  void queryClient.cancelQueries({ queryKey: key });
  queryClient.setQueryData<AgentThread | undefined>(key, (prev) =>
    prev ? updater(prev) : prev,
  );
}

function invalidateThreadList(queryClient: QueryClient) {
  // `agentThreadKeys.all` 是 thread 列表 key，也是 detail key 的前缀。
  // 流式运行结束时只能刷新侧边栏列表，不能让当前 detail 重新请求并覆盖
  // SSE 已经追加好的 messages。
  void queryClient.invalidateQueries({ queryKey: agentThreadKeys.all, exact: true });
}

function upsertMessage(thread: AgentThread, message: Message): AgentThread {
  const index = thread.messages.findIndex((item) => item.id === message.id);
  if (index < 0) {
    return { ...thread, messages: [...thread.messages, message] };
  }
  const messages = thread.messages.slice();
  messages[index] = message;
  return { ...thread, messages };
}

function runIdFromMessageId(messageId: string): string | null {
  const patterns = [
    /-user-(.+)$/,
    /-live-plan-(.+)$/,
    /-live-assistant-(.+)$/,
  ];
  for (const pattern of patterns) {
    const match = messageId.match(pattern);
    if (match?.[1]) return match[1];
  }
  return null;
}

function payloadRunId(data: { run_id?: string; message_id?: string }): string | null {
  return data.run_id || (data.message_id ? runIdFromMessageId(data.message_id) : null);
}

function findRunMessageIndex(
  messages: Array<Message>,
  runId: string | null,
  role: "user" | "plan" | "assistant",
): number {
  if (!runId) return -1;
  const marker =
    role === "user"
      ? `-user-${runId}`
      : role === "plan"
        ? `-live-plan-${runId}`
        : `-live-assistant-${runId}`;
  return messages.findIndex((message) => message.id.endsWith(marker));
}

function insertOrReplaceAt(messages: Array<Message>, message: Message, insertAt: number): Array<Message> {
  const withoutCurrent = messages.filter((item) => item.id !== message.id);
  const boundedIndex = Math.min(Math.max(insertAt, 0), withoutCurrent.length);
  const next = withoutCurrent.slice();
  next.splice(boundedIndex, 0, message);
  return next;
}

function ensureAgentMessage(thread: AgentThread, messageId: string): Message {
  const existing = thread.messages.find((message) => message.id === messageId);
  if (existing) return existing;
  return {
    id: messageId,
    author: "agent",
    timestamp: new Date().toISOString(),
    chunks: [],
  };
}

/**
 * 移除本轮的"启动占位消息"（"正在理解你的需求..."）。
 *
 * 后端在真实模型输出前会先推一条 `{threadId}-assistant-startup-{uuid}` 占位消息，
 * 用来降低初始化阶段的"页面无响应"感。真实回答是另一条消息，占位消息如果
 * 不主动移除，就会永远留在回答上方（checkpoint 历史里没有它，所以重进会话
 * 又"看起来正常"）。在第一段真实正文到达时、以及轮次收尾时调用。
 */
function stripStartupPlaceholder(thread: AgentThread, threadId: string): AgentThread {
  const prefix = `${threadId}-assistant-startup-`;
  if (!thread.messages.some((message) => message.id.startsWith(prefix))) return thread;
  return {
    ...thread,
    messages: thread.messages.filter((message) => !message.id.startsWith(prefix)),
  };
}

function setTextChunk(message: Message, text: string, mode: "append" | "replace"): Message {
  const chunks = message.chunks.slice();
  const textIndex = chunks.findIndex((chunk) => chunk.kind === "text");
  if (textIndex >= 0) {
    const previous = chunks[textIndex] as TextChunk;
    chunks[textIndex] = {
      ...previous,
      text: mode === "replace" ? text : previous.text + text,
    };
  } else {
    chunks.push({ kind: "text", text });
  }
  return { ...message, chunks };
}

function applyTextDelta(thread: AgentThread, data: TextDeltaPayload): AgentThread {
  const messageId = data.message_id;
  const content = data.content ?? "";
  if (!messageId || !content) return thread;
  const message = ensureAgentMessage(thread, messageId);
  const nextMessage = setTextChunk(message, content, data.mode ?? "append");
  const existingIndex = thread.messages.findIndex((item) => item.id === messageId);
  if (existingIndex >= 0) {
    return upsertMessage(thread, nextMessage);
  }

  const runId = payloadRunId(data);
  const messages = thread.messages.slice();
  const planIndex = findRunMessageIndex(messages, runId, "plan");
  const userIndex = findRunMessageIndex(messages, runId, "user");
  const insertAt =
    planIndex >= 0
      ? planIndex + 1
      : userIndex >= 0
        ? userIndex + 1
        : messages.length;
  return { ...thread, messages: insertOrReplaceAt(messages, nextMessage, insertAt) };
}

function applyUserMessage(thread: AgentThread, data: UserMessagePayload): AgentThread {
  const content = data.content?.trim();
  if (!content) return thread;
  const messageId = data.message_id || `user-${Date.now()}`;

  // 正式 SSE user_message 代表一个 run 的真实用户输入，只能按 message_id 去重。
  // 不要按文本去重：多轮对话里用户可能反复输入“确认”“开始实施”，文本相同
  // 也必须作为独立轮次保留。
  if (thread.messages.some((message) => message.id === messageId)) return thread;

  const userMessage: Message = {
    id: messageId,
    author: "user",
    timestamp: data.timestamp || new Date().toISOString(),
    chunks: [{ kind: "text", text: content }],
  };
  const runId = payloadRunId(data);
  const messages = thread.messages.slice();
  const planIndex = findRunMessageIndex(messages, runId, "plan");
  const assistantIndex = findRunMessageIndex(messages, runId, "assistant");
  const insertAt =
    planIndex >= 0
      ? planIndex
      : assistantIndex >= 0
        ? assistantIndex
        : messages.length;
  return { ...thread, messages: insertOrReplaceAt(messages, userMessage, insertAt) };
}

function applyTodoDelta(thread: AgentThread, data: TodoDeltaPayload): AgentThread {
  const messageId = data.message_id;
  const todos = data.todos ?? [];
  if (!messageId || todos.length === 0) return thread;

  const current = ensureAgentMessage(thread, messageId);
  const todoChunk: Chunk = { kind: "todo", todos };
  const chunks: Array<Chunk> = [
    { kind: "text", text: "任务计划" },
    todoChunk,
  ];

  const planMessage: Message = {
    ...current,
    author: "agent",
    chunks,
  };

  const withoutOldPlan = thread.messages.filter((message) => message.id !== messageId);
  // 任务计划属于某一轮 run，必须插到同 run 的 user_message 后面。
  // 不能再用“最后一条用户消息”猜位置，因为第二轮用户输入可能还只是 pending prompt，
  // 不在 thread.messages 里，会导致第二轮任务计划插到上一轮内容前面。
  const runId = payloadRunId(data);
  const userIndex = findRunMessageIndex(withoutOldPlan, runId, "user");
  const assistantIndex = findRunMessageIndex(withoutOldPlan, runId, "assistant");
  const insertAt =
    userIndex >= 0
      ? userIndex + 1
      : assistantIndex >= 0
        ? assistantIndex
        : withoutOldPlan.length;
  return { ...thread, messages: insertOrReplaceAt(withoutOldPlan, planMessage, insertAt) };
}

function applyThreadMeta(thread: AgentThread, data: ThreadMetaPayload): AgentThread {
  return {
    ...thread,
    status: data.status ?? thread.status,
    branch: data.branch ?? thread.branch,
    pr: data.pr ?? thread.pr,
    updatedAt: data.updatedAt ?? thread.updatedAt,
    // 只有后端真的带了用量才更新，避免把已有环数据清空。
    usage: data.usage ?? thread.usage,
  };
}

export function handleStreamPart(
  threadId: string,
  part: StreamPart,
  queryClient: QueryClient,
) {
  const event = part.event ?? "";
  const data = isRecord(part.data) ? part.data : {};

  if (event === "thread_snapshot" || event === "thread_done") {
    const key = agentThreadKeys.detail(threadId);
    void queryClient.cancelQueries({ queryKey: key });
    queryClient.setQueryData<AgentThread | undefined>(key, (prev) => {
      if (!prev) {
        // 新建会话：snapshot 是本条流的第一个事件，此时 detail 缓存还没有数据。
        // 必须在这里把 thread 创建出来，否则同一批 SSE 里紧跟着 snapshot 的
        // user_message / message_start 会因 prev 为空被 setQueryData 静默丢弃：
        // 用户气泡消失、pending 永远无法确认，底部状态动画停不下来。
        return event === "thread_snapshot"
          ? threadFromSnapshot(threadId, data)
          : prev;
      }
      // 任务失败后，thread_done 里携带的是数据库中的旧状态（可能仍是 running）。
      // 不能让它把已经确定的 error 覆盖回“运行中”，否则用户只会看到永久转圈。
      if (event === "thread_done" && prev.status === "error") return prev;
      return applyThreadMeta(prev, data);
    });
    if (event === "thread_done") {
      invalidateThreadList(queryClient);
    }
    return;
  }

  if (event === "todo_delta") {
    updateThread(threadId, queryClient, (thread) =>
      applyTodoDelta(thread, data),
    );
    return;
  }

  if (event === "user_message") {
    const payload = data as UserMessagePayload;
    // 精确移除对应的乐观气泡：后端把前端传的 pending id 作为 message_id 回传，
    // 这里按 id 直接删除 sessionStorage 中的该 pending，不再依赖位置扫描，
    // 避免真实气泡插入位置与 pending.insertAt 错位时漏删（导致重复显示）。
    if (payload.message_id) {
      dropPendingPrompts(payload.thread_id || threadId, (entry) => entry.id === payload.message_id);
    }
    updateThread(threadId, queryClient, (thread) =>
      applyUserMessage(thread, payload),
    );
    return;
  }

  if (event === "message_start") {
    const messageId = String(data.message_id || "");
    if (!messageId) return;
    updateThread(threadId, queryClient, (thread) =>
      upsertMessage(thread, ensureAgentMessage(thread, messageId)),
    );
    return;
  }

  if (event === "text_delta") {
    updateThread(threadId, queryClient, (thread) => {
      const next = applyTextDelta(thread, data);
      // 启动占位消息只在真实回答输出前有意义；第一段真实正文（非 startup 消息）
      // 到达后就把它移除，否则"正在理解你的需求..."会永远留在回答上方。
      const messageId = (data as TextDeltaPayload).message_id ?? "";
      if (messageId && !messageId.includes("-assistant-startup-")) {
        return stripStartupPlaceholder(next, threadId);
      }
      return next;
    });
    return;
  }

  if (event === "tool_update") {
    // 工具事件默认只保留在后端日志 / run_events 中，不进入聊天正文。
    // 这样页面重点展示任务计划和有价值的 AIMessage 文本。
    return;
  }

  if (event === "done") {
    updateThread(threadId, queryClient, (thread) => {
      const next = {
        ...thread,
        // 失败轮次的收尾事件不能把 error 洗成 finished。
        status: thread.status === "error" ? ("error" as const) : ("finished" as const),
      };
      // 兜底：本轮即使没有产生任何真实正文（例如只执行了工具），启动占位
      // 消息也不能留着，否则"正在理解你的需求..."会在结束后仍然可见。
      return stripStartupPlaceholder(next, threadId);
    });
    invalidateThreadList(queryClient);
    return;
  }

  if (event === "error") {
    // 之前这里只改状态、不渲染任何文本，用户看到的就是“卡在启动提示上”。
    // 现在把失败原因作为一条错误消息直接写进会话正文。
    const detail =
      typeof data.message === "string" && data.message.trim()
        ? data.message.trim()
        : "任务执行失败，详细信息请查看后端日志。";

    updateThread(threadId, queryClient, (thread) => {
      const next = {
        ...thread,
        status: "error" as const,
        messages: [
          // 同一轮重试时不要堆叠多条错误消息。
          ...thread.messages.filter((item) => !item.id.startsWith(`${threadId}-error-`)),
          {
            id: `${threadId}-error-${Date.now()}`,
            author: "agent" as const,
            timestamp: new Date().toISOString(),
            chunks: [{ kind: "error" as const, text: `任务失败：${detail}` }],
          },
        ],
      };
      // 失败轮次同样移除启动占位消息，错误信息本身已经足够说明状态。
      return stripStartupPlaceholder(next, threadId);
    });
    return;
  }
}

export function useAgentThreadStream(threadId: string, enabled: boolean) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled) return;

    const controller = new AbortController();
    let cancelled = false;

    async function consume() {
      try {
        const res = await fetch(agentsApi.streamUrl(threadId), {
          credentials: "include",
          signal: controller.signal,
          headers: { Accept: "text/event-stream" },
        });
        if (!res.ok || !res.body) return;

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (!cancelled) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let boundary = buffer.indexOf("\n\n");
          while (boundary !== -1) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            for (const line of block.split("\n")) {
              if (!line.startsWith("data: ")) continue;
              const raw = line.slice(6);
              try {
                handleStreamPart(threadId, JSON.parse(raw) as StreamPart, queryClient);
              } catch {
                // 单个事件解析失败时不刷新整条会话，避免旧快照覆盖本地流式内容。
                console.debug("failed to parse agent stream event", raw);
              }
            }
            boundary = buffer.indexOf("\n\n");
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          console.debug("agent thread stream closed", error);
        }
      }
    }

    void consume();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [enabled, queryClient, threadId]);
}

/** 解析一段 SSE 文本块（`event: xxx` + `data: json` 命名事件格式）。 */
function parseSSEBlock(raw: string): StreamPart | null {
  let event = "";
  const dataLines: Array<string> = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!event && dataLines.length === 0) return null;
  let data: unknown = {};
  try {
    data = dataLines.length > 0 ? JSON.parse(dataLines.join("\n")) : {};
  } catch {
    data = {};
  }
  return { event, data };
}

/** 用 thread_snapshot 的元信息拼出一个最小 Thread 对象，供 mutation 立即 resolve。 */
export function threadFromSnapshot(
  threadId: string,
  data: Record<string, unknown>,
): AgentThread {
  return {
    id: threadId,
    title: String(data.title ?? ""),
    repo: String(data.repo ?? ""),
    repoFullName: String(data.repoFullName ?? data.repo ?? ""),
    branch: String(data.branch ?? "master"),
    model: String(data.model ?? ""),
    effort: null,
    source: "dashboard",
    status: (data.status as AgentStatus) ?? "running",
    createdAt: Number(data.createdAt ?? Date.now()),
    updatedAt: Number(data.updatedAt ?? Date.now()),
    usage: null,
    messages: [],
  };
}

export interface ThreadStreamHandlers {
  onSnapshot?: (thread: AgentThread) => void;
  onError?: (message: string) => void;
}

/**
 * 消费后端 POST SSE 响应（创建/发送消息共用）。
 *
 * thread_id 从第一个 `thread_snapshot` 事件里取，所以创建会话时调用方不需要
 * 预先知道 id。返回的 Promise 在整条流结束后才 resolve；调用方通常只关心
 * onSnapshot（拿到 thread 元信息后就可以跳转页面），剩余事件继续写入
 * react-query 缓存，由会话页消费。
 */
export async function consumeThreadStream(
  response: Response,
  queryClient: QueryClient,
  handlers: ThreadStreamHandlers = {},
): Promise<void> {
  if (!response.body) throw new Error("流式响应不可用");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let threadId = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");

      const part = parseSSEBlock(block);
      if (!part) continue;
      const data = isRecord(part.data) ? part.data : {};

      if (part.event === "thread_snapshot") {
        threadId = String(data.id ?? threadId);
        if (threadId) {
          handleStreamPart(threadId, part, queryClient);
          handlers.onSnapshot?.(threadFromSnapshot(threadId, data));
        }
        continue;
      }

      if (part.event === "error") {
        handlers.onError?.(String(data.message ?? "任务执行失败"));
      }
      if (threadId) handleStreamPart(threadId, part, queryClient);
    }
  }
}
