import {  useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import {  agentsApi, postThreadStream } from "./api"
import { addPendingPrompt } from "./pendingPrompts"
import { consumeThreadStream } from "./useThreadStream"
import type {FileUpload, ScheduleUpdateRequest } from "./api";
import type {QueryClient} from "@tanstack/react-query";
import type { AgentThread, ImageChunk } from "./types"

export const agentThreadKeys = {
  all: ["agent-threads"] as const,
  detail: (threadId: string) => ["agent-threads", threadId] as const,
}

export const agentScheduleKeys = {
  all: ["agent-schedules"] as const,
}

function invalidateThreadList(queryClient: ReturnType<typeof useQueryClient>) {
  // `agentThreadKeys.all` 是列表 key，同时也是 detail key 的前缀。
  // TanStack Query 默认按前缀匹配，如果不加 exact，会连当前 detail
  // `["agent-threads", threadId]` 一起 refetch。Agent 正在或刚刚完成流式输出时，
  // detail refetch 会用 checkpoint 快照覆盖前端本地已经追加好的 messages。
  void queryClient.invalidateQueries({ queryKey: agentThreadKeys.all, exact: true })
}

export function useAgentThreads() {
  return useQuery({
    queryKey: agentThreadKeys.all,
    queryFn: () => agentsApi.listThreads(),
  })
}

export function useAgentThread(threadId: string) {
  return useQuery({
    queryKey: agentThreadKeys.detail(threadId),
    queryFn: () => agentsApi.getThread(threadId),
  })
}

export function useAgentSchedules() {
  return useQuery({
    queryKey: agentScheduleKeys.all,
    queryFn: () => agentsApi.listSchedules(),
  })
}

export function useCreateAgentSchedule() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: agentsApi.createSchedule,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: agentScheduleKeys.all })
    },
  })
}

export function useUpdateAgentSchedule() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (vars: { scheduleId: string; body: ScheduleUpdateRequest }) =>
      agentsApi.updateSchedule(vars.scheduleId, vars.body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: agentScheduleKeys.all })
    },
  })
}

export function useDeleteAgentSchedule() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: agentsApi.deleteSchedule,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: agentScheduleKeys.all })
    },
  })
}

export interface CreateAgentThreadVariables {
  prompt: string
  images?: Array<ImageChunk>
  files?: Array<FileUpload>
  repo?: string | null
  repo_explicitly_none?: boolean
  model_id?: string | null
  effort?: string | null
}

/** 发起 POST SSE 请求，并在拿到 thread_snapshot 后立即 resolve。 */
function startThreadStream(
  path: string,
  body: unknown,
  queryClient: QueryClient,
): Promise<AgentThread> {
  return postThreadStream(path, body).then(
    (response) =>
      new Promise<AgentThread>((resolve, reject) => {
        let settled = false
        const settle = (fn: () => void) => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          fn()
        }
        // 兜底超时：流一直不吐 thread_snapshot 时不能让 mutation 永久挂起。
        const timer = setTimeout(
          () => settle(() => reject(new Error("等待任务开始超时，请查看后端日志"))),
          60_000,
        )
        void consumeThreadStream(response, queryClient, {
          onSnapshot: (thread) => settle(() => resolve(thread)),
          onError: (message) => settle(() => reject(new Error(message))),
        }).catch((error: unknown) =>
          settle(() => reject(error instanceof Error ? error : new Error(String(error)))),
        )
      }),
  )
}

export function useCreateAgentThread() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  return useMutation({
    mutationFn: (variables: CreateAgentThreadVariables) =>
      startThreadStream(
        "/threads/stream-message",
        {
          // 后端 SSE 接口用 content 承载用户真实输入。
          content: variables.prompt,
          repo: variables.repo ?? undefined,
          model_id: variables.model_id ?? null,
          effort: variables.effort ?? null,
        },
        queryClient,
      ),
    onSuccess: (thread, variables) => {
      // snapshot 不带 messages，pending prompt 由前端乐观插入，等 SSE 的
      // user_message 到达后再按 message_id 去重替换。
      addPendingPrompt(thread.id, variables.prompt, 0, variables.images)
      // 快照分支此时已经把 thread 种进了 detail 缓存，且同一批 SSE 里更晚的
      // user_message 可能已经写入 messages。这里不能再整包覆盖（会清掉刚收到
      // 的用户消息、让 pending 永远无法确认），只做补缺式合并。
      queryClient.setQueryData<AgentThread | undefined>(
        agentThreadKeys.detail(thread.id),
        (prev) => {
          if (!prev) {
            return {
              ...thread,
              status: thread.status === "idle" ? ("running" as const) : thread.status,
            }
          }
          return {
            ...prev,
            title: prev.title || thread.title,
            branch: prev.branch || thread.branch,
            model: prev.model || thread.model,
            usage: prev.usage ?? thread.usage,
          }
        },
      )
      invalidateThreadList(queryClient)
      navigate({ to: "/agents/$threadId", params: { threadId: thread.id } })
    },
  })
}

export interface SendAgentMessageVariables {
  content: string
  images?: Array<ImageChunk>
  files?: Array<FileUpload>
  model_id?: string | null
  effort?: string | null
  pending_id?: string
}

export function useSendAgentMessage(threadId: string) {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (vars: SendAgentMessageVariables) =>
      startThreadStream(
        `/threads/${encodeURIComponent(threadId)}/stream-message`,
        {
          content: vars.content,
          model_id: vars.model_id ?? null,
          effort: vars.effort ?? null,
          pending_id: vars.pending_id ?? null,
        },
        queryClient,
      ),
    onSuccess: (thread) => {
      queryClient.setQueryData(
        agentThreadKeys.detail(threadId),
        (prev: typeof thread | undefined) => {
          if (!prev) return thread
          return {
            ...thread,
            // 发送新消息后，实时内容由 SSE 增量追加。这里不能用刚返回的
            // 快照覆盖本地消息，否则用户刚输入的内容和上一轮输出会闪烁或消失。
            messages: prev.messages,
          }
        }
      )
      invalidateThreadList(queryClient)
    },
  })
}

export function useCancelAgentThread(threadId: string) {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: () => agentsApi.cancelThread(threadId),
    onSuccess: (thread) => {
      queryClient.setQueryData(agentThreadKeys.detail(threadId), thread)
      invalidateThreadList(queryClient)
    },
  })
}

export function useDeleteAgentThread() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  return useMutation({
    mutationFn: (threadId: string) => agentsApi.deleteThread(threadId),
    onSuccess: (_, threadId) => {
      queryClient.removeQueries({ queryKey: agentThreadKeys.detail(threadId) })
      invalidateThreadList(queryClient)
      const path = window.location.pathname
      if (path.includes(`/agents/${threadId}`)) {
        navigate({ to: "/agents" })
      }
    },
  })
}
