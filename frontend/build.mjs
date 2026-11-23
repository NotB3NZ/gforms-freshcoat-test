import {mkdir, copyFile, rm} from 'node:fs/promises';
await rm(new URL('./dist', import.meta.url), {recursive: true, force: true});
await mkdir(new URL('./dist', import.meta.url));
for (const name of ['index.html', 'app.js', 'camera.js', 'pending.js', 'review.js', 'style.css']) {
  await copyFile(new URL(name, import.meta.url), new URL(`dist/${name}`, import.meta.url));
}
await mkdir(new URL('./dist/vendor', import.meta.url));
for (const name of ['jsQR.js', 'jsQR.LICENSE']) await copyFile(new URL(`vendor/${name}`, import.meta.url), new URL(`dist/vendor/${name}`, import.meta.url));
console.log('Built dependency-free FreshCoat staff frontend.');
