/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  darkMode: 'media',
  theme: {
    extend: {
      fontFamily: {
        sans: ['-apple-system', 'system-ui', 'Segoe UI', 'sans-serif'],
      },
      colors: {
        page: 'var(--page)',
        surface: 'var(--surface)',
        'surface-2': 'var(--surface-2)',
        ink: 'var(--ink)',
        'ink-2': 'var(--ink-2)',
        muted: 'var(--muted)',
        hairline: 'var(--hairline)',
        accent: 'var(--accent)',
        'accent-ink': 'var(--accent-ink)',
        danger: 'var(--danger)',
        'danger-ink': 'var(--danger-ink)',
        success: 'var(--success)',
        'success-ink': 'var(--success-ink)',
      },
      boxShadow: {
        card: '0 1px 2px rgb(var(--shadow-color) / 0.04), 0 10px 30px -8px rgb(var(--shadow-color) / 0.14)',
        pop: '0 20px 60px -12px rgb(var(--shadow-color) / 0.35)',
      },
      keyframes: {
        'pulse-ring': {
          '0%':   { transform: 'scale(.92)', opacity: '.7' },
          '70%':  { transform: 'scale(1.25)', opacity: '0' },
          '100%': { transform: 'scale(1.25)', opacity: '0' },
        },
        'slide-up': {
          from: { transform: 'translateY(10px)', opacity: '0' },
          to:   { transform: 'translateY(0)', opacity: '1' },
        },
        'pop': {
          '0%':   { transform: 'scale(.9)', opacity: '0' },
          '60%':  { transform: 'scale(1.02)', opacity: '1' },
          '100%': { transform: 'scale(1)', opacity: '1' },
        },
        shimmer: {
          '100%': { transform: 'translateX(100%)' },
        },
        // ---- Engine Room (frontend/src/pages/EngineRoom.jsx) ----
        shake: {
          '0%, 100%': { transform: 'translateX(0)' },
          '20%': { transform: 'translateX(-4px)' },
          '40%': { transform: 'translateX(4px)' },
          '60%': { transform: 'translateX(-3px)' },
          '80%': { transform: 'translateX(3px)' },
        },
        'lock-clamp': {
          '0%, 100%': { transform: 'scaleY(1)' },
          '50%': { transform: 'scaleY(.85)' },
        },
        blink: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '.25' },
        },
        rise: {
          from: { transform: 'translateY(10px)', opacity: '0' },
          to: { transform: 'translateY(0)', opacity: '1' },
        },
      },
      animation: {
        'pulse-ring': 'pulse-ring 1.6s ease-out infinite',
        'slide-up': 'slide-up .35s cubic-bezier(0.16,1,0.3,1) both',
        pop: 'pop .4s cubic-bezier(0.16,1,0.3,1) both',
        shimmer: 'shimmer 2s infinite',
        shake: 'shake .5s ease-in-out infinite',
        'lock-clamp': 'lock-clamp 2s ease-in-out infinite',
        blink: 'blink 1.4s ease-in-out infinite',
        rise: 'rise .5s ease-out both',
      },
    },
  },
  plugins: [],
}
