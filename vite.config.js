import { defineConfig } from "vite";

export default defineConfig({
  root: "ui",
  publicDir: false,
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: "ui/index.html"
      }
    }
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ui/static": "http://127.0.0.1:8000"
    }
  }
});
