import { defineConfig } from "vitest/config";

// Playwright、插件与 artifacts 内的复现脚本各自运行，不能由 Vitest 误收集。
export default defineConfig({
  test: {
    // 8C/16T 工作站的已选并行上限；CLI 可为低内存环境覆盖。
    maxWorkers: 6,
    include: ["tests/js/**/*.test.ts"],
  },
});
