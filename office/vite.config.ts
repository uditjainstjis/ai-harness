import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  // Relative asset paths so the build works under /VirtualOffice/ on GitHub Pages.
  base: './',
  plugins: [react()],
  css: { postcss: false },
  // `npm run build` writes straight into the Studio, which serves it at /office (no node needed at runtime)
  build: { outDir: '../pramana/web/static/office', emptyOutDir: true },
  server: { port: 5190, strictPort: true },
});
