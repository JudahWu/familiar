import {
  BooksIcon,
  CaretDownIcon,
  CaretLeftIcon,
  CaretRightIcon,
  PlusIcon,
  TrashIcon,
  UploadSimpleIcon,
} from "@phosphor-icons/react"
import { useEffect, useState } from "react"

import type { DocStatus, KnowledgeBase } from "@/lib/knowledge/types"
import { cn } from "@/lib/utils"

/**
 * 知识库侧栏：库 → 文档，两层树。
 *
 * 文档直接挂在所属的库下面，而不是像之前那样"上面列库、下面单独一块列当前库的
 * 文档"。分两段的问题很明显：库一多就要来回对照哪篇文档属于哪个库，而且切换
 * 库时下面的文档区整个换掉，视线要重新定位。
 *
 * 文档超过 5 篇就折叠，只露前 5 篇加一个「展开全部 N 篇」——一本技术书拆成
 * 十几份 PDF 是常态，全铺开会把库列表挤到屏幕外。
 *
 * 面板本身还能折叠成 44px 窄轨：问答区需要宽度，折起来时用圆点表示各文档状态。
 */

/**
 * 状态点是展开态文档行的一部分：绿=已入库、橙=解析中（呼吸）、红=失败。
 * 折叠成 44px 窄轨时**不显示状态点**——那一列没有文字说明，光剩几个点
 * 既看不出是什么状态，也会在解析时一闪一闪地晃眼。
 */
const STATUS_META: Record<DocStatus, { dot: string; label: string }> = {
  indexed: { dot: "bg-[var(--ui-success-vivid)]", label: "已入库" },
  parsing: { dot: "bg-[var(--ui-warning-vivid)] animate-pulse", label: "解析中" },
  pending: { dot: "bg-[var(--ui-border)]", label: "排队中" },
  failed: { dot: "bg-[var(--ui-danger-vivid)]", label: "失败" },
}

/** 库里文档超过这个数就折叠。 */
const COLLAPSE_LIMIT = 5

interface KnowledgeSidebarProps {
  bases: Array<KnowledgeBase>
  activeBaseId: string
  /** 上传/解析进行中时置灰上传按钮并给一行提示。 */
  busy?: boolean
  onSelectBase: (id: string) => void
  onCreateBase: (name: string) => void
  /** 打开导入抽屉。传 kbId 表示上传到那个库，不传就是当前库。 */
  onOpenImport: (kbId?: string) => void
  /** 删一篇文档。传下去后由上层负责确认与调用接口。 */
  onDeleteDocument?: (docId: string, title: string) => void
  /** 删整个知识库。 */
  onDeleteBase?: (kbId: string, name: string, docCount: number) => void
  className?: string
}

