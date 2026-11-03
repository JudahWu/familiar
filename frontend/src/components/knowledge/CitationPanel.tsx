import { ImageIcon, XIcon } from "@phosphor-icons/react"

import type { SearchHit } from "@/lib/knowledge/types"
import { knowledgeApi } from "@/lib/knowledge/api"
import { cn } from "@/lib/utils"

/**
 * 命中条目详情（右侧抽屉）。
 *
 * 点开一条检索结果，最想知道的是"原文到底怎么写的"。文本段落放出处、页码、
 * 相关度和完整正文；插图条目额外展示图片本身——多模态检索的价值就在这，
 * 只给一句文字描述等于把图藏起来了。
 */

interface CitationPanelProps {
  hit: SearchHit
  onClose: () => void
  className?: string
}

export function CitationPanel({ hit, onClose, className }: CitationPanelProps) {
  const isImage = hit.category === "image"
  return (
    <aside
      className={cn(
        "flex w-[304px] shrink-0 flex-col border-l border-[var(--ui-border)] bg-[var(--ui-sidebar)]",
        className,
      )}
    >
      <header className="flex items-center justify-between border-b border-[var(--ui-border)] px-3 py-2.5">
        <div className="flex min-w-0 items-center gap-1.5 text-[12.5px] text-[var(--ui-text)]">
          {isImage && <ImageIcon className="size-3.5 shrink-0" />}
          <span className="truncate">{hit.title || hit.doc_title}</span>
        </div>
        <button
          type="button"
          aria-label="关闭引用"
          onClick={onClose}
          className="flex size-6 items-center justify-center rounded text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
        >
          <XIcon className="size-3.5" />
        </button>
      </header>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3">
        <div className="space-y-1 text-[11.5px] text-[var(--ui-text-muted)]">
          <p className="truncate">{hit.doc_title}</p>
          <p className="text-[var(--ui-text-dim)]">
            第 {hit.page} 页 · {isImage ? "插图" : `第 ${hit.chunk_index + 1} 段`} · 相关度{" "}
            {hit.score.toFixed(2)}
          </p>
        </div>

        {isImage && hit.image_path && (
          <div className="rounded-md border border-[var(--ui-border)] bg-[var(--ui-surface)] p-1.5">
            <img
              src={knowledgeApi.imageUrl(hit.doc_id, hit.image_path)}
              alt={hit.title || "插图"}
              className="w-full rounded object-contain"
            />
          </div>
        )}

        <div className="rounded-md border border-[var(--ui-border)] bg-[var(--ui-surface)] px-2.5 py-2">
          <p className="text-[12px] leading-[1.8] whitespace-pre-wrap text-[var(--ui-text)]">
            {hit.text}
          </p>
        </div>

        <p className="text-[11px] leading-relaxed text-[var(--ui-text-dim)]">
          {isImage
            ? "这段文字是视觉模型对插图写的中文描述（画面内容 + 结合正文推断的用途），用来让这张图能被文字搜到。"
            : "这是从 PDF 抽出来的原始段落，未做改写。阶段 ④ 会让模型基于这些段落生成回答，届时这里的正文会作为引用出处挂到答案下方。"}
        </p>
      </div>
    </aside>
  )
}
