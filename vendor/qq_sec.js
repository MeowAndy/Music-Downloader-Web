// QQ音乐 musics.fcg 签名+加密通道模块
// 提取自 y.qq.com 官方 vendor.chunk 的请求模块（含 sign/encrypt/decrypt VM 代码）
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, 'new_sign_module.js'), 'utf8');

// regenerator-runtime（模块内的 transpile 代码依赖）
try {
  require('regenerator-runtime/runtime');
} catch (e) {
  require(path.join(__dirname, 'node_modules', 'regenerator-runtime', 'runtime.js'));
}

// 浏览器全局环境模拟（模块直接引用 window/document/location 等）
const browserEnv = {
  location: { href: 'https://y.qq.com/n/ryqq/playlist', protocol: 'https:', host: 'y.qq.com', hostname: 'y.qq.com', origin: 'https://y.qq.com' },
  navigator: { userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36', language: 'zh-CN', platform: 'Win32' },
  document: { cookie: '', createElement: () => ({ setAttribute() {}, appendChild() {} }), addEventListener() {}, removeEventListener() {} },
  setTimeout, clearTimeout, setInterval, clearInterval,
  XMLHttpRequest: require('./xhr.js'),
  addEventListener() {}, removeEventListener() {},
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
  btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
};
for (const [k, v] of Object.entries(browserEnv)) {
  if (!(k in globalThis)) globalThis[k] = v;
}
globalThis.window = globalThis;
globalThis.self = globalThis;

const moduleFn = eval('(' + src + ')');

// 模块81 = 浏览器全局对象模拟
const inner = { define: () => {} };
const g81 = new Proxy(inner, {
  get(target, prop) {
    if (prop === 'global' || prop === 'window' || prop === 'self') return g81;
    if (prop in target) return target[prop];
    return globalThis[prop];
  },
  set(target, prop, value) { target[prop] = value; return true; },
  has(target, prop) { return prop in target || prop in globalThis; },
});

const t = {};
const n = (id) => { if (id === 81) return g81; throw new Error('module ' + id + ' not shimmed'); };
n.d = (exports, name, getter) => Object.defineProperty(exports, name, { enumerable: true, get: getter });

moduleFn.call({}, {}, t, n);

module.exports = { exports: t, global81: g81 };
