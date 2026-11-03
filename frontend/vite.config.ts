import http from "node:http"
import {  defineConfig } from "vite"
import { devtools } from "@tanstack/devtools-vite"
import { tanstackStart } from "@tanstack/react-start/plugin/vite"
import viteReact from "@vitejs/plugin-react"
import viteTsConfigPaths from "vite-tsconfig-paths"
import tailwindcss from "@tailwindcss/vite"
import { nitro } from "nitro/vite"
import { VitePWA } from "vite-plugin-pwa"
import type {Plugin} from "vite";

/** 本地后端地址。dev server 内部代理用，浏览器不直接访问它。 */
const API_TARGET = process.env.DASHBOARD_API_TARGET ?? "http://127.0.0.1:2024"

/**
 * 把 /dashboard/api 反向代理到后端，让浏览器只发同源请求。
 *
 * 为什么不用 vite 的 `server.proxy`：这个项目用 @tanstack/react-start + nitro，
 * dev 时请求会先被应用自己的中间件接走，`server.proxy` 根本匹配不到，
 * 表现是 /dashboard/api/me 直接返回前端的 404 页面。所以这里手写一个
 * `enforce: "pre"` 的中间件，抢在应用之前处理。
 *
 * 为什么非要同源：会话 Cookie 是 SameSite=Lax。页面在 localhost:3000、
 * 接口在 127.0.0.1:2024 时浏览器视为跨站，登录写下的 Cookie 不会随 fetch 发出
 * （/me 一直 401）；换成内网穿透域名后直连 :2024 更是直接 502（隧道只映射 80）。
 * 同源代理一次解决这两个问题。
 */
function dashboardApiProxy(): Plugin {
  return {
    name: "dashboard-api-proxy",
    enforce: "pre",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url?.startsWith("/dashboard/api")) return next()
        const target = new URL(API_TARGET)
        const proxyReq = http.request(
          {
            protocol: target.protocol,
            hostname: target.hostname,
            port: target.port,
            path: req.originalUrl ?? req.url,
            method: req.method,
            headers: { ...req.headers, host: target.host },
          },
          (proxyRes) => {
            res.writeHead(proxyRes.statusCode ?? 502, proxyRes.headers)
            proxyRes.pipe(res)
          },
        )
        proxyReq.on("error", (err) => {
          res.statusCode = 502
          res.setHeader("content-type", "application/json")
          res.end(JSON.stringify({ detail: `后端不可达（${API_TARGET}）：${err.message}` }))
        })
        req.pipe(proxyReq)
      })
    },
  }
}

const config = defineConfig({
  plugins: [
    dashboardApiProxy(),
    devtools(),
    nitro(),
    viteTsConfigPaths({
      projects: ["./tsconfig.json"],
    }),
    tailwindcss(),
    tanstackStart({ spa: { enabled: true } }),
    VitePWA({
      injectRegister: false,
      registerType: "prompt",
      outDir: ".output/public",
      devOptions: {
        enabled: true,
      },
      integration: {
        closeBundleOrder: "pre",
      },
      manifest: {
        id: "/",
        name: "Familiar",
        short_name: "Familiar",
        description: "Enterprise AI coding service.",
        start_url: "/",
        scope: "/",
        display: "standalone",
        theme_color: "#000000",
        background_color: "#ffffff",
        icons: [
          {
            src: "/favicon.png",
            sizes: "192x192",
            type: "image/png",
          },
          {
            src: "/logo512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "any maskable",
          },
        ],
      },
      workbox: {
        navigateFallback: "/_shell.html",
        navigateFallbackDenylist: [/^\/dashboard\/api\//, /^\/_serverFn\//],
        globPatterns: ["**/*.{js,css,png,svg,ico,webmanifest}"],
        additionalManifestEntries: [
          { url: "/_shell.html", revision: new Date().toISOString() },
        ],
      },
    }),
    viteReact(),
  ],
  server: {
    host: '0.0.0.0', // 确保允许外部监听（通常配合内网穿透需要开启）
    allowedHosts: [
      // 'f6de463a.natappfree.cc', // 方法1：精确匹配当前报错的域名
      '.natappfree.cc'       // 方法2：如果你的 natapp 每次重启域名随机变动，建议直接写这一行泛匹配
    ],
  }
})

export default config
