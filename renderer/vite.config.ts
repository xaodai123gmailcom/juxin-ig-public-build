import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const rendererDir = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig(({ command }) => {
  // Publishing must not inherit a developer shell's React development mode.
  // Set this before Vite resolves isProduction and before plugins are created.
  if (command === "build") process.env.NODE_ENV = "production";
  return {
  root: rendererDir,
  resolve: { alias: { "@": resolve(rendererDir, "src") } },
  define: {
    "process.env.NEXT_PUBLIC_ENABLE_DEMO_DATA": JSON.stringify("false"),
    "process.env.NODE_ENV": JSON.stringify(process.env.NODE_ENV ?? "production")
  },
  plugins: [react()],
  base: "./",
  server: { host: "127.0.0.1", port: 5173, strictPort: true },
  build: { outDir: "dist", emptyOutDir: true }
  };
});
