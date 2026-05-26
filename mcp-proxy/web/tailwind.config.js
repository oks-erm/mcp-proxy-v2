/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["IBM Plex Sans", "system-ui", "sans-serif"],
        mono: ["IBM Plex Mono", "ui-monospace", "monospace"],
      },
      colors: {
        surface: {
          DEFAULT: "#0c0e12",
          raised: "#13161d",
          border: "#1e2430",
        },
        accent: {
          DEFAULT: "#3ee6b0",
          dim: "#2a9f7a",
        },
      },
    },
  },
  plugins: [],
};
