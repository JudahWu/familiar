import {
  ArrowUpIcon,
  ArrowsClockwiseIcon,
  GlobeIcon,
  ImageIcon,
  PlusIcon,
  UploadSimpleIcon,
} from "@phosphor-icons/react"
import { useRef, useState } from "react"

import type { ChatTurn, KnowledgeBase, SearchHit } from "@/lib/knowledge/types"
import { knowledgeApi } from "@/lib/knowledge/api"
import { cn } from "@/lib/utils"

/**
 * 知识库问答区：答案 + 引用 + 自评分。
 *
 * 三个排版决定：
 * 1. **答案就是正文，不套气泡**。它是要被读的，不是聊天记录。
 * 2. **引用编号是行内小胶囊**，点一下右侧出原文。这样读者能立刻知道哪句话有出处，
 *    而不必滚到下面数编号。模型没标的句子就是它自己的组织语言，这也是一种信息。
 * 3. **检索到的段落折在最后**，默认收起。答案已经是整理过的版本，原始段落是佐证，
 *    想看再展开——不然每轮都刷出五段原文，答案会被淹没。
 *
 * 对话有记忆（存在后端 PG 里，按 session 归集），所以刷新页面能接上；"新对话"只是
 * 丢掉当前 session id，历史记录仍留在库里。
 */

interface KnowledgeChatProps {
  base?: KnowledgeBase
  turns: Array<ChatTurn>
  pending: boolean
  activeHitKey: string | null
  /** 提问。第二参数是 data URL 形式的图片（纯图片提问时 query 为空串）。 */
  onAsk: (query: string, image?: string) => void
  /** 用户点"换联网搜索"时，用原问题重发一次。 */
  onRetryWeb: (query: string) => void
  /** 用户认可一条待确认的答案，把它标成已确认。 */
  onDismissReview: (turnId: string) => void
  /** 开一段新对话（清空当前会话记忆）。 */
  onNewSession: () => void
  onSelectHit: (hit: SearchHit) => void
  onOpenImport: () => void
  className?: string
}

export function hitKey(hit: SearchHit): string {
  return `${hit.doc_id}:${hit.chunk_index}`
}

/** 分数配色：及格绿、及格边缘黄、明显不行红。 */
function scoreTone(score: number, threshold: number): string {
  if (score >= threshold) return "var(--ui-success)"
  if (score >= threshold - 0.2) return "var(--ui-warning)"
  return "var(--ui-danger)"
}

/**
 * 把答案正文里的 `[n]` 变成可点的引用，`**加粗**` 变成粗体。
 *
 * 只认这两种标记——模型被要求不要写 Markdown 标题，所以不需要完整渲染器。
 * 编号超出命中范围时原样显示：那是模型编的引用，用户应该看到这个错误，而不是
 * 被我们悄悄抹掉。
 */
function renderAnswer(text: string, hits: Array<SearchHit>, onSelect: (hit: SearchHit) => void) {
  return text.split(/(\*\*[^*\n]+\*\*|\[\d{1,2}\])/g).map((part, index) => {
    const citation = /^\[(\d{1,2})\]$/.exec(part)
    if (citation) {
      const hit = hits[Number(citation[1]) - 1]
      if (!hit) {
        return (
          <span key={index} className="text-[var(--ui-danger)]">
            {part}
          </span>
        )
      }
      return (
        <button
          key={index}
          type="button"
          onClick={() => onSelect(hit)}
          title={`${hit.title || hit.doc_title} · 第 ${hit.page} 页`}
          className="mx-0.5 inline-flex min-w-[15px] items-center justify-center rounded-[3px] bg-[var(--ui-panel-2)] px-1 align-[1px] text-[10.5px] leading-[15px] text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-accent)] hover:text-[var(--ui-bg)]"
        >
          {citation[1]}
        </button>
      )
    }
    if (part.startsWith("**") && part.endsWith("**")) {
      return (
        <strong key={index} className="font-medium">
          {part.slice(2, -2)}
        </strong>
      )
    }
    return <span key={index}>{part}</span>
  })
}

