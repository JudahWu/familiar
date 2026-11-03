import { Link, useRouterState } from "@tanstack/react-router"
import {
  BooksIcon,
  MagnifyingGlassIcon,
  MoonIcon,
  PlusIcon,
  SidebarSimpleIcon,
  SunIcon,
  XIcon,
} from "@phosphor-icons/react"
import { useEffect, useMemo, useRef, useState } from "react"

import type { SessionUser } from "@/lib/api"
import type { AgentStatus, AgentThread } from "@/lib/agents/types"
import { SidebarUserMenu } from "@/components/SidebarUserMenu"
import { SidebarCollapseButton, SidebarFrame, useSidebarLayout } from "@/components/sidebar-layout"
import { ConfirmDialog } from "@/components/ui/confirm-dialog"
import { groupThreads } from "@/lib/agents/api"
import { useAgentThreads, useDeleteAgentThread } from "@/lib/agents/queries"
import { setNavSelection, useNavSelection } from "@/lib/nav-selection"
import { useTheme } from "@/lib/theme"
import { cn } from "@/lib/utils"
import { BrandLockup, BrandRailMark } from "@/components/BrandLockup"

const STATUS_META: Record<AgentStatus, { label: string; dot: string }> = {
  // 状态灯语义固定为：运行中=橙、已完成=绿、失败=红。
  // 圆点用小面积高亮色（*-vivid），文字标签仍走可读色。
  running: { label: "运行中", dot: "bg-[var(--warning-vivid)] animate-pulse" },
  finished: { label: "已完成", dot: "bg-[var(--success-vivid)]" },
  interrupted: { label: "已中断", dot: "bg-[var(--ui-text-dim)]" },
  error: { label: "失败", dot: "bg-[var(--destructive-vivid)]" },
  idle: { label: "空闲", dot: "bg-[var(--ui-border)]" },
}

function relativeTime(ms: number): string {
  const diff = Date.now() - ms
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 1) return "刚刚"
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return new Date(ms).toLocaleDateString("zh-CN")
}

interface AgentsSidebarProps {
  user: SessionUser
  activeThreadId?: string
}

