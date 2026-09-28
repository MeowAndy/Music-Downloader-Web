// Unlock Music (um-web legacy v1.10.8) 的 mflac/mgg/qmc 解密模块
// 在 Node 中运行官方 worker（含 WASM 解密核心），支持内嵌 ekey 的新版格式
//
// 用法:
//   CLI:   node um_decrypt.js <mflac文件> [输出目录]
//   模块:  const { decryptFile } = require('./um_decrypt.js')
const fs = require('fs');
const path = require('path');

// um-web worker（与本文件同目录，随应用打包）
const WORKER_JS = path.join(__dirname, 'um_worker.js');

// ---------- 浏览器 worker 环境模拟 ----------
globalThis.self = globalThis;
globalThis.WorkerGlobalScope = function WorkerGlobalScope() {};
globalThis.window = globalThis;
globalThis.location = { href: 'file:///worker.js', origin: 'file://' };
globalThis.importScripts = () => {};
globalThis.addEventListener = () => {};
globalThis.removeEventListener = () => {};
globalThis.postMessage = () => {};

// 真实 XHR（worker 解密后联网取曲目信息/封面用，API 已下线时会走本地回退）
globalThis.XMLHttpRequest = require('../xhr.js');

// FileReader shim（必须触发 onloadend，parseBlob 依赖它）
globalThis.FileReader = class FileReader {
  constructor() {
    this.result = null;
    this.onload = null;
    this.onloadend = null;
    this.onerror = null;
    this.onabort = null;
    this.readyState = 0;
  }
  _done(result) {
    this.result = result;
    this.readyState = 2;
    if (this.onload) this.onload({ target: this });
    if (this.onloadend) this.onloadend({ target: this });
  }
  _fail(ev) {
    if (this.onerror) this.onerror({ type: ev });
    if (this.onloadend) this.onloadend({ target: this });
  }
  readAsArrayBuffer(blob) {
    blob.arrayBuffer().then(buf => this._done(buf)).catch(() => this._fail('error'));
  }
  readAsText(blob) {
    blob.text().then(txt => this._done(txt)).catch(() => this._fail('error'));
  }
  readAsDataURL(blob) {
    blob.arrayBuffer().then(buf => {
      this._done('data:application/octet-stream;base64,' + Buffer.from(buf).toString('base64'));
    }).catch(() => this._fail('error'));
  }
};

// ---------- 加载 worker 并捕获解密 API ----------
let src = fs.readFileSync(WORKER_JS, 'utf8');
// 在线元数据 API 已下线，加超时防止挂起（失败会回退本地标签）
src = src.replace(
  'case 2:return[4,f(a,r,o,e)]',
  'case 2:return[4,Promise.race([f(a,r,o,e),new Promise((_,rej)=>setTimeout(()=>rej(new Error("online meta timeout")),10000))])]'
);
// 捕获 expose 的解密函数
const hook = '}n(se)}';
const idx = src.indexOf(hook);
if (idx < 0) throw new Error('um worker: expose call not found');
src = src.slice(0, idx) + '}globalThis.__umDecrypt = se;}' + src.slice(idx + hook.length);

try {
  eval(src);
} catch (e) {
  throw new Error('um worker 加载失败: ' + (e && e.message));
}
if (typeof globalThis.__umDecrypt !== 'function') {
  throw new Error('um worker: 解密函数捕获失败');
}

// 支持的加密格式（与 worker 内部一致）
const SUPPORTED_EXTS = ['mflac', 'mflac0', 'mflach', 'mgg', 'mgg0', 'mgg1', 'mggl',
  'mmp4', 'qmcflac', 'qmcogg', 'qmc0', 'qmc2', 'qmc3', 'qmc4', 'qmc6', 'qmc8'];

/**
 * 解密一个加密音乐文件
 * @param {string} filePath 输入文件路径
 * @param {string} outDir 输出目录（默认输入文件所在目录）
 * @returns {Promise<{title, artist, ext, album, outPath, size}>}
 */
async function decryptFile(filePath, outDir) {
  const buf = fs.readFileSync(filePath);
  const name = path.basename(filePath);
  const blob = new Blob([buf]);
  const result = await globalThis.__umDecrypt({ raw: blob, name }, {});
  const outBuf = Buffer.from(await result.blob.arrayBuffer());
  if (outBuf.length < 1024) throw new Error('解密结果异常（内容过短）');

  outDir = outDir || path.dirname(filePath);
  fs.mkdirSync(outDir, { recursive: true });
  const base = name.replace(/\.(mflac|mgg|qmc)\w*$/i, '') || '未命名';
  const outPath = path.join(outDir, base + '.' + result.ext);
  fs.writeFileSync(outPath, outBuf);
  return {
    title: result.title || base,
    artist: result.artist || '',
    ext: result.ext,
    album: result.album || '',
    outPath,
    size: outBuf.length,
  };
}

module.exports = { decryptFile, SUPPORTED_EXTS };

// ---------- CLI 模式 ----------
if (require.main === module) {
  const input = process.argv[2];
  if (!input) {
    console.error('用法: node um_decrypt.js <mflac/mgg/qmc文件> [输出目录]');
    process.exit(1);
  }
  decryptFile(input, process.argv[3])
    .then(r => {
      console.log(JSON.stringify({ ok: true, ...r }));
    })
    .catch(e => {
      console.error(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
      process.exit(1);
    });
}
