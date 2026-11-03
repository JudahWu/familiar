import { Navigate, createFileRoute, useNavigate } from "@tanstack/react-router"
import { useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { AppShell, SettingsRow, SettingsSection } from "@/components/AppShell"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"
import {
  buildProfileUpdate,
  useOptions,
  useProfile,
  useSaveProfile,
} from "@/lib/profile"
import { useSession } from "@/lib/session"
import { cn } from "@/lib/utils"

export const Route = createFileRoute("/my-settings")({
  component: MySettingsPage,
})

const TABS = [
  { id: "profile", label: "个人资料" },
  { id: "review", label: "代码审查" },
  { id: "account", label: "账户" },
] as const

type TabId = (typeof TABS)[number]["id"]

export function MySettingsPage() {
  const session = useSession()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const profile = useProfile()
  const options = useOptions()
  const save = useSaveProfile()
  const [tab, setTab] = useState<TabId>("profile")
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (session.isLoading) {
    return (
      <main className="p-6">
        <Skeleton className="h-40 w-full" />
      </main>
    )
  }
  if (!session.data) return <Navigate to="/login" />

  const handleLogout = async () => {
    await api.logout()
    qc.setQueryData(["session"], null)
    void navigate({ to: "/login" })
  }

  const firstModel = options.data?.models[0]
  const fallbackModel = options.data?.default_agent_model ?? firstModel?.id ?? ""
  const fallbackEffort =
    options.data?.default_agent_reasoning_effort ?? firstModel?.default_effort ?? ""

  const patch = (next: Parameters<typeof buildProfileUpdate>[1]) => {
    setError(null)
    setSaved(false)
    save
      .mutateAsync(
        buildProfileUpdate(current ?? undefined, next, fallbackModel, fallbackEffort)
      )
      .then(() => setSaved(true))
      .catch((e: Error) => setError(e.message))
  }

  const current = profile.data

  return (
    <AppShell user={session.data} title="设置" description="默认模型、目标仓库和审查偏好。">
      <div className="mb-6 inline-flex rounded-lg border border-border bg-muted p-0.5">
        {TABS.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setTab(item.id)}
            className={cn(
              "rounded-md px-3 py-1.5 text-[13px] transition-colors",
              tab === item.id
                ? "bg-card text-foreground font-medium shadow-none"
                : "text-muted-foreground hover:text-foreground"
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      {(error || saved) && (
        <p className={cn("mb-4 text-[13px]", error ? "text-destructive" : "text-success")}>
          {error ?? "已保存"}
        </p>
      )}

      {tab === "profile" && (
        <div className="space-y-6">
          <SettingsSection title="账号" description="登录身份，由控制台分配。">
            <SettingsRow
              label="邮箱"
              control={
                <span className="text-[13px] text-muted-foreground">
                  {session.data.email ?? session.data.login}
                </span>
              }
            />
          </SettingsSection>

          <SettingsSection title="默认模型" description="新建任务时预选的模型。">
            <SettingsRow
              label="模型"
              control={
                <Select
                  value={current?.default_model ?? fallbackModel ?? undefined}
                  onValueChange={(value) =>
                    patch({ default_model: value ?? fallbackModel })
                  }
                  disabled={profile.isLoading || save.isPending}
                >
                  <SelectTrigger className="w-56">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {(options.data?.models ?? []).map((model) => (
                      <SelectItem key={model.id} value={model.id}>
                        {model.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              }
            />
          </SettingsSection>

          <SettingsSection title="默认仓库" description="创建任务时默认操作的仓库（Gitee / GitHub / GitLab）。">
            <SettingsRow
              label="仓库"
              control={
                <Input
                  className="w-72"
                  value={current?.default_repo ?? ""}
                  placeholder="https://github.com/owner/repo"
                  onChange={(event) => patch({ default_repo: event.target.value })}
                />
              }
            />
            <SettingsRow
              label="基础分支"
              control={
                <Input
                  className="w-40"
                  value={current?.base_branch ?? ""}
                  placeholder="master"
                  onChange={(event) => patch({ base_branch: event.target.value })}
                />
              }
            />
          </SettingsSection>
        </div>
      )}

      {tab === "review" && (
        <div className="space-y-6">
          <SettingsSection title="审查偏好">
            <SettingsRow
              label="审查草稿 PR"
              description="你发起的草稿 PR 是否也进入审查队列。"
              control={
                <Switch
                  checked={current?.review_draft_prs ?? false}
                  onCheckedChange={(checked) => patch({ review_draft_prs: checked })}
                  disabled={save.isPending}
                />
              }
            />
            <SettingsRow
              label="任务结束后自动创建 PR"
              description="编码任务完成时自动推送分支并创建 Pull Request。"
              control={
                <Switch
                  checked={current?.create_prs ?? true}
                  onCheckedChange={(checked) => patch({ create_prs: checked })}
                  disabled={save.isPending}
                />
              }
            />
            <SettingsRow
              label="自动修复 CI"
              description="CI 失败时让 Agent 自动跟进修复。"
              control={
                <Switch
                  checked={current?.auto_fix_ci ?? true}
                  onCheckedChange={(checked) => patch({ auto_fix_ci: checked })}
                  disabled={save.isPending}
                />
              }
            />
          </SettingsSection>
        </div>
      )}

      {tab === "account" && (
        <SettingsSection title="账户" description="结束当前登录会话。">
          <SettingsRow
            label="退出登录"
            control={
              <Button size="sm" variant="outline" onClick={() => void handleLogout()}>
                退出登录
              </Button>
            }
          />
        </SettingsSection>
      )}
    </AppShell>
  )
}
