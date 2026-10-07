/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        fordBlue: '#003478',
        fordDark: '#1a1c23',
      }
    },
  },
  plugins: [],
}