export function AgentsSidebar({ user, activeThreadId }: AgentsSidebarProps) {
  const threadsQuery = useAgentThreads()
  const allThreads = threadsQuery.data ?? []
  const layout = useSidebarLayout()
  // 知识库入口的选中态不跟路由走，只在用户点它之后才亮（详见 lib/nav-selection.ts）
  const navSelection = useNavSelection()
  const pathname = useRouterState({ select: (state) => state.location.pathname })
  // 选中 = 「点过它」且「现在确实在它的页面上」。
  // 两个条件缺一不可：只靠 pathname 会让直接输地址/刷新也高亮；只靠点击记录
  // 会在从知识库返回首页后仍残留高亮。这样写也不需要 effect 去清理，
  // 点一下导航完成就立刻亮（effect 版本会在点击瞬间按旧 pathname 误清，得点两次）。
  const knowledgeActive = navSelection === "knowledge" && pathname.startsWith("/knowledge")
  const newTaskActive = navSelection === "agents" && pathname === "/agents"
  const { theme, setTheme } = useTheme()
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState("")
  const searchInputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!searchOpen) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault()
        setSearchOpen(true)
        searchInputRef.current?.focus()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [searchOpen])

  const threads = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return allThreads
    return allThreads.filter(
      (thread) =>
        thread.title.toLowerCase().includes(keyword) ||
        (thread.repoFullName || thread.repo || "").toLowerCase().includes(keyword),
    )
  }, [allThreads, query])

  const groups = groupThreads(threads)

  return (
    <SidebarFrame
      {...layout}
      className="border-r border-[var(--ui-border)] bg-[var(--ui-sidebar)]"
      rail={
        <div className="flex flex-col items-center gap-1">
          <BrandRailMark />
          <Link
            to="/agents"
            aria-label="新任务"
            className="flex size-8 items-center justify-center rounded-md text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
          >
            <PlusIcon className="size-4" />
          </Link>
          <Link
            to="/knowledge"
            aria-label="知识库"
            onClick={() => setNavSelection("knowledge")}
            className={cn(
              "flex size-8 items-center justify-center rounded-md transition-colors",
              knowledgeActive
                ? "bg-[var(--ui-sidebar-hover)] text-[var(--ui-text)]"
                : "text-[var(--ui-text-muted)] hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]",
            )}
          >
            <BooksIcon className="size-4" />
          </Link>
          <button
            type="button"
            aria-label="展开侧栏"
            onClick={layout.toggle}
            className="mt-auto flex size-8 items-center justify-center rounded-md text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
          >
            <SidebarSimpleIcon className="size-4" />
          </button>
        </div>
      }
    >
      <div className="flex items-center gap-2 px-3 pt-4 pb-3">
        <BrandLockup />
        <div className="ml-auto flex items-center gap-0.5">
          <button
            type="button"
            aria-label={theme === "dark" ? "切换到浅色" : "切换到深色"}
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            className="flex size-5 items-center justify-center rounded text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
          >
            {theme === "dark" ? <SunIcon className="size-3.5" /> : <MoonIcon className="size-3.5" />}
          </button>
          <button
            type="button"
            aria-label="搜索会话"
            onClick={() => {
              setSearchOpen((open) => {
                if (!open) requestAnimationFrame(() => searchInputRef.current?.focus())
                return !open
              })
            }}
            className={cn(
              "flex size-5 items-center justify-center rounded transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]",
              searchOpen ? "text-[var(--ui-text)]" : "text-[var(--ui-text-muted)]",
            )}
          >
            <MagnifyingGlassIcon className="size-3.5" />
          </button>
          <SidebarCollapseButton onToggle={layout.toggle} />
        </div>
      </div>

      <div className="px-2 pb-2">
        <Link
          to="/agents"
          onClick={() => {
            setNavSelection("agents")
            layout.closeOnMobile()
          }}
          className={cn(
            "relative flex h-8 w-full items-center justify-between rounded-md px-2.5 text-[13px] transition-colors",
            newTaskActive
              ? "bg-[var(--ui-sidebar-hover)] font-medium text-[var(--ui-text)]"
              : "text-[var(--ui-text-muted)] hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]",
          )}
        >
          <span className="flex items-center gap-2">
            {newTaskActive && (
              <span className="absolute top-1.5 left-0 h-5 w-[2px] rounded-full bg-[var(--ui-accent)]" />
            )}
            <PlusIcon className="size-3.5" />
            <span>新任务</span>
          </span>
          <span className="text-[11px] text-[var(--ui-text-dim)]">⌘N</span>
        </Link>
      </div>

      <div className="px-2 pb-2">
        <Link
          to="/knowledge"
          onClick={() => {
            setNavSelection("knowledge")
            layout.closeOnMobile()
          }}
          className={cn(
            "relative flex h-8 w-full items-center gap-2 rounded-md px-2.5 text-[13px] transition-colors",
            knowledgeActive
              ? "bg-[var(--ui-sidebar-hover)] font-medium text-[var(--ui-text)]"
              : "text-[var(--ui-text-muted)] hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]",
          )}
        >
          {knowledgeActive && (
            <span className="absolute top-1.5 left-0 h-5 w-[2px] rounded-full bg-[var(--ui-accent)]" />
          )}
          <BooksIcon className="size-3.5" />
          <span>知识库</span>
        </Link>
      </div>

      {searchOpen && (
        <div className="px-2 pb-2">
          <div className="relative">
            <input
              ref={searchInputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  setQuery("")
                  setSearchOpen(false)
                }
              }}
              placeholder="搜索会话，Esc 关闭"
              className="h-7 w-full rounded-md border border-border bg-card px-2 text-xs text-foreground outline-none transition-colors placeholder:text-muted-foreground focus:border-ring"
            />
          </div>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {threads.length === 0 && (
          <p className="px-2 py-6 text-center text-xs text-[var(--ui-text-dim)]">
            {query.trim() ? "没有匹配的会话" : "还没有会话"}
          </p>
        )}
        <ThreadGroup
          label="今天"
          threads={groups.today}
          activeThreadId={activeThreadId}
          onNavigate={layout.closeOnMobile}
        />
        <ThreadGroup
          label="最近 30 天"
          threads={groups.last30}
          activeThreadId={activeThreadId}
          onNavigate={layout.closeOnMobile}
        />
        <ThreadGroup
          label="更早"
          threads={groups.older}
          activeThreadId={activeThreadId}
          onNavigate={layout.closeOnMobile}
        />
      </div>

      <div className="border-t border-[var(--ui-border)] p-2">
        <SidebarUserMenu user={user} />
      </div>
    </SidebarFrame>
  )
}

