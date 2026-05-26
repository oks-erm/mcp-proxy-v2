import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Production build is served under /app/ on the API; Vite dev uses / so http://localhost:5173/login works.
export default defineConfig(({ mode }) => {
  const base = mode === "production" ? "/app/" : "/";
  return {
    plugins: [
      react(),
      {
        name: "html-favicon",
        transformIndexHtml(html) {
          const href = `${base}favicon.svg`;
          const link = `<link rel="icon" type="image/svg+xml" href="${href}" />\n    `;
          if (html.includes('rel="icon"')) return html;
          return html.replace("<title>", `${link}<title>`);
        },
      },
    ],
    base,
    server: {
      port: 5173,
      proxy: {
        "/auth": "http://127.0.0.1:8080",
        "/me": "http://127.0.0.1:8080",
        "/admin": "http://127.0.0.1:8080",
        "/users": "http://127.0.0.1:8080",
        "/health": "http://127.0.0.1:8080",
      },
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  };
});
