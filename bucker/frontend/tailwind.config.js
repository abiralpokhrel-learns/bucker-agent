/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        ide: {
          bg: '#0d1117',
          sidebar: '#161b22',
          panel: '#1c2333',
          border: '#30363d',
          active: '#21262d',
          hover: '#30363d',
          text: '#e6edf3',
          muted: '#8b949e',
          blue: '#58a6ff',
          green: '#3fb950',
          red: '#f85149',
          amber: '#d29922',
          purple: '#bc8cff',
          cyan: '#39c5cf',
        }
      }
    },
  },
  plugins: [],
}
