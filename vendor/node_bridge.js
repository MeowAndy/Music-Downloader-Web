// Node 桥接脚本：供 Flask 后端调用，走签名加密通道请求 QQ音乐歌单接口
// 用法: node node_bridge.js playlist <disstid> [song_num]
const path = require('path');
const { exports: t } = require(path.join(__dirname, 'qq_sec.js'));
const request = t.c;

async function main() {
  const [cmd, ...args] = process.argv.slice(2);

  if (cmd === 'search') {
    // node node_bridge.js search <keyword> [num]
    const kw = args[0];
    const num = parseInt(args[1] || '30', 10);
    if (!kw) throw new Error('keyword required');
    let last = null;
    for (let i = 0; i < 5; i++) {
      const res = await request({
        url: 'https://u.y.qq.com/cgi-bin/musicu.fcg',
        dataType: 'json', postType: true, type: 'POST',
        data: {
          comm: { uin: 0, format: 'json', ct: 24, cv: 0 },
          req_1: {
            method: 'DoSearchForQQMusicDesktop',
            module: 'music.search.SearchCgiService',
            param: { search_type: 0, query: kw, page_num: 1, num_per_page: num },
          },
        },
      });
      last = res;
      const songs = res && res.req_1 && res.req_1.data && res.req_1.data.body
        && res.req_1.data.body.song && res.req_1.data.body.song.list;
      if (songs && songs.length) {
        const out = songs.map(s => ({
          name: s.name || s.songname || '',
          singer: (s.singer || []).map(x => x.name).join('/'),
          mid: s.mid || s.songmid || '',
          album: (s.album || {}).name || '',
          duration: s.interval || 0,
          pay_play: (s.pay && s.pay.pay_play) || 0,
        }));
        process.stdout.write(JSON.stringify(out));
        return;
      }
      await new Promise(r => setTimeout(r, 600 + 400 * i));
    }
    const code = last && last.req_1 ? last.req_1.code : 'null';
    if (code !== 0) throw new Error('search api error, code=' + code);
    process.stdout.write('[]');
    return;
  }

  if (cmd !== 'playlist') {
    throw new Error('unknown command: ' + cmd);
  }
  const disstid = args[0];
  const songNum = parseInt(args[1] || '500', 10);
  if (!disstid) throw new Error('disstid required');

  const res = await request({
    url: 'https://u.y.qq.com/cgi-bin/musicu.fcg',
    dataType: 'json',
    postType: true,
    type: 'POST',
    data: {
      comm: { uin: 0, format: 'json', ct: 24, cv: 0 },
      req_1: {
        method: 'uniform_get_Dissinfo',
        module: 'music.srfDissInfo.aiDissInfo',
        param: { disstid: Number(disstid), enc_fail: 1, tag: 1, userinfo: 1, song_begin: 0, song_num: songNum }
      }
    }
  });

  if (!res || !res.req_1 || res.req_1.code !== 0) {
    const code = res && res.req_1 ? res.req_1.code : 'null';
    throw new Error('playlist api error, code=' + code);
  }
  const d = res.req_1.data;
  const info = d.dirinfo || {};
  const rawSongs = d.songlist || [];
  const songs = rawSongs.map(s => {
    const f = s.file || {};
    const sizeNew = f.size_new || [];
    return {
      name: s.name || s.songname || '',
      singer: (s.singer || []).map(x => x.name).join('/'),
      mid: s.mid || s.songmid || '',
      album: (s.album || {}).name || '',
      duration: s.interval || 0,
      pay_play: (s.pay && s.pay.pay_play) || 0,
      file: {
        media_mid: f.media_mid || '',
        standard: !!(f.size_128mp3 || f.size_m4a),
        hq: !!f.size_320mp3,
        sq: !!f.size_flac,
        master: !!(sizeNew.length > 0 && sizeNew[0]),
      },
    };
  });
  const out = {
    name: info.title || info.dissname || '',
    logo: (info.picurl || info.logo || '').replace('http://', 'https://'),
    song_count: songs.length,
    songs,
  };
  process.stdout.write(JSON.stringify(out));
}

main().catch(e => {
  process.stderr.write(String(e && e.message || e));
  process.exit(1);
});
