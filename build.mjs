// Build statis untuk Vercel: salin index.html ke dist/ dan tulis config.js dari env CLIPFORGE_API_URL.
import { cpSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';

const api = (process.env.CLIPFORGE_API_URL || '').trim().replace(/\/+$/, '');
const ok = !api || /^https:\/\/[A-Za-z0-9.-]+(:\d+)?(\/[^\s"'<>]*)?$/.test(api) || /^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(api);
if (!ok) {
  console.error(`CLIPFORGE_API_URL tidak valid: "${api}". Harus berupa https://host (tanpa spasi/kutip).`);
  process.exit(1);
}
rmSync('dist', { recursive: true, force: true });
mkdirSync('dist');
cpSync('index.html', 'dist/index.html');
writeFileSync('dist/config.js', `window.CLIPFORGE_API_URL = ${JSON.stringify(api)};\n`);
console.log(api ? `Build OK. Backend: ${api}` : 'Build OK. CLIPFORGE_API_URL kosong: atur alamat backend lewat Pengaturan di halaman.');
