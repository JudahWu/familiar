import { useEffect, useMemo, useState } from "react";

import type { SessionUser } from "@/lib/api";
import type { PendingPrompt } from "@/lib/agents/pendingPrompts";
import type { AgentThread, ImageChunk, Message } from "@/lib/agents/types";
import type { ModelSelection } from "@/lib/agents/useModelOptions";
import { AgentPromptBar } from "@/components/agents/AgentPromptBar";
import { AgentsShell } from "@/components/agents/AgentsSidebar";
import { MessageView } from "@/components/agents/ported";
import { useCancelAgentThread, useSendAgentMessage } from "@/lib/agents/queries";
import { addPendingPrompt, dropPendingPrompts, getPendingPrompts } from "@/lib/agents/pendingPrompts";
import { useModelOptions } from "@/lib/agents/useModelOptions";

interface AgentThreadViewProps {
  user: SessionUser;
  thread: AgentThread;
}

function messageText(message: Message): string {
  return message.chunks
    .filter((chunk) => chunk.kind === "text")
    .map((chunk) => chunk.text)
    .join("");
}

function messageImageKey(message: Message): string {
  return message.chunks
    .filter((chunk) => chunk.kind === "image")
    .map((chunk) => `${chunk.mimeType}:${chunk.base64}`)
    .join("\u0000");
}

function pendingImageKey(entry: PendingPrompt): string {
  return (entry.images ?? [])
    .map((image) => `${image.mimeType}:${image.base64}`)
    .join("\u0000");
}

function isPendingPromptConfirmed(entry: PendingPrompt, messages: Array<Message>): boolean {
  // pending 只能被它提交位置之后的新用户消息确认。
  // 不能全历史按文本匹配，否则“确认”“开始实施”这类重复短指令会被旧消息误删。
  const candidateMessages = messages.slice(Math.max(0, entry.insertAt));
  return candidateMessages.some((message) => {
    if (message.author !== "user") return false;
    return messageText(message) === entry.prompt && messageImageKey(message) === pendingImageKey(entry);
  });
}

function liveStatusFor(thread: AgentThread, hasPendingPrompt: boolean): string | null {
  if (thread.status !== "running") return null;

  for (const message of [...thread.messages].reverse()) {
    for (const chunk of [...message.chunks].reverse()) {
      if (chunk.kind !== "tool-execution") continue;
      const status =
        chunk.status === "in_progress"
          ? "运行中"
          : chunk.status === "pending"
            ? "已排队"
            : chunk.status === "error"
              ? "失败"
              : "已完成";
      return `${status}: ${chunk.title}`;
    }
  }

  return hasPendingPrompt ? "正在启动智能体运行..." : "正在等待智能体输出...";
}

