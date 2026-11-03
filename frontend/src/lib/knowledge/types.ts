/** 知识库相关的数据类型，和 `api/knowledge_routes.py` 返回的 JSON 一一对应。 */

export type DocStatus = "pending" | "parsing" | "indexed" | "failed"

export interface KnowledgeDoc {
  id: string
  title: string
  filename: string
  /** 原件大小（字节）。 */
  size_bytes: number
  pages: number
  status: DocStatus
  /** 0-100。 */
  progress: number
  paragraphs: number
  /** 抽到的插图数量。 */
  images: number
  /** PyMuPDF 直接抽到文字的页数。 */
  text_pages: number
  /** 需要版面识别的页数（阶段 ③ 补）。 */
  vision_pages: number
  error: string | null
  updated_at: string
}

export interface KnowledgeBase {
  id: string
  name: string
  created_at: string
  /** 该库的段落总数（来自 PostgreSQL）。 */
  chunks: number
  documents: Array<KnowledgeDoc>
}

export interface SearchHit {
  text: string
  /** 段落所属的最近标题，可能是空串（正文里没有识别到标题行）。 */
  title: string
  doc_title: string
  doc_id: string
  page: number
  chunk_index: number
  score: number
  /** text 是正文块，image 是插图条目（text 字段存的是图片描述）。 */
  category: "text" | "image"
  /** 插图在 MinIO 里的 key；文本条目为空串。 */
  image_path: string
}

/** 后台导入任务的进度。 */
export interface ImportProgress {
  doc_id: string
  kb_id: string
  status: "pending" | "running" | "done" | "failed"
  /** text 是切正文，image 是处理插图。 */
  stage: string
  done: number
  total: number
  message: string
  error: string | null
}

/** 一次问答的结果。 */
export interface AnswerResult {
  answer: string
  /** 与答案里 [1][2] 对应的命中段落，下标 0 对应 [1]。 */
  hits: Array<SearchHit>
  /** 忠实度 0-1（主分）；联网重答后为 null，因为那一版没做质检。 */
  score: number | null
  /** 资料对问题的覆盖程度：full 能答、partial 只答一部分、none 库里没料。 */
  coverage: "full" | "partial" | "none" | "unknown"
  /** 切题度 0-1：答案像不像在回答问题。 */
  relevancy: number | null
  /** 材料相关度：问题与命中段落的最高相似度，只作诊断。 */
  support: number | null
  /** 评委给的判断依据。 */
  reason: string
  /** 要不要请用户看一眼。 */
  needs_review: boolean
  /** insufficient = 库里没料，该换联网；low_score = 答得可疑。 */
  review_reason: "insufficient" | "low_score" | ""
  /**
   * 图停在了人工审批之前。true 时这条答案还是"待确认"，
   * 必须调 resume 才能收尾或改走联网。
   */
  interrupted: boolean
  /** 用户已经做过的裁决，空串表示还没选。 */
  human_answer: "approve" | "rejected" | ""
  model: string
  web_used: boolean
  /** 请求了联网但没成功时的原因（如 key 无效）。 */
  web_error: string
  /** 低分线，前端用来解释"为什么提醒你"。 */
  threshold: number
  /** 会话 id。第一次提问时后端新建并返回，之后前端带着它才能有记忆。 */
  session_id: string
  /** 实际拿去检索的那句。有历史时可能是改写后的独立问题。 */
  search_query: string
  /** 这次回答参考了几条历史问答（跨会话回忆的条数）。 */
  history_hits: number
}

/** 从后端恢复出来的一轮历史。 */
export interface SessionTurn {
  role: "user" | "assistant"
  content: string
  search_query: string
  /** 助手轮的命中段落快照，恢复后引用仍可点开。 */
  citations: Array<SearchHit>
  score: number | null
  coverage: string
  needs_review: boolean
  created_at: string
}

/** 一次问答里的一轮。 */
export interface ChatTurn {
  id: string
  role: "user" | "assistant"
  /** 用户轮的原文。纯图片提问时为空。 */
  text?: string
  /** 用户轮上传的图片（本地 data URL，仅用于界面回显；刷新后从历史恢复的没有这个）。 */
  image?: string
  /** 助手轮的答案正文。 */
  answer?: string
  hits?: Array<SearchHit>
  score?: number | null
  coverage?: AnswerResult["coverage"]
  reason?: string
  needsReview?: boolean
  reviewReason?: AnswerResult["review_reason"]
  model?: string
  webUsed?: boolean
  webError?: string
  /** 助手轮对应的原始问题，供"换联网重答"重发。 */
  query?: string
  /** 低分线，用来在提示里说明"为什么提醒你"。 */
  threshold?: number
  error?: string
}
