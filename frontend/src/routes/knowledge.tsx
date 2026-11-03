import { Navigate, createFileRoute } from "@tanstack/react-router"
import { useEffect, useState } from "react"

import type {
  AnswerResult,
  ChatTurn,
  KnowledgeDoc,
  SearchHit,
  SessionTurn,
} from "@/lib/knowledge/types"
import { AgentsSidebar } from "@/components/agents/AgentsSidebar"
import { CitationPanel } from "@/components/knowledge/CitationPanel"
import { ImportDrawer } from "@/components/knowledge/ImportDrawer"
import { KnowledgeChat, hitKey } from "@/components/knowledge/KnowledgeChat"
import { KnowledgeSidebar } from "@/components/knowledge/KnowledgeSidebar"
import { ConfirmDialog } from "@/components/ui/confirm-dialog"
import { knowledgeApi } from "@/lib/knowledge/api"
import {
  useCreateBase,
  useDeleteBase,
  useDeleteDocument,
  useImportProgress,
  useKnowledgeBases,
  useUploadDocument,
} from "@/lib/knowledge/queries"
import { useSession } from "@/lib/session"
import agentsCss from "@/styles/agents.css?url"

/**
 * 知识库页面（阶段 ④：检索 + 生成 + 自评）。
 *
 * 三栏：应用侧栏 → 知识库/文档 → 问答区，引用详情叠在右侧。
 *
 * 问答是慢操作（检索 + 生成 + 自评，5-20 秒），所以只给 loading 态，不做假进度条。
 * 低分或资料没覆盖时，答案下方会出现一条提示，让用户选"换联网搜索"还是"就这样"。
 */
export const Route = createFileRoute("/knowledge")({
  head: () => ({
    links: [{ rel: "stylesheet", href: agentsCss }],
  }),
  component: KnowledgePage,
})

/**
 * 把后端恢复出来的历史转成界面上的轮次。
 *
 * 助手轮的 `query`（"换联网重答"要重发的那句）没有单独存，用紧挨着的上一个用户轮
 * 补上——历史本来就是成对写入的，这个对应关系是确定的。
 */
function hydrateTurns(history: Array<SessionTurn>): Array<ChatTurn> {
  let lastQuery = ""
  return history.map((turn, index) => {
    if (turn.role === "user") {
      lastQuery = turn.content
      return { id: `hist-u-${index}`, role: "user" as const, text: turn.content }
    }
    return {
      id: `hist-a-${index}`,
      role: "assistant" as const,
      answer: turn.content,
      hits: turn.citations,
      score: turn.score,
      coverage: turn.coverage as ChatTurn["coverage"],
      needsReview: turn.needs_review,
      query: lastQuery,
    }
  })
}

function sessionKey(kbId: string): string {
  return `knowledge:session:${kbId}`
}

