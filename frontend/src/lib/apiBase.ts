/**
 * 后端 API 基地址解析（dashboard api 与 agents api 共用）。
 *
 * 规则很简单，但每条都有原因：
 *
 * 1. **本地访问**（页面跑在 localhost / 127.0.0.1）→ 直连配置里的端口
 *    （默认 http://127.0.0.1:2024），只把 hostname 对齐成页面自己的 hostname。
 *    否则会碰上 SameSite=Lax 的坑：页面 localhost:3000、接口 127.0.0.1:2024
 *    会被浏览器当成跨站，登录写下的 Cookie 后续请求不带上，表现为 /me 一直 401。
 *
 * 2. **远程访问**（内网穿透域名、局域网 IP、真域名）→ 一律走同源相对路径，
 *    由 vite dev server 的 `/dashboard/api` 代理转发到后端（见 vite.config.ts）。
 *    因为内网穿透通常只映射了 80/443，直连 `:2024` 会 502；走同源还有个附带好处：
 *    Cookie 天然属于当前域名，不存在跨站问题。
 *
 * 想强制用绝对地址（比如前后端分开部署），设
 * `VITE_DASHBOARD_API_FORCE_ABSOLUTE=true`。
 */

const CONFIGURED_API_BASE = (import.meta.env.VITE_DASHBOARD_API_BASE_URL ?? "").replace(
  /\/$/,
  ""
)

const LOCAL_HOSTNAMES = new Set(["localhost", "127.0.0.1", "::1", "[::1]"])

function resolveApiBase(): string {
  if (!CONFIGURED_API_BASE) return ""
  if (typeof window === "undefined") return CONFIGURED_API_BASE

  const forceAbsolute = import.meta.env.VITE_DASHBOARD_API_FORCE_ABSOLUTE === "true"
  const pageHost = window.location.hostname

  // 远程访问：交给 dev server / 网关做同源代理
  if (!LOCAL_HOSTNAMES.has(pageHost) && !forceAbsolute) return ""

  try {
    const target = new URL(CONFIGURED_API_BASE, window.location.origin)
    if (target.hostname === pageHost) return CONFIGURED_API_BASE
    target.hostname = pageHost
    return target.origin
  } catch {
    return CONFIGURED_API_BASE
  }
}

export const API_BASE = resolveApiBase()

if (!API_BASE && typeof window !== "undefined") {
  console.info("[api] 接口走同源相对路径（由 dev server 的 /dashboard/api 代理转发）")
}
