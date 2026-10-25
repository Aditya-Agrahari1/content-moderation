/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  preview: { port: 3000, strictPort: true },
  test: { environment: "jsdom", setupFiles: ["./src/setupTests.ts"] },
});