function ThreadGroup({
  label,
  threads,
  activeThreadId,
  onNavigate,
}: {
  label: string
  threads: Array<AgentThread>
  activeThreadId?: string
  onNavigate?: () => void
}) {
  if (threads.length === 0) return null

  return (
    <div className="mb-2">
      <div className="px-2 py-1 text-[11px] text-[var(--ui-text-dim)]">{label}</div>
      {threads.map((thread) => (
        <ThreadRow
          key={thread.id}
          thread={thread}
          isActive={thread.id === activeThreadId}
          onNavigate={onNavigate}
        />
      ))}
    </div>
  )
}

function ThreadRow({
  thread,
  isActive,
  onNavigate,
}: {
  thread: AgentThread
  isActive: boolean
  onNavigate?: () => void
}) {
  const deleteThread = useDeleteAgentThread()
  const badge =
    thread.diffStats && thread.diffStats.additions > 0 ? `+${thread.diffStats.additions}` : null
  const isDeleting = deleteThread.isPending && deleteThread.variables === thread.id
  const status = STATUS_META[thread.status] ?? STATUS_META.idle
  const [confirming, setConfirming] = useState(false)

  const onDelete = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (isDeleting) return
    setConfirming(true)
  }

  return (
    <>
      <Link
        to="/agents/$threadId"
        params={{ threadId: thread.id }}
        onClick={onNavigate}
        className={cn(
          "group mb-0.5 flex flex-col gap-0.5 rounded-md px-2 py-1.5 transition-colors",
          isActive
            ? "bg-[var(--ui-accent-bubble)]"
            : "hover:bg-[var(--ui-sidebar-hover)]",
          isDeleting && "opacity-50",
        )}
      >
        <div className="flex items-center gap-1.5">
          <span className={cn("size-1.5 shrink-0 rounded-full", status.dot)} />
          <span
            className={cn(
              "min-w-0 flex-1 truncate text-[13px]",
              isActive ? "font-medium text-[var(--ui-text)]" : "text-[var(--ui-text)]",
            )}
          >
            {thread.title}
          </span>
          <span className="shrink-0 text-[11px] text-[var(--ui-text-dim)]">
            {relativeTime(thread.updatedAt)}
          </span>
          {badge && (
            <span className="shrink-0 rounded bg-[var(--ui-panel-2)] px-1 py-0.5 text-[11px] text-[var(--ui-text-dim)] group-hover:hidden">
              {badge}
            </span>
          )}
          <button
            type="button"
            aria-label="删除会话"
            onClick={onDelete}
            disabled={isDeleting}
            className="hidden size-4 shrink-0 items-center justify-center rounded text-[var(--ui-text-dim)] transition-colors group-hover:flex hover:text-destructive"
          >
            <XIcon className="size-3" weight="bold" />
          </button>
        </div>
        <div className="flex items-center gap-1 pl-3 text-[11px] text-[var(--ui-text-dim)]">
          <span className="min-w-0 truncate">{thread.repoFullName || thread.repo || "未绑定仓库"}</span>
          <span aria-hidden>·</span>
          <span className="shrink-0">{status.label}</span>
        </div>
      </Link>

      <ConfirmDialog
        open={confirming}
        destructive
        title="删除任务？"
        // description={`「${thread.title}」将被永久删除。`}
        hint="删除后，这些任务将从你的所有设备中移除，且无法恢复。"
        confirmText="删除"
        loading={isDeleting}
        onConfirm={() => {
          setConfirming(false)
          deleteThread.mutate(thread.id)
        }}
        onCancel={() => setConfirming(false)}
      />
    </>
  )
}

export function AgentsShell({
  user,
  activeThreadId,
  children,
}: {
  user: SessionUser
  activeThreadId?: string
  children: React.ReactNode
}) {
  return (
    <div className="agents-ui flex h-svh overflow-hidden bg-[var(--ui-bg)]">
      <AgentsSidebar user={user} activeThreadId={activeThreadId} />
      <div className="flex min-w-0 flex-1">{children}</div>
    </div>
  )
}
