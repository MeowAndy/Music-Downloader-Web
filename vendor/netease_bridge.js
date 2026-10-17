'use strict';
const fs = require('node:fs');
const path = require('node:path');
const output = process.stdout.write.bind(process.stdout);
for (const method of ['log', 'info', 'warn', 'error', 'debug']) console[method] = () => {};
process.env.NETEASE_COOKIE = '';
process.env.ENABLE_GENERAL_UNBLOCK = 'false';
process.env.ENABLE_RANDOM_CN_IP = 'false';
process.env.ENABLE_PROXY = 'false';
const MAX_INPUT = 128 * 1024;
const root = path.join(__dirname, 'netease');
const operations = {
  search: 'cloudsearch', detail: 'song_detail', download: 'song_download_url_v1', lyric: 'lyric',
  qr_check: 'login_qr_check', login_status: 'login_status',
};
function invalid() { throw Object.assign(new Error(), {safeCode: 'INVALID_INPUT'}); }
function text(value, max) {
  if (typeof value !== 'string' || !value.trim() || value.length > max || /[\x00-\x1f]/.test(value)) invalid();
  return value.trim();
}
function argsFor(op, args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) invalid();
  if (op === 'search') {
    const limit = Number(args.limit ?? 30);
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) invalid();
    return {keywords:text(args.keywords,200),limit,type:1,offset:0};
  }
  if (op === 'detail') {
    const ids = text(args.ids, 500);
    if (!/^\d{1,20}(,\d{1,20}){0,19}$/.test(ids)) invalid();
    return {ids};
  }
  if (op === 'download') {
    const id = text(String(args.id ?? ''),20);
    if (!/^\d{1,20}$/.test(id) || !['standard','exhigh','lossless'].includes(args.level)) invalid();
    return {id,level:args.level};
  }
  if (op === 'lyric') {
    // A single canonical ID only: no upstream URL, crypto, proxy or cookie options.
    if (Object.keys(args).length !== 1 || !Object.hasOwn(args, 'id')) invalid();
    if (typeof args.id !== 'string' || !/^[1-9][0-9]{0,19}$/.test(args.id)) invalid();
    return {id:args.id};
  }
  if (op === 'qr_check') return {key:text(args.key,512)};
  if (op === 'login_status' || op === 'qr_start' || op === 'selftest') return {};
  invalid();
}
function cookiesFor(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const cookie = {};
  for (const [key,val] of Object.entries(value)) {
    if (!/^[A-Za-z0-9_]{1,64}$/.test(key) || typeof val !== 'string' || val.length > 16384 || /[\x00-\x20;]/.test(val)) invalid();
    cookie[key] = val;
  }
  return cookie;
}
async function run(message) {
  const op = message.op;
  const args = argsFor(op, message.args || {});
  const temp = require('node:os').tmpdir();
  const token = path.join(temp,'anonymous_token');
  if (!fs.existsSync(token)) fs.writeFileSync(token,'',{flag:'wx'});
  const request = require(path.join(root,'util/request'));
  if (op === 'selftest') {
    for (const name of [...Object.values(operations),'login_qr_key','login_qr_create']) require(path.join(root,'module',name));
    const image = await require(path.join(root,'module/login_qr_create'))({key:'offline-test-only',qrimg:true});
    return {ok:true,body:{ready:image.body.data.qrimg.startsWith('data:image/png;base64,')},cookies:[]};
  }
  const cookie = ['qr_start','qr_check'].includes(op) ? {} : cookiesFor(message.cookie);
  const query = {...args,cookie,timeout:15000,randomCNIP:false};
  let result;
  if (op === 'qr_start') {
    const keyResult = await require(path.join(root,'module/login_qr_key'))(query,request);
    const data = keyResult.body?.data;
    if (data?.code !== 200 || typeof data.unikey !== 'string') throw new Error();
    const image = await require(path.join(root,'module/login_qr_create'))({key:data.unikey,qrimg:true});
    return {ok:true,body:{key:data.unikey,image:image.body.data.qrimg},cookies:[]};
  }
  result = await require(path.join(root,'module',operations[op]))(query,request);
  if (!result || !result.body || typeof result.body !== 'object') throw new Error();
  return {ok:true,body:result.body,cookies:Array.isArray(result.cookie)?result.cookie:[]};
}
async function main() {
  let chunks = [], size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT) invalid();
    chunks.push(chunk);
  }
  let message;
  try { message = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch (_) { invalid(); }
  if (!message || typeof message !== 'object' || Array.isArray(message)) invalid();
  return run(message);
}
if (require.main === module) {
  const deadline = setTimeout(() => {
    output(JSON.stringify({ok:false,code:'NETWORK_ERROR',error:'网易云请求超时，请稍后重试。'}));
    process.exit(0);
  },35000);
  main().then(result => {
    clearTimeout(deadline); output(JSON.stringify(result)); process.exit(0);
  }).catch(error => {
    clearTimeout(deadline);
    const code = error.safeCode || (error?.body?.code === 502 || error?.code === 'ECONNABORTED' ? 'NETWORK_ERROR' : 'UPSTREAM_ERROR');
    output(JSON.stringify({ok:false,code,error:code === 'INVALID_INPUT'?'网易云请求参数无效。':'网易云接口暂不可用，请检查网络后重试。'})); process.exit(0);
  });
}
module.exports = {argsFor,cookiesFor};
