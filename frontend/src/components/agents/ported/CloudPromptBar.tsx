import {
  ArrowUp,
  ChevronDown,
  FileTextIcon,
  LoaderCircle,
  Plus,
  Square,
  X,
} from "lucide-react"
import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react"

import type { ModelOption } from "@/lib/api"
import type { ImageChunk } from "@/lib/agents/types"
import type { ModelSelection } from "@/lib/agents/useModelOptions"
import { RepoSelector } from "@/components/agents/RepoSelector"
import { formatModelSelection } from "@/lib/agents/useModelOptions"
import { normalizeRepoFullName } from "@/lib/repo"
import { cn } from "@/lib/utils"

const PROMPT_TEXTAREA_MAX_HEIGHT = 200
const MAX_IMAGE_COUNT = 5
const MAX_IMAGE_BYTES = 10 * 1024 * 1024
const MAX_FILE_COUNT = 5
const MAX_FILE_BYTES = 20 * 1024 * 1024
const MAX_INLINE_TEXT_BYTES = 50 * 1024
const SUPPORTED_IMAGE_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
])

interface PendingFile {
  name: string
  size: number
  mimeType: string
  /** text：内容会内联进 prompt；unsupported：二进制文件，base64 上传到工作区。 */
  kind: "text" | "unsupported"
  text?: string
  base64?: string
}

const TEXT_FILE_EXTENSIONS = new Set([
  "txt", "md", "markdown", "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg", "conf",
  "py", "js", "jsx", "ts", "tsx", "mjs", "cjs", "java", "go", "rs", "rb", "php",
  "c", "h", "cpp", "hpp", "cs", "swift", "kt", "kts", "scala",
  "sh", "bash", "zsh", "fish", "ps1", "bat", "cmd", "sql", "graphql",
  "html", "htm", "css", "scss", "less", "vue", "svelte", "xml", "csv", "tsv",
  "log", "gitignore", "dockerfile", "makefile",
])

const TEXT_MIME_PREFIXES = [
  "text/",
  "application/json",
  "application/xml",
  "application/yaml",
  "application/x-yaml",
  "application/javascript",
  "application/typescript",
  "application/x-sh",
]

function isTextFile(file: File): boolean {
  if (file.type && TEXT_MIME_PREFIXES.some((prefix) => file.type.startsWith(prefix))) return true
  const ext = file.name.split(".").pop()?.toLowerCase() ?? ""
  return TEXT_FILE_EXTENSIONS.has(ext)
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer())
  let binary = ""
  const chunkSize = 0x8000
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize))
  }
  return btoa(binary)
}

async function fileToPendingFile(file: File): Promise<PendingFile | null> {
  if (file.size > MAX_FILE_BYTES) return null
  if (!isTextFile(file)) {
    return {
      name: file.name,
      size: file.size,
      mimeType: file.type || "application/octet-stream",
      kind: "unsupported",
      base64: await fileToBase64(file),
    }
  }
  let text = await file.text()
  if (text.length > MAX_INLINE_TEXT_BYTES) {
    text = `${text.slice(0, MAX_INLINE_TEXT_BYTES)}\n…（文件过大，已截断）`
  }
  return { name: file.name, size: file.size, mimeType: file.type || "text/plain", kind: "text", text }
}

export interface CloudPromptBarProps {
  placeholder?: string
  compact?: boolean
  disabled?: boolean
  busy?: boolean
  /** Called to stop the running agent. When set, the send button becomes a stop button while busy and the input is empty. */
  onStop?: () => void
  stopping?: boolean
  models?: Array<ModelOption>
  selection?: ModelSelection | null
  onSelectionChange?: (next: ModelSelection) => void
  /** Repos the user can target. When provided with onRepoChange, a repo picker is shown. */
  repos?: Array<{ full_name: string }>
  selectedRepo?: string | null
  onRepoChange?: (repo: string | null) => void
  /** 上下文窗口占用；有数据时在模型旁渲染环形图，没有则不显示。 */
  contextUsage?: { used: number; total: number } | null
  /** 提交回调：正文、图片、以及需要上传到工作区的二进制文件。 */
  onSubmit?: (
    value: string,
    images: Array<ImageChunk>,
    files?: Array<{ name: string; mimeType: string; size: number; base64: string }>
  ) => void
}

