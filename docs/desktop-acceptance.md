# 1.3.1-desktop 本地验收结果

构建与验收时间：2026-10-20（开发机本地时间）。平台：开发机 Windows x64，Python 3.14.5，已安装并注册 WebView2 Runtime。未在另一台干净电脑上测试。发布入口：[v1.3.1-desktop](https://github.com/MeowAndy/Music-Downloader-Web/releases/tag/v1.3.1-desktop)；保留旧版 v1.2.0，不覆盖其发布附件。

## 最终成品

- 构建：`release-artifacts/v1.3.1-desktop-20261020-002319/dist/MusicDownloaderWeb-v1.3.1-desktop.exe`
- GitHub 发布分发目录：`release-artifacts/github-v1.3.1-desktop-20261020-publish/`。沿用已验收且哈希相同的干净 EXE，更新随包发布说明后重生成 ZIP；原构建中间目录不作为分发依赖。
- EXE：114977735 bytes。
- EXE SHA-256：`4b3498d92739b0f08735d9d23df6ebb447f57a4d571c2e18871311a56be349b4`
- 发布 ZIP：114230204 bytes。
- 发布 ZIP SHA-256：`a16e0434ff404efa401463dd311dcbaf83e19bc183d1d30f6e85a9cac39b920b`
- 构建检查：Windows GUI PE 子系统；包含应用图标、WebView2 DLL、Loader、Python.Runtime；2708 条打包资源；无账号／缓存／output 等私有运行数据。
- ZIP 使用文件白名单，仅包含原始 EXE、使用说明与许可证；已验证条目和 ZIP 完整性。

## 本次界面改动

- 页面加载时显示现有应用图标的淡入、轻微缩放、光晕、旋转环与进度提示，完成后平滑移除。动画在单文件解包和服务自检之后出现，不覆盖最早的解包等待。
- 最短展示约 1050ms，初始请求等待上限约 2400ms，整体 JavaScript 兜底约 4200ms，另有 CSS 超时退场，避免初始化异常一直挡住操作。
- 系统启用 `prefers-reduced-motion` 时减少动画，取消最短展示等待。
- 宽屏按工作区实际顶部位置计算可用高度，细圆角滚动条和稳定槽位减少抖动；中小屏、较矮窗口改用页面整体滚动。
- QQ 启动只刷新登录状态，不再自动打开登录卡；用户点击登录按钮后仍可扫码。

## 已通过

- Python 全量 413 测试。
- 前端 Node VM 1443 条断言，包含严格未处理 Promise 拒绝模式；覆盖启动层、失败／超时解除、减少动画、布局测量、滚动位置稳定和登录状态初始化，保留原有业务回归。
- 固定 `/app.ico` 路由 6 项测试，覆盖图标字节、MIME、缺失资源、额外路径、路径穿越和 query 参数隔离。
- 真实源码 WebView2：图标存在且已加载，启动层最终移除；四面板、同源 fetch、外链和主窗口外部导航拦截、消息桥和宿主对象关闭、真实“另存为”和“打开”文件对话框弹出及取消、关闭释放端口。记录：`portable-test-desktop-source-zoyddc3o/desktop-probe.json`。
- 默认窗口内容区 1484×881：四面板均在首屏内，文档高度 881；调整到内容区 984×761、764×541 和 1484×881 后均无横向溢出。小窗口文档自然增长并由页面滚动。
- 最终 EXE 在新建中文空格目录、仅 System32 PATH、无账号环境中连续启动并关闭三次：独立窗口、无控制台、实际 WebView2 子进程、8899 占用回退、实例身份／隔离目录／匿名账号验证、正常关闭返回 0、全部后代退出、端口释放。记录：`portable-test-desktop-中文 空目录-j4x6rglc/desktop-acceptance.json`。
- 最终 EXE 业务验收 28/28 项通过：启动和资源、账号隔离、FFmpeg 真实转换及完整解码、Node 离线依赖与 QR PNG 生成、NCM 工具固定哈希、网易云搜索 30 条、咪咕搜索 20 条、HiFiTi 搜索 24 条、QQ 歌单 66 条，以及三个平台官方二维码创建／等待／取消。记录：`portable-test-1.3.1-desktop-中文 空目录-luddcqo4/acceptance-summary.json`。
- 最终内嵌 HTML 与当前源码逐字节完全相同。
- Python 语法检查和 `git diff --check` 通过。

## 验收中修正的问题

首轮业务验收的 `exact-current-frontend` 失败。检查证实 EXE 内嵌 HTML 与源码原始字节完全一致，原因是脚本用 `read_text()` 读取源码时规范化 Windows 换行，而 HTTP 内容保留原换行。已将该项改为 HTTP 原始字节与源码 `read_bytes()` 比较，重跑后 28/28 通过；未以忽略失败或重打包旧资源规避问题。

## 未覆盖与边界

本次没有真实手机扫码确认、账号密码登录、注册账号、购买／积分操作、全新 AI 下载／安装或目标歌曲权益下载验证。NCM 本轮只验证工具固定哈希，未重复执行个人授权样本转换。真实保存／上传对话框验证了弹出与取消，未选择任意个人文件。缺失运行时流程由模拟测试覆盖，未卸载本机运行时。

截图已采集，但当前视觉输入不可用，因此不将截图采集称为人工视觉审查或审美验收；布局结论来自真实 WebView2 几何测量与 DOM 检查。开发机验收不等于另一台缺少运行时的干净电脑已验证，也不保证第三方平台网络与账号权益持续可用。

使用方式、运行时要求及兼容模式见 `desktop-windows.md`。此更改不包含安卓 APK。
