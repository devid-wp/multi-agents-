/** @type {import('tailwindcss').Config} */
export default {
  content: ["./ui/**/*.{html,js}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        "bg-1": "var(--bg-1)",
        "bg-2": "var(--bg-2)",
        "bg-3": "var(--bg-3)",
        fg: "var(--fg)",
        "fg-dim": "var(--fg-dim)",
        "fg-muted": "var(--fg-muted)",
        border: "var(--border)",
        accent: "var(--accent)",
        "accent-2": "var(--accent-2)",
        ok: "var(--ok)",
        info: "var(--info)",
        critic: "var(--critic)",
        executor: "var(--executor)",
        error: "var(--error)"
      },
      fontFamily: {
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"]
      }
    }
  },
  plugins: []
};
