import { TrashIcon } from "@phosphor-icons/react"
import { useEffect } from "react"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * 确认弹窗。
 *
 * 替代 `window.confirm`：原生的那个完全脱离主题，样式是浏览器自带的，
 * 弹在视口顶部而不是内容中间，跟应用的气质差太远。
 *
 * 几个刻意的选择：
 * - 危险操作默认聚焦「取消」，回车不会直接删掉；
 * - Esc 取消、点遮罩取消，符合桌面端的肌肉记忆；
 * - 确认按钮支持 loading 态，避免连点两次发两次请求。
 */

export interface ConfirmDialogProps {
  open: boolean
  title: string
  /** 主描述，说明发生了什么。 */
  description?: React.ReactNode
  /** 补充说明，渲染成一块弱化底色，用来交代"会连带删掉什么"。 */
  hint?: React.ReactNode
  confirmText?: string
  cancelText?: string
  /** 危险操作：图标和确认按钮用红色。 */
  destructive?: boolean
  /** 请求进行中：禁用两个按钮并把确认文案换成 loading。 */
  loading?: boolean
  onConfirm: () => void
  onCancel: () => void
  className?: string
}

export function ConfirmDialog({
  open,
  title,
  description,
  hint,
  confirmText = "确认",
  cancelText = "取消",
  destructive = false,
  loading = false,
  onConfirm,
  onCancel,
  className,
}: ConfirmDialogProps) {
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !loading) {
        event.preventDefault()
        onCancel()
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [open, loading, onCancel])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 animate-in bg-black/40 fade-in-0 duration-150 backdrop-blur-[2px]"
        onClick={loading ? undefined : onCancel}
        aria-hidden
      />
      <div
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        className={cn(
          "relative w-full max-w-[400px] animate-in rounded-xl border border-border bg-card text-card-foreground shadow-xl duration-150 fade-in-0 zoom-in-95",
          className,
        )}
      >
        <div className="flex gap-3 px-4 pt-4">
          {destructive && (
            <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-destructive/10 text-destructive">
              <TrashIcon className="size-4" />
            </span>
          )}
          <div className="min-w-0 flex-1 pt-0.5">
            <h2 className="text-[14px] leading-5 font-medium">{title}</h2>
            {description && (
              <p className="mt-1 text-[12.5px] leading-[1.7] text-muted-foreground">{description}</p>
            )}
          </div>
        </div>

        {hint && (
          <div className="mx-4 mt-3 rounded-lg bg-muted/60 px-3 py-2 text-[11.5px] leading-[1.7] text-muted-foreground">
            {hint}
          </div>
        )}

        <div className="mt-4 flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          {/* 危险操作把焦点放在「取消」上：回车/空格直接触发的是"不删"。 */}
          <Button
            variant="outline"
            size="sm"
            onClick={onCancel}
            disabled={loading}
            autoFocus
          >
            {cancelText}
          </Button>
          <Button
            size="sm"
            onClick={onConfirm}
            disabled={loading}
            className={cn(
              destructive
                ? "bg-destructive text-white hover:bg-destructive/90"
                : "bg-primary text-primary-foreground hover:bg-primary/80",
            )}
          >
            {loading ? "处理中…" : confirmText}
          </Button>
        </div>
      </div>
    </div>
  )
}