function KnowledgePage() {
  const session = useSession()
  const basesQuery = useKnowledgeBases()
  const createBase = useCreateBase()
  const upload = useUploadDocument()
  const deleteDocument = useDeleteDocument()
  const deleteBase = useDeleteBase()

  const [activeBaseId, setActiveBaseId] = useState("")
  const [turns, setTurns] = useState<Array<ChatTurn>>([])
  const [pending, setPending] = useState(false)
  // 会话 id：有它才有记忆。首次提问时后端新建并返回，前端存到 sessionStorage，
  // 刷新后还在，所以"刷新即失忆"这件事被治掉了。
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [activeHit, setActiveHit] = useState<SearchHit | null>(null)
  const [importOpen, setImportOpen] = useState(false)
  // 导入抽屉归属哪个库。上传入口可以从某个库的树节点直接触发，所以单独记一份，
  // 不依赖 activeBaseId 的异步更新。
  const [importTargetId, setImportTargetId] = useState<string | null>(null)
  const [uploadedDoc, setUploadedDoc] = useState<KnowledgeDoc | null>(null)
  const [uploadedDocId, setUploadedDocId] = useState<string | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  // 待确认删除的文档。null 表示弹窗关着。
  const [pendingDelete, setPendingDelete] = useState<{ docId: string; title: string } | null>(null)
  // 待确认删除的知识库。
  const [pendingDeleteBase, setPendingDeleteBase] = useState<{
    kbId: string
    name: string
    docCount: number
  } | null>(null)

  // 导入是后台任务，靠轮询进度跟进
  const progressQuery = useImportProgress(uploadedDocId)
  const progress = progressQuery.data ?? null

  const bases = basesQuery.data ?? []
  const base = bases.find((item) => item.id === activeBaseId) ?? bases[0]
  const importTarget = bases.find((item) => item.id === importTargetId) ?? base

  // 首次加载或删库后，把选中项收敛到实际存在的库上
  useEffect(() => {
    if (!base && bases.length > 0) setActiveBaseId(bases[0]!.id)
    if (base && base.id !== activeBaseId) setActiveBaseId(base.id)
  }, [base, bases, activeBaseId])

  // 换库等于换上下文：清空界面，再把这个库上次的会话接回来
  useEffect(() => {
    setTurns([])
    setActiveHit(null)
    setUploadedDoc(null)
    setUploadedDocId(null)
    setUploadError(null)
    setSessionId(null)

    const kbId = base?.id
    if (!kbId) return
    const stored = sessionStorage.getItem(sessionKey(kbId))
    if (!stored) return

    let cancelled = false
    setSessionId(stored)
    // 两件事一起做：把历史接回来，再看有没有一条"待确认"的答案挂在图上。
    // 待确认的那条**不在历史里**（图跑到中断时我们还没落库），所以必须单独取，
    // 否则刷新之后按钮就没了，而图还停在那儿——用户再问一句会被当成"通过"。
    void Promise.all([
      knowledgeApi.getSession(stored).catch(() => ({ turns: [] as Array<SessionTurn> })),
      knowledgeApi.answerState(stored).catch(() => null),
    ]).then(([history, state]) => {
      if (cancelled) return
      const restored = history.turns?.length ? hydrateTurns(history.turns) : []
      const pending = state?.interrupted ? toAssistantTurn(state, "") : null
      setTurns(pending ? [...restored, pending] : restored)
    })
    return () => {
      cancelled = true
    }
  }, [base?.id])

  // 导入完成后，列表里的那条才是最终状态（页数、段落数、插图数都是那时才有的）
  useEffect(() => {
    if (progress?.status !== "done" || !uploadedDocId) return
    const finished = bases
      .flatMap((item) => item.documents)
      .find((item) => item.id === uploadedDocId)
    if (finished) setUploadedDoc(finished)
  }, [progress?.status, bases, uploadedDocId])

  if (session.isLoading) return null
  if (!session.data) return <Navigate to="/login" />

  /** 把后端返回的一次结果转成界面上的一轮。 */
  const toAssistantTurn = (result: AnswerResult, query: string): ChatTurn => ({
    id: `a-${Date.now()}`,
    role: "assistant",
    answer: result.answer,
    hits: result.hits,
    score: result.score,
    coverage: result.coverage,
    reason: result.reason,
    needsReview: result.needs_review,
    reviewReason: result.review_reason,
    model: result.model,
    webUsed: result.web_used,
    webError: result.web_error,
    query,
    threshold: result.threshold,
  })

  const runAsk = async (query: string, options?: { image?: string }) => {
    if (!base) return
    const image = options?.image
    setTurns((prev) => [
      ...prev,
      // 纯图片提问时 text 为空，用户气泡里只显示缩略图
      { id: `q-${Date.now()}`, role: "user", text: query || undefined, image },
    ])
    setPending(true)
    try {
      const result = await knowledgeApi.ask(base.id, query, { sessionId, image })
      // 后端每次都会回报 session_id（首问时是新建的），存下来后续才带着走
      if (result.session_id) {
        setSessionId(result.session_id)
        sessionStorage.setItem(sessionKey(base.id), result.session_id)
      }
      setTurns((prev) => [
        ...prev,
        toAssistantTurn(result, query),
      ])
    } catch (error) {
      setTurns((prev) => [
        ...prev,
        { id: `e-${Date.now()}`, role: "assistant", error: (error as Error).message, query },
      ])
    } finally {
      setPending(false)
    }
  }

  /**
   * 对"待确认"的答案做裁决，继续跑图。
   *
   * 原来这两个按钮只是前端标记（`dismissReview` 改个字段就完事），现在要真的
   * 请求后端：图的流程停在 `human_approval` 之前，不 resume 就永远停在那儿。
   * 联网重答也是这条路上的一支（`rejected`），不再是独立接口。
   */
  const runResume = async (decision: "approve" | "rejected") => {
    if (!sessionId) return
    setPending(true)
    try {
      const result = await knowledgeApi.resume(sessionId, decision)
      // 替换掉那条待确认的答案，而不是追加：同一个问题的两版答案不该并排显示
      setTurns((prev) => {
        const index = prev.findIndex((turn) => turn.needsReview)
        const next = toAssistantTurn(result, prev[index]?.query ?? "")
        if (index < 0) return [...prev, next]
        return [...prev.slice(0, index), next, ...prev.slice(index + 1)]
      })
    } catch (error) {
      setTurns((prev) => [
        ...prev,
        { id: `e-${Date.now()}`, role: "assistant", error: (error as Error).message },
      ])
    } finally {
      setPending(false)
    }
  }

  /** 开一段新对话：清界面、丢掉这个库的 session id。旧记录留在库里不删。 */
  const startNewSession = () => {
    if (base) sessionStorage.removeItem(sessionKey(base.id))
    setSessionId(null)
    setTurns([])
    setActiveHit(null)
  }

  /** 用户点了"就这样"：确认这一版答案，让图收尾。 */
  const dismissReview = (_turnId: string) => {
    void runResume("approve")
  }

  /** 打开导入抽屉。传 kbId 表示上传到那个库，不传就是当前库。 */
  const openImport = (kbId?: string) => {
    const target = kbId ?? base?.id ?? null
    setImportTargetId(target)
    if (kbId) setActiveBaseId(kbId)
    setImportOpen(true)
  }

  const handleUpload = (file: File) => {
    const target = importTarget ?? base
    if (!target) return
    setUploadedDoc(null)
    setUploadedDocId(null)
    setUploadError(null)
    upload.mutate(
      { kbId: target.id, file },
      {
        onSuccess: (doc) => {
          setUploadedDoc(doc)
          // 拿到 doc_id 就开始轮询进度
          setUploadedDocId(doc.id)
        },
        onError: (error) => setUploadError((error).message),
      },
    )
  }

  const handleCreateBase = (name: string) => {
    createBase.mutate(name, {
      onSuccess: (created) => {
        setActiveBaseId(created.id)
        setImportOpen(true)
      },
    })
  }

  const handleDeleteDocument = (docId: string, title: string) => {
    setPendingDelete({ docId, title })
  }

  const confirmDeleteDocument = () => {
    if (!pendingDelete || !base) return
    deleteDocument.mutate(
      { kbId: base.id, docId: pendingDelete.docId },
      {
        onSuccess: () => setPendingDelete(null),
        onError: (error) => {
          setUploadError((error).message)
          setPendingDelete(null)
        },
      },
    )
  }

  const confirmDeleteBase = () => {
    if (!pendingDeleteBase) return
    deleteBase.mutate(pendingDeleteBase.kbId, {
      onSuccess: () => {
        setPendingDeleteBase(null)
        // 删掉的可能正是当前库，把选中项交给列表刷新后的 useEffect 收敛
        setActiveBaseId("")
      },
      onError: (error) => {
        setUploadError((error).message)
        setPendingDeleteBase(null)
      },
    })
  }

  return (
    <div className="agents-ui flex h-svh overflow-hidden bg-[var(--ui-bg)] text-[var(--ui-text)]">
      <AgentsSidebar user={session.data} />
      <div className="relative flex min-w-0 flex-1">
        <KnowledgeSidebar
          bases={bases}
          activeBaseId={base?.id ?? ""}
          busy={upload.isPending}
          onSelectBase={setActiveBaseId}
          onCreateBase={handleCreateBase}
          onOpenImport={openImport}
          onDeleteDocument={handleDeleteDocument}
          onDeleteBase={(kbId, name, docCount) => setPendingDeleteBase({ kbId, name, docCount })}
        />
        <KnowledgeChat
          base={base}
          turns={turns}
          pending={pending}
          activeHitKey={activeHit ? hitKey(activeHit) : null}
          onAsk={(query, image) => void runAsk(query, { image })}
          onRetryWeb={() => void runResume("rejected")}
          onDismissReview={dismissReview}
          onNewSession={startNewSession}
          onSelectHit={setActiveHit}
          onOpenImport={() => openImport()}
        />
        {activeHit && <CitationPanel hit={activeHit} onClose={() => setActiveHit(null)} />}
        <ImportDrawer
          open={importOpen}
          baseName={importTarget?.name}
          busy={upload.isPending || progress?.status === "running" || progress?.status === "pending"}
          result={uploadedDoc}
          progress={progress}
          error={uploadError ?? (progress?.status === "failed" ? progress.error : null)}
          onClose={() => setImportOpen(false)}
          onUpload={handleUpload}
        />
        <ConfirmDialog
          open={pendingDelete !== null}
          destructive
          title="删除文档？"
          // description={`「${pendingDelete?.title ?? ""}」将被永久删除。`}
          hint="删除后，这些文档将从你的所有设备中移除，且无法恢复。"
          confirmText="删除"
          loading={deleteDocument.isPending}
          onConfirm={confirmDeleteDocument}
          onCancel={() => setPendingDelete(null)}
        />
        <ConfirmDialog
          open={pendingDeleteBase !== null}
          destructive
          title="删除知识库？"
          // description={`「${pendingDeleteBase?.name ?? ""}」${
          //   pendingDeleteBase?.docCount
          //     ? ` 及其中的 ${pendingDeleteBase.docCount} 篇文档`
          //     : ""
          // }将被永久删除。`}
          hint="删除后，这个知识库将从你的所有设备中移除，且无法恢复。"
          confirmText="删除"
          loading={deleteBase.isPending}
          onConfirm={confirmDeleteBase}
          onCancel={() => setPendingDeleteBase(null)}
        />
      </div>
    </div>
  )
}
