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
      },
      animation: {
        'pulse-ring': 'pulse-ring 1.6s ease-out infinite',
        'slide-up': 'slide-up .35s ease-out both',
        pop: 'pop .4s cubic-bezier(.2,.9,.3,1.2) both',
        shimmer: 'shimmer 2s infinite',
      },
    },
  },
  plugins: [],
}
