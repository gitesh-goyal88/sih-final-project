/** UI-UX §13.1 design tokens — same palette as Android `Color.kt`. Red is reserved for emergency only (§3.1). */
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        primary:   { DEFAULT: '#1E4FA3', dark: '#163B7A', tint: '#EAF1FB' },
        emergency: '#D32F2F',
        success:   '#1E8E3E',
        warning:   '#F9A825',
        accent:    '#F7931E',
        offline:   '#6B7280',
        surface:   '#F4F6FA',
        ink:       { DEFAULT: '#1F2937', muted: '#5B6475' },
      },
      fontFamily: { sans: ['"Noto Sans"', '"Noto Sans Devanagari"', 'system-ui', 'sans-serif'] },
      minHeight:  { touch: '56px' },
    },
  },
  plugins: [],
};
