/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        obsidian: {
          canvas: "#0a0a0a",
          surface: "#121212",
          elevated: "#181818",
          card: "#141414",
          cardHover: "#1c1c1c",
          borderSubtle: "#222222",
          borderDefault: "#2a2a2a",
        },
        amber: {
          led: "#e5a00d",
          ledGlow: "rgba(229, 160, 13, 0.25)",
        },
        // In-progress / informational status colour. Restyle here, never with raw blue/sky classes.
        info: {
          300: "#93c5fd",
          400: "#60a5fa",
          500: "#3b82f6",
          800: "#1e40af",
          950: "#172554",
        },
      },
      borderRadius: {
        fillet: "4px",
        btn: "3px",
      },
      boxShadow: {
        'transport-bay': 'inset 0 2px 4px rgba(0, 0, 0, 0.8), 0 1px 0 rgba(255, 255, 255, 0.04)',
        'deck-btn': '0 2px 0 #050505, inset 0 1px 0 rgba(255, 255, 255, 0.08)',
        'deck-btn-engaged': 'inset 0 2px 5px rgba(0, 0, 0, 0.9), inset 0 0 0 1px rgba(255, 255, 255, 0.04)',
        'amber-jewel': '0 0 6px rgba(229, 160, 13, 0.8)',
      },
    },
  },
  plugins: [],
}
