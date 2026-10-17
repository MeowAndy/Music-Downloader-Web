// Run with Node.js 18+ from any working directory; no npm packages required.
// These are frontend logic tests, not browser rendering or live API/codec tests.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const frontendPath = path.join(__dirname, '..', 'static', 'index.html');
const html = fs.readFileSync(frontendPath, 'utf8');
const embedded = html.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(embedded, 'index.html must contain its frontend script');
new vm.Script(embedded[1], { filename: frontendPath });
// Suppress only automatic startup requests, retaining real function definitions
// and event bindings. Fail explicitly if the startup block changes.
const startup = /refreshFiles\(\);\s*initializeLogin\(\);\s*neteaseLogin.refreshStatus\(\);\s*refreshVip\(\);\s*refreshVocal\(\);\s*$/;
assert.ok(startup.test(embedded[1]), 'Update the VM startup suppression for the new frontend entry point');
const script = embedded[1].replace(startup, '');
let assertions = 0;
function check(value, message) { assert.ok(value, message); assertions++; }
function equal(actual, expected, message) { assert.deepEqual(actual, expected, message); assertions++; }
function setup(storage = {}) {
  const ids = new Map(), mutations = [];
  class Element {
    constructor(name = '') {
      this.name = name; this.dataset = {}; this.style = {}; this.disabled = false; this.hidden = false;
      this.value = ''; this.checked = false; this.textContent = ''; this._html = ''; this.children = []; this.parent = null;
      this.classes = new Set(); this.listeners = new Map(); this.attributes = new Map(); this.focusCount = 0;
      this.classList = { add: s => this.classes.add(s), remove: s => this.classes.delete(s), contains: s => this.classes.has(s), toggle: (s, on) => on ? this.classes.add(s) : this.classes.delete(s) };
    }
    set innerHTML(value) {
      mutations.push({ type: 'html', target: this, children: [...this.children], threadParent: ids.get('#otherThread')?.parent, threadHidden: ids.get('#otherThread')?.hidden });
      for (const child of [...this.children]) this.removeChild(child);
      this._html = value;
      if (this.name === '#otherResults') {
        this.forumRows = [...value.matchAll(/<article class="forum-row"[^>]*>[\s\S]*?data-other-index="(\d+)"/g)].map(match => {
          const row = new Element('forum-row'); row.className = 'forum-row'; this.appendChild(row);
          const button = new Element('forum-title'); button.className = 'forum-title'; button.dataset.otherIndex = match[1]; row.appendChild(button);
          return row;
        });
        this.forumButtons = this.forumRows.map(row => row.children[0]);
      }
      if (this.name === '#otherThreadLinks') {
        this.copyButtons = [...value.matchAll(/<button[^>]*data-other-copy="([^"]+)"[^>]*data-resource-index="(\d+)"[^>]*>/g)].map(match => {
          const button = new Element(); button.dataset.otherCopy = match[1]; button.dataset.resourceIndex = match[2];
          button.disabled = /\sdisabled(?:\s|>)/.test(match[0]); this.appendChild(button); return button;
        });
      }
      if (this.name === '#result') {
        this.rows = [...value.matchAll(/data-song-index="(\d+)"([\s\S]*?)(?=<div class="song-row-wrap"|$)/g)].map(match => {
          const row = new Element('row'); row.className = 'song-row-wrap'; this.appendChild(row); row.actions = {};
          for (const action of ['download-action', 'lyric-preview-action', 'lyric-download-action']) {
            const tag = [...match[0].matchAll(/<button[^>]*>/g)].find(m => (m[0].match(/class="([^"]*)"/)?.[1] || '').split(/\s+/).includes(action));
            if (tag) { const b = new Element(action); row.appendChild(b); b.disabled = /\sdisabled(?:\s|>)/.test(tag[0]); if (tag[0].includes('data-unavailable="true"')) b.dataset.unavailable = 'true'; row.actions['.' + action] = b; }
          }
          row.slot = new Element('slot'); row.appendChild(row.slot);
          return row;
        });
        for (const id of ['dlAll', 'lyricAll', 'pbar', 'pfill', 'ptext']) {
          ids.delete('#' + id);
          const tag = value.match(new RegExp('<[^>]*id="' + id + '"[^>]*>'));
          if (tag) { const el = new Element('#' + id); el.disabled = /\sdisabled(?:\s|>)/.test(tag[0]); if (tag[0].includes('data-unavailable="true"')) el.dataset.unavailable = 'true'; ids.set('#' + id, el); }
        }
      }
    }
    get innerHTML() { return this._html; }
    querySelector(selector) {
      if (selector === '.lyric-preview-slot') return this.slot;
      if (selector === '.lyric-preview') return this.children.find(c => c.className === 'lyric-preview') || null;
      if (this.actions) return this.actions[selector] || null;
      if (selector.includes(':disabled')) return (this.rows || []).flatMap(r => Object.values(r.actions)).find(b => b.disabled && !b.classList.contains('done')) || null;
      return null;
    }
    querySelectorAll(selector) {
      if (selector === 'button, input, a[href], [tabindex="0"]' || selector === 'a[href]') {
        const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
        return [...new Set(descendants)].filter(child => (child.tagName === 'A' && child.getAttribute('href')) || (selector !== 'a[href]' && (['BUTTON', 'INPUT'].includes(child.tagName) || child.getAttribute('tabindex') === '0')));
      }
      if (selector === '.forum-row') return this.forumRows || [];
      if (selector === '[data-other-index]') return this.forumButtons || [];
      if (selector === '[data-other-copy]') return this.copyButtons || [];
      if (selector === '.song-row-wrap') return this.rows || [];
      if (selector === '.download-action') return (this.rows || []).map(r => r.actions['.download-action']);
      if (selector === 'button') return [...(this.rows || []).flatMap(r => Object.values(r.actions)), ...['#dlAll', '#lyricAll'].map(id => ids.get(id)).filter(Boolean)];
      return [];
    }
    get parentNode() { return this.parent; }
    get parentElement() { return this.parent; }
    closest(selector) {
      for (let node = this; node; node = node.parent) {
        if (selector === '[hidden]' && node.hidden) return node;
        if (selector.startsWith('.') && (node.className || '').split(/\s+/).includes(selector.slice(1))) return node;
      }
      return null;
    }
    contains(node) { return !!node && (this === node || this.children.some(child => child.contains(node))); }
    appendChild(child) {
      assert.ok(!child.contains(this), 'appendChild must not create a DOM cycle');
      if (child.parent) child.parent.removeChild(child);
      child.parent = this; this.children.push(child);
      mutations.push({ type: 'append', target: this, child, hidden: child.hidden });
      return child;
    }
    removeChild(child) {
      const index = this.children.indexOf(child);
      assert.notEqual(index, -1, 'removeChild requires a direct child');
      this.children.splice(index, 1); child.parent = null;
      return child;
    }
    remove() { if (this.parent) this.parent.removeChild(this); }
    addEventListener(name, fn) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(fn); }
    removeEventListener(name, fn) { this.listeners.set(name, (this.listeners.get(name) || []).filter(listener => listener !== fn)); }
    dispatchEvent(event) {
      event.target ??= this; event.currentTarget = this;
      this['on' + event.type]?.(event);
      for (const fn of this.listeners.get(event.type) || []) fn(event);
      return !event.defaultPrevented;
    }
    focus() { document.activeElement = this; this.focusCount++; }
    get isConnected() { return this.name.startsWith('#') || !!this.parent?.isConnected; }
    setAttribute(key, value) { this.attributes.set(key, String(value)); this[key] = String(value); }
    getAttribute(key) { return this.attributes.get(key) ?? null; }
    removeAttribute(key) { this.attributes.delete(key); delete this[key]; }
  }
  const get = key => { if (!ids.has(key)) ids.set(key, new Element(key)); return ids.get(key); };
  const sources = ['qq', 'netease', 'migu', 'other'].map(source => { const e = new Element(); e.dataset.source = source; return e; });
  const formats = ['standard', 'hq', 'sq', 'master'].map(fmt => { const e = new Element(); e.dataset.fmt = fmt; return e; });
  const options = ['#lyricsEnabled', '#lyricsFormat', '#lyricsTranslation', '#lyricsRomanization'].map(get);
  const documentEvents = new Map();
  const document = {
    querySelector: get,
    querySelectorAll: selector => selector === '.source-btn' ? sources : selector === '.fmt-btn' ? formats : [...sources, ...formats, ...options],
    createElement: () => new Element(), documentElement: new Element(), body: new Element('body'), activeElement: null,
    addEventListener(name, fn) { if (!documentEvents.has(name)) documentEvents.set(name, []); documentEvents.get(name).push(fn); },
    removeEventListener(name, fn) { documentEvents.set(name, (documentEvents.get(name) || []).filter(listener => listener !== fn)); },
    dispatchEvent(event) { event.target ??= document; for (const fn of documentEvents.get(event.type) || []) fn(event); return !event.defaultPrevented; }
  };
  document.activeElement = document.body;
  const calls = [], messages = [], timers = new Map(), events = new Map();
  let timerId = 0;
  for (const id of ['#loginPanel', '#neteaseLoginPanel', '#miguLoginPanel']) get(id).style.display = 'none';
  for (const id of ['#otherPanel', '#otherLoginPanel', '#otherThread', '#otherPagination']) get(id).hidden = true;
  get('#otherPanel').appendChild(get('#otherResults'));
  get('#otherPanel').appendChild(get('#otherThread'));
  for (const id of ['#otherThreadTitle', '#otherThreadText', '#otherThreadNotice', '#otherThreadLinks', '#otherCopyStatus', '#otherCopyText', '#otherThreadReload', '#otherThreadClose', '#otherThreadOfficial']) get('#otherThread').appendChild(get(id));
  get('#otherScope').value = 'title';
  // Retain the actual modal's hierarchy/attributes so focus-trap tests cannot
  // mistake hidden registration inputs for visible login controls.
  const modalMarkup = html.match(/<div\b[^>]*id="otherLoginPanel"[\s\S]*?(?=<script>)/)?.[0];
  if (modalMarkup) {
    const stack = [];
    for (const token of modalMarkup.matchAll(/<\/?([a-z][\w-]*)\b[^>]*>/gi)) {
      const tag = token[1].toLowerCase();
      if (token[0].startsWith('</')) { stack.pop(); continue; }
      const id = token[0].match(/\bid="([^"]+)"/)?.[1];
      const element = id ? get('#' + id) : new Element(tag);
      element.tagName = tag.toUpperCase();
      for (const attribute of token[0].matchAll(/\s([\w-]+)(?:="([^"]*)")?/g)) element.setAttribute(attribute[1], attribute[2] ?? '');
      element.hidden = /\shidden(?:\s|>)/.test(token[0]); element.disabled = /\sdisabled(?:\s|>)/.test(token[0]);
      if (stack.length) stack.at(-1).appendChild(element);
      if (!['input', 'br', 'hr', 'img', 'meta', 'link'].includes(tag)) stack.push(element);
    }
  }
  const sandbox = { document, localStorage: { getItem: k => storage[k] ?? null, setItem: (k,v) => storage[k] = v }, window: { addEventListener: (name, fn) => { if (!events.has(name)) events.set(name, []); events.get(name).push(fn); } }, console, setTimeout: fn => { timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id), setInterval: () => 1, clearInterval() {}, confirm: () => true, URL, URLSearchParams };
  sandbox.fetch = async url => { calls.push(url); return { ok: true, json: async () => ({ ok: true, name: 'saved.txt', size: 20, sections: { lyric: '<original>' }, warnings: [] }) }; };
  const context = vm.createContext(sandbox);
  vm.runInContext(script, context);
  vm.runInContext('refreshFiles = () => {}; toast = (message, error) => testMessages.push({message, error});', Object.assign(context, { testMessages: messages }));
  return {
    context, get, calls, messages, sources, formats, storage, events, documentEvents, timers, mutations,
    keydown(key, options = {}) {
      const event = { type: 'keydown', key, shiftKey: false, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...options };
      document.dispatchEvent(event);
      for (const fn of events.get('keydown') || []) fn(event);
      return event;
    },
    async tick() { const next = timers.entries().next().value; if (next) { timers.delete(next[0]); await next[1](); } await new Promise(setImmediate); },
    evaluate: code => vm.runInContext(code, context)
  };
}
async function main() {
  {
    const t = setup();
    equal(t.evaluate('_lyricsFormat'), 'txt', 'default TXT');
    t.context.song = { id: '123&x=2', mid: 'qq?mid', name: '歌 & name', singer: 'A/B + C', file: { media_mid: 'm&1' } };
    let u = new URL(t.evaluate('lyricsUrl(song, "netease", true)'), 'http://offline');
    equal(u.pathname, '/api/netease/lyrics'); equal(u.searchParams.get('id'), t.context.song.id); equal(u.searchParams.get('format'), 'txt'); equal(u.searchParams.get('preview'), '1'); check(!u.searchParams.has('mid'));
    t.evaluate('_lyricsFormat="lrc"; _lyricsEnabled=true; _lyricsTranslation=true; _lyricsRomanization=true; _source="netease"; _fmt="hq"; _sourceFormats.qq="master"; _sourceFormats.netease="hq";');
    u = new URL(t.evaluate('lyricsUrl(song, "netease")'), 'http://offline');
    equal(u.pathname, '/api/netease/lyrics/download'); equal(u.searchParams.get('name'), t.context.song.name); equal(u.searchParams.get('singer'), t.context.song.singer);
    t.evaluate('_sourceLyrics.qq = lyricsOptions();'); // Explicit per-provider preferences, not whichever platform is active.
    for (const builder of ['neteaseDownloadUrl(song)', 'qqDownloadUrl(song)']) {
      const audio = new URL(t.evaluate(builder), 'http://offline');
      equal(audio.searchParams.get('lyrics'), '1'); equal(audio.searchParams.get('lyrics_format'), 'lrc'); equal(audio.searchParams.get('translation'), '1'); equal(audio.searchParams.get('romanization'), '1');
      equal(audio.searchParams.get('quality'), builder.startsWith('qq') ? 'master' : 'hq');
    }
    equal(u.searchParams.get('format'), 'lrc'); equal(u.searchParams.get('translation'), '1'); equal(u.searchParams.get('romanization'), '1');
    u = new URL(t.evaluate('lyricsUrl(song)'), 'http://offline'); equal(u.pathname, '/api/lyrics/download'); equal(u.searchParams.get('mid'), t.context.song.mid); check(!u.searchParams.has('id'));
    t.evaluate('_lyricsEnabled=false;'); check(!new URL(t.evaluate('neteaseDownloadUrl(song)'), 'http://offline').searchParams.has('lyrics'));
    t.evaluate('_sourceFormats.netease="master";'); equal(new URL(t.evaluate('neteaseDownloadUrl(song)'), 'http://offline').searchParams.get('quality'), 'sq');
    equal(setup({ lyricsFormat: 'bad' }).evaluate('_lyricsFormat'), 'txt');
    console.log('PASS query builders: identity, encoding, TXT/LRC, flags, sidecars and provider-specific quality');
  }
  {
    const t = setup();
    t.evaluate('closeQqLogin = () => {}; selectSource("netease");');
    check(!t.get('.lyrics-options').hidden); check(t.formats.find(b => b.dataset.fmt === 'master').disabled);
    t.evaluate('selectSource("qq");'); check(!t.get('.lyrics-options').hidden); check(!t.formats.find(b => b.dataset.fmt === 'master').disabled);
    for (const source of ['qq', 'netease']) {
      t.context.source = source;
      t.evaluate('renderSongs([{id: 1, mid: "q", name: "song"}], "test", source === "netease" ? neteaseDownloadUrl : undefined, {source});');
      check(t.get('#result').innerHTML.includes('lyric-preview-action')); check(t.get('#result').innerHTML.includes('id="lyricAll"'));
      const row = t.get('#result').rows[0];
      t.evaluate('var bound = []; previewLyrics = (b,s,p) => bound.push(["preview",p]); downloadLyrics = (b,s,p) => bound.push(["lyrics",p]); downloadSong = (b,url) => bound.push(["audio",url]); downloadAllLyrics = (s,p) => bound.push(["batchLyrics",p]); downloadAll = (s,fn) => bound.push(["batchAudio",fn(s[0])]); runResultBatch = fn => fn(); _source = source === "qq" ? "netease" : "qq";');
      row.actions['.lyric-preview-action'].onclick(); row.actions['.lyric-download-action'].onclick(); row.actions['.download-action'].onclick(); t.get('#lyricAll').onclick(); t.get('#dlAll').onclick();
      equal(t.evaluate('JSON.stringify(bound.map(x => x[0]))'), JSON.stringify(['preview', 'lyrics', 'audio', 'batchLyrics', 'batchAudio']));
      for (const index of [0,1,3]) equal(t.evaluate(`bound[${index}][1]`), source);
      for (const index of [2,4]) equal(new URL(t.evaluate(`bound[${index}][1]`), 'http://offline').pathname, source === 'netease' ? '/api/netease/download' : '/api/download');
    }
    t.evaluate('_source="netease"; renderPlaylist({name:"playlist",songs:[{mid:"QQ",name:"p"}]}); bound=[];');
    t.get('#result').rows[0].actions['.lyric-download-action'].onclick(); t.get('#result').rows[0].actions['.download-action'].onclick();
    equal(t.evaluate('bound[0][1]'), 'qq'); equal(new URL(t.evaluate('bound[1][1]'), 'http://offline').pathname, '/api/download');
    console.log('PASS provider binding: old row/batch actions ignore changed _source; QQ playlist default');
  }
  {
    const t = setup();
    t.evaluate('renderSongs([{id:1,name:"n"}], "n", neteaseDownloadUrl, {source:"netease"});');
    const row = t.get('#result').rows[0]; t.context.btn = row.actions['.lyric-preview-action']; t.context.song = {id:1,name:'n'};
    let resolve;
    t.context.fetch = url => { t.calls.push(url); return new Promise(r => resolve = r); };
    let pending = t.evaluate('previewLyrics(btn,song,"netease")');
    t.evaluate('_searchGeneration++;'); resolve({ok:true,json:async()=>({ok:true,sections:{lyric:'stale'}})}); await pending;
    equal(row.slot.children.length, 0); check(!t.context.btn.disabled); equal(t.messages.length, 0);
    pending = t.evaluate('previewLyrics(btn,song,"netease")');
    resolve({ok:true,json:async()=>({ok:true,sections:{lyric:'<lyric>'},warnings:['<warning>'],truncated:true})}); await pending;
    equal(row.slot.children.length, 1); check(row.slot.children[0].innerHTML.includes('&lt;lyric&gt;')); check(row.slot.children[0].innerHTML.includes('&lt;warning&gt;')); check(row.slot.children[0].innerHTML.includes('预览已截断'));
    await t.evaluate('previewLyrics(btn,song,"netease")'); equal(row.slot.children.length, 0);
    pending = t.evaluate('previewLyrics(btn,song,"netease")'); t.evaluate('renderSongs([],"replacement");'); resolve({ok:false,json:async()=>({ok:false,error:'stale error'})}); await pending; equal(t.messages.length, 0);
    console.log('PASS async preview: stale success/error suppression, escaping, warnings, truncation and toggle');
  }
  {
    const t = setup();
    t.context.songs = [1,2,3,4].map(id => ({id, name:'track'+id}));
    t.evaluate('_source="netease"; renderSongs(songs,"batch",neteaseDownloadUrl,{source:"netease"}); _lyricsEnabled=true; _lyricsFormat="lrc";');
    const responses = [
      {ok:true,name:'one.flac',size:100,converted:true,format:'flac'},
      {ok:true,name:'two.ncm',size:100,conversion_error:'decode problem',lyrics_error:'missing lyric',lyrics_warnings:['translation missing']},
      {ok:true,name:'three.mp3',size:100,lyrics_name:'three.lrc',lyrics_warnings:['romanization missing']},
      {ok:false,error:'rights denied'}
    ];
    t.context.fetch = async url => { const i=t.calls.length; t.calls.push(url); t.evaluate('_lyricsFormat="txt"; _source="qq";'); return {ok:i!==3,json:async()=>responses[i]}; };
    await t.evaluate('downloadAll(songs,neteaseDownloadUrl)');
    equal(t.calls.length,4); check(t.calls.every(url=>new URL(url,'http://offline').pathname==='/api/netease/download')); check(t.calls.every(url=>new URL(url,'http://offline').searchParams.get('lyrics_format')==='lrc'));
    const report=t.get('#ptext').textContent;
    for (const text of ['成功 3 首','失败 1 首','转换成功 1 首','转换警告 1 首','歌词警告 2 首','decode problem','已保留 NCM','已转换为 flac','missing lyric','romanization missing','rights denied']) check(report.includes(text),text);
    const buttons=t.get('#result').rows.map(r=>r.actions['.download-action']); check(buttons[1].classList.contains('done')); check(buttons[2].classList.contains('done')); check(!buttons[3].classList.contains('done'));
    t.calls.length=0;
    t.context.fetch=async url=>{t.calls.push(url);return {ok:true,json:async()=>({ok:true,name:'retry.mp3',size:100})};};
    await t.evaluate('downloadAll(songs,neteaseDownloadUrl)'); equal(t.calls.length,1); check(t.get('#ptext').textContent.includes('转换警告 1 首')); check(t.get('#ptext').textContent.includes('歌词警告 2 首'));
    console.log('PASS audio batches: separate warning counts, format/reasons, success preserved, retry feedback, query snapshots');
  }
  {
    const t=setup(); t.context.songs=[{id:1,name:'a'},{id:2,name:'b'}];
    t.evaluate('renderSongs(songs,"lyrics",neteaseDownloadUrl,{source:"netease"});');
    t.context.fetch=async url=>{const i=t.calls.length;t.calls.push(url);return {ok:i===0,json:async()=>i===0?{ok:true,name:'a.txt',size:1,warnings:['translation absent']}:{ok:false,error:'not found'}};};
    await t.evaluate('downloadAllLyrics(songs,"netease")');
    check(t.calls.every(u=>new URL(u,'http://offline').pathname==='/api/netease/lyrics/download'));
    for(const text of ['成功 1 首','失败 1 首','歌词警告 1 首','translation absent','not found']) check(t.get('#ptext').textContent.includes(text));
    const btn=t.get('#result').rows[0].actions['.download-action']; t.context.btn=btn;
    t.context.fetch=async()=>({ok:true,json:async()=>({ok:true,name:'retained.ncm',size:2,conversion_error:'cannot decode',lyrics_error:'no lyrics'})});
    await t.evaluate('downloadSong(btn,"/api/netease/download?id=1")'); check(btn.classList.contains('done')); check(!t.messages.at(-1).error); check(t.messages.at(-1).message.includes('cannot decode')); check(t.messages.at(-1).message.includes('已保留 NCM')); check(t.messages.at(-1).message.includes('no lyrics'));
    t.context.fetch=async()=>({ok:true,json:async()=>({ok:true,name:'actual.mp3',size:2,converted:true,format:'mp3'})});
    await t.evaluate('downloadSong(btn,"/api/netease/download?id=1")'); check(t.messages.at(-1).message.includes('已转换为 mp3'));
    t.context.fetch=async()=>({ok:true,json:async()=>({ok:true,name:'a.txt',size:2,warnings:['optional section absent']})});
    await t.evaluate('downloadLyrics(btn,songs[0],"netease")'); check(t.messages.at(-1).message.includes('optional section absent'));
    console.log('PASS lyric batches/single feedback: warnings, retained NCM, actual conversion format');
  }
  {
    const t=setup(); t.evaluate('renderSongs([{id:1,name:"n"}],"n",neteaseDownloadUrl,{source:"netease"});');
    t.context.btn=t.get('#result').rows[0].actions['.download-action'];
    t.evaluate('btn.disabled=true; var ran=false;'); await t.evaluate('runResultBatch(async()=>{ran=true;})'); check(!t.evaluate('ran')); check(t.messages.at(-1).error);
    t.evaluate('btn.disabled=false;'); let release; t.context.wait=new Promise(r=>release=r);
    const pending=t.evaluate('runResultBatch(async()=>{ran=true;await wait;})'); check(t.evaluate('_batchBusy')); check(t.sources.every(b=>b.disabled));
    t.evaluate('selectSource("netease");'); equal(t.evaluate('_source'),'qq');
    release(); await pending; check(!t.evaluate('_batchBusy')); check(t.sources.every(b=>!b.disabled));
    t.evaluate('var token=++_searchGeneration;'); check(t.evaluate('requestIsCurrent("qq",token)')); t.evaluate('_searchGeneration++;'); check(!t.evaluate('requestIsCurrent("qq",token)')); check(!t.evaluate('requestIsCurrent("netease",_searchGeneration)'));
    console.log('PASS races: batch admission/locking/restoration and source/search generation guards');
  }
  {
    const t=setup(); let resolve;
    t.context.fetch=()=>new Promise(r=>resolve=r);
    t.evaluate('_source="netease"; var token=++_searchGeneration;');
    let pending=t.evaluate('doSearch("test","netease",token)');
    t.evaluate('_source="qq"; _searchGeneration++; showStatus("newer results");');
    resolve({ok:true,json:async()=>({ok:true,songs:[{id:1,name:'stale'}]})}); await pending;
    check(t.get('#result').innerHTML.includes('newer results'));
    t.evaluate('_source="netease"; token=++_searchGeneration;');
    pending=t.evaluate('doSearch("test","netease",token)');
    resolve({ok:true,json:async()=>({ok:true,songs:[{id:1,name:'current'}]})}); await pending;
    check(t.get('#result').innerHTML.includes('lyric-preview-action')); check(t.get('#result').innerHTML.includes('lyric-download-action')); check(t.get('#result').innerHTML.includes('id="lyricAll"'));
    t.context.songs=[{id:1,name:'a'},{id:2,name:'b'}];
    t.evaluate('_lyricsFormat="lrc"; _lyricsTranslation=true; _lyricsRomanization=true;');
    t.context.fetch=async url=>{t.calls.push(url);t.evaluate('_lyricsFormat="txt"; _lyricsTranslation=false; _lyricsRomanization=false; _source="qq";');return {ok:true,json:async()=>({ok:true,name:'saved.lrc',size:1})};};
    await t.evaluate('downloadAllLyrics(songs,"netease")');
    check(t.calls.every(u=>{const p=new URL(u,'http://offline');return p.pathname==='/api/netease/lyrics/download'&&p.searchParams.get('format')==='lrc'&&p.searchParams.get('translation')==='1'&&p.searchParams.get('romanization')==='1';}));
    console.log('PASS async search: stale result suppression, NetEase lyric controls rendered, lyric batch option/provider snapshots');
  }
  {
    const t = setup({ lyricsEnabled: '1', lyricsTranslation: '1', lyricsRomanization: '1' });
    check(html.includes('data-source="migu"')); check(html.includes('id="miguLoginBtn"'));
    t.evaluate('closeQqLogin = () => {};');
    t.context.fetch = async url => { t.calls.push(url); return {ok:true,json:async()=>({ok:true,capabilities:{search:true,download:false,lyrics:true,login:false},message:'已恢复搜索；暂无授权下载地址'})}; };
    t.evaluate('selectSource("migu");'); await new Promise(setImmediate);
    equal(t.evaluate('_source'), 'migu'); check(t.formats.find(b=>b.dataset.fmt==='master').disabled);
    check(t.get('#lyricsTranslation').disabled); check(t.get('#lyricsRomanization').disabled); check(!t.get('#lyricsTranslation').checked); check(!t.get('#lyricsRomanization').checked);
    check(!t.get('#lyricsEnabled').disabled); check(t.get('#miguHint').textContent.includes('暂无授权下载地址')); check(t.get('#miguHint').textContent.includes('下载不可用'));
    t.evaluate('_lyricsEnabled=true; _lyricsFormat="lrc"; _lyricsTranslation=true; _lyricsRomanization=true; _sourceFormats.migu="master";');
    t.context.song = { source:'migu',id:'69905307315&x=?',name:'泪桥',singer:'古巨基/周深',album:'声生不息家年华12期',duration:257,rights_hint:'<权益待核验>',rights:{download_hint:false,requires_verification:true} };
    for (const expression of ['miguDownloadUrl(song)', 'lyricsUrl(song,"migu")', 'lyricsUrl(song,"migu",true)']) {
      const u = new URL(t.evaluate(expression),'http://offline');
      check(u.pathname.startsWith('/api/migu/')); equal(u.searchParams.get('id'), t.context.song.id); check(!u.searchParams.has('mid'));
      equal(u.searchParams.get('translation'),'0'); equal(u.searchParams.get('romanization'),'0');
      if (u.pathname==='/api/migu/download') { equal(u.searchParams.get('quality'),'sq'); equal(u.searchParams.get('lyrics_format'),'lrc'); }
      else equal(u.searchParams.get('format'),'lrc');
    }
    for (const expression of ['providerFor("typo")', 'providerFor("toString")', 'lyricsUrl(song,"typo")', 'qqDownloadUrl(song)', 'neteaseDownloadUrl(song)', 'renderSongs([song],"wrong",undefined,{source:"qq"})']) {
      assert.throws(()=>t.evaluate(expression)); assertions++;
    }
    t.evaluate('selectSource("qq");'); check(t.get('#lyricsTranslation').checked); check(t.get('#lyricsRomanization').checked); check(!t.get('#lyricsTranslation').disabled);
    t.evaluate('_lyricsFormat="txt"; _lyricsTranslation=false; saveLyricsOptions(); selectSource("netease");'); check(t.get('#lyricsTranslation').checked);
    t.evaluate('_lyricsFormat="lrc"; saveLyricsOptions(); selectSource("qq");'); equal(t.get('#lyricsFormat').value,'txt'); check(!t.get('#lyricsTranslation').checked);
    t.evaluate('selectSource("netease");'); equal(t.get('#lyricsFormat').value,'lrc'); check(t.get('#lyricsTranslation').checked);
    t.evaluate('selectSource("typo");'); equal(t.evaluate('_source'),'netease'); check(t.messages.at(-1).error);
    t.evaluate('renderSongs([song],"咪咕",undefined,{source:"migu"});');
    const row=t.get('#result').rows[0]; check(row.actions['.download-action'].disabled); check(t.get('#dlAll').disabled);
    check(t.get('#result').innerHTML.includes('&lt;权益待核验&gt;')); check(t.get('#result').innerHTML.includes('权益待核验')); check(!t.get('#result').innerHTML.includes('>免费<'));
    const before=t.calls.length; row.actions['.download-action'].onclick(); t.get('#dlAll').onclick(); equal(t.calls.length,before);
    check(!!row.actions['.lyric-preview-action']);
    t.evaluate('var ran=false;'); await t.evaluate('runResultBatch(async()=>{ran=true;})'); check(t.evaluate('ran')); check(row.actions['.download-action'].disabled); check(t.get('#dlAll').disabled);
    t.evaluate('renderSongs([song],"unsupported",undefined,{source:"migu",lyrics:false});'); check(!t.get('#result').innerHTML.includes('lyric-preview-action')); check(!t.get('#result').innerHTML.includes('id="lyricAll"'));
    console.log('PASS Migu dispatch: encoded identity, original-only lyrics, no master, independent login, preferences, unknown/mismatched sources, capability-disabled audio with lyric batch admission');
  }
  {
    const t=setup(); t.evaluate('closeQqLogin=()=>{};');
    const pending=[];
    t.context.fetch=url=>{t.calls.push(url);return new Promise(resolve=>pending.push(resolve));};
    t.evaluate('selectSource("migu"); selectSource("qq");');
    pending.shift()({ok:true,json:async()=>({ok:true,capabilities:{search:true,download:true,lyrics:true},message:'stale'})}); await new Promise(setImmediate);
    equal(t.evaluate('_source'),'qq'); check(!t.evaluate('PROVIDERS.migu.capabilities.download')); check(!t.get('#miguHint').textContent.includes('stale'));
    t.evaluate('selectSource("migu"); selectSource("qq"); selectSource("migu");');
    pending[1]({ok:true,json:async()=>({ok:true,capabilities:{search:true,download:false,lyrics:true,login:false},message:'newest'})}); await new Promise(setImmediate);
    pending[0]({ok:true,json:async()=>({ok:true,capabilities:{search:false,download:true,lyrics:false},message:'old visit'})}); await new Promise(setImmediate);
    check(t.get('#miguHint').textContent.includes('newest')); check(!t.get('#miguHint').textContent.includes('old visit')); check(!t.evaluate('PROVIDERS.migu.capabilities.download')); check(t.evaluate('PROVIDERS.migu.capabilities.lyrics'));
    t.context.fetch=async url=>{t.calls.push(url);return {ok:false,json:async()=>({ok:false,error:'upstream unavailable'})};};
    await t.evaluate('refreshMiguStatus()'); check(t.get('#miguHint').textContent.includes('upstream unavailable')); check(t.get('#lyricsEnabled').disabled); check(!t.get('#goBtn').disabled);
    let available=false;
    t.context.fetch=async url=>{t.calls.push(url);return {ok:true,json:async()=>url==='/api/migu/status'?{ok:true,capabilities:{search:available,download:false,lyrics:available,login:false},message:available?'recovered':'服务维护'}:{ok:true,songs:[{source:'migu',id:'69905307315',name:'泪桥',singer:'古巨基/周深',duration:257,album:'声生不息家年华12期'}]}};};
    let before=t.calls.length;
    await t.evaluate('doSearch("周深 泪桥","migu",++_searchGeneration)'); equal(t.calls.length,before+1); check(t.get('#result').innerHTML.includes('服务维护')); check(!t.get('#goBtn').disabled);
    available=true; await t.evaluate('doSearch("周深 泪桥","migu",++_searchGeneration)');
    const u=new URL(t.calls.at(-1),'http://offline'); equal(u.pathname,'/api/migu/search'); equal(u.searchParams.get('kw'),'周深 泪桥'); equal(u.searchParams.get('num'),'30');
    check(t.get('#result').innerHTML.includes('古巨基/周深')); check(t.get('#result').rows[0].actions['.download-action'].disabled); check(!!t.get('#result').rows[0].actions['.lyric-download-action']);
    before=t.calls.length; t.get('#input').value='https://example.com/song'; t.evaluate('go()'); equal(t.calls.length,before); check(t.get('#result').innerHTML.includes('仅支持关键词搜索'));
    await t.evaluate('doParse("https://example.com/song","migu",_searchGeneration)'); equal(t.calls.length,before);
    // A late search response must not replace the new provider's result.
    let resolveSearch;
    t.context.fetch=async url=>url==='/api/migu/status'?{ok:true,json:async()=>({ok:true,capabilities:{search:true,download:false,lyrics:true},message:'ready'})}:new Promise(r=>resolveSearch=r);
    const search=t.evaluate('doSearch("周深 泪桥","migu",++_searchGeneration)'); await new Promise(setImmediate);
    t.evaluate('selectSource("qq"); showStatus("new provider");'); resolveSearch({ok:true,json:async()=>({ok:true,songs:[{source:'migu',id:'69905307315',name:'stale'}]})}); await search;
    check(t.get('#result').innerHTML.includes('new provider'));
    console.log('PASS Migu status/search: source/visit race guards, failure reasons, retry recovery, 周深 泪桥 query, capability-disabled downloads, link rejection, stale search suppression');
  }
  {
    const t=setup();
    t.context.songs=[{source:'migu',id:'69905307315',name:'泪桥',singer:'古巨基/周深'},{source:'migu',id:'id&2',name:'second'}];
    t.evaluate('PROVIDERS.migu.capabilities={search:true,download:true,lyrics:true,login:false}; _source="migu"; _lyricsEnabled=true; _lyricsFormat="lrc"; _lyricsTranslation=true; _lyricsRomanization=true; _sourceFormats.migu="hq"; renderSongs(songs,"migu",undefined,{source:"migu"});');
    const row=t.get('#result').rows[0];
    check(!row.actions['.download-action'].disabled); check(!t.get('#result').innerHTML.includes('>免费<')); check(t.get('#result').innerHTML.includes('尚未核验'));
    t.evaluate('var bound=[]; previewLyrics=(b,s,p)=>bound.push(["preview",p]); downloadLyrics=(b,s,p)=>bound.push(["lyrics",p]); downloadSong=(b,u)=>bound.push(["audio",u]); var realAudio=downloadAll,realLyrics=downloadAllLyrics; downloadAllLyrics=(s,p)=>bound.push(["batchLyrics",p]); downloadAll=(s,fn)=>bound.push(["batchAudio",fn(s[0])]); runResultBatch=fn=>fn(); _source="qq";');
    row.actions['.lyric-preview-action'].onclick(); row.actions['.lyric-download-action'].onclick(); row.actions['.download-action'].onclick(); t.get('#lyricAll').onclick(); t.get('#dlAll').onclick();
    for(const index of [0,1,3]) equal(t.evaluate(`bound[${index}][1]`),'migu');
    for(const index of [2,4]) {const u=new URL(t.evaluate(`bound[${index}][1]`),'http://offline'); equal(u.pathname,'/api/migu/download'); equal(u.searchParams.get('id'),'69905307315');}
    t.evaluate('downloadAll=realAudio; downloadAllLyrics=realLyrics; _source="migu"; _lyricsEnabled=true; _lyricsFormat="lrc";');
    t.context.fetch=async url=>{const index=t.calls.length;t.calls.push(url);t.evaluate('_source="qq"; _lyricsFormat="txt"; _sourceFormats.migu="standard";');return {ok:true,json:async()=>({ok:true,name:'song.mp3',size:100,lyrics_error:index===0?'原文暂不可用':'',lyrics_warnings:index===1?['sidecar warning']:[]})};};
    await t.evaluate('downloadAll(songs,miguDownloadUrl)');
    equal(t.calls.length,2);
    check(t.calls.every(url=>{const u=new URL(url,'http://offline');return u.pathname==='/api/migu/download'&&u.searchParams.get('quality')==='hq'&&u.searchParams.get('lyrics_format')==='lrc'&&u.searchParams.get('translation')==='0'&&u.searchParams.get('romanization')==='0';}));
    for(const text of ['成功 2 首','歌词警告 2 首','原文暂不可用','sidecar warning']) check(t.get('#ptext').textContent.includes(text));
    t.calls.length=0; t.evaluate('_source="migu"; _lyricsFormat="lrc";');
    t.context.fetch=async url=>{const index=t.calls.length;t.calls.push(url);t.evaluate('_source="qq"; _lyricsFormat="txt";');return {ok:true,json:async()=>({ok:true,name:'song.lrc',size:100,warnings:index===0?['歌词提示']:[]})};};
    await t.evaluate('downloadAllLyrics(songs,"migu")'); equal(t.calls.length,2);
    check(t.calls.every(url=>{const u=new URL(url,'http://offline');return u.pathname==='/api/migu/lyrics/download'&&u.searchParams.get('format')==='lrc'&&u.searchParams.get('translation')==='0'&&u.searchParams.get('romanization')==='0';}));
    check(t.get('#ptext').textContent.includes('歌词警告 1 首'));
    console.log('PASS Migu closures/batches: source identity after switching, query snapshots, original-only flags, audio sidecar failures remain warnings, lyric warnings');
  }
  {
    const t=setup();
    t.context.songs=[
      {source:'migu',id:'track&1=?',name:'track one',CanListen:false,metadata0:0,rights:{download_hint:false,requires_verification:true}},
      {source:'migu',id:'track/2',name:'track two',CanListen:0,metadata0:0}
    ];
    t.context.fetch=async url=>{t.calls.push(url);return {ok:true,json:async()=>url==='/api/migu/status'
      ?{ok:true,capabilities:{search:true,download:true,lyrics:true,login:false},message:'下载服务可用，权限逐曲判定'}
      :{ok:true,songs:t.context.songs}};};
    t.evaluate('closeQqLogin=()=>{}; selectSource("migu");'); await new Promise(setImmediate);
    await t.evaluate('doSearch("test tracks","migu",++_searchGeneration)');
    check(t.calls.includes('/api/migu/status')); check(t.calls.some(url=>url.startsWith('/api/migu/search?')));
    check(t.evaluate('PROVIDERS.migu.capabilities.download')); check(!t.evaluate('PROVIDERS.migu.capabilities.login'));
    const hint=t.get('#miguHint').textContent;
    for(const text of ['下载可尝试','标准 / HQ / SQ（128k / 320k / 无损）','不支持母带','平台逐曲判定','匿名仅可下载获准歌曲','不保证全曲库可下载','登录不保证可下载']) check(hint.includes(text),text);
    check(!/当前音频下载未接通|均不可用/.test(hint));
    check(!t.get('#dlAll').disabled); check(t.get('#result').rows.every(row=>!row.actions['.download-action'].disabled));
    check(!t.get('#result').innerHTML.includes('data-unavailable="true"')); check(!t.get('#result').innerHTML.includes('下载暂不可用'));
    t.get('#lyricsEnabled').checked=true; t.get('#lyricsEnabled').onchange();
    t.get('#lyricsFormat').value='lrc'; t.get('#lyricsFormat').onchange();
    check(t.get('#lyricsTranslation').disabled); check(t.get('#lyricsRomanization').disabled);
    t.calls.length=0;
    t.context.fetch=async url=>{t.calls.push(url);return {ok:true,json:async()=>({ok:true,name:'actual.flac',size:100,quality_name:'实际音质',lyrics_error:'暂无原文歌词'})};};
    for(const quality of ['standard','hq','sq']) {
      t.formats.find(button=>button.dataset.fmt===quality).onclick();
      t.evaluate('renderSongs(songs,"download ready",undefined,{source:"migu"});');
      const button=t.get('#result').rows[0].actions['.download-action'];
      check(!button.disabled); check(!t.get('#dlAll').disabled);
      const before=t.calls.length; button.onclick(); await new Promise(setImmediate);
      equal(t.calls.length,before+1);
      const u=new URL(t.calls.at(-1),'http://offline');
      equal(u.pathname,'/api/migu/download'); equal(u.searchParams.get('id'),t.context.songs[0].id); check(!u.searchParams.has('mid'));
      equal(u.searchParams.get('quality'),quality); equal(u.searchParams.get('lyrics'),'1'); equal(u.searchParams.get('lyrics_format'),'lrc');
      equal(u.searchParams.get('translation'),'0'); equal(u.searchParams.get('romanization'),'0');
      check(button.classList.contains('done')); check(!t.messages.at(-1).error); check(t.messages.at(-1).message.includes('暂无原文歌词'));
    }
    // A per-track transaction failure is backend feedback, not a listening-rights gate.
    t.evaluate('renderSongs(songs,"retry",undefined,{source:"migu"});');
    t.context.fetch=async url=>{t.calls.push(url);return {ok:false,json:async()=>({ok:false,error:'下载价格查询失败'})};};
    const failed=t.get('#result').rows[0].actions['.download-action'];
    failed.onclick(); await new Promise(setImmediate);
    check(!failed.disabled); check(!failed.classList.contains('done')); check(t.messages.at(-1).error); equal(t.messages.at(-1).message,'下载价格查询失败');
    check(!t.get('#result').rows[1].actions['.download-action'].disabled); check(!t.get('#dlAll').disabled);
    t.calls.length=0;
    let release;
    t.context.fetch=url=>{const index=t.calls.length;t.calls.push(url);return index===0?new Promise(resolve=>release=resolve):Promise.resolve({ok:true,json:async()=>({ok:true,name:'second.flac',size:100})});};
    const batch=t.get('#dlAll').onclick();
    check(t.evaluate('_batchBusy')); check(t.sources.every(button=>button.disabled)); check(t.formats.every(button=>button.disabled));
    t.evaluate('selectSource("qq");'); equal(t.evaluate('_source'),'migu');
    // Deliberately mutate options after admission to verify the actual batch query snapshot.
    t.evaluate('_source="qq"; _sourceFormats.migu="standard"; _lyricsFormat="txt";');
    release({ok:false,json:async()=>({ok:false,error:'下载价格查询失败'})}); await batch;
    equal(t.calls.length,2);
    for(const [index,url] of t.calls.entries()) {
      const u=new URL(url,'http://offline'); equal(u.pathname,'/api/migu/download'); equal(u.searchParams.get('id'),t.context.songs[index].id);
      equal(u.searchParams.get('quality'),'sq'); equal(u.searchParams.get('lyrics'),'1'); equal(u.searchParams.get('lyrics_format'),'lrc');
      equal(u.searchParams.get('translation'),'0'); equal(u.searchParams.get('romanization'),'0'); check(!u.searchParams.has('mid'));
    }
    for(const text of ['成功 1 首','失败 1 首','下载价格查询失败']) check(t.get('#ptext').textContent.includes(text),text);
    check(t.get('#result').rows[1].actions['.download-action'].classList.contains('done')); check(!failed.disabled);
    check(!t.evaluate('_batchBusy')); check(t.sources.every(button=>!button.disabled)); check(!t.get('#dlAll').disabled);
    check(t.formats.find(button=>button.dataset.fmt==='master').disabled);
    console.log('PASS Migu enabled status: real row standard/HQ/SQ requests, source IDs/options, no metadata/listening gate, backend error feedback, real batch snapshots/locking and restoration');
  }
  {
    const t=setup({lyricsEnabled:'1',lyricsTranslation:'1'}), requests=[];
    check(!/<iframe\b|createElement\(['"]iframe/i.test(html),'no embedded website');
    check(html.includes('data-source="other" aria-pressed="false">HiFiTi渠道</button>'));
    check(!html.includes('其他渠道'), 'all visible channel labels use HiFiTi渠道');
    const workspace = html.match(/<div class="layout">([\s\S]*?)<footer>/)[1];
    equal([...workspace.matchAll(/<section class="panel" id="([^"]+)"/g)].map(match => match[1]),
      ['musicResultsPanel', 'conversionPanel', 'downloadedFilesPanel', 'vocalPanel'], 'workspace DOM follows desktop reading order');
    equal((workspace.match(/<section class="panel"/g) || []).length, 4, 'four independent panels');
    check(!html.includes('col-mid'), 'conversion and vocal separation are no longer stacked in one column');
    check(/@media\s*\(max-width:\s*1199px\)\s*\{\s*\.layout\s*\{\s*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/.test(html), 'medium screens use two columns');
    check(/@media\s*\(max-width:\s*700px\)\s*\{\s*\.layout\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\)/.test(html), 'narrow screens use one column');
    for (const id of ['vipRefresh','vipSetFolder','vipFolder','vipFolderState','vipFiles','vipHistory','files','openFolderBtn','clearFilesBtn','vocalState','vocalBody','vocalSetupStatus','vocalInputs','vocalFile','vocalModel','vocalGo','vocalUploadBtn','vocalUpload','vocalProgress','vocalProgressText','vocalResults']) {
      equal((workspace.match(new RegExp('id="' + id + '"', 'g')) || []).length, 1, id + ' retained exactly once');
    }
    t.get('#input').value='private unsent keyword'; t.evaluate('selectSource("other")');
    equal(t.calls.length,0); equal(t.evaluate('Object.keys(PROVIDERS).join(",")'),'qq,netease,migu');
    check(t.get('#audioOptions').hidden); check(t.get('.lyrics-options').hidden); check(t.get('#musicResultsPanel').hidden); check(!t.get('#otherPanel').hidden);
    check(t.formats.every(b=>b.disabled)); check(t.get('#lyricsEnabled').disabled); check(!t.get('#lyricsEnabled').checked); check(!t.get('#lyricsTranslation').checked);
    t.context.fetch=async(url,opts)=>{requests.push({url,opts}); return {ok:true,json:async()=>url.endsWith('/status')?{ok:true,set:false,csrf:'forum-csrf',persistent:false}:{ok:true,keyword:'ignored',page:1,pages:191,total:4566,has_next:true,items:[
      {id:'104813',title:'古巨基/周深《泪桥》[完整标题] <img onerror=x>',url:'https://www.hifiti.com/thread-104813.htm',author:'<author>',created:'2023-12-23',last_reply:'2026-10-17 <reply>',views:0,tags:['百度网盘','<script>tag</script>']},
      {id:'68903',title:'second original title',url:'javascript:alert(1)',author:'second'}]}};};
    for(const word of ['周深 泪桥 &x=1#<script>"','https://evil.invalid/path?x=1&keyword=oops','//evil.invalid','javascript:alert(1)']) {
      t.get('#input').value=word; await t.evaluate('go()');
      const u=new URL(requests.at(-1).url,'http://offline');
      equal(u.pathname,'/api/hifiti/search'); equal(u.searchParams.get('kw'),word); equal(u.searchParams.get('page'),'1'); equal(u.searchParams.get('scope'),'title'); equal(u.searchParams.get('exact'),'0'); equal([...u.searchParams].length,4);
      const external=new URL(t.get('#otherOpenSearch').href); equal(external.origin,'https://www.hifiti.com'); equal(external.searchParams.get('keyword'),word);
    }
    equal(requests.filter(r=>r.url.endsWith('/status')).length,1,'one owner handshake before public search');
    const markup=t.get('#otherResults').innerHTML;
    check(markup.indexOf('古巨基/周深')<markup.indexOf('second original title')); check(markup.includes('《泪桥》[完整标题] &lt;img onerror=x&gt;'));
    for(const text of ['&lt;author&gt;','2023-12-23','2026-10-17 &lt;reply&gt;','浏览：0','百度网盘','&lt;script&gt;tag&lt;/script&gt;']) check(markup.includes(text),text);
    check(!/download-action|lyric-preview|data-song|javascript:/.test(markup)); equal(t.get('#otherResults').forumButtons.length,2);
    check(t.get('#otherStatus').textContent.includes('4566')); equal(t.get('#otherPageInfo').textContent,'第 1 / 191 页'); check(t.get('#otherPrev').disabled); check(!t.get('#otherNext').disabled);
    t.get('#input').value='changed unsent'; t.get('#otherScope').value='post'; t.get('#otherExact').checked=true;
    await t.get('#otherNext').onclick();
    let u=new URL(requests.at(-1).url,'http://offline'); equal(u.searchParams.get('kw'),'javascript:alert(1)'); equal(u.searchParams.get('scope'),'title'); equal(u.searchParams.get('exact'),'0'); equal(u.searchParams.get('page'),'2');
    await t.evaluate('go()'); u=new URL(requests.at(-1).url,'http://offline'); equal(u.searchParams.get('scope'),'post'); equal(u.searchParams.get('exact'),'1'); equal(u.searchParams.get('page'),'1');
    for(const {url,opts} of requests) {check(url.startsWith('/api/hifiti/')); equal(opts.credentials,'same-origin'); equal(opts.cache,'no-store'); check(!opts.method);}
    for(const id of ['otherOpenLogin','otherOpenSearch']) check(new RegExp('id="'+id+'"[^>]*target="_blank"[^>]*rel="noopener noreferrer"').test(html));
    for(const text of ['外部网盘','不会自动登录','不会自动出现在','不会自动关注、扣积分、购买或回复','https://www.hifiti.com 官方登录','重启后需重新登录']) check(html.includes(text),text);
    assert.throws(()=>t.evaluate('providerFor("other")')); assertions++; assert.throws(()=>t.evaluate('lyricsUrl({},"other")')); assertions++;
    t.evaluate('selectSource("toString")'); equal(t.evaluate('_source'),'other');
    t.evaluate('selectSource("qq")'); check(!t.get('#audioOptions').hidden); check(!t.get('.lyrics-options').hidden); check(t.get('#lyricsEnabled').checked); check(t.get('#lyricsTranslation').checked);
    const before=requests.length; t.evaluate('selectSource("other")'); equal(requests.length,before); equal(t.get('#otherOpenSearch').href,'https://www.hifiti.com/');
    check(!Object.keys(t.storage).some(k=>/hifiti|password|cookie|csrf|token/i.test(k)));
    console.log('PASS native forum search: explicit other routing, no iframe/music controls, owner-cookie public search, encoded queries, original order/title/tags/metadata, pagination snapshots, defaults and independent preferences');
  }
  {
    const t=setup(), requests=[]; t.evaluate('selectSource("other")');
    t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>url.endsWith('/status')?{ok:true,set:false,csrf:'token'}:url.includes('/search?')?{ok:true,page:1,pages:2009,total:null,has_next:true,items:[{id:104813,pid:1,title:'reply one',excerpt:'<reply excerpt>'},{id:104813,pid:2,title:'reply two'}]}:{ok:true,id:104813,title:'<thread title>',url:'https://www.hifiti.com/thread-104813.htm',text:'<script>alert(1)</script>\n歌词正文',notice:'回复后查看；不自动回复',links:[{label:'<网盘>',url:'https://pan.baidu.com/s/example?x=1&y=2'},{label:'bad',url:'javascript:alert(1)'},{url:'data:text/html,evil'},{url:'https://user:secret@evil.invalid/'},{label:'encoded',url:'https://example.com/"onclick="x'}]}};};
    await t.evaluate('searchOther("泪桥")'); equal(t.get('#otherResults').forumButtons.length,2); check(t.get('#otherResults').innerHTML.includes('&lt;reply excerpt&gt;')); check(t.get('#otherStatus').textContent.includes('官网未提供总数')); check(!t.get('#otherStatus').textContent.includes('共 0')); equal(t.get('#otherPageInfo').textContent,'第 1 / 2009 页');
    await t.get('#otherResults').forumButtons[1].onclick(); equal(new URL(requests.at(-1).url,'http://offline').searchParams.get('id'),'104813');
    check(!t.get('#otherThread').hidden); equal(t.get('#otherThreadTitle').textContent,'<thread title>'); equal(t.get('#otherThreadText').textContent,'<script>alert(1)</script>\n歌词正文'); check(t.get('#otherThreadNotice').textContent.includes('2 个可见资源')); check(t.get('#otherThreadNotice').textContent.includes('公开会话')); check(!t.get('#otherThreadNotice').textContent.includes('回复后查看')); equal(t.get('#otherThread').getAttribute?.('aria-busy') ?? t.get('#otherThread')['aria-busy'],'false');
    const links=t.get('#otherThreadLinks').innerHTML; check(links.includes('&lt;网盘&gt;')); check(links.includes('x=1&amp;y=2')); check(!/javascript:|data:|secret@|onclick="x/.test(links)); equal((links.match(/rel="noopener noreferrer"/g)||[]).length,2); check(!t.get('#otherThreadOfficial').hidden);
    equal(requests.length,3,'no automatic resource follows, replies, points or download requests');
    t.get('#otherThreadClose').onclick(); check(t.get('#otherThread').hidden); equal(t.get('#otherThreadText').textContent,''); equal(t.get('#otherThreadLinks').innerHTML,'');
    t.context.fetch=async()=>({ok:true,json:async()=>({ok:true,page:1,pages:0,total:0,has_next:false,items:[]})}); await t.evaluate('searchOther("missing")'); check(t.get('#otherResults').innerHTML.includes('没有找到')); check(t.get('#otherNext').disabled); check(t.get('#otherPrev').disabled);
    console.log('PASS native forum details: duplicate thread rows, optional escaped excerpts, unknown totals, native safe text, hidden-resource notice, explicit safe resource links only, close and empty results');
  }
  {
    const t=setup(), panel=t.get('#otherPanel'), results=t.get('#otherResults'), detail=t.get('#otherThread');
    const row=t.context.document.createElement('article'), nested=t.context.document.createElement('button');
    row.className='forum-row'; row.appendChild(nested); results.appendChild(row);
    check(nested.closest('.forum-row')===row,'closest finds the actual forum row'); check(row.closest('.forum-row')===row,'closest includes self');
    check(results.contains(nested)); check(results.contains(results)); check(!results.contains(null));
    row.appendChild(detail); check(!panel.children.includes(detail),'appendChild removes previous ownership'); check(results.contains(detail),'contains follows nested descendants');
    row.appendChild(detail); equal(row.children.filter(child=>child===detail).length,1,'same-parent append moves instead of duplicates');
    panel.appendChild(detail); check(!row.contains(detail)); check(detail.parentNode===panel); check(detail.parentElement===panel);
    row.remove(); check(!results.contains(nested)); check(row.contains(nested),'removal preserves detached subtree'); check(row.parentNode===null); row.remove();
    results.appendChild(row); results.innerHTML=''; check(row.parentNode===null,'innerHTML detaches existing children'); check(!results.contains(row)); check(detail.parentNode===panel);
    console.log('PASS fake DOM ownership: reparenting, repeat append, recursive contains, closest self/ancestor, detached subtrees and innerHTML removal');
  }
  {
    const t=setup(), requests=[], pending=[]; t.evaluate('selectSource("other")');
    t.context.fetch=(url,opts)=>{
      requests.push({url,opts});
      return url.includes('/thread?')?new Promise(resolve=>pending.push({url,resolve})):Promise.resolve({ok:true,json:async()=>url.endsWith('/status')?{ok:true,set:false,csrf:'inline-token'}:{ok:true,page:1,pages:1,total:3,items:[{id:104813,pid:1,title:'duplicate first'},{id:104813,pid:2,title:'duplicate second'},{id:68903,title:'another thread'}]}});
    };
    await t.evaluate('searchOther("inline")');
    const results=t.get('#otherResults'), rows=results.forumRows, buttons=results.forumButtons, detail=t.get('#otherThread'), panel=t.get('#otherPanel');
    const finish=async (request,text)=>{request.resolve({ok:true,json:async()=>({ok:true,id:'response-id-not-requested',title:text,text,resources:[]})});await new Promise(setImmediate);};
    const first=buttons[1].onclick(); await new Promise(setImmediate);
    check(detail.parentNode===rows[1],'loading panel is inside exact clicked duplicate-ID row'); check(!rows[0].contains(detail)); check(!panel.children.includes(detail)); check(!detail.hidden);
    await finish(pending.at(-1),'second duplicate'); await first; check(detail.parentNode===rows[1],'response preserves clicked row');
    const reload=t.get('#otherThreadReload').onclick(); await new Promise(setImmediate);
    equal(new URL(pending.at(-1).url,'http://offline').searchParams.get('id'),'104813','row reload uses requested ID'); check(detail.parentNode===rows[1],'reload loading stays in clicked duplicate row');
    await finish(pending.at(-1),'reloaded duplicate'); await reload; check(detail.parentNode===rows[1],'reload response stays in row');
    const old=buttons[0].onclick(); await new Promise(setImmediate); const oldRequest=pending.at(-1);
    check(detail.parentNode===rows[0],'same ID clicked in another row moves the shared panel'); check(!rows[1].contains(detail));
    const newest=buttons[2].onclick(); await new Promise(setImmediate); const newestRequest=pending.at(-1);
    check(detail.parentNode===rows[2],'different thread moves shared panel'); check(!rows[0].contains(detail));
    await finish(newestRequest,'newest body'); await newest;
    await finish(oldRequest,'delayed old body'); await old;
    equal(t.get('#otherThreadText').textContent,'newest body','delayed old row response cannot replace current detail'); check(detail.parentNode===rows[2],'old response cannot move detail back');
    equal(rows.reduce((sum,row)=>sum+row.children.filter(child=>child===detail).length,0),1,'only one row owns shared panel');
    t.mutations.length=0; t.get('#otherThreadClose').onclick();
    check(detail.hidden); check(detail.parentNode===panel,'close parks panel outside rows'); check(!results.contains(detail));
    const park=t.mutations.findIndex(m=>m.type==='append'&&m.child===detail&&m.target===panel);
    const cleanup=t.mutations.findIndex(m=>m.type==='html'&&m.target===t.get('#otherThreadLinks'));
    check(park>=0&&cleanup>park,'close parks before clearing resource content'); check(t.mutations[park].hidden,'panel is hidden before parking');
    console.log('PASS inline forum rows: exact duplicate-ID click, shared-panel moves, reload row retention, hidden close parking before cleanup, delayed old response suppression');
  }
  {
    for(const action of ['search','source','status auth','login','logout','pagehide']) {
      const t=setup(); t.evaluate('selectSource("other")'); let auth={set:true,nickname:'owner',expired:false};
      t.context.fetch=async url=>({ok:true,json:async()=>url.endsWith('/status')?{ok:true,...auth,csrf:'park-token'}:url.includes('/search?')?{ok:true,auth,page:1,pages:1,total:1,items:[{id:2,title:'row'}]}:url.endsWith('/login')?{ok:true,...auth,csrf:'park-token'}:{ok:true,auth,text:'private body',resources:[{url:'https://example.com/private',code:'secret'}]}});
      await t.evaluate('searchOther("initial")'); const results=t.get('#otherResults'), row=results.forumRows[0], detail=t.get('#otherThread'), panel=t.get('#otherPanel');
      await results.forumButtons[0].onclick(); check(detail.parentNode===row,action+' starts with inline detail');
      if(action==='login') await t.get('#otherLogin').onclick();
      t.mutations.length=0;
      if(action==='search') await t.evaluate('searchOther("replacement")');
      else if(action==='source') t.evaluate('selectSource("qq")');
      else if(action==='status auth') {auth={set:false,nickname:'',expired:true};await t.evaluate('refreshOtherStatus()');}
      else if(action==='login') {t.get('#otherUsername').value='owner';t.get('#otherPassword').value='password';await t.get('#otherLoginForm').onsubmit({preventDefault(){}});}
      else if(action==='logout') await t.get('#otherLogout').onclick();
      else await Promise.all(t.events.get('pagehide').map(fn=>fn()));
      const clear=t.mutations.find(m=>m.type==='html'&&m.target===results&&m.children.includes(row));
      check(!!clear,action+' removes original rows'); check(clear.threadParent===panel,action+' parks before row removal'); check(clear.threadHidden,action+' hides before row removal');
      check(row.parentNode===null,action+' detaches old row'); check(!row.contains(detail),action+' leaves no detail in detached row'); check(detail.parentNode===panel,action+' retains shared panel in stable host'); check(detail.hidden,action+' hides parked detail');
      equal(t.get('#otherThreadText').textContent,'',action+' clears private body'); equal(t.get('#otherThreadLinks').innerHTML,'',action+' clears private links');
    }
    console.log('PASS inline forum invalidation: search/source/status-auth/login/logout/pagehide hide and park before removing rows, preserving stable shared panel');
  }
  {
    const t=setup(); t.evaluate('selectSource("other")'); let expired=false;
    t.context.fetch=async url=>({ok:true,json:async()=>url.endsWith('/status')?{ok:true,set:true,nickname:'owner',csrf:'transition-token'}:url.includes('/search?')?{ok:true,page:1,pages:1,total:1,items:[{id:2,title:'row'}]}:{ok:true,auth:expired?{set:false,nickname:'',expired:true}:{set:true,nickname:'owner',expired:false},text:expired?'public transition body':'private body',resources:expired?[]:[{url:'https://example.com/private'}]}});
    await t.evaluate('searchOther("transition")'); const results=t.get('#otherResults'), row=results.forumRows[0], detail=t.get('#otherThread'), panel=t.get('#otherPanel');
    await results.forumButtons[0].onclick(); check(detail.parentNode===row); expired=true; t.mutations.length=0;
    await t.get('#otherThreadReload').onclick();
    const clear=t.mutations.find(m=>m.type==='html'&&m.target===results&&m.children.includes(row));
    check(!!clear,'thread response auth transition removes stale rows'); check(clear.threadParent===panel,'response auth parks before row removal'); check(clear.threadHidden,'response auth hides before row removal');
    check(row.parentNode===null); check(!row.contains(detail)); check(detail.parentNode===panel,'transition response never reattaches to dead row');
    check(!detail.hidden,'compatible auth-transition response remains visible in parked host'); equal(t.get('#otherThreadText').textContent,'public transition body'); equal(t.get('#otherThreadLinks').innerHTML,''); check(t.get('#otherThreadNotice').textContent.includes('登录已过期'));
    await t.get('#otherThreadReload').onclick(); check(detail.parentNode===panel,'reload captured dead row cannot reclaim parked detail'); check(!row.contains(detail));
    console.log('PASS inline forum response-auth transition: park before stale row removal, retain current public response in stable host, dead-row reload never reattaches');
  }
  {
    const t=setup(), requests=[], copied=[]; t.evaluate('selectSource("other")');
    const url='https://pan.baidu.com/s/demo?x=1&y=2#提取码-frag';
    let thread={ok:true,id:'different-response-id',title:'<title>',text:'<body>',notice:'所有链接需要回复',auth:{set:true,nickname:'<account>',expired:false},resources:[
      {url,label:'<img src=x onerror=alert(1)>',code:'a<&"\'b'},
      {url:'https://example.com/file#keep',label:'second',code:''},
      {url:'javascript:alert(1)',label:'unsafe',code:'bad'},
      {url:'https://user:secret@example.com/',code:'bad'}],links:[{url:'https://legacy.invalid/ignored'}]};
    t.context.navigator={clipboard:{writeText:async text=>copied.push(text)}};
    t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>url.endsWith('/status')?{ok:true,set:true,nickname:'<account>',csrf:'token'}:thread};};
    await t.evaluate('openOtherThread("id&7=?")'); equal(copied.length,0,'render never copies');
    const markup=t.get('#otherThreadLinks').innerHTML;
    for(const text of ['&lt;img src=x onerror=alert(1)&gt;','提取码：a&lt;&amp;&quot;&#39;b','未提供明确提取码','复制链接','复制提取码','复制全部','x=1&amp;y=2']) check(markup.includes(text),text);
    check(!/javascript:|secret@|legacy\.invalid|<img/.test(markup));
    check(t.get('#otherThreadNotice').textContent.includes('2 个可见资源')); check(t.get('#otherThreadNotice').textContent.includes('当前账号（<account>）')); check(!t.get('#otherThreadNotice').textContent.includes('需要回复'));
    const buttons=t.get('#otherThreadLinks').copyButtons; equal(buttons.length,6); check(buttons[4].disabled);
    await buttons[0].onclick(); await buttons[1].onclick(); await buttons[2].onclick(); await buttons[3].onclick(); await buttons[4].onclick(); await buttons[5].onclick();
    const safeUrl=new URL(url).href;
    equal(copied,[safeUrl,'a<&"\'b',safeUrl+'\n提取码：a<&"\'b','https://example.com/file#keep','https://example.com/file#keep']);
    equal(new URL(copied[0]).hash,new URL(url).hash,'URL fragment preserved');
    check(t.get('#otherCopyStatus').textContent.includes('已复制')); check(t.get('#otherCopyText').hidden);
    const oldCopy=buttons[0].onclick, oldReload=t.get('#otherThreadReload').onclick;
    thread={ok:true,text:'new body',auth:{set:true,nickname:'<account>',expired:false},resources:[{url:'https://new.invalid/#new',code:'1234'}]};
    await oldReload(); equal(new URL(requests.at(-1).url,'http://offline').searchParams.get('id'),'id&7=?','reload uses requested ID, not response ID');
    equal(copied.length,5,'reload never copies'); check(!t.get('#otherThreadLinks').innerHTML.includes('pan.baidu.com')); equal(buttons[0].onclick,null,'detached buttons unbound');
    await oldCopy(); const count=requests.length; await oldReload(); equal(requests.length,count,'old reload closure invalid'); equal(copied.length,5,'old detail closure cannot copy');
    t.context.navigator={}; await t.get('#otherThreadLinks').copyButtons[2].onclick();
    check(!t.get('#otherCopyText').hidden); equal(t.get('#otherCopyText').textContent,'https://new.invalid/#new\n提取码：1234'); check(t.get('#otherCopyStatus').textContent.includes('手动选择'));
    t.context.navigator={clipboard:{writeText:async()=>{throw new Error('<denied>');}}}; await t.get('#otherThreadLinks').copyButtons[1].onclick();
    equal(t.get('#otherCopyText').textContent,'1234'); check(t.get('#otherCopyStatus').textContent.includes('复制失败')); check(!t.get('#otherCopyStatus').textContent.includes('<denied>'));
    let reject; t.context.navigator={clipboard:{writeText:()=>new Promise((_,r)=>reject=r)}};
    const pending=t.get('#otherThreadLinks').copyButtons[0].onclick(); t.get('#otherThreadClose').onclick(); reject(new Error('late denial')); await pending;
    check(t.get('#otherThread').hidden); equal(t.get('#otherCopyStatus').textContent,''); equal(t.get('#otherCopyText').textContent,''); check(t.get('#otherCopyText').hidden); equal(t.get('#otherThreadReload').onclick,null);
    for(const text of ['官网浏览器登录与本应用后端会话相互独立','请在本应用 HiFiTi 登录面板主动登录','不向网盘或其他站点提交 HiFiTi 凭据']) check(html.includes(text));
    console.log('PASS forum resource cards: escaped labels/codes/URLs, safe legacy-independent cards, fragments, per-resource click-only copy, missing/rejected clipboard selectable fallback, stale detached handlers, same-ID reload');
  }
  {
    const t=setup(), copied=[]; t.evaluate('selectSource("other")');
    let auth={set:true,nickname:'owner',expired:false}, resources=[{url:'https://private.invalid/#original',code:'secret'}];
    t.context.navigator={clipboard:{writeText:async text=>copied.push(text)}};
    t.context.fetch=async url=>({ok:true,json:async()=>url.endsWith('/status')?{ok:true,...auth,csrf:'token'}:url.includes('/search?')?{ok:true,auth,page:1,pages:1,total:1,items:[{id:2,title:'row'}]}:{ok:true,auth,resources,text:'body'}});
    await t.evaluate('searchOther("first")'); const oldRow=t.get('#otherResults').forumButtons[0]; await oldRow.onclick();
    let oldCopy=t.get('#otherThreadLinks').copyButtons[0].onclick, oldReload=t.get('#otherThreadReload').onclick;
    auth={set:false,nickname:'',expired:true}; await t.evaluate('refreshOtherStatus()');
    check(t.get('#otherLoginStatus').textContent.includes('已过期')); check(t.get('#otherLoginStatus').textContent.includes('公开内容')); check(t.get('#otherThread').hidden); equal(t.get('#otherThreadLinks').innerHTML,'');
    equal(t.get('#otherLoginBtn').textContent,'HiFiTi · 登录已过期'); equal(t.get('#otherLogin').textContent,'重新登录');
    await oldCopy(); await oldReload(); await oldRow.onclick(); equal(copied.length,0); check(t.get('#otherThread').hidden);
    auth={set:true,nickname:'owner',expired:false}; await t.evaluate('refreshOtherStatus()'); await t.evaluate('openOtherThread(2)');
    oldCopy=t.get('#otherThreadLinks').copyButtons[0].onclick;
    auth={set:false,nickname:'',expired:true}; resources=[]; await t.get('#otherThreadReload').onclick();
    check(t.get('#otherLoginStatus').textContent.includes('已过期')); check(t.get('#otherThreadNotice').textContent.includes('登录已过期')); check(t.get('#otherThreadNotice').textContent.includes('未发现')); equal(t.get('#otherThreadLinks').innerHTML,''); await oldCopy(); equal(copied.length,0);
    auth={set:true,nickname:'owner',expired:false}; resources=[{url:'https://private.invalid/',code:'secret'}]; await t.evaluate('openOtherThread(2)');
    oldCopy=t.get('#otherThreadLinks').copyButtons[0].onclick;
    auth={set:false,nickname:'',expired:true}; await t.evaluate('searchOther("expired search")'); await oldCopy(); check(t.get('#otherThread').hidden); equal(t.get('#otherThreadLinks').innerHTML,''); check(t.get('#otherLoginStatus').textContent.includes('已过期')); equal(copied.length,0);
    // Source change and logout invalidate even saved references to prior click closures.
    auth={set:true,nickname:'owner',expired:false}; await t.evaluate('openOtherThread(2)'); oldCopy=t.get('#otherThreadLinks').copyButtons[0].onclick; t.evaluate('selectSource("qq"); selectSource("other")'); await oldCopy(); equal(copied.length,0);
    await t.evaluate('openOtherThread(2)'); oldCopy=t.get('#otherThreadLinks').copyButtons[0].onclick; await t.get('#otherLogout').onclick(); await oldCopy(); equal(copied.length,0); equal(t.get('#otherThreadLinks').innerHTML,'');
    console.log('PASS forum auth/resource guards: status/search/thread expiry updates login label, clears stale private cards, invalidates row/copy/reload closures; source and logout block detached copy');
  }
  {
    const t=setup(), requests=[], panel=t.get('#otherLoginPanel'), header=t.get('#otherLoginBtn');
    equal(panel.getAttribute('role'),'dialog'); equal(panel.getAttribute('aria-modal'),'true'); check(panel.hidden);
    check(/id="otherLoginBtn"[^>]*aria-controls="otherLoginPanel"/.test(html));
    for(const guidance of ['输入你自己的 HiFiTi 账号和密码','点击“提交登录”','官网会话不会自动同步到本应用','密码在提交、切换标签或关闭时清空']) check(html.includes(guidance),guidance);
    t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>({ok:true,set:false,csrf:'modal-csrf'})};};
    header.focus(); t.get('#input').value='do not search from header';
    await header.onclick({currentTarget:header});
    equal(t.evaluate('_source'),'other','header selects the independent forum source'); check(!panel.hidden);
    equal(requests.map(r=>r.url),['/api/hifiti/login/status'],'opening checks status only, never searches or submits');
    check(!t.get('#otherLoginTabPanel').hidden); check(t.get('#otherRegisterTabPanel').hidden);
    equal(t.context.document.activeElement,t.get('#otherUsername'),'opening focuses account input');
    for(const id of ['otherLogin','otherLoginBtn']) equal(t.get('#'+id).getAttribute('aria-expanded'),'true');
    t.get('#otherPassword').value='clear on tab'; t.get('#otherRegisterPassword').value='old register'; t.get('#otherRegisterConfirm').value='old confirm';
    t.get('#otherRegisterTab').onclick();
    check(t.get('#otherLoginTabPanel').hidden); check(!t.get('#otherRegisterTabPanel').hidden);
    equal(t.get('#otherRegisterTab').getAttribute('aria-selected'),'true'); equal(t.get('#otherLoginTab').getAttribute('tabindex'),'-1');
    for(const id of ['otherPassword','otherRegisterPassword','otherRegisterConfirm']) equal(t.get('#'+id).value,'','tab switch clears '+id);
    equal(requests.length,1,'switching to registration never implicitly submits or requests codes');
    const tabKey={type:'keydown',key:'Home',defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};
    t.get('#otherRegisterTab').dispatchEvent(tabKey); check(tabKey.defaultPrevented); check(!t.get('#otherLoginTabPanel').hidden); equal(t.context.document.activeElement,t.get('#otherLoginTab'));
    const panelChildren=t.get('#otherLoginPanel').querySelectorAll('button, input, a[href], [tabindex="0"]');
    const visible=panelChildren.filter(control=>!control.disabled&&control.getAttribute('tabindex')!=='-1'&&!control.closest('[hidden]'));
    check(visible.includes(t.get('#otherUsername'))); check(!visible.includes(t.get('#otherRegisterPassword')),'focus trap excludes hidden registration controls');
    visible.at(-1).focus(); check(t.keydown('Tab').defaultPrevented); equal(t.context.document.activeElement,visible[0]);
    visible[0].focus(); check(t.keydown('Tab',{shiftKey:true}).defaultPrevented); equal(t.context.document.activeElement,visible.at(-1));
    header.focus(); check(t.keydown('Tab').defaultPrevented); equal(t.context.document.activeElement,visible[0],'Tab from outside recaptures modal focus');
    t.get('#otherPassword').value='do not leak'; panel.dispatchEvent({type:'click',target:t.get('#otherPassword')}); check(!panel.hidden,'inside clicks do not dismiss dialog');
    const escape=t.keydown('Escape'); check(escape.defaultPrevented); check(panel.hidden); equal(t.get('#otherPassword').value,''); equal(t.context.document.activeElement,header,'Escape restores invoking header focus');
    for(const id of ['otherLogin','otherLoginBtn']) equal(t.get('#'+id).getAttribute('aria-expanded'),'false');
    check(!t.keydown('Escape').defaultPrevented,'hidden modal leaves normal keyboard handling alone');
    const inline=t.get('#otherLogin'); inline.focus(); await inline.onclick({currentTarget:inline});
    for(const id of ['otherUsername','otherPassword','otherRegisterEmail','otherRegisterUsername','otherRegisterPassword','otherRegisterConfirm']) t.get('#'+id).value='clear at backdrop';
    panel.dispatchEvent({type:'click',target:panel}); check(panel.hidden); equal(t.context.document.activeElement,inline,'backdrop restores exact inline opener');
    for(const id of ['otherUsername','otherPassword','otherRegisterEmail','otherRegisterUsername','otherRegisterPassword','otherRegisterConfirm']) equal(t.get('#'+id).value,'','backdrop clears '+id);
    check(requests.every(r=>r.url==='/api/hifiti/login/status'&&!r.opts.method),'all modal-only interaction remains read-only');
    t.evaluate('_batchBusy=true; _source="qq"'); const before=requests.length; await header.onclick({currentTarget:header});
    equal(t.evaluate('_source'),'qq'); check(panel.hidden); equal(requests.length,before,'busy music batch prevents opening or handshake');
    console.log('PASS native account modal: header routing, guidance, status-only open, tabs/secret clearing, keyboard tab activation, hidden-control focus trap, Escape/backdrop cleanup and exact opener focus restore, batch guard');
  }
  {
    const t=setup(), requests=[];
    t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>url.endsWith('/register')?{ok:true,registered:true,set:false,nickname:'not-an-authenticated-identity',csrf:'after-register',persistent:false,message:'<untrusted response message>'}:{ok:true,set:false,csrf:'register-csrf'}};};
    await t.get('#otherLoginBtn').onclick(); t.get('#otherRegisterTab').onclick();
    check(!t.get('#otherRegisterSubmit').disabled); check(/id="otherRegisterEmail"[^>]*type="email"[^>]*autocomplete="email"/.test(html));
    for(const id of ['otherRegisterPassword','otherRegisterConfirm']) check(new RegExp('id="'+id+'"[^>]*type="password"[^>]*autocomplete="new-password"').test(html));
    check(html.includes('关闭仅关闭窗口')); check(html.includes('提交后可能创建账号'));
    const fill=(email=' owner@example.com ',username=' native-user ',password='registration secret',confirm=password)=>{
      for(const [id,value] of Object.entries({otherRegisterEmail:email,otherRegisterUsername:username,otherRegisterPassword:password,otherRegisterConfirm:confirm})) t.get('#'+id).value=value;
    };
    const event={preventDefault(){this.prevented=true;}};
    for(const [email,username,password,confirm,notice] of [
      ['', 'u','p','p','请填写'], ['invalid','u','p','p','有效邮箱'], ['a@b.com','x'.repeat(65),'p','p','64'],
      ['a@b.com','u','x'.repeat(129),'x'.repeat(129),'128'], ['a@b.com','u','one','two','不一致']
    ]) {
      fill(email,username,password,confirm); const before=requests.length; await t.get('#otherRegisterForm').onsubmit(event);
      equal(requests.length,before,'invalid registration never sends requests'); equal(t.get('#otherRegisterPassword').value,''); equal(t.get('#otherRegisterConfirm').value,''); check(t.get('#otherRegisterMessage').textContent.includes(notice));
    }
    fill(); t.get('#otherPassword').value='hidden login password';
    const submit=t.get('#otherRegisterForm').onsubmit(event); check(event.prevented);
    for(const id of ['otherPassword','otherRegisterPassword','otherRegisterConfirm']) equal(t.get('#'+id).value,'','register clears all password fields immediately');
    for(const id of ['otherLoginSubmit','otherRegisterSubmit','otherLogout','otherStatusRefresh']) check(t.get('#'+id).disabled,'auth busy disables '+id);
    await submit;
    const registration=requests.filter(r=>r.url==='/api/hifiti/register'); equal(registration.length,1);
    equal(JSON.parse(registration[0].opts.body),{email:'owner@example.com',username:'native-user',password:'registration secret',repeat_password:'registration secret'});
    equal({...registration[0].opts.headers},{'Content-Type':'application/json','X-CSRF-Token':'register-csrf'}); equal(registration[0].opts.credentials,'same-origin'); equal(registration[0].opts.cache,'no-store'); equal(registration[0].opts.method,'POST');
    equal(requests.filter(r=>r.opts.method==='POST').map(r=>r.url),['/api/hifiti/register'],'registration never automatically logs in or sends codes');
    check(t.get('#otherRegisterMessage').textContent.includes('官网已接受注册请求')); check(t.get('#otherRegisterMessage').textContent.includes('邮箱验证')); check(t.get('#otherRegisterMessage').textContent.includes('不会自动提交'));
    check(!t.get('#otherLoginStatus').textContent.includes('not-an-authenticated-identity'),'set:false cannot claim registered nickname is logged in');
    check(!t.get('#otherRegisterMessage').textContent.includes('<untrusted response message>'),'acceptance uses static guidance, not untrusted backend text'); check(!t.get('#otherLoginPanel').hidden); check(!t.get('#otherRegisterTabPanel').hidden);
    for(const id of ['otherLoginSubmit','otherRegisterSubmit','otherLogout','otherStatusRefresh']) check(!t.get('#'+id).disabled);
    equal(requests.filter(r=>r.url.endsWith('/status')).length,2,'registration reconciles authoritative status once after completion');
    check(!/registration secret|register-csrf|after-register/.test(JSON.stringify(t.storage)));
    console.log('PASS native registration: required/email/length/equality validation, immediate all-secret clearing, exact owner/CSRF POST, shared auth busy controls, static accepted-not-activated guidance, no identity/auto-login/code requests or secret storage');
  }
  {
    for(const operation of ['login','register']) {
      const t=setup(), requests=[]; let resolve;
      t.context.fetch=(url,opts)=>{requests.push({url,opts});return url.endsWith('/status')?Promise.resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'tab-guard-csrf'})}):new Promise(r=>resolve=r);};
      await t.get('#otherLoginBtn').onclick();
      if(operation==='register') t.get('#otherRegisterTab').onclick();
      for(const [id,value] of Object.entries({otherUsername:'u',otherPassword:'login secret',otherRegisterEmail:'a@example.com',otherRegisterUsername:'u',otherRegisterPassword:'register secret',otherRegisterConfirm:'register secret'})) t.get('#'+id).value=value;
      const form=t.get(operation==='login'?'#otherLoginForm':'#otherRegisterForm');
      const submit=form.onsubmit({preventDefault(){}});
      for(const id of ['otherPassword','otherRegisterPassword','otherRegisterConfirm']) equal(t.get('#'+id).value,'',operation+' clears all password fields at submission');
      await new Promise(setImmediate);
      if(operation==='login') {
        t.get('#otherRegisterTab').onclick(); const message=t.get('#otherLoginMessage').textContent;
        resolve({ok:true,json:async()=>({ok:true,set:true,nickname:'stale tab login'})}); await submit;
        equal(t.get('#otherLoginMessage').textContent,message); check(!t.get('#otherLoginStatus').textContent.includes('stale tab login')); check(!t.get('#otherRegisterTabPanel').hidden);
      } else {
        resolve({ok:false,json:async()=>({ok:false,code:'verification_required',error:'请完成验证码'})}); await submit;
        const message=t.get('#otherRegisterMessage').textContent;
        for(const text of ['官方页面','不会绕过','不会自动重试','勿重复注册']) check(message.includes(text),text);
        check(!message.includes('官网已接受注册请求')); check(!t.get('#otherRegisterSubmit').disabled);
      }
      equal(requests.filter(r=>r.opts.method==='POST').length,1,'no retry/implicit login for '+operation);
    }
    for(const action of ['close','tab']) {
      const t=setup(), requests=[]; let resolve;
      t.context.fetch=(url,opts)=>{requests.push({url,opts});return requests.length===1?new Promise(r=>resolve=r):Promise.resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'reconciled-csrf'})});};
      const opening=t.get('#otherLoginBtn').onclick(); await new Promise(setImmediate); t.get('#otherRegisterTab').onclick();
      for(const [id,value] of Object.entries({otherRegisterEmail:'a@example.com',otherRegisterUsername:'u',otherRegisterPassword:'never submitted',otherRegisterConfirm:'never submitted'})) t.get('#'+id).value=value;
      const submit=t.get('#otherRegisterForm').onsubmit({preventDefault(){}});
      equal(t.get('#otherRegisterPassword').value,''); equal(t.get('#otherRegisterConfirm').value,'');
      if(action==='close') t.get('#otherLoginClose').onclick(); else t.get('#otherLoginTab').onclick();
      const message=t.get('#otherRegisterMessage').textContent;
      resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'cancelled-register-csrf'})}); await Promise.all([opening,submit]);
      equal(requests.filter(r=>r.url==='/api/hifiti/register').length,0,action+' while owner handshake pending cancels before register POST');
      equal(t.get('#otherRegisterMessage').textContent,message); check(!t.get('#otherRegisterSubmit').disabled);
    }
    console.log('PASS native account submit guards: login/register clear every password, login tab switch suppresses stale success, active registration challenge guides without bypass/retry/false acceptance, close/tab during owner handshake prevents signup POST');
  }
  {
    // Opening an account dialog must not detach or erase the inline forum; only
    // an explicit account mutation invalidates that session's private content.
    const t=setup(), requests=[];
    t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>url.endsWith('/status')?{ok:true,set:false,csrf:'retention-token'}:url.includes('/search?')?{ok:true,page:1,pages:1,total:1,items:[{id:2,title:'retained row'}]}:{ok:true,title:'retained detail',text:'long forum body',resources:[{url:'https://example.com/file',code:'1234'}]}};};
    t.evaluate('selectSource("other")'); await t.evaluate('searchOther("retention")'); await t.get('#otherResults').forumButtons[0].onclick();
    const row=t.get('#otherResults').forumRows[0], detail=t.get('#otherThread'), links=t.get('#otherThreadLinks').innerHTML;
    const before=requests.length; await t.get('#otherLoginBtn').onclick(); t.get('#otherRegisterTab').onclick(); t.get('#otherLoginClose').onclick();
    check(detail.parentNode===row); check(!detail.hidden); equal(t.get('#otherThreadLinks').innerHTML,links); equal(t.get('#otherThreadText').textContent,'long forum body'); equal(t.get('#otherResults').forumRows[0],row);
    equal(requests.slice(before).map(r=>r.url),['/api/hifiti/login/status'],'dialog open/tabs/close only checks status, preserving forum row');
    check(/<details\b[^>]*id="otherThreadFullText"[^>]*>\s*<summary>/.test(html)); check(!/<details\b[^>]*id="otherThreadFullText"[^>]*\sopen(?:\s|>)/.test(html),'full body collapsed by default');
    check(html.indexOf('id="otherThreadLinks"')<html.indexOf('id="otherThreadFullText"'),'resources precede collapsible long body'); equal(t.get('#otherThreadFullText').open,false);
    t.get('#otherThreadFullText').open=true; await t.get('#otherThreadReload').onclick(); equal(t.get('#otherThreadFullText').open,false,'reload collapses old body'); check(detail.parentNode===row,'reload retains exact row after modal interactions');
    console.log('PASS compact forum retention: modal open/tab/close keeps inline row and resource content, resources-first collapsed full body, reload retains row and resets body expansion');
  }
  {
    for(const mode of ['accepted','challenge','error']) {
      for(const action of ['tab','close','source','pagehide']) {
        const t=setup(), requests=[]; let resolve;
        t.context.fetch=(url,opts)=>{requests.push({url,opts});return url.endsWith('/register')?new Promise(r=>resolve=r):Promise.resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'race-csrf'})});};
        await t.get('#otherLoginBtn').onclick(); t.get('#otherRegisterTab').onclick();
        for(const [id,value] of Object.entries({otherRegisterEmail:'a@example.com',otherRegisterUsername:'u',otherRegisterPassword:'secret',otherRegisterConfirm:'secret'})) t.get('#'+id).value=value;
        const submit=t.get('#otherRegisterForm').onsubmit({preventDefault(){}}); await new Promise(setImmediate);
        equal(requests.filter(r=>r.url.endsWith('/register')).length,1,mode+'/'+action+' starts exactly one explicit register');
        const count=requests.length; await t.evaluate('searchOther("blocked during register")'); equal(requests.length,count,'register busy blocks new searches');
        if(action==='tab') t.get('#otherLoginTab').onclick();
        else if(action==='close') t.keydown('Escape');
        else if(action==='source') t.evaluate('selectSource("qq")');
        else await Promise.all(t.events.get('pagehide').map(fn=>fn()));
        const registerMessage=t.get('#otherRegisterMessage').textContent, loginMessage=t.get('#otherLoginMessage').textContent;
        resolve({ok:mode==='accepted',json:async()=>mode==='accepted'?{ok:true,registered:true,set:false,nickname:'stale signup identity',csrf:'stale-secret',message:'late accepted response'}:{ok:false,code:mode==='challenge'?'verification_required':'register_failed',error:'late '+mode}});
        await submit;
        equal(t.get('#otherRegisterMessage').textContent,registerMessage,mode+'/'+action+' stale registration cannot repaint another tab/closed modal'); equal(t.get('#otherLoginMessage').textContent,loginMessage);
        equal(requests.filter(r=>r.url.endsWith('/register')).length,1,'stale result never retries signup'); equal(requests.filter(r=>r.url==='/api/hifiti/login').length,0,'stale signup never auto logs in');
        for(const id of ['otherPassword','otherRegisterPassword','otherRegisterConfirm']) equal(t.get('#'+id).value,'');
        check(!t.get('#otherRegisterSubmit').disabled); check(!t.get('#otherLoginStatus').textContent.includes('stale signup identity'));
        if(action==='tab') {check(!t.get('#otherLoginPanel').hidden); check(!t.get('#otherLoginTabPanel').hidden);}
        else check(t.get('#otherLoginPanel').hidden,'late signup does not reopen dismissed modal');
      }
    }
    console.log('PASS native registration stale results: accepted/challenge/error after tab/close/source/pagehide cannot repaint tab, reopen modal, restore passwords, claim identity, retry or auto login; busy search guard and controls released');
  }
  {
    const t=setup(), requests=[]; t.evaluate('selectSource("other")'); let loggedIn=false;
    t.context.fetch=async(url,opts)=>{requests.push({url,opts}); if(url==='/api/hifiti/login') loggedIn=true; if(url==='/api/hifiti/logout') loggedIn=false; return {ok:true,json:async()=>({ok:true,set:loggedIn,nickname:'<forum nickname>',csrf:'secret-csrf',persistent:false,cookie:'never-display'})};};
    check(/id="otherUsername"[^>]*autocomplete="username"/.test(html)); check(/id="otherPassword"[^>]*type="password"[^>]*autocomplete="current-password"/.test(html));
    const opener=t.get('#otherLogin'); opener.focus(); await opener.onclick({currentTarget:opener}); check(!t.get('#otherLoginPanel').hidden); equal(requests.length,1); check(!requests[0].opts.method,'open is status only');
    t.get('#otherUsername').value=' my account '; t.get('#otherPassword').value='sensitive password'; let prevented=false;
    for(const [id,value] of Object.entries({otherRegisterEmail:'hidden@example.com',otherRegisterUsername:'hidden signup',otherRegisterPassword:'hidden signup secret',otherRegisterConfirm:'hidden signup secret'})) t.get('#'+id).value=value;
    const submit=t.get('#otherLoginForm').onsubmit({preventDefault(){prevented=true;}}); check(prevented); equal(t.get('#otherPassword').value,''); check(t.get('#otherLoginSubmit').disabled); await submit;
    const login=requests.find(r=>r.url==='/api/hifiti/login'); equal(JSON.parse(login.opts.body),{username:'my account',password:'sensitive password'}); equal(login.opts.headers['X-CSRF-Token'],'secret-csrf');
    check(t.get('#otherLoginStatus').textContent.includes('<forum nickname>')); check(t.get('#otherLoginStatus').textContent.includes('仅服务内存')); check(!t.get('#otherLoginStatus').textContent.includes('never-display')); check(t.get('#otherLoginPanel').hidden,'verified login automatically closes dialog'); check(!t.get('#otherLoginSubmit').disabled);
    equal(t.get('#otherLoginBtn').textContent,'HiFiTi · 已登录：<forum nickname>'); equal(opener.textContent,'已登录：<forum nickname>');
    equal(t.context.document.activeElement,opener,'verified success restores the exact inline opener focus');
    for(const id of ['otherLogin','otherLoginBtn']) equal(t.get('#'+id).getAttribute('aria-expanded'),'false');
    for(const id of ['otherUsername','otherPassword','otherRegisterEmail','otherRegisterUsername','otherRegisterPassword','otherRegisterConfirm']) equal(t.get('#'+id).value,'','success clears '+id);
    equal(t.messages.length,1); check(t.messages[0].message.includes('HiFiTi 登录成功：<forum nickname>')); check(!t.messages[0].error);
    equal(requests.filter(r=>r.url.endsWith('/status')).length,2,'verified login reconciles status after closing');
    t.get('#otherPassword').value='clear me'; t.get('#otherLoginClose').onclick(); equal(t.get('#otherUsername').value,''); equal(t.get('#otherPassword').value,''); check(t.get('#otherLoginPanel').hidden);
    await t.get('#otherLogout').onclick(); const logout=requests.find(r=>r.url==='/api/hifiti/logout'); equal(logout.opts.body,'{}'); equal(logout.opts.headers['X-CSRF-Token'],'secret-csrf'); check(t.get('#otherLoginStatus').textContent.includes('退出登录'));
    equal(t.get('#otherLoginBtn').textContent,'HiFiTi 登录 / 注册'); equal(opener.textContent,'登录 / 注册');
    for(const {url,opts} of requests) {check(url.startsWith('/api/hifiti/')); equal(opts.credentials,'same-origin'); equal(opts.cache,'no-store'); if(opts.method==='POST') equal(opts.headers['Content-Type'],'application/json');}
    check(!JSON.stringify(t.storage).includes('sensitive')); check(!JSON.stringify(t.storage).includes('secret-csrf')); check(!t.get('#cookieStatus').textContent.includes('forum')); check(!t.get('#miguCookieStatus').textContent.includes('forum')); check(!t.get('#neteaseCookieStatus').textContent.includes('forum'));
    await t.get('#otherLogin').onclick(); t.get('#otherUsername').value='clear on switch'; t.get('#otherPassword').value='clear on switch'; t.evaluate('selectSource("qq")'); equal(t.get('#otherUsername').value,''); equal(t.get('#otherPassword').value,''); check(t.get('#otherLoginPanel').hidden);
    console.log('PASS native forum login: explicit username/password submit only, autocomplete, immediate password clearing, CSRF, memory-only independent status, verified auto-close/toast/status/focus/credential cleanup, source cleanup and anonymous logout labels');
  }
  {
    for(const [name,auth,verified] of [
      ['verified',{set:true,nickname:'header owner',expired:false},true],
      ['false',{set:false,nickname:'unverified owner'},false],
      ['truthy string',{set:'true',nickname:'unverified owner'},false],
      ['truthy number',{set:1,nickname:'unverified owner'},false],
      ['missing',{nickname:'unverified owner'},false],
      ['expired',{set:true,nickname:'expired owner',expired:true},false]
    ]) {
      const t=setup(), requests=[], header=t.get('#otherLoginBtn');
      t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>({ok:true,csrf:'strict-csrf',...(requests.length===1?{set:false}:auth)})};};
      header.focus(); await header.onclick({currentTarget:header});
      t.get('#otherUsername').value='owner'; t.get('#otherPassword').value='strict secret';
      await t.get('#otherLoginForm').onsubmit({preventDefault(){}});
      equal(t.get('#otherLoginPanel').hidden,verified,name+' only strict non-expired set:true auto-closes');
      equal(requests.map(r=>r.url),['/api/hifiti/login/status','/api/hifiti/login','/api/hifiti/login/status'],name+' gets one explicit POST and fresh final status');
      equal(t.get('#otherPassword').value,''); check(!t.get('#otherLoginSubmit').disabled);
      for(const id of ['otherLogin','otherLoginBtn']) equal(t.get('#'+id).getAttribute('aria-expanded'),String(!verified),name+' opener state');
      if(verified) {
        equal(t.context.document.activeElement,header,'header-triggered success restores header focus'); equal(t.get('#otherUsername').value,'');
        equal(header.textContent,'HiFiTi · 已登录：header owner'); equal(t.get('#otherLogin').textContent,'已登录：header owner');
        equal(t.messages.length,1); check(t.messages[0].message.includes('登录成功')); check(!t.messages[0].error);
      } else {
        equal(t.messages.length,0,name+' never announces success'); check(t.get('#otherLoginMessage').textContent.includes('登录尚未确认'));
        equal(t.get('#otherUsername').value,'owner',name+' retains account for manual retry'); check(!t.get('#otherLoginTabPanel').hidden);
        check(!t.get('#otherLoginStatus').textContent.includes('已登录：'),name+' cannot claim authenticated identity');
        equal(header.textContent,auth.expired?'HiFiTi · 登录已过期':'HiFiTi 登录 / 注册');
        equal(t.get('#otherLogin').textContent,auth.expired?'重新登录':'登录 / 注册');
        t.get('#otherPassword').value='retry only by user'; t.get('#otherLoginClose').onclick(); equal(t.get('#otherUsername').value,''); equal(t.get('#otherPassword').value,'');
      }
      check(!/strict secret|strict-csrf/.test(JSON.stringify(t.storage)),name+' stores no credentials or CSRF');
    }
    console.log('PASS native forum verified login gate: normal header success restores focus, false/truthy/missing/expired results stay open without success toast, fresh final status and anonymous/expired labels');
  }
  {
    for(const action of ['close','reopen','tab']) {
      const t=setup(), requests=[], header=t.get('#otherLoginBtn'); let resolveLogin, resolveStatus;
      t.context.fetch=(url,opts)=>{
        requests.push({url,opts});
        if(url==='/api/hifiti/login') return new Promise(resolve=>resolveLogin=resolve);
        if(requests.length===1) return Promise.resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'race-login-csrf'})});
        return new Promise(resolve=>resolveStatus=resolve);
      };
      await header.onclick({currentTarget:header}); t.get('#otherUsername').value='owner'; t.get('#otherPassword').value='race secret';
      const submit=t.get('#otherLoginForm').onsubmit({preventDefault(){}}); await new Promise(setImmediate);
      equal(requests.at(-1).url,'/api/hifiti/login',action+' waits for explicit login POST');
      if(action==='tab') t.get('#otherRegisterTab').onclick();
      else {
        t.get('#otherLoginClose').onclick();
        if(action==='reopen') {await header.onclick({currentTarget:header});t.get('#otherUsername').value='new attempt';}
      }
      const message=t.get('#otherLoginMessage').textContent, registerMessage=t.get('#otherRegisterMessage').textContent;
      const focus=t.context.document.activeElement;
      resolveLogin({ok:true,json:async()=>({ok:true,set:true,nickname:'stale POST owner',csrf:'stale-post-csrf'})}); await new Promise(setImmediate);
      equal(requests.at(-1).url,'/api/hifiti/login/status',action+' starts authoritative status after old POST finishes');
      equal(requests.filter(r=>r.url.endsWith('/status')).length,2,action+' bypasses cached opening handshake');
      check(!t.get('#otherLoginStatus').textContent.includes('stale POST owner'),action+' ignores stale POST identity');
      equal(t.get('#otherLoginPanel').hidden,action==='close',action+' old POST cannot close newly opened modal'); equal(t.messages.length,0,action+' stale login cannot toast success');
      check(!t.get('#otherLoginSubmit').disabled,action+' releases controls before final status');
      resolveStatus({ok:true,json:async()=>({ok:true,set:true,nickname:'fresh backend owner',csrf:'fresh-race-csrf'})}); await submit;
      equal(t.get('#otherLoginBtn').textContent,'HiFiTi · 已登录：fresh backend owner'); equal(t.get('#otherLogin').textContent,'已登录：fresh backend owner');
      check(t.get('#otherLoginStatus').textContent.includes('fresh backend owner'),action+' reconciles actual backend authentication');
      equal(t.get('#otherLoginPanel').hidden,action==='close',action+' fresh status never changes dialog visibility');
      equal(t.get('#otherLoginMessage').textContent,message); equal(t.get('#otherRegisterMessage').textContent,registerMessage); equal(t.context.document.activeElement,focus,action+' status never steals focus');
      if(action==='reopen') equal(t.get('#otherUsername').value,'new attempt','old completion cannot clear new attempt');
      if(action==='tab') check(!t.get('#otherRegisterTabPanel').hidden,'fresh status preserves selected registration tab');
      equal(requests.filter(r=>r.opts.method==='POST').length,1,action+' no automatic retry'); equal(t.messages.length,0);
      for(const id of ['otherPassword','otherRegisterPassword','otherRegisterConfirm']) equal(t.get('#'+id).value,'');
      check(!/race secret|race-login-csrf|stale-post-csrf|fresh-race-csrf/.test(JSON.stringify(t.storage)));
    }
    console.log('PASS native forum delayed login reconciliation: close/reopen/tab suppress stale POST close/toast/identity, fresh backend status updates both labels without closing new modal, repainting messages, clearing new account, stealing focus or retrying');
  }
  {
    const t=setup(), requests=[], pending=[]; t.evaluate('selectSource("other")');
    t.context.fetch=(url,opts)=>{requests.push({url,opts});return new Promise(resolve=>pending.push({url,resolve}));};
    const first=t.evaluate('searchOther("old")'); const second=t.evaluate('searchOther("latest")'); await new Promise(setImmediate);
    equal(requests.length,1,'simultaneous searches share initial owner-cookie handshake'); pending[0].resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'csrf-one'})}); await new Promise(setImmediate);
    equal(requests.length,2); check(requests[1].url.includes('kw=latest')); pending[1].resolve({ok:true,json:async()=>({ok:true,page:1,pages:1,total:1,items:[{id:1,title:'latest result'}]})}); await Promise.all([first,second]); check(t.get('#otherResults').innerHTML.includes('latest result'));
    const old=t.evaluate('searchOther("old visit")'); await new Promise(setImmediate); t.evaluate('selectSource("qq"); selectSource("other")'); const fresh=t.evaluate('searchOther("new visit")'); await new Promise(setImmediate);
    const oldSearch=pending.find(p=>p.url.includes('kw=old+visit')); const handshake=pending.filter(p=>p.url.endsWith('/status')).at(-1); check(!!oldSearch); check(!!handshake);
    oldSearch.resolve({ok:false,json:async()=>({ok:false,error:'stale failure'})}); await old; check(!t.get('#otherStatus').textContent.includes('stale failure'));
    handshake.resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'csrf-new'})}); await new Promise(setImmediate); pending.at(-1).resolve({ok:true,json:async()=>({ok:true,page:1,pages:1,total:1,items:[{id:2,title:'new visit'}]})}); await fresh;
    const detail=t.evaluate('openOtherThread("id&2=?")'); await new Promise(setImmediate); equal(new URL(pending.at(-1).url,'http://offline').searchParams.get('id'),'id&2=?'); const oldDetail=pending.at(-1);
    t.get('#otherThreadClose').onclick(); oldDetail.resolve({ok:true,json:async()=>({ok:true,title:'stale title',text:'stale detail',links:[]})}); await detail; check(t.get('#otherThread').hidden); equal(t.get('#otherThreadText').textContent,'');
    const nextDetail=t.evaluate('openOtherThread(2)'); await new Promise(setImmediate); const nextPending=pending.at(-1); t.evaluate('selectSource("qq"); selectSource("other")'); nextPending.resolve({ok:false,json:async()=>({ok:false,error:'late thread error'})}); await nextDetail; check(!t.get('#otherThreadNotice').textContent.includes('late'));
    console.log('PASS native forum races: shared serialized handshake, latest search only, source/visit stale search errors, encoded thread ID, close/source invalidates thread success/error');
  }
  {
    const t=setup(), requests=[], pending=[]; t.evaluate('selectSource("other")');
    t.context.fetch=(url,opts)=>{requests.push({url,opts});return new Promise(resolve=>pending.push({url,resolve}));};
    const old=t.evaluate('refreshOtherStatus()'); await new Promise(setImmediate); t.evaluate('selectSource("qq"); selectSource("other")'); const fresh=t.evaluate('refreshOtherStatus()'); await new Promise(setImmediate); equal(requests.length,1,'visits serialize owner cookie establishment');
    pending[0].resolve({ok:true,json:async()=>({ok:true,set:true,nickname:'old owner',csrf:'old-secret'})}); await old; await new Promise(setImmediate); check(!t.get('#otherLoginStatus').textContent.includes('old owner')); equal(requests.length,2);
    pending[1].resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'new-secret'})}); await fresh;
    const a=t.evaluate('searchOther("before login")'), b=t.evaluate('openOtherThread(1)'); await new Promise(setImmediate);
    const search=pending.find(p=>p.url.includes('/search?')), detail=pending.find(p=>p.url.includes('/thread?'));
    const open=t.get('#otherLogin').onclick(); await new Promise(setImmediate); pending.at(-1).resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'new-secret'})}); await open;
    t.get('#otherUsername').value='user'; t.get('#otherPassword').value='password';
    const submit=t.get('#otherLoginForm').onsubmit({preventDefault(){}}); await new Promise(setImmediate);
    equal(requests.at(-1).url,'/api/hifiti/login'); equal(requests.at(-1).opts.headers['X-CSRF-Token'],'new-secret');
    search.resolve({ok:true,json:async()=>({ok:true,items:[{id:1,title:'stale before login'}]})}); detail.resolve({ok:true,json:async()=>({ok:true,title:'stale title',text:'stale private text'})}); await Promise.all([a,b]);
    equal(t.get('#otherResults').innerHTML,''); equal(t.get('#otherThreadText').textContent,''); check(t.get('#otherThread').hidden);
    const count=requests.length; await t.evaluate('searchOther("blocked during login")'); equal(requests.length,count);
    t.get('#otherLoginClose').onclick(); pending.at(-1).resolve({ok:true,json:async()=>({ok:true,set:true,nickname:'late closed login'})}); await new Promise(setImmediate);
    equal(requests.at(-1).url,'/api/hifiti/login/status','closed login reconciles backend state using a fresh status request');
    pending.at(-1).resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'logout-secret'})}); await submit; check(!t.get('#otherLoginStatus').textContent.includes('late closed')); check(t.get('#otherLoginPanel').hidden); equal(t.get('#otherPassword').value,'');
    t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>({ok:true,set:false,csrf:'logout-secret'})};};
    const s=t.evaluate('searchOther("before logout")'); await s;
    let detailResolve; t.context.fetch=(url,opts)=>{requests.push({url,opts});return url.includes('/thread?')?new Promise(resolve=>detailResolve=resolve):Promise.resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'logout-secret'})});};
    const late=t.evaluate('openOtherThread(1)'); await new Promise(setImmediate); await t.get('#otherLogout').onclick(); detailResolve({ok:true,json:async()=>({ok:true,text:'stale after logout'})}); await late;
    check(t.get('#otherThread').hidden); equal(t.get('#otherThreadText').textContent,''); equal(t.get('#otherResults').innerHTML,'');
    console.log('PASS native forum auth races: serialized owner handshakes across visits, newest CSRF, login/logout invalidate in-flight content, auth blocks new searches, closed panel suppresses late status');
  }
  {
    const t=setup(), requests=[]; t.evaluate('selectSource("other")');
    t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:!url.includes('/search?'),json:async()=>url.includes('/search?')?{ok:false,code:'verification_required',error:'需要人机验证'}:{ok:true,set:false,csrf:'error-token'}};};
    await t.evaluate('searchOther("test")'); check(t.get('#otherStatus').textContent.includes('官方页面')); check(t.get('#otherStatus').textContent.includes('不会绕过')); equal(t.get('#otherResults')['aria-busy'],'false'); check(t.get('#otherPagination').hidden);
    await t.get('#otherLogin').onclick(); t.get('#otherUsername').value='user'; t.get('#otherPassword').value='bad-secret';
    t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:false,json:async()=>({ok:false,code:'verification_required',error:'请完成验证码'})};};
    await t.get('#otherLoginForm').onsubmit({preventDefault(){}}); equal(t.get('#otherPassword').value,''); check(t.get('#otherLoginMessage').textContent.includes('官方页面')); check(t.get('#otherLoginMessage').textContent.includes('不会自动重试')); equal(requests.filter(r=>r.url==='/api/hifiti/login').length,1);
    const before=requests.length; t.get('#otherUsername').value=''; t.get('#otherPassword').value=''; await t.get('#otherLoginForm').onsubmit({preventDefault(){}}); equal(requests.length,before); check(t.get('#otherLoginMessage').textContent.includes('请输入'));
    t.context.fetch=async()=>({ok:false,json:async()=>({ok:false,error:'<backend failure>'})}); await t.evaluate('openOtherThread(2)'); equal(t.get('#otherThreadNotice').textContent,'<backend failure>');
    t.get('#otherUsername').value='clear on pagehide'; t.get('#otherPassword').value='clear on pagehide'; await Promise.all(t.events.get('pagehide').map(fn=>fn())); equal(t.get('#otherUsername').value,''); equal(t.get('#otherPassword').value,''); check(t.get('#otherLoginPanel').hidden); check(t.get('#otherThread').hidden);
    const missing=setup(); missing.evaluate('selectSource("other")'); missing.context.fetch=async()=>({ok:true,json:async()=>({ok:true,set:false})}); await missing.evaluate('searchOther("blocked without csrf")'); check(missing.get('#otherStatus').textContent.includes('安全校验不可用')); equal(missing.get('#otherResults').innerHTML,'');
    console.log('PASS native forum errors/security: official verification guidance without bypass/retry, native API errors, empty credentials, no-CSRF failure, pagehide secret/detail cleanup');
  }
  {
    const t=setup(), requests=[]; let resolveStatus;
    t.evaluate('selectSource("other")');
    t.context.fetch=(url,opts)=>{requests.push({url,opts});return new Promise(resolve=>resolveStatus=resolve);};
    const opening=t.get('#otherLogin').onclick(); await new Promise(setImmediate);
    t.get('#otherUsername').value='user'; t.get('#otherPassword').value='never send after close';
    const submit=t.get('#otherLoginForm').onsubmit({preventDefault(){}}); equal(t.get('#otherPassword').value,'');
    t.get('#otherLoginClose').onclick(); resolveStatus({ok:true,json:async()=>({ok:true,set:false,csrf:'closed-panel-token'})}); await new Promise(setImmediate);
    equal(requests.at(-1).url,'/api/hifiti/login/status','cancelled login also reconciles with fresh status');
    resolveStatus({ok:true,json:async()=>({ok:true,set:false,csrf:'reconciled-closed-panel-token'})}); await Promise.all([opening,submit]);
    equal(requests.filter(r=>r.opts.method==='POST').length,0,'closing while handshake pending cancels credential submit before POST'); equal(requests.length,2,'only opening and final status requests are sent'); check(!t.get('#otherLoginSubmit').disabled); equal(t.get('#otherUsername').value,''); check(t.get('#otherLoginPanel').hidden);
    // New searches invalidate pending detail responses and old row closures.
    t.context.fetch=async(url,opts)=>{requests.push({url,opts});return {ok:true,json:async()=>url.endsWith('/status')?{ok:true,set:false,csrf:'detail-token'}:{ok:true,page:1,pages:1,total:1,items:[{id:1,title:'first row'}]}};};
    await t.evaluate('searchOther("first")'); const oldRow=t.get('#otherResults').forumButtons[0];
    let resolveDetail;
    t.context.fetch=(url,opts)=>{requests.push({url,opts});return url.includes('/thread?')?new Promise(resolve=>resolveDetail=resolve):Promise.resolve({ok:true,json:async()=>({ok:true,page:1,pages:1,total:1,items:[{id:2,title:'new row'}]})});};
    const detail=oldRow.onclick(); await new Promise(setImmediate); await t.evaluate('searchOther("new")'); const before=requests.length; oldRow.onclick(); equal(requests.length,before,'stale row closure cannot open old detail');
    resolveDetail({ok:true,json:async()=>({ok:true,title:'late old detail',text:'late private content'})}); await detail; check(t.get('#otherThread').hidden); equal(t.get('#otherThreadText').textContent,''); check(t.get('#otherResults').innerHTML.includes('new row'));
    check(!JSON.stringify(t.storage).includes('never send'));
    console.log('PASS native forum cancellation: close during owner handshake prevents login POST, cleared credentials, new searches invalidate thread responses and stale row actions');
  }
  {
    const t=setup(); const requests=[]; let loggedIn=false, pollStatus='waiting';
    t.context.fetch=async (url,opts={})=>{
      requests.push({url,opts});
      const body=url.endsWith('/status')&&!url.includes('/qr/')?{ok:true,set:loggedIn,nickname:'<nickname>',csrf:'csrf-secret',persistent:false,uin:'hidden-account',cookie:'hidden-cookie'}
        :url.endsWith('/qr/start')?{ok:true,id:'task&1',qr:'data:image/png;base64,YQ==',expires_in:120,status:'waiting'}
        :url.includes('/qr/status?')?{ok:true,id:'task&1',status:pollStatus,message:pollStatus==='scanned'?'手机已扫码，请确认':''}
        :{ok:true};
      if(url.endsWith('/logout')) loggedIn=false;
      return {ok:true,json:async()=>body};
    };
    equal(t.evaluate('PROVIDERS.migu.capabilities.login'),true); equal(requests.length,0);
    t.evaluate('miguLogin.open()'); await new Promise(setImmediate);
    equal(requests.filter(r=>r.url==='/api/migu/login/status').length,1,'one initial owner handshake');
    equal(requests.filter(r=>r.url.endsWith('/qr/start')).length,1);
    equal(t.get('#miguLoginQr').src,'data:image/png;base64,YQ=='); check(t.get('#miguQrHint').textContent.includes('咪咕官方客户端')); check(t.get('#miguQrCountdown').textContent.includes('120'));
    await t.tick(); check(requests.at(-1).url.includes('id=task%261')); check(t.get('#miguQrHint').textContent.includes('扫码'));
    pollStatus='scanned'; await t.tick(); check(t.get('#miguQrHint').textContent.includes('手机已扫码'));
    loggedIn=true; pollStatus='success'; await t.tick(); check(t.get('#miguCookieStatus').textContent.includes('<nickname>')); check(t.get('#miguCookieStatus').textContent.includes('服务内存')); check(!t.get('#miguCookieStatus').textContent.includes('hidden')); equal(t.get('#miguLoginQr').style.display,'none'); equal(t.timers.size,0);
    const before=requests.length; await t.get('#miguStatusRefresh').onclick(); equal(requests.length,before+1); check(!requests.at(-1).url.includes('/qr/'));
    await t.get('#miguLogout').onclick(); await new Promise(setImmediate);
    check(requests.some(r=>r.url.endsWith('/qr/cancel')&&JSON.parse(r.opts.body).id==='task&1')); check(requests.some(r=>r.url==='/api/migu/login/logout')); check(t.get('#miguCookieStatus').textContent.includes('未登录咪咕'));
    for(const {url,opts} of requests) {
      equal(opts.credentials,'same-origin'); equal(opts.cache,'no-store'); check(url.startsWith('/api/migu/login/'));
      if(opts.method==='POST') {equal(opts.headers['X-CSRF-Token'],'csrf-secret'); check(!/nickname|password|cookie|hidden/.test(opts.body));}
    }
    check(!JSON.stringify(t.storage).includes('csrf-secret')); check(!JSON.stringify(t.storage).includes('hidden-cookie'));
    check(!t.get('#cookieStatus').textContent.includes('nickname')); check(!t.get('#neteaseCookieStatus').textContent.includes('nickname'));
    for(const state of ['expired','error','cancelled']) {
      pollStatus=state; t.evaluate('miguLogin.open()'); await new Promise(setImmediate); await t.tick(); equal(t.timers.size,0); equal(t.get('#miguLoginQr').style.display,'none');
    }
    console.log('PASS Migu login: owner handshake, CSRF POSTs, PNG, poll states, nickname-only memory status, refresh without QR, logout, independent accounts and no secret storage');
  }
  {
    const t=setup(); const requests=[]; let releaseStart;
    t.context.fetch=(url,opts={})=>{
      requests.push({url,opts});
      if(url.endsWith('/qr/start')) return new Promise(resolve=>releaseStart=resolve);
      return Promise.resolve({ok:true,json:async()=>({ok:true,set:false,csrf:'race-csrf'})});
    };
    t.evaluate('miguLogin.open()'); await new Promise(setImmediate);
    const close=t.evaluate('miguLogin.close()');
    releaseStart({ok:true,json:async()=>({ok:true,id:'late-task',qr:'data:image/png;base64,YQ==',status:'waiting'})}); await close;
    check(requests.some(r=>r.url.endsWith('/qr/cancel')&&JSON.parse(r.opts.body).id==='late-task')); equal(t.get('#miguLoginQr').src,undefined); equal(t.timers.size,0);
    t.evaluate('miguLogin.open()'); await new Promise(setImmediate);
    const logout=t.get('#miguLogout').onclick(); await new Promise(setImmediate);
    check(!requests.some(r=>r.url.endsWith('/logout')),'logout waits for pending start cleanup');
    releaseStart({ok:true,json:async()=>({ok:true,id:'logout-task',qr:'data:image/png;base64,YQ==',status:'waiting'})}); await logout; await new Promise(setImmediate);
    const cancelled=requests.findIndex(r=>r.url.endsWith('/qr/cancel')&&JSON.parse(r.opts.body).id==='logout-task'); const loggedout=requests.findIndex(r=>r.url.endsWith('/logout'));
    check(cancelled>=0&&loggedout>cancelled); equal(t.timers.size,0);
    console.log('PASS Migu QR races: late starts cancelled after close, logout waits for orphan cleanup');
  }
  {
    const t=setup(); const pending=[]; const requests=[];
    t.context.fetch=(url,opts={})=>{requests.push({url,opts});return new Promise(resolve=>pending.push({url,resolve}));};
    const old=t.evaluate('miguLogin.refreshStatus()'); t.evaluate('selectSource("other"); selectSource("qq")');
    const fresh=t.evaluate('miguLogin.refreshStatus()');
    equal(pending.length,1,'source changes serialize initial owner-cookie handshakes');
    pending[0].resolve({ok:true,json:async()=>({ok:true,set:true,nickname:'old visit',csrf:'old-csrf'})}); await old; await new Promise(setImmediate);
    check(!t.get('#miguCookieStatus').textContent.includes('old visit'));
    pending[1].resolve({ok:true,json:async()=>({ok:true,set:true,nickname:'new visit',csrf:'new-csrf'})}); await fresh;
    check(t.get('#miguCookieStatus').textContent.includes('new visit')); check(!t.get('#miguCookieStatus').textContent.includes('old visit'));
    t.evaluate('miguLogin.open()'); await new Promise(setImmediate);
    const start=pending.find(p=>p.url.endsWith('/qr/start')); check(!!start);
    const req=requests.find(r=>r.url.endsWith('/qr/start')); equal(req.opts.headers['X-CSRF-Token'],'new-csrf');
    start.resolve({ok:true,json:async()=>({ok:true,id:'poll-race',qr:'data:image/png;base64,YQ==',status:'waiting'})}); await new Promise(setImmediate);
    const tick=t.tick(); await new Promise(setImmediate);
    const poll=pending.find(p=>p.url.includes('/qr/status?')); check(!!poll);
    t.evaluate('selectSource("other"); selectSource("qq")');
    poll.resolve({ok:true,json:async()=>({ok:true,status:'success'})}); await tick;
    check(!t.messages.some(m=>m.message.includes('登录成功'))); equal(t.timers.size,0);
    console.log('PASS Migu source/visit races: old status cannot replace nickname/CSRF; late poll success never reopens UI or announces login');
  }
  {
    const t=setup(); const requests=[];
    t.context.fetch=async (url,opts={})=>{requests.push({url,opts});return {ok:!url.endsWith('/qr/start'),status:503,json:async()=>url.endsWith('/qr/start')?{ok:false,message:'官方扫码服务暂不可用'}:{ok:true,set:false,csrf:'safe-csrf'}};};
    t.evaluate('miguLogin.open()'); await new Promise(setImmediate);
    equal(t.get('#miguQrHint').textContent,'官方扫码服务暂不可用'); equal(t.timers.size,0);
    t.context.fetch=async (url,opts={})=>{requests.push({url,opts});return {ok:true,json:async()=>url.endsWith('/qr/start')?{ok:true,id:'invalid-image',qr:'https://evil.invalid/track',status:'waiting'}:{ok:true,set:false,csrf:'safe-csrf'}};};
    await t.get('#miguQrRefresh').onclick();
    equal(t.get('#miguQrHint').textContent,'二维码格式无效，请重新扫码'); equal(t.get('#miguLoginQr').src,undefined); equal(t.timers.size,0); check(requests.some(r=>r.url.endsWith('/qr/cancel')&&JSON.parse(r.opts.body).id==='invalid-image'));
    await t.evaluate('miguLogin.close()');
    t.context.fetch=async (url,opts={})=>{requests.push({url,opts});return {ok:true,json:async()=>url.endsWith('/qr/start')?{ok:true,id:'pagehide',qr:'data:image/png;base64,YQ==',status:'waiting'}:{ok:true,set:false,csrf:'safe-csrf'}};};
    t.evaluate('miguLogin.open()'); await new Promise(setImmediate);
    await Promise.all(t.events.get('pagehide').map(fn=>fn()));
    check(requests.some(r=>r.url.endsWith('/qr/cancel')&&JSON.parse(r.opts.body).id==='pagehide'&&r.opts.keepalive)); equal(t.get('#miguLoginPanel').style.display,'none'); equal(t.timers.size,0);
    console.log('PASS Migu errors/security: backend message feedback, untrusted QR rejected/cancelled, pagehide cancels with owner/CSRF keepalive');
  }
}
main().then(() => console.log(`PASS ${assertions} assertions; offline VM DOM stubs; no browser/backend used.`)).catch(error => { console.error(error); process.exitCode = 1; });
