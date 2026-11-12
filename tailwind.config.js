/** Build the stylesheet with: npm run css */
module.exports = {
  content: ['./index.html'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Segoe UI Variable Display"', '"Segoe UI"', 'system-ui', 'Arial', 'sans-serif'],
        mono: ['"Cascadia Mono"', 'Consolas', '"Courier New"', 'monospace'],
      },
      colors: {
        ink: { 950: '#070b14', 900: '#0b1120', 800: '#111a2e', 700: '#1a2640', 600: '#27365a' },
        amber: { glow: '#f5b941' },
        cyanx: '#4fd8e0',
      },
    },
  },
};
