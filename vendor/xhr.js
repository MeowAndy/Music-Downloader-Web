// Node 环境下的 XMLHttpRequest 实现（供 QQ音乐 vendor 模块使用）
class XMLHttpRequestShim {
  constructor() {
    this.readyState = 0;
    this.status = 0;
    this.statusText = '';
    this.responseText = '';
    this.response = null;
    this.responseType = '';
    this.onreadystatechange = null;
    this.onload = null;
    this.onerror = null;
    this._headers = {};
    this._method = 'GET';
    this._url = '';
  }
  open(method, url) {
    this._method = method;
    this._url = String(url);
    this.readyState = 1;
  }
  setRequestHeader(k, v) { this._headers[k] = v; }
  getResponseHeader(k) {
    return this._responseHeaders ? (this._responseHeaders[k.toLowerCase()] || null) : null;
  }
  getAllResponseHeaders() {
    if (!this._responseHeaders) return '';
    return Object.entries(this._responseHeaders).map(([k, v]) => k + ': ' + v).join('\r\n');
  }
  _setReady(state) {
    this.readyState = state;
    if (typeof this.onreadystatechange === 'function') this.onreadystatechange();
  }
  async send(body) {
    const url = this._url.startsWith('//') ? 'https:' + this._url : this._url;
    try {
      const res = await fetch(url, {
        method: this._method,
        headers: { ...this._headers, 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36', 'Referer': 'https://y.qq.com/' },
        body: body === undefined ? undefined : body,
      });
      this.status = res.status;
      this.statusText = res.statusText;
      this._responseHeaders = {};
      res.headers.forEach((v, k) => { this._responseHeaders[k] = v; });
      const buf = Buffer.from(await res.arrayBuffer());
      this.responseText = buf.toString('utf-8');
      this.response = this.responseType === 'arraybuffer'
        ? buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)
        : this.responseText;
      this._setReady(2);
      this._setReady(3);
      this._setReady(4);
      if (typeof this.onload === 'function') this.onload();
    } catch (e) {
      this.status = 0;
      this._setReady(4);
      if (typeof this.onerror === 'function') this.onerror(e);
    }
  }
  abort() {}
}
module.exports = XMLHttpRequestShim;
