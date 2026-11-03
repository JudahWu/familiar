import type {
  AnswerResult,
  ImportProgress,
  KnowledgeBase,
  KnowledgeDoc,
  SearchHit,
  SessionTurn,
} from "./types"
import { API_BASE } from "@/lib/apiBase"


/**
 * 知识库接口客户端。
 *
 * 和其它 dashboard 接口一样走同源（dev 时由 vite 代理转发到后端）并带上会话
 * Cookie。上传用 multipart，所以这里不能像 JSON 接口那样统一塞
 * `Content-Type`——浏览器要自己带 boundary。
 */

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}/dashboard/api/knowledge${path}`, {
    ...init,
    credentials: "include",
  })
  if (!res.ok) {
    let message = res.statusText
    try {
      const body = await res.json()
      if (body?.detail) {
        message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)
      }
    } catch {
      /* 响应不是 JSON 就用状态文本 */
    }
    throw new Error(message)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const knowledgeApi = {
  listBases: () => request<Array<KnowledgeBase>>("/bases"),

  createBase: (name: string) =>
    request<KnowledgeBase>("/bases", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),

  deleteBase: (kbId: string) => request<{ ok: boolean }>(`/bases/${kbId}`, { method: "DELETE" }),

  deleteDocument: (kbId: string, docId: string) =>
    request<{ ok: boolean }>(`/bases/${kbId}/documents/${docId}`, { method: "DELETE" }),

  uploadDocument: (kbId: string, file: File) => {
    const form = new FormData()
    form.append("file", file)
    return request<KnowledgeDoc>(`/bases/${kbId}/documents`, { method: "POST", body: form })
  },

  /** 查后台导入的进度。 */
  getProgress: (docId: string) => request<ImportProgress>(`/documents/${docId}/progress`),

  /**
   * 插图的可访问地址。
   *
   * 走后端代理而不是 MinIO 的预签名 URL：预签名里带的 endpoint 是
   * `localhost:9002`，用 natapp 域名访问时浏览器会指向用户自己机器的
   * localhost，必然拿不到图。
   */
  imageUrl: (docId: string, objectKey: string) => {
    const name = objectKey.split("/").pop() ?? ""
    return `${API_BASE}/dashboard/api/knowledge/documents/${docId}/images/${encodeURIComponent(name)}`
  },

  search: (kbId: string, query: string, topK = 5) =>
    request<{ hits: Array<SearchHit> }>("/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kb_id: kbId, query, top_k: topK }),
    }),

  /**
   * 基于检索结果生成答案。
   *
   * 这是个慢接口（改写 + 历史回忆 + 检索 + 生成 + 三维评分，通常 8-25 秒），
   * 所以前端用 loading 态承接，不做假进度条。
   *
   * 返回 `interrupted: true` 表示图停在了人工审批之前——答案是"待确认"的，
   * 必须调 `resume` 才能收尾。联网重答不再是入参，而是 `resume("rejected")`
   * 的结果（图里 human_approval 的另一条边）。
   */
  ask: (
    kbId: string,
    query: string,
    options: { sessionId?: string | null; image?: string | null } = {},
  ) =>
    request<AnswerResult>("/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kb_id: kbId,
        query,
        session_id: options.sessionId ?? null,
        // data URL 直接传，后端会自己剥掉前缀
        image_base64: options.image ?? null,
      }),
    }),

  /** 对"待确认"的答案做裁决，继续跑图。approve 收尾，rejected 走联网重答。 */
  resume: (sessionId: string, decision: "approve" | "rejected") =>
    request<AnswerResult>("/answer/resume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, decision }),
    }),

  /**
   * 只读当前问答状态，不推进图。
   *
   * 刷新页面后用它判断这个会话是不是挂着一条待确认的答案，是的话把按钮恢复出来，
   * 而不是让用户重新问一遍。
   */
  answerState: (sessionId: string) =>
    request<AnswerResult>(`/answer/state?session_id=${encodeURIComponent(sessionId)}`),

  /** 恢复一个会话。刷新页面后靠它把对话接上，引用也一起回来。 */
  getSession: (sessionId: string) =>
    request<{ session_id: string; turns: Array<SessionTurn> }>(`/sessions/${sessionId}`),
}