export function KnowledgeChat({
  base,
  turns,
  pending,
  activeHitKey,
  onAsk,
  onRetryWeb,
  onDismissReview,
  onNewSession,
  onSelectHit,
  onOpenImport,
  className,
}: KnowledgeChatProps) {
  const [draft, setDraft] = useState("")
  // 待发送的图片（data URL）。用 state 而不是 ref，是为了能显示缩略图。
  const [draftImage, setDraftImage] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const submit = () => {
    const text = draft.trim()
    if ((!text && !draftImage) || pending || !base) return
    onAsk(text, draftImage ?? undefined)
    setDraft("")
    setDraftImage(null)
  }

  /** 读成 data URL。限制 8MB：和后端的上限对齐，超了在后端也只是白传。 */
  const pickImage = (file: File | undefined) => {
    if (!file || !file.type.startsWith("image/")) return
    if (file.size > 8 * 1024 * 1024) return
    const reader = new FileReader()
    reader.onload = () => setDraftImage(String(reader.result))
    reader.readAsDataURL(file)
  }

  const indexedDocs = base?.documents.filter((doc) => doc.status === "indexed").length ?? 0
  const totalPages = base?.documents.reduce((sum, doc) => sum + doc.pages, 0) ?? 0
  const totalImages = base?.documents.reduce((sum, doc) => sum + doc.images, 0) ?? 0

  return (
    <section className={cn("flex min-w-0 flex-1 flex-col bg-[var(--ui-bg)]", className)}>
      <header className="flex items-center justify-between gap-3 border-b border-[var(--ui-border)] px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-2 text-[12px] text-[var(--ui-text-muted)]">
          <span className="truncate font-medium text-[var(--ui-text)]">
            {base?.name ?? "未选择知识库"}
          </span>
          {base && (
            <>
              <span className="text-[var(--ui-text-dim)]">·</span>
              <span>{indexedDocs} 篇已入库</span>
              <span className="text-[var(--ui-text-dim)]">·</span>
              <span>{totalPages} 页</span>
              <span className="text-[var(--ui-text-dim)]">·</span>
              <span>{base.chunks} 段</span>
              {totalImages > 0 && (
                <>
                  <span className="text-[var(--ui-text-dim)]">·</span>
                  <span>{totalImages} 图</span>
                </>
              )}
            </>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {turns.length > 0 && (
            <button
              type="button"
              onClick={onNewSession}
              title="清空当前对话，开始新的会话"
              className="flex items-center gap-1 rounded-md px-2 py-1 text-[12px] text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
            >
              <PlusIcon className="size-3.5" />
              新对话
            </button>
          )}
          <button
            type="button"
            disabled={!base}
            onClick={onOpenImport}
            className="flex items-center gap-1 rounded-md border border-[var(--ui-border)] px-2 py-1 text-[12px] text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)] disabled:opacity-40"
          >
            <UploadSimpleIcon className="size-3.5" />
            上传 PDF
          </button>
          <button
            type="button"
            disabled
            title="重建索引在阶段 ③ 提供"
            className="flex items-center gap-1 rounded-md border border-[var(--ui-border)] px-2 py-1 text-[12px] text-[var(--ui-text-dim)] opacity-50"
          >
            <ArrowsClockwiseIcon className="size-3.5" />
            重建索引
          </button>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        <div className="mx-auto flex max-w-2xl flex-col gap-6">
          {turns.length === 0 && (
            <div className="rounded-lg border border-dashed border-[var(--ui-border)] px-4 py-6 text-[12.5px] leading-relaxed text-[var(--ui-text-muted)]">
              {base
                ? "提一个问题，我会基于这个库里的资料回答，并在句子后面标出处。点编号可以看原文。答不好我会明确告诉你，而不是编。"
                : "先在左侧新建一个知识库，然后上传 PDF。"}
            </div>
          )}

          {turns.map((turn) =>
            turn.role === "user" ? (
              <div key={turn.id} className="flex justify-end">
                <div className="max-w-[75%] rounded-[12px] rounded-br-[4px] bg-[var(--ui-panel)] px-3 py-2 text-[13px] leading-relaxed text-[var(--ui-text)]">
                  {turn.image && (
                    <img
                      src={turn.image}
                      alt="查询图片"
                      className="mb-1.5 max-h-40 rounded border border-[var(--ui-border)] object-contain"
                    />
                  )}
                  {turn.text || (turn.image ? "" : "[图片]")}
                </div>
              </div>
            ) : (
              <div key={turn.id} className="space-y-2.5">
                {turn.error ? (
                  <p className="text-[12.5px] text-[var(--ui-danger)]">生成失败：{turn.error}</p>
                ) : (
                  <>
                    {turn.webUsed && (
                      <p className="flex items-center gap-1 text-[11px] text-[var(--ui-text-dim)]">
                        <GlobeIcon className="size-3" />
                        这次回答用了联网搜索的结果，不只有库里的资料
                      </p>
                    )}
                    {turn.answer && (
                      <p className="text-[13.5px] leading-[1.9] whitespace-pre-wrap text-[var(--ui-text)]">
                        {renderAnswer(turn.answer, turn.hits ?? [], onSelectHit)}
                      </p>
                    )}

                    {turn.needsReview && (
                      <div className="rounded-md border-l-2 border-[var(--ui-warning)] py-1.5 pl-3 pr-2">
                        <p className="text-[12px] leading-relaxed text-[var(--ui-text)]">
                          {turn.reviewReason === "insufficient"
                            ? "这个知识库里没有直接对应的内容，上面说的是资料里的边界。要不要换联网搜索再答一次？"
                            : `这个回答可能不够可靠（自评 ${turn.score?.toFixed(2) ?? "-"}，低于 ${
                                turn.threshold ?? 0.7
                              }）。`}
                        </p>
                        {turn.reason && (
                          <p className="mt-1 text-[11px] leading-relaxed text-[var(--ui-text-dim)]">
                            判断依据：{turn.reason}
                          </p>
                        )}
                        <div className="mt-2 flex items-center gap-1.5">
                          <button
                            type="button"
                            onClick={() => turn.query && onRetryWeb(turn.query)}
                            className="flex items-center gap-1 rounded border border-[var(--ui-border)] bg-[var(--ui-surface)] px-2 py-1 text-[11.5px] text-[var(--ui-text)] transition-colors hover:bg-[var(--ui-sidebar-hover)]"
                          >
                            <GlobeIcon className="size-3" />
                            换联网搜索
                          </button>
                          <button
                            type="button"
                            onClick={() => onDismissReview(turn.id)}
                            className="rounded border border-transparent px-2 py-1 text-[11.5px] text-[var(--ui-text-muted)] transition-colors hover:text-[var(--ui-text)]"
                          >
                            就这样
                          </button>
                        </div>
                      </div>
                    )}

                    {turn.webError && (
                      <p className="text-[11px] leading-relaxed text-[var(--ui-warning)]">
                        联网搜索没成功：{turn.webError}。上面这份仍然只用了库里的资料。
                      </p>
                    )}

                    {turn.hits && turn.hits.length > 0 && (
                      <details className="group/refs">
                        <summary className="flex cursor-pointer items-center gap-1.5 text-[11.5px] text-[var(--ui-text-dim)] transition-colors hover:text-[var(--ui-text-muted)]">
                          <span>
                            {turn.hits.length} 段参考
                            {turn.score != null && (
                              <span
                                className="ml-1"
                                style={{ color: scoreTone(turn.score, turn.threshold ?? 0.7) }}
                              >
                                · 自评 {turn.score.toFixed(2)}
                              </span>
                            )}
                            {turn.coverage && turn.coverage !== "unknown" && (
                              <span className="ml-1">
                                ·{" "}
                                {turn.coverage === "full"
                                  ? "资料覆盖完整"
                                  : turn.coverage === "partial"
                                    ? "资料只覆盖一部分"
                                    : "资料未覆盖"}
                              </span>
                            )}
                          </span>
                        </summary>
                        <div className="mt-2 space-y-1.5">
                          {turn.hits.map((hit, index) => (
                            <button
                              key={hitKey(hit)}
                              type="button"
                              onClick={() => onSelectHit(hit)}
                              className={cn(
                                "w-full rounded-lg border px-2.5 py-2 text-left transition-colors",
                                activeHitKey === hitKey(hit)
                                  ? "border-[var(--ui-text-muted)]"
                                  : "border-[var(--ui-border)] hover:border-[var(--ui-text-dim)]",
                              )}
                            >
                              <div className="flex items-center gap-1.5 text-[11px] text-[var(--ui-text-dim)]">
                                <span>[{index + 1}]</span>
                                {hit.category === "image" && (
                                  <ImageIcon className="size-3 shrink-0" />
                                )}
                                <span className="truncate text-[var(--ui-text-muted)]">
                                  {hit.title || hit.doc_title}
                                </span>
                                <span>p{hit.page}</span>
                              </div>
                              {hit.category === "image" && hit.image_path && (
                                <img
                                  src={knowledgeApi.imageUrl(hit.doc_id, hit.image_path)}
                                  alt=""
                                  loading="lazy"
                                  className="mt-1.5 max-h-36 w-full rounded border border-[var(--ui-border)] object-contain"
                                />
                              )}
                              <p className="mt-1 line-clamp-3 text-[12px] leading-[1.7] text-[var(--ui-text)]">
                                {hit.text}
                              </p>
                            </button>
                          ))}
                        </div>
                      </details>
                    )}
                  </>
                )}
              </div>
            ),
          )}

          {pending && (
            <div className="flex items-center gap-2 text-[12px] text-[var(--ui-text-muted)]">
              <span className="size-1.5 animate-pulse rounded-full bg-[var(--ui-warning-vivid)]" />
              正在检索并生成回答…
            </div>
          )}
        </div>
      </div>

      <div className="border-t border-[var(--ui-border)] px-6 py-3">
        <div className="mx-auto max-w-2xl">
          {draftImage && (
            <div className="mb-1.5 flex items-center gap-2">
              <div className="relative">
                <img
                  src={draftImage}
                  alt="待发送图片"
                  className="h-14 rounded border border-[var(--ui-border)] object-contain"
                />
                <button
                  type="button"
                  aria-label="移除图片"
                  onClick={() => setDraftImage(null)}
                  className="absolute -top-1.5 -right-1.5 flex size-4 items-center justify-center rounded-full bg-[var(--ui-text-muted)] text-[10px] text-[var(--ui-bg)] hover:bg-[var(--ui-danger-vivid)]"
                >
                  ×
                </button>
              </div>
              <span className="text-[11px] text-[var(--ui-text-dim)]">
                将按这张图去库里找相似内容
              </span>
            </div>
          )}
          <div className="flex items-end gap-2 rounded-lg border border-[var(--ui-border)] bg-[var(--ui-surface)] px-2.5 py-2">
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(event) => {
                pickImage(event.target.files?.[0])
                event.target.value = ""
              }}
            />
            <button
              type="button"
              aria-label="上传图片检索"
              title="上传一张图片，按图找相似内容"
              disabled={!base || pending}
              onClick={() => fileRef.current?.click()}
              className="flex size-6 shrink-0 items-center justify-center rounded text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)] disabled:opacity-40"
            >
              <ImageIcon className="size-4" />
            </button>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault()
                  submit()
                }
              }}
              onPaste={(event) => {
                // 截图直接 Ctrl+V 是最顺手的入口，优先于文字粘贴处理
                const file = Array.from(event.clipboardData.files).find((item) =>
                  item.type.startsWith("image/"),
                )
                if (file) {
                  event.preventDefault()
                  pickImage(file)
                }
              }}
              onDrop={(event) => {
                event.preventDefault()
                pickImage(event.dataTransfer.files?.[0])
              }}
              onDragOver={(event) => event.preventDefault()}
              rows={1}
              disabled={!base}
              placeholder={base ? "就这个库里的资料提问，也可以粘贴/拖入一张图" : "先新建一个知识库"}
              className="max-h-32 min-h-[22px] flex-1 resize-none bg-transparent text-[13px] leading-relaxed text-[var(--ui-text)] outline-none placeholder:text-[var(--ui-text-dim)] disabled:cursor-not-allowed"
            />
            <button
              type="button"
              aria-label="提问"
              onClick={submit}
              disabled={(!draft.trim() && !draftImage) || pending || !base}
              className={cn(
                "flex size-6 shrink-0 items-center justify-center rounded transition-colors",
                (draft.trim() || draftImage) && !pending && base
                  ? "bg-[var(--ui-accent)] text-[var(--ui-bg)]"
                  : "bg-[var(--ui-panel-2)] text-[var(--ui-text-dim)]",
              )}
            >
              <ArrowUpIcon className="size-3.5" />
            </button>
          </div>
        </div>
      </div>
    </section>
  )
}
