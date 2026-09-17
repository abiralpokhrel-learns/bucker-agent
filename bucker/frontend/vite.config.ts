import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  base: './',
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    rollupOptions: {
      input: {
        desktop: path.resolve(__dirname, 'desktop.html'),
        index: path.resolve(__dirname, 'index.html'),
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8123',
      '/tasks': 'http://localhost:8123',
      '/templates': 'http://localhost:8123',
      '/schedules': 'http://localhost:8123',
      '/skills': 'http://localhost:8123',
      '/memory': 'http://localhost:8123',
      '/health': 'http://localhost:8123',
      '/ws': {
        target: 'ws://localhost:8123',
        ws: true,
      },
    },
  },
});
