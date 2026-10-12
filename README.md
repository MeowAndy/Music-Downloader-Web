# Music Downloader Web

🎵 一站式音乐工具：**QQ音乐搜索/下载（标准·HQ·SQ无损·臻品母带四档音质）+ 全民K歌下载 + mflac解密 + 本地AI人声分离**，纯本地运行的 Web 应用。

基于 [Music-Downloader](https://github.com/Li4n0/Music-Downloader) 与 [KG-Downloader](https://github.com/T-K-233/KG-Downloader) 重制，接口全部更新为当前可用版本。

![界面预览](docs/screenshot-dark.png)
![日间主题](docs/screenshot-light.png)

## ✨ 功能

| 功能 | 说明 |
|------|------|
| QQ音乐搜索 | 关键词搜索乐库，免费/VIP 标记 |
| **四档音质下载** | 标准128k / HQ 320k / **SQ无损(默认)** / 臻品母带，自动降级 |
| QQ音乐歌单 | 粘贴歌单链接，批量下载全部歌曲 |
| **QQ音乐歌词** | 搜索结果/QQ歌单支持预览、单曲或批量仅歌词下载，也可随音频下载（默认关闭）；TXT 默认去时间轴，LRC 可选保留时间轴 |
| 全民K歌 | 作品/主页链接解析下载（支持新旧链接格式） |
| mflac 解密 | 客户端下载的加密 mflac 转可用 flac（Unlock Music WASM） |
| 人声分离 | 本地 AI 模型分离人声/伴奏（2/4/5 分轨，启动时隐身自动拉起，缺失可一键下载） |
| 格式转换 | ffmpeg 转换 MP3，文件一键转换 |
| 界面 | 三栏布局 · 夜间/日间主题 · 下载文件夹一键打开/清空 |

## 🚀 快速开始（普通用户）

1. 到 [Releases](../../releases) 下载 `MusicDownloaderWeb.exe`（单文件，内置全部依赖）
2. 双击运行，等待自检完成，浏览器自动打开 `http://127.0.0.1:8899`
3. 点右上角「🔑 登录设置」，使用 QQ音乐 App 扫码并确认

> 首次启动需解包内置依赖，约 10~20 秒属正常现象。

### 扫码登录

QQ音乐搜索与高品质下载需要登录态：

1. 在手机 QQ音乐 App 中准备扫码入口
2. 打开页面右上角「🔑 登录设置」，等待二维码出现
3. 使用 QQ音乐 App 扫码并在手机上确认，页面会自动完成登录

登录信息只保存在本机的 `qq_cookie.txt`，二维码、会话状态和凭据不会上传到本项目服务器。二维码登录依赖 QQ音乐当前内部接口，平台升级后可能暂时不可用；如需恢复旧版本，可继续使用后端兼容接口导入已有 Cookie。

## 🔧 源码运行（开发者）

```bash
git clone https://github.com/MeowAndy/Music-Downloader-Web.git
cd Music-Downloader-Web
pip install flask requests paho-mqtt
# 放入 node.exe 和 ffmpeg.exe 到 bin/ 目录（歌单/转换功能需要）
python -m unittest discover -s tests -v   # 单元测试入口（测试将加入）
python selftest.py   # 仅保留旧版手动冒烟流程；会启动服务并可能打开浏览器
```

单元测试入口不会自动安装依赖、启动服务、打开浏览器、访问真实 QQ 接口或使用真实 Cookie；测试临时文件应使用临时目录。打包时通过最新版本显式导入 `lyrics_api.py`，不新增依赖：歌词功能使用 Python 标准库与现有 `requests`。

### QQ音乐歌词

- 歌词功能默认关闭，可在页面勾选「同时下载歌词」；搜索结果和 QQ歌单都支持「歌词预览」「仅歌词」及批量歌词下载。
- 默认保存为 `.txt` 纯文本，自动去除 `[00:12.50]` 等时间标签；切换为 `.lrc` 后保留标准时间轴。
- 翻译歌词和罗马音均为可选项。服务端没有对应内容时不会伪造内容，也不会因此阻止音频下载。
- 歌词缓存保存在运行目录的 `lyrics_cache.json`，正常歌词默认缓存 7 天、无歌词结果短期缓存；缓存不保存 Cookie、token 或请求头，并已加入 `.gitignore`。
- 本功能只接入 QQ音乐歌词，不包含全民K歌歌词。实现借鉴了 [163MusicLyrics](https://github.com/jitwxs/163MusicLyrics) 的接口/格式处理思路，但不引入其 C#/.NET 桌面程序；歌词和音频请遵守版权及 QQ音乐平台服务条款。

## 📦 打包 EXE

```bash
pip install pyinstaller
# bin/ 下需有 node.exe、ffmpeg.exe
pyinstaller --clean --noconfirm --onefile --name MusicDownloaderWeb ^
  --icon app.ico ^
  --add-data "static;static" --add-data "vendor;vendor" --add-data "bin;bin" ^
  --hidden-import paho.mqtt.client --hidden-import paho.mqtt.properties ^
  --hidden-import paho.mqtt.packettypes --hidden-import py7zr ^
  app.py
```

## 🏗️ 技术架构

```
app.py            Flask 主应用（端口 8899）
paths.py          路径解析（源码/打包双模式）
qq_api.py         QQ音乐：搜索 / 四档音质 vkey / 歌单
lyrics_api.py      QQ音乐歌词：获取、缓存、TXT/LRC 渲染
qq_login.py       QQ音乐 App 二维码登录与 MQTT 状态管理
kg_api.py         全民K歌：单曲 / 主页解析
vip_api.py        mflac 手动转换
vocal_api.py      人声分离（调用本地 vocal-separate）
ffmpeg_api.py     格式转换
vendor/
  node_bridge.js      Flask ↔ Node 桥接（歌单走签名加密通道）
  new_sign_module.js  y.qq.com 官方签名模块提取版
  um/um_decrypt.js    Unlock Music WASM 解密（mflac→flac）
static/index.html 前端（单文件，无框架）
```

- **音质原理**：vkey 接口按 `media_mid` + 音质前缀构造文件名（`F000`=SQ flac、`M800`=HQ mp3、`AI00`=母带），带 VIP Cookie 可获取无损直链
- **歌单原理**：官方网页改用带签名+加密的 `musics.fcg` 通道，签名模块从官方 JS 提取后在 Node 中运行
- **人声分离**：调用 [vocal-separate](https://github.com/jianchang512/vocal-separate)。EXE 启动时**自动隐身拉起** AI 引擎（无窗口、无浏览器弹窗）；未安装时面板提供「📥 下载AI组件」一键下载安装（749MB，仅需一次）

## ⚠️ 免责声明

- 本项目仅供**个人学习交流**使用，请勿用于商业用途
- 音乐版权归原作者及平台所有，下载内容请在授权范围内使用
- VIP 音质下载需要你自己的有效会员账号，本工具不绕过任何付费授权
- `vendor/` 内含第三方组件（Unlock Music 等），其版权归原项目所有

## 🙏 致谢

- [Li4n0/Music-Downloader](https://github.com/Li4n0/Music-Downloader) — 原始项目灵感
- [T-K-233/KG-Downloader](https://github.com/T-K-233/KG-Downloader) — K歌解析参考
- [jianchang512/vocal-separate](https://github.com/jianchang512/vocal-separate) — 人声分离
- [Unlock Music](https://git.unlock-music.dev/um/web) — mflac 解密
