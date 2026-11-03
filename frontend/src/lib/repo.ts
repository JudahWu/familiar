/** 支持的代码托管平台域名。 */
const KNOWN_HOSTS = ["gitee.com", "www.gitee.com", "github.com", "www.github.com", "gitlab.com", "www.gitlab.com"]

function stripGit(url: string): string {
  return url.replace(/\/$/, "").replace(/\.git$/i, "")
}

/**
 * 把仓库输入规范化成后端能直接解析的形式：
 *
 * - `owner/repo`            → 原样返回（历史写法，后端按 Gitee 处理）
 * - `https://gitee.com/o/r` → `owner/repo`（Gitee 是默认平台，不带域名更简洁）
 * - `https://github.com/o/r`→ `github.com/o/r`
 * - `git@gitlab.com:g/s/r.git` → `gitlab.com/g/s/r`
 *
 * 为什么要带域名：Gitee / GitHub / GitLab 都可能有同名的 owner/repo，
 * 只留 `owner/repo` 会分不清是哪一家。后端 `app/tools/scm.py` 会读 host 决定平台。
 * 自建 GitLab 也走这条路径（域名原样保留，后端用 GITLAB_BASE_URL 兜底识别）。
 */
export function normalizeRepoFullName(raw: string): string | null {
  let v = raw.trim()
  if (!v) return null

  // git@host:owner/repo(.git)
  const scp = v.match(/^git@([^:]+):(.+)$/i)
  if (scp) {
    v = `${scp[1]}/${scp[2]}`
  } else {
    v = v.replace(/^[a-z]+:\/\//i, "")
    // 去掉可能带上的 user@ 前缀
    v = v.replace(/^[^/@]+@/, "")
  }

  const parts = v.split("/").filter(Boolean)
  const [first, second] = parts
  if (parts.length === 2 && first && second) {
    // 没有域名：按历史写法当作 Gitee 的 owner/repo
    return `${first}/${stripGit(second)}`
  }
  if (parts.length < 3 || !first) return null

  const host = first.toLowerCase()
  const path = parts.slice(1).map((part, index) =>
    index === parts.length - 2 ? stripGit(part) : part,
  )
  const normalizedHost = host.startsWith("www.") ? host.slice(4) : host
  const fullPath = path.join("/")

  if (normalizedHost === "gitee.com") {
    // Gitee 是默认平台，保持 owner/repo 的旧形式，避免影响已有数据
    return path.length === 2 ? fullPath : null
  }
  if (KNOWN_HOSTS.includes(host) || path.length >= 2) {
    return `${normalizedHost}/${fullPath}`
  }
  return null
}
