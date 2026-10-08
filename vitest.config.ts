import { defineConfig } from "vitest/config";

// Playwright、插件与 artifacts 内的复现脚本各自运行，不能由 Vitest 误收集。
export default defineConfig({
  test: {
    include: ["tests/js/**/*.test.ts"],
  },
});
