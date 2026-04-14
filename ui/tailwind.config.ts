import type { Config } from 'tailwindcss'

const config: Config = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        'slack-purple': '#3F0E40',
        'slack-purple-dark': '#350D36',
        'slack-blue': '#1164A3',
        'slack-text': '#1D1C1D',
      },
    },
  },
  plugins: [],
}
export default config