export function AgentThreadView({ user, thread }: AgentThreadViewProps) {
  const sendMessage = useSendAgentMessage(thread.id);
  const cancelThread = useCancelAgentThread(thread.id);
  // 实时事件由“发送消息”的 POST SSE 直接写入 react-query 缓存，
  // 不再单独订阅 GET /stream（当前后端未提供该路由）。
  const [pendingPrompts, setPendingPrompts] = useState<Array<PendingPrompt>>(() =>
    getPendingPrompts(thread.id),
  );

  const { models, defaultSelection } = useModelOptions();
  const threadSelection = useMemo<ModelSelection | null>(() => {
    if (!thread.model || !thread.effort) return null;
    const supported = models.some(
      (m) => m.id === thread.model && m.efforts.includes(thread.effort ?? ""),
    );
    if (!supported) return null;
    return { modelId: thread.model, effort: thread.effort };
  }, [models, thread.model, thread.effort]);
  const [selection, setSelection] = useState<ModelSelection | null>(null);
  const activeSelection = selection ?? threadSelection ?? defaultSelection;

  useEffect(() => {
    setPendingPrompts((prev) => {
      if (prev.length === 0) return prev;
      // 结束时不能无条件清空 pending。若正式 user_message 因网络或后端时序尚未
      // 进入 thread.messages，直接清空会让用户刚输入的指令从页面消失。
      // 新建会话路径下 pending 是在 user_message 已经写入之后才补加的
      // （useCreateAgentThread.onSuccess），所以这里必须把 pendingPrompts 放进
      // 依赖里再跑一遍确认，否则最后一条 pending 永远无法被确认移除，
      // isStreaming 卡在 true，底部状态动画会一直轮换。
      const next = dropPendingPrompts(thread.id, (entry) =>
        isPendingPromptConfirmed(entry, thread.messages),
      );
      return next.length === prev.length ? prev : next;
    });
  }, [thread.id, thread.messages, thread.status, pendingPrompts]);

  const displayMessages = useMemo<Array<Message>>(() => {
    if (pendingPrompts.length === 0) return thread.messages;
    const result = thread.messages.slice();
    pendingPrompts.forEach((entry, i) => {
      if (isPendingPromptConfirmed(entry, thread.messages)) return;
      const chunks: Message["chunks"] = [...(entry.images ?? [])];
      if (entry.prompt) chunks.push({ kind: "text", text: entry.prompt });
      const synth: Message = {
        id: `pending-user-${entry.id}-${i}`,
        author: "user",
        timestamp: new Date(entry.createdAt || Date.now()).toISOString(),
        chunks,
      };
      const at = Math.min(Math.max(entry.insertAt, 0), result.length);
      result.splice(at, 0, synth);
    });
    return result;
  }, [thread.messages, pendingPrompts]);

  const hasMessages = displayMessages.length > 0;
  const hasActiveRun = thread.status === "running";
  const isStreaming = hasActiveRun || pendingPrompts.length > 0;
  const runFailed = thread.status === "error";
  const sendError = sendMessage.error instanceof Error ? sendMessage.error.message : null;
  const liveStatus = liveStatusFor(thread, pendingPrompts.length > 0);
  const handleSubmit = (
    content: string,
    images: Array<ImageChunk>,
    files?: Array<{ name: string; mimeType: string; size: number; base64: string }>
  ) => {
    const insertAt = displayMessages.length;
    const nextPendingPrompts = addPendingPrompt(thread.id, content, insertAt, images);
    setPendingPrompts(nextPendingPrompts);
    // 把本轮 pending 的 id 透传给后端，让回传的 user_message 用同一个 id，
    // 前端即可按 id 精确移除乐观气泡（见 useThreadStream 的 user_message 分支）。
    const pendingId = nextPendingPrompts[nextPendingPrompts.length - 1]?.id;
    sendMessage.mutate({
      content,
      images,
      files,
      model_id: activeSelection?.modelId ?? null,
      effort: activeSelection?.effort ?? null,
      pending_id: pendingId,
    });
  };

  return (
    <AgentsShell user={user} activeThreadId={thread.id}>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex min-h-0 flex-1 flex-col">
          {hasMessages ? (
            <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
              <MessageView
                messages={displayMessages}
                isStreaming={isStreaming}
                contentWidthClass="max-w-3xl"
                liveStatus={liveStatus}
              />
              <div className="shrink-0 px-4 pb-4">
                <div className="mx-auto w-full min-w-0 max-w-3xl">
                  {runFailed ? (
                    <p className="mb-3 text-sm text-red-600 dark:text-red-400">
                      运行失败。请查看后端控制台日志。
                    </p>
                  ) : null}
                  {sendError ? (
                    <p className="mb-3 text-sm text-red-600 dark:text-red-400">
                      {sendError}
                    </p>
                  ) : null}
                  <AgentPromptBar
                    placeholder="继续输入指令"
                    compact
                    busy={hasActiveRun}
                    disabled={sendMessage.isPending}
                    onSubmit={handleSubmit}
                    onStop={() => cancelThread.mutate()}
                    stopping={cancelThread.isPending}
                    models={models}
                    selection={activeSelection}
                    onSelectionChange={setSelection}
                    contextUsage={thread.usage}
                  />
                </div>
              </div>
            </div>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6">
              <p className="text-sm text-[var(--ui-text-dim)]">当前线程还没有消息。</p>
              {runFailed ? (
                <p className="text-sm text-red-600 dark:text-red-400">
                  运行失败。请查看后端控制台日志。
                </p>
              ) : null}
              {sendError ? (
                <p className="text-sm text-red-600 dark:text-red-400">
                  {sendError}
                </p>
              ) : null}
              <div className="w-full max-w-3xl">
                <AgentPromptBar
                  placeholder="发送第一条指令"
                  compact
                  busy={hasActiveRun}
                  disabled={sendMessage.isPending}
                  onSubmit={handleSubmit}
                  onStop={() => cancelThread.mutate()}
                  stopping={cancelThread.isPending}
                  models={models}
                  selection={activeSelection}
                  onSelectionChange={setSelection}
                />
              </div>
            </div>
          )}
        </div>
      </div>
    </AgentsShell>
  );
}
