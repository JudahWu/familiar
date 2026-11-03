import { useState } from "react"

import type { ModelSelection } from "@/lib/agents/useModelOptions"
import { AgentPromptBar } from "@/components/agents/AgentPromptBar"
import { SlackConnectDialog } from "@/components/agents/SlackConnectDialog"
import { useCreateAgentThread } from "@/lib/agents/queries"
import { useModelOptions } from "@/lib/agents/useModelOptions"
import { useProfile, useRepos } from "@/lib/profile"

const QUICK_STARTS = [
  {
    label: "先摸清仓库",
    hint: "目录结构、依赖、入口都读一遍",
    prompt:
      "先通读这个仓库：目录结构、主要依赖、入口文件和模块划分，输出一份结构说明。本轮只做分析，不要修改任何代码。",
  },
  {
    label: "审查当前分支",
    hint: "按规则出结构化问题清单",
    prompt:
      "审查当前分支相对 master 的改动：先读取审查规则，再逐文件看 diff，输出结构化问题清单（文件、行号、严重级别、说明）。本轮不要修改代码，也不要提交。",
  },
  {
    label: "查看工作区状态",
    hint: "本地有哪些项目、git 是否干净",
    prompt: "列出本地工作区有哪些项目，以及各自当前分支和 git 状态。",
  },
] as const

export function AgentsHome() {
  const createThread = useCreateAgentThread()
  const createError =
    createThread.error instanceof Error ? createThread.error.message : null
  const { models, defaultSelection } = useModelOptions()
  const [selection, setSelection] = useState<ModelSelection | null>(null)
  const activeSelection = selection ?? defaultSelection

  const reposQuery = useRepos()
  const profileQuery = useProfile()
  // undefined = untouched (fall back to the profile default); null = explicitly "no repo".
  const [repoOverride, setRepoOverride] = useState<string | null | undefined>(
    undefined
  )
  const repo =
    repoOverride === undefined
      ? (profileQuery.data?.default_repo ?? null)
      : repoOverride

  const submit = (prompt: string) => {
    createThread.mutate({
      prompt,
      images: [],
      repo,
      repo_explicitly_none: repoOverride === null,
      model_id: activeSelection?.modelId ?? null,
      effort: activeSelection?.effort ?? null,
    })
  }

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-y-auto px-6 py-10">
      <SlackConnectDialog />
      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col">
        <h1 className="text-lg font-medium tracking-tight text-[var(--ui-text)]">
          今天要做点什么？
        </h1>
        <p className="mt-1.5 text-[13px] text-[var(--ui-text-muted)]">
          一句话说清问题。我会先出技术方案，你确认之后才会动代码。
        </p>

        <div className="mt-6">
          <AgentPromptBar
            onSubmit={(prompt, images, files) =>
              createThread.mutate({
                prompt,
                images,
                files,
                repo,
                repo_explicitly_none: repoOverride === null,
                model_id: activeSelection?.modelId ?? null,
                effort: activeSelection?.effort ?? null,
              })
            }
            disabled={createThread.isPending}
            models={models}
            selection={activeSelection}
            onSelectionChange={setSelection}
            repos={reposQuery.data?.repositories}
            selectedRepo={repo}
            onRepoChange={setRepoOverride}
          />
        </div>

        {createError ? (
          <p className="mt-3 text-[13px] text-destructive">{createError}</p>
        ) : null}

        <div className="mt-8">
          <div className="text-[11px] text-[var(--ui-text-dim)]">快捷起点</div>
          <div className="mt-2 flex flex-col gap-1.5">
            {QUICK_STARTS.map((item) => (
              <button
                key={item.label}
                type="button"
                disabled={createThread.isPending}
                onClick={() => submit(item.prompt)}
                className="group flex items-center gap-2.5 rounded-md border border-[var(--ui-border)] px-3 py-2 text-left transition-colors hover:bg-[var(--ui-panel)] disabled:cursor-default disabled:opacity-50"
              >
                <span className="text-[13px] font-medium text-[var(--ui-text)]">
                  {item.label}
                </span>
                <span className="min-w-0 flex-1 truncate text-[11px] text-[var(--ui-text-dim)]">
                  {item.hint}
                </span>
                <span className="shrink-0 text-[11px] text-[var(--ui-text-dim)] transition-transform group-hover:translate-x-0.5">
                  →
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