function fileToImageChunk(file: File): Promise<ImageChunk | null> {
  if (!SUPPORTED_IMAGE_TYPES.has(file.type) || file.size > MAX_IMAGE_BYTES) {
    return Promise.resolve(null)
  }

  return new Promise((resolve) => {
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = typeof reader.result === "string" ? reader.result : ""
      const base64 = dataUrl.split(",")[1]
      resolve(
        base64
          ? {
              kind: "image",
              base64,
              mimeType: file.type,
              fileName: file.name,
            }
          : null
      )
    }
    reader.onerror = () => resolve(null)
    reader.readAsDataURL(file)
  })
}

/** Web-adapted PromptBar from open-swe-app — local state, no Electron/Zustand deps. */
export const CloudPromptBar = memo(function CloudPromptBarComponent({
  placeholder = "让 Familiar 构建、修复或检查一个 代码 仓库",
  compact = false,
  disabled = false,
  busy = false,
  onSubmit,
  onStop,
  stopping = false,
  models = [],
  selection = null,
  onSelectionChange,
  repos,
  selectedRepo = null,
  onRepoChange,
  contextUsage = null,
}: CloudPromptBarProps) {
  const [value, setValue] = useState("")
  const [pendingImages, setPendingImages] = useState<Array<ImageChunk>>([])
  const [pendingFiles, setPendingFiles] = useState<Array<PendingFile>>([])
  const [isDragOver, setIsDragOver] = useState(false)
  const [modelDropdownOpen, setModelDropdownOpen] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const dragDepthRef = useRef(0)
  const modelDropdownRef = useRef<HTMLDivElement>(null)

  const combos = useMemo<Array<ModelSelection>>(() => {
    const list: Array<ModelSelection> = []
    for (const model of models) {
      for (const effort of model.efforts) {
        list.push({ modelId: model.id, effort })
      }
    }
    return list
  }, [models])

  const selectionLabel = formatModelSelection(models, selection)

  const canSubmit =
    !disabled &&
    (value.trim().length > 0 ||
      pendingImages.length > 0 ||
      pendingFiles.some((file) => file.kind === "text"))

  const handleSubmit = useCallback(() => {
    const trimmed = value.trim()
    if (!canSubmit) return
    const attachments = pendingFiles
      .filter((file) => file.kind === "text" && file.text)
      .map((file) => `\n\n【附件：${file.name}】\n\`\`\`\n${file.text}\n\`\`\``)
      .join("")
    const uploads = pendingFiles
      .filter((file) => file.kind === "unsupported" && file.base64)
      .map((file) => ({
        name: file.name,
        mimeType: file.mimeType,
        size: file.size,
        base64: file.base64 as string,
      }))
    onSubmit?.(
      attachments ? `${trimmed}${attachments}` : trimmed,
      pendingImages,
      uploads.length > 0 ? uploads : undefined
    )
    setValue("")
    setPendingImages([])
    setPendingFiles([])
  }, [canSubmit, onSubmit, pendingFiles, pendingImages, value])

  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return

    el.style.height = "auto"
    const clampedHeight = Math.min(el.scrollHeight, PROMPT_TEXTAREA_MAX_HEIGHT)
    el.style.height = `${clampedHeight}px`
    el.style.overflowY =
      el.scrollHeight > PROMPT_TEXTAREA_MAX_HEIGHT ? "auto" : "hidden"
  }, [value])

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      const target = e.target as Node
      if (
        modelDropdownRef.current &&
        !modelDropdownRef.current.contains(target)
      ) {
        setModelDropdownOpen(false)
      }
    }
    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  const addFiles = useCallback(async (files: FileList | Array<File>) => {
    const incoming = Array.from(files)
    const images: Array<File> = []
    const others: Array<File> = []
    for (const file of incoming) {
      if (file.type.startsWith("image/")) images.push(file)
      else others.push(file)
    }
    if (images.length > 0) {
      const nextImages = await Promise.all(images.map(fileToImageChunk))
      const validImages = nextImages.filter(
        (image): image is ImageChunk => image !== null
      )
      if (validImages.length > 0) {
        setPendingImages((prev) =>
          [...prev, ...validImages].slice(0, MAX_IMAGE_COUNT)
        )
      }
    }
    if (others.length > 0) {
      const nextFiles = await Promise.all(others.map(fileToPendingFile))
      const validFiles = nextFiles.filter(
        (file): file is PendingFile => file !== null
      )
      if (validFiles.length > 0) {
        setPendingFiles((prev) =>
          [...prev, ...validFiles].slice(0, MAX_FILE_COUNT)
        )
      }
    }
  }, [])

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = e.target.files
      if (files) void addFiles(files)
      e.target.value = ""
    },
    [addFiles]
  )

  const handleDragEnter = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    if (!e.dataTransfer.types.includes("Files")) return
    e.preventDefault()
    dragDepthRef.current += 1
    setIsDragOver(true)
  }, [])

  const handleDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    if (!e.dataTransfer.types.includes("Files")) return
    e.preventDefault()
    setIsDragOver(true)
  }, [])

  const handleDragLeave = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    if (!e.dataTransfer.types.includes("Files")) return
    e.preventDefault()
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1)
    if (dragDepthRef.current === 0) setIsDragOver(false)
  }, [])

  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      if (!e.dataTransfer.types.includes("Files")) return
      e.preventDefault()
      dragDepthRef.current = 0
      setIsDragOver(false)
      void addFiles(e.dataTransfer.files)
    },
    [addFiles]
  )

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && canSubmit) {
      e.preventDefault()
      handleSubmit()
    }
  }

  const pickerDisabled = combos.length === 0 || !onSelectionChange
  const showStop =
    busy && !disabled && !value.trim() && pendingImages.length === 0 && !!onStop

  // 仓库芯片显示 owner/repo 短名，完整 URL 只在设置页出现。
  const repoChipLabel = selectedRepo
    ? (normalizeRepoFullName(selectedRepo) ?? selectedRepo)
    : "未绑定仓库"

  const contextRing = (() => {    if (!contextUsage || contextUsage.total <= 0) return null
    const ratio = Math.min(1, Math.max(0, contextUsage.used / contextUsage.total))
    const percent = Math.round(ratio * 100)
    const circumference = 2 * Math.PI * 6
    const stroke =
      ratio >= 0.85
        ? "var(--ui-danger)"
        : ratio >= 0.6
          ? "#BA7517"
          : "var(--ui-text-dim)"
    return (
      <span
        className="flex shrink-0 items-center gap-1"
        title={`上下文 ${percent}% · 约 ${Math.round(contextUsage.used / 1000)}k / ${Math.round(contextUsage.total / 1000)}k tokens`}
      >
        <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden>
          <circle cx="8" cy="8" r="6" fill="none" stroke="var(--ui-border)" strokeWidth="2" />
          <circle
            cx="8"
            cy="8"
            r="6"
            fill="none"
            stroke={stroke}
            strokeWidth="2"
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - ratio)}
            transform="rotate(-90 8 8)"
          />
        </svg>
        <span className="text-[11px] text-[color:var(--ui-text-dim)]">{percent}%</span>
      </span>
    )
  })()

  return (
    <div
      className={cn(
        "relative w-full font-sans text-[13px]",
        compact ? "max-w-none" : "max-w-2xl"
      )}
    >
      <div
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className={cn(
          "relative flex min-h-[106px] flex-col rounded-2xl border border-[var(--ui-border)] bg-[var(--ui-surface)] px-3 py-2.5 shadow-sm",
          compact && "min-h-[88px]",
          isDragOver && "border-[var(--ui-accent)]"
        )}
      >
        {isDragOver && (
          <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center rounded-2xl bg-[var(--ui-surface)]/80 backdrop-blur-sm">
            <span className="rounded-md bg-[var(--ui-panel-2)] px-3 py-1.5 text-sm font-medium text-[color:var(--ui-accent)]">
              松开鼠标上传文件
            </span>
          </div>
        )}

        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={handleFileChange}
        />

        {pendingImages.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {pendingImages.map((image, index) => (
              <div
                key={`${image.fileName ?? "image"}-${index}`}
                className="group relative"
              >
                <img
                  src={`data:${image.mimeType};base64,${image.base64}`}
                  alt={image.fileName || "pending image"}
                  className="size-16 rounded-lg border border-[var(--ui-border)] object-cover"
                />
                <button
                  type="button"
                  aria-label="移除图片"
                  onClick={() =>
                    setPendingImages((prev) =>
                      prev.filter((_, i) => i !== index)
                    )
                  }
                  className="absolute -top-1.5 -right-1.5 flex size-5 items-center justify-center rounded-full border border-[var(--ui-border)] bg-[var(--ui-panel-2)] text-[color:var(--ui-text-muted)] opacity-0 shadow-sm transition-opacity group-hover:opacity-100 hover:text-[color:var(--ui-text)]"
                >
                  <X className="size-3" />
                </button>
              </div>
            ))}
          </div>
        )}

        {pendingFiles.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {pendingFiles.map((file, index) => (
              <span
                key={`${file.name}-${index}`}
                title={
                  file.kind === "text"
                    ? `${file.name} · ${formatFileSize(file.size)} · 内容将随消息发送`
                    : `${file.name} · ${formatFileSize(file.size)} · 将上传到工作区，Agent 可读取`
                }
                className="group inline-flex max-w-[220px] items-center gap-1.5 rounded-md border border-[var(--ui-border)] bg-[var(--ui-panel)] py-1 pr-1.5 pl-2 text-xs text-[color:var(--ui-text)]"
              >
                <FileTextIcon className="size-3.5 shrink-0 text-[color:var(--ui-text-muted)]" />
                <span className="min-w-0 flex-1 truncate">{file.name}</span>
                <span className="shrink-0 text-[11px] text-[color:var(--ui-text-dim)]">
                  {formatFileSize(file.size)}
                </span>
                {file.kind === "unsupported" && (
                  <span className="shrink-0 rounded bg-[var(--ui-panel-2)] px-1 text-[11px] text-[color:var(--ui-text-dim)]">
                    待上传
                  </span>
                )}                <button
                  type="button"
                  aria-label={`移除 ${file.name}`}
                  onClick={() =>
                    setPendingFiles((prev) => prev.filter((_, i) => i !== index))
                  }
                  className="flex size-4 shrink-0 items-center justify-center rounded text-[color:var(--ui-text-dim)] transition-colors hover:text-[color:var(--ui-text)]"
                >
                  <X className="size-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        <textarea
          ref={inputRef}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={busy ? "Send a message to queue next..." : placeholder}
          disabled={disabled}
          className={cn(
            "w-full min-w-0 resize-none overflow-hidden bg-transparent text-[13px] leading-[1.45] text-[color:var(--ui-text)] outline-none placeholder:text-[color:var(--ui-text-dim)]",
            compact ? "min-h-[36px]" : "min-h-[52px]"
          )}
          style={{ maxHeight: PROMPT_TEXTAREA_MAX_HEIGHT }}
        />

        <div className="mt-auto flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 pt-2 text-xs text-[color:var(--ui-text-dim)]">
          {onRepoChange && (
            <div className="relative min-w-0 shrink-0">
              <RepoSelector
                repos={repos}
                selectedRepo={selectedRepo}
                onRepoChange={onRepoChange}
                disabled={disabled}
                label={repoChipLabel}
                placeholder="未指定仓库"
                emptySelectionLabel="使用后端默认仓库"
                triggerClassName="max-w-[190px] gap-1 rounded-md bg-[var(--ui-panel)] px-2 py-1 text-[11px] text-[color:var(--ui-text)]"
                dropdownClassName="bottom-full top-auto mb-1"
              />
            </div>
          )}
          <div ref={modelDropdownRef} className="relative min-w-0 shrink">
            <button
              type="button"
              disabled={pickerDisabled}
              onClick={() => setModelDropdownOpen((open) => !open)}
              className="flex max-w-[220px] cursor-pointer items-center gap-0.5 text-[13px] text-[color:var(--ui-text-muted)] transition-opacity hover:opacity-80 disabled:cursor-default disabled:opacity-60"
            >
              <span className="truncate">{selectionLabel}</span>
              {!pickerDisabled && (
                <ChevronDown className="size-3.5 shrink-0 opacity-60" />
              )}
            </button>
            {modelDropdownOpen && combos.length > 0 && (
              <div className="absolute bottom-full left-0 z-50 mb-1 max-h-72 overflow-hidden overflow-y-auto rounded border border-[var(--ui-border)] bg-[var(--ui-surface)] shadow-lg">
                {combos.map((combo) => {
                  const selected =
                    !!selection &&
                    selection.modelId === combo.modelId &&
                    selection.effort === combo.effort
                  return (
                    <button
                      key={`${combo.modelId}::${combo.effort}`}
                      type="button"
                      onClick={() => {
                        onSelectionChange?.(combo)
                        setModelDropdownOpen(false)
                      }}
                      className={cn(
                        "flex w-full items-center gap-2 px-3 py-1.5 text-left whitespace-nowrap transition-colors hover:bg-[var(--ui-panel-2)]",
                        selected
                          ? "text-[color:var(--ui-text)]"
                          : "text-[color:var(--ui-text-muted)]"
                      )}
                    >
                      {formatModelSelection(models, combo)}
                      {selected && (
                        <span className="ml-auto pl-3 text-[color:var(--ui-text-dim)]">
                          ✓
                        </span>
                      )}
                    </button>
                  )
                })}
              </div>
            )}
          </div>

          {contextRing}

          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={
              disabled ||
              pendingImages.length + pendingFiles.length >=
                MAX_IMAGE_COUNT + MAX_FILE_COUNT
            }
            aria-label="上传文件"
            title="上传文件（图片或任意类型文件）"
            className="ml-auto flex size-7 shrink-0 items-center justify-center rounded-full text-[color:var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-panel-2)] hover:text-[color:var(--ui-text)] disabled:cursor-default disabled:opacity-40"
          >
            <Plus className="size-4" />
          </button>

          {showStop ? (
            <button
              type="button"
              onClick={onStop}
              disabled={stopping}
              aria-label="Stop run"
              className="flex size-7 shrink-0 items-center justify-center rounded-full bg-[var(--ui-accent)] text-white transition-opacity hover:opacity-90 disabled:cursor-default disabled:opacity-40"
            >
              {stopping ? (
                <LoaderCircle className="size-3.5 animate-spin" />
              ) : (
                <Square className="size-3 fill-current" strokeWidth={0} />
              )}
            </button>
          ) : (
            <button
              type="button"
              onClick={handleSubmit}
              disabled={!canSubmit}
              aria-label="Send message"
              className="flex size-7 shrink-0 items-center justify-center rounded-full bg-[var(--ui-accent)] text-white transition-opacity hover:opacity-90 disabled:cursor-default disabled:opacity-40"
            >
              {disabled ? (
                <LoaderCircle className="size-3.5 animate-spin" />
              ) : (
                <ArrowUp className="size-3.5" strokeWidth={2.5} />
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  )
})
