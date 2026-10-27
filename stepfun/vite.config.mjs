// 作者：FDE WorkMate
// 前端构建：两个页面（应用 index.html、基准 bench.html），产物由 server/ 静态托管。
import { resolve } from "node:path";
import { defineConfig } from "vite";

export default defineConfig({
  root: "web",
  base: "/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
    rollupOptions: {
      input: {
        index: resolve(import.meta.dirname, "web/index.html"),
        bench: resolve(import.meta.dirname, "web/bench.html"),
      },
    },
  },
  worker: { format: "es" },
  server: {
    // 开发模式：API、模型和 ORT 运行时都走 node 服务
    proxy: { "/api": "http://127.0.0.1:8787", "/models": "http://127.0.0.1:8787", "/ort": "http://127.0.0.1:8787", "/data": "http://127.0.0.1:8787" },
    headers: { "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Embedder-Policy": "require-corp" },
  },
});
