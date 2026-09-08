/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      keyframes: {
        'pulse-ring': {
          '0%':   { transform: 'scale(.92)', opacity: '.7' },
          '70%':  { transform: 'scale(1.25)', opacity: '0' },
          '100%': { transform: 'scale(1.25)', opacity: '0' },
        },
        'slide-up': {
          from: { transform: 'translateY(14px)', opacity: '0' },
          to:   { transform: 'translateY(0)', opacity: '1' },
        },
        'pop': {
          '0%':   { transform: 'scale(.85)', opacity: '0' },
          '60%':  { transform: 'scale(1.04)', opacity: '1' },
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
        'slide-up': 'slide-up .35s ease-out both',
        pop: 'pop .4s cubic-bezier(.2,.9,.3,1.2) both',
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
