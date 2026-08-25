import { defineConfig } from "vitest/config";

// .worktrees/ 是 git worktree 并行开发目录（分批实施用），其中的 tests/js 与
// 主检出同源，不排除会被 `vitest run tests/js` 的过滤器重复收集双跑。
export default defineConfig({
  test: {
    exclude: ["**/node_modules/**", "**/dist/**", ".worktrees/**"],
  },
});
