//  @ts-check

import { tanstackConfig } from "@tanstack/eslint-config"

export default [
  {
    ignores: [
      ".output/**",
      ".nitro/**",
      ".tanstack/**",
      "dist/**",
      "src/routeTree.gen.ts",
      "src/components/ui/**",
      "src/components/agents/ported/**",
    ],
  },
  ...tanstackConfig,
  {
    rules: {
      // 误报率高：处理 API 返回、拖拽事件对象、跨边界类型时，`?.`/`??` 是必要的
      // 防御（运行时值可能真的是 undefined，与静态类型声明不符）。删掉反而引入 NPE。
      "@typescript-eslint/no-unnecessary-condition": "off",
    },
  },
]
