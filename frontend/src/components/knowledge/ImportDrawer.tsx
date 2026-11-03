import { XIcon } from "@phosphor-icons/react"
import { useRef } from "react"

import type { ImportProgress, KnowledgeDoc } from "@/lib/knowledge/types"
import { cn } from "@/lib/utils"

/**
 * 导入抽屉。
 *
 * 导入是**后台任务**：上传接口立刻返回，解析在服务端跑，前端靠轮询进度跟进。
 * 一本书要几十秒（每张插图要一次视觉描述 + 一次向量），所以这里显示的是真实
 * 的阶段和计数，不是假进度条。
 */

interface ImportDrawerProps {
  open: boolean
  baseName?: string
  busy: boolean
  result: KnowledgeDoc | null
  progress: ImportProgress | null
  error: string | null
  onClose: () => void
  onUpload: (file: File) => void
  className?: string
}

export function ImportDrawer({
  open,
  baseName,
  busy,
  result,
  progress,
  error,
  onClose,
  onUpload,
  className,
}: ImportDrawerProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  if (!open) return null

  const pick = () => inputRef.current?.click()
  const ratio =
    progress && progress.total > 0 ? Math.min(1, progress.done / progress.total) : 0

  return (
    <>
      <div className="absolute inset-0 z-10 bg-black/10" onClick={busy ? undefined : onClose} aria-hidden />
      <aside
        className={cn(
          "absolute inset-y-0 right-0 z-20 flex w-[420px] flex-col border-l border-[var(--ui-border)] bg-[var(--ui-bg)]",
          className,
        )}
      >
        <header className="flex items-start justify-between border-b border-[var(--ui-border)] px-4 py-3">
          <div>
            <p className="text-[13px] font-medium text-[var(--ui-text)]">
              {result ? "导入完成" : busy ? "正在解析" : "导入 PDF"}
            </p>
            <p className="mt-0.5 text-[11.5px] text-[var(--ui-text-muted)]">
              {baseName ? `导入到「${baseName}」` : "先选择知识库"}
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭"
            disabled={busy}
            onClick={onClose}
            className="flex size-6 items-center justify-center rounded text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)] disabled:opacity-40"
          >
            <XIcon className="size-3.5" />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">
          <input
            ref={inputRef}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) onUpload(file)
              event.target.value = ""
            }}
          />

          {!busy && !result && (
            <button
              type="button"
              onClick={pick}
              className="flex w-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-[var(--ui-border)] px-4 py-10 text-[12.5px] text-[var(--ui-text-muted)] transition-colors hover:border-[var(--ui-text-dim)] hover:text-[var(--ui-text)]"
            >
              <span>选择 PDF 文件</span>
              <span className="text-[11px] text-[var(--ui-text-dim)]">
                目前只支持有文字层的 PDF；扫描件要等阶段 ③ 的版面识别
              </span>
            </button>
          )}

          {busy && (
            <div className="space-y-2.5">
              <div className="flex items-center gap-2 text-[12.5px] text-[var(--ui-text)]">
                <span className="size-1.5 animate-pulse rounded-full bg-[var(--ui-warning-vivid)]" />
                {progress?.message || "正在上传并开始解析…"}
              </div>
              {progress && progress.total > 0 && (
                <>
                  <div className="h-1 overflow-hidden rounded-full bg-[var(--ui-border)]">
                    <div
                      className="h-full rounded-full bg-[var(--ui-accent)] transition-[width] duration-300"
                      style={{ width: `${Math.round(ratio * 100)}%` }}
                    />
                  </div>
                  <p className="text-[11px] leading-relaxed text-[var(--ui-text-dim)]">
                    {progress.stage === "page"
                      ? "正在逐页做版面识别（每页约 3-8 秒），这一步决定正文质量。"
                      : progress.stage === "image"
                        ? "每张插图要生成中文描述并算向量，这一步最慢。"
                        : "正在切分正文并生成向量。"}
                  </p>
                </>
              )}
            </div>
          )}

          {result && (
            <div className="space-y-3">
              <div className="rounded-md border border-[var(--ui-border)] bg-[var(--ui-surface)] px-3 py-2">
                <p className="truncate text-[12.5px] text-[var(--ui-text)]">{result.title}</p>
                <p className="mt-0.5 text-[11px] text-[var(--ui-text-dim)]">
                  {result.filename} · {result.status === "indexed" ? "已入库" : result.status}
                </p>
              </div>
              <div className="flex flex-wrap gap-5">
                <Stat label="页数" value={String(result.pages)} />
                <Stat label="段落" value={String(result.paragraphs)} />
                <Stat label="插图" value={String(result.images)} />
                <Stat label="有正文的页" value={String(result.text_pages)} />
                <Stat label="没取到正文" value={String(result.vision_pages)} />
              </div>
              {result.images > 0 && (
                <p className="text-[11px] leading-relaxed text-[var(--ui-text-dim)]">
                  {result.images} 张插图已经入库：每张都配了中文描述和向量，可以用文字搜到它们。
                </p>
              )}
              {result.vision_pages > 0 && (
                <p className="text-[11px] leading-relaxed text-[var(--ui-warning)]">
                  有 {result.vision_pages} 页没取到正文（版面识别失败，或这页本来就没有文字），
                  这部分内容现在检索不到。整本重新导入一次通常能好。
                </p>
              )}
              <button
                type="button"
                onClick={pick}
                className="rounded-md border border-[var(--ui-border)] px-2 py-1 text-[11.5px] text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
              >
                再导入一个
              </button>
            </div>
          )}

          {error && (
            <div className="rounded-md border border-[var(--ui-danger)] px-3 py-2 text-[12px] text-[var(--ui-danger)]">
              {error}
            </div>
          )}
        </div>

        <footer className="flex items-center justify-between border-t border-[var(--ui-border)] px-4 py-2.5">
          <span className="text-[11.5px] text-[var(--ui-text-dim)]">
            解析完自动切片入库，不用逐本确认
          </span>
          <button
            type="button"
            disabled={busy}
            onClick={onClose}
            className="rounded-md border border-[var(--ui-border)] px-2 py-1 text-[11.5px] text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)] disabled:opacity-40"
          >
            {result ? "完成" : "关闭"}
          </button>
        </footer>
      </aside>
    </>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[10.5px] text-[var(--ui-text-dim)]">{label}</p>
      <p className="text-[16px] font-medium text-[var(--ui-text)]">{value}</p>
    </div>
  )
}
