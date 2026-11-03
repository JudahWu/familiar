import { useSyncExternalStore } from "react"

/**
 * 侧栏入口的「点击选中」状态。
 *
 * 为什么需要它而不是组件内 useState：`/knowledge` 和 `/agents` 是两棵独立的路由树，
 * 点过去整个 `AgentsSidebar` 会重新挂载，组件内的 state 立刻丢，点击的高亮存不住。
 * 状态放在模块级变量里，组件重挂载后还能读到；整页刷新时模块重新加载、回到初始值，
 * 正好满足「进入页面不要默认高亮」。
 *
 * 只记一个值就够了——侧栏里需要表达选中的入口只有「新任务」和「知识库」两个。
 */

export type NavSelection = "knowledge" | "agents" | null

let selected: NavSelection = null
const listeners = new Set<() => void>()

export function setNavSelection(next: NavSelection): void {
  if (selected === next) return
  selected = next
  for (const listener of listeners) listener()
}

export function useNavSelection(): NavSelection {
  return useSyncExternalStore(
    (onChange) => {
      listeners.add(onChange)
      return () => listeners.delete(onChange)
    },
    () => selected,
    () => null,
  )
}