export function KnowledgeSidebar({
  bases,
  activeBaseId,
  busy = false,
  onSelectBase,
  onCreateBase,
  onOpenImport,
  onDeleteDocument,
  onDeleteBase,
  className,
}: KnowledgeSidebarProps) {
  const [collapsed, setCollapsed] = useState(false)
  const [creating, setCreating] = useState(false)
  const [draftName, setDraftName] = useState("")
  // 展开了文档树的库 / 展开了全部文档（超过 5 篇时）的库
  const [openBases, setOpenBases] = useState<Set<string>>(new Set())
  const [expandedBases, setExpandedBases] = useState<Set<string>>(new Set())

  const activeBase = bases.find((base) => base.id === activeBaseId) ?? bases[0]
  const paragraphCount = activeBase?.chunks ?? 0
  const imageCount = activeBase?.documents.reduce((sum, doc) => sum + doc.images, 0) ?? 0

  // 当前库总是展开的，否则切换过去看到的是空的
  useEffect(() => {
    if (!activeBaseId) return
    setOpenBases((prev) => (prev.has(activeBaseId) ? prev : new Set(prev).add(activeBaseId)))
  }, [activeBaseId])

  const submitCreate = () => {
    const name = draftName.trim()
    if (!name) return
    onCreateBase(name)
    setDraftName("")
    setCreating(false)
  }

  const toggleSet = (
    setter: React.Dispatch<React.SetStateAction<Set<string>>>,
    id: string,
  ) => {
    setter((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  if (collapsed) {
    return (
      <aside
        className={cn(
          "flex w-11 shrink-0 flex-col items-center gap-2 border-r border-[var(--ui-border)] bg-[var(--ui-sidebar)] py-3",
          className,
        )}
      >
        <button
          type="button"
          aria-label="展开知识库"
          onClick={() => setCollapsed(false)}
          className="flex size-7 items-center justify-center rounded-md text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
        >
          <BooksIcon className="size-4" />
        </button>
        <div className="mt-1 flex max-h-full flex-col items-center gap-1.5 overflow-y-auto">
          {/* 窄轨里不放状态点（用户要求）：只有几个点、没文字说明，既看不出
              是什么状态，解析时还会一闪一闪。改成列知识库本身——点一下就展开
              面板并切到那个库。 */}
          {bases.map((base) => (
            <button
              key={base.id}
              type="button"
              title={base.name}
              aria-label={base.name}
              onClick={() => {
                onSelectBase(base.id)
                setCollapsed(false)
              }}
              className={cn(
                "flex size-7 shrink-0 items-center justify-center rounded-md text-[12px] transition-colors",
                base.id === activeBaseId
                  ? "bg-[var(--ui-accent-bubble)] font-medium text-[var(--ui-text)]"
                  : "bg-[var(--ui-panel-2)] text-[var(--ui-text-muted)] hover:text-[var(--ui-text)]",
              )}
            >
              {base.name.slice(0, 1)}
            </button>
          ))}
        </div>
        <button
          type="button"
          aria-label="上传 PDF"
          disabled={!activeBase || busy}
          onClick={() => onOpenImport()}
          className="mt-auto flex size-7 items-center justify-center rounded-md text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)] disabled:opacity-40"
        >
          <UploadSimpleIcon className="size-4" />
        </button>
      </aside>
    )
  }

  return (
    <aside
      className={cn(
        "flex w-[228px] shrink-0 flex-col border-r border-[var(--ui-border)] bg-[var(--ui-sidebar)]",
        className,
      )}
    >
      <div className="flex items-center justify-between px-3 pt-4 pb-2">
        <span className="text-[13px] font-medium text-[var(--ui-text)]">知识库</span>
        <div className="flex items-center gap-0.5">
          <button
            type="button"
            aria-label="新建知识库"
            onClick={() => setCreating((value) => !value)}
            className="flex size-6 items-center justify-center rounded text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
          >
            <PlusIcon className="size-3.5" />
          </button>
          <button
            type="button"
            aria-label="收起知识库"
            onClick={() => setCollapsed(true)}
            className="flex size-6 items-center justify-center rounded text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
          >
            <CaretLeftIcon className="size-3.5" />
          </button>
        </div>
      </div>

      {creating && (
        <div className="px-2 pb-2">
          <input
            autoFocus
            value={draftName}
            onChange={(event) => setDraftName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") submitCreate()
              if (event.key === "Escape") {
                setCreating(false)
                setDraftName("")
              }
            }}
            placeholder="知识库名称，回车确认"
            className="h-7 w-full rounded-md border border-[var(--ui-border)] bg-[var(--ui-surface)] px-2 text-[12px] text-[var(--ui-text)] outline-none placeholder:text-[var(--ui-text-dim)] focus:border-[var(--ui-text-muted)]"
          />
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {bases.length === 0 && !creating && (
          <p className="px-2 py-3 text-[11.5px] leading-relaxed text-[var(--ui-text-dim)]">
            还没有知识库。点右上角 + 新建一个，再上传 PDF。
          </p>
        )}
        {bases.map((base) => {
          const active = base.id === activeBaseId
          const open = openBases.has(base.id)
          const docs = base.documents
          const showAll = expandedBases.has(base.id)
          const visible = showAll ? docs : docs.slice(0, COLLAPSE_LIMIT)
          return (
            <div key={base.id} className="mb-0.5">
              <div
                className="group relative flex items-center rounded-md transition-colors hover:bg-[var(--ui-sidebar-hover)]"
                // 非空库不给删除按钮，但要说清楚为什么不给——否则用户 hover
                // 看不到垃圾桶会以为功能缺失。
                title={
                  docs.length > 0
                    ? `还有 ${docs.length} 篇文档，删完才能删除这个库`
                    : undefined
                }
              >
                <button
                  type="button"
                  aria-label={open ? `收起 ${base.name}` : `展开 ${base.name}`}
                  onClick={() => toggleSet(setOpenBases, base.id)}
                  className={cn(
                    "flex size-5 shrink-0 items-center justify-center rounded transition-colors",
                    active
                      ? "text-[var(--ui-accent)]"
                      : "text-[var(--ui-text-dim)] hover:text-[var(--ui-text)]",
                  )}
                >
                  {open ? (
                    <CaretDownIcon className="size-3" />
                  ) : (
                    <CaretRightIcon className="size-3" />
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    onSelectBase(base.id)
                    setOpenBases((prev) => new Set(prev).add(base.id))
                  }}
                  className={cn(
                    "flex min-w-0 flex-1 items-center gap-1.5 rounded-md py-1.5 text-left text-[13px] transition-colors",
                    active
                      ? "font-medium text-[var(--ui-text)]"
                      : "text-[var(--ui-text-muted)] hover:text-[var(--ui-text)]",
                  )}
                >
                  <span className="truncate">{base.name}</span>
                  {/* 悬停时把计数让给操作按钮，两者不会挤在一起 */}
                  <span className="shrink-0 text-[11px] text-[var(--ui-text-dim)] group-hover:hidden">
                    {docs.length > 0 ? docs.length : ""}
                  </span>
                </button>

                <div className="flex shrink-0 items-center gap-0.5 pr-1.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
                  <button
                    type="button"
                    aria-label={`向 ${base.name} 上传 PDF`}
                    title="上传 PDF 到这个库"
                    disabled={busy}
                    onClick={() => onOpenImport(base.id)}
                    className="flex size-5 items-center justify-center rounded text-[var(--ui-text-dim)] transition-colors hover:text-[var(--ui-text)] disabled:opacity-40"
                  >
                    <UploadSimpleIcon className="size-3.5" />
                  </button>
                  {/* 只有空库才给删入口：后端也是同一条规则（库里还有文档会拒绝），
                      两边一致，免得按钮能点但点了报错。 */}
                  {onDeleteBase && docs.length === 0 && (
                    <button
                      type="button"
                      aria-label={`删除知识库 ${base.name}`}
                      title="删除这个知识库"
                      onClick={() => onDeleteBase(base.id, base.name, docs.length)}
                      className="flex size-5 items-center justify-center rounded text-[var(--ui-text-dim)] transition-colors hover:text-[var(--ui-danger-vivid)]"
                    >
                      <TrashIcon className="size-3.5" />
                    </button>
                  )}
                </div>
              </div>

              {open && (
                <div className="ml-[13px] border-l border-[var(--ui-border)] pl-1.5">
                  {docs.length === 0 && (
                    <p className="px-2 py-1.5 text-[11px] leading-relaxed text-[var(--ui-text-dim)]">
                      这个库还是空的。
                    </p>
                  )}
                  {visible.map((doc) => {
                    return (
                      <div
                        key={doc.id}
                        className="group/doc relative rounded-md py-1 pr-1 pl-1.5 transition-colors hover:bg-[var(--ui-sidebar-hover)]"
                      >
                        <div className="flex items-start gap-1.5">
                          <span
                            className={cn(
                              "mt-[5px] size-1.5 shrink-0 rounded-full",
                              STATUS_META[doc.status].dot,
                            )}
                          />
                          <span className="min-w-0 flex-1 truncate text-[12.5px] text-[var(--ui-text)]">
                            {doc.title}
                          </span>
                          {onDeleteDocument && (
                            <button
                              type="button"
                              aria-label={`删除 ${doc.title}`}
                              title="删除这篇文档"
                              onClick={() => onDeleteDocument(doc.id, doc.title)}
                              className="flex size-5 shrink-0 items-center justify-center rounded text-[var(--ui-text-dim)] opacity-0 transition-opacity hover:text-[var(--ui-danger-vivid)] focus-visible:opacity-100 group-hover/doc:opacity-100"
                            >
                              <TrashIcon className="size-3.5" />
                            </button>
                          )}
                        </div>
                        <div className="mt-0.5 flex items-center gap-1.5 pl-3 text-[11px] text-[var(--ui-text-dim)]">
                          {doc.status === "failed" ? (
                            <span className="truncate text-[var(--ui-danger)]" title={doc.error ?? ""}>
                              失败：{doc.error ?? "未知错误"}
                            </span>
                          ) : doc.status === "parsing" ? (
                            // 不显示百分比：PG 里的 progress 字段在后台导入期间
                            // 不会更新，写死 0% 反而误导。真实进度在导入抽屉里。
                            <span>解析中</span>
                          ) : doc.status === "pending" ? (
                            <span>{doc.pages} 页 · 排队</span>
                          ) : (
                            <span>
                              {doc.pages} 页 · {doc.paragraphs} 段
                              {doc.images > 0 ? ` · ${doc.images} 图` : ""}
                              {doc.vision_pages > 0 ? ` · ${doc.vision_pages} 页缺正文` : ""}
                            </span>
                          )}
                        </div>
                      </div>
                    )
                  })}
                  {docs.length > COLLAPSE_LIMIT && (
                    <button
                      type="button"
                      onClick={() => toggleSet(setExpandedBases, base.id)}
                      className="my-0.5 flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-[var(--ui-text-muted)] transition-colors hover:bg-[var(--ui-sidebar-hover)] hover:text-[var(--ui-text)]"
                    >
                      {showAll ? (
                        <>
                          <CaretDownIcon className="size-3" />
                          收起
                        </>
                      ) : (
                        <>
                          <CaretRightIcon className="size-3" />
                          展开全部 {docs.length} 篇
                        </>
                      )}
                    </button>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      <div className="border-t border-[var(--ui-border)] px-3 py-2 text-[11px] text-[var(--ui-text-dim)]">
        段落 {paragraphCount} · 图片 {imageCount}
      </div>
    </aside>
  )
}
