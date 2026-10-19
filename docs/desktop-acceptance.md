# 1.3.0-desktop 本地验收结果

日期：2026-10-19。平台：开发机 Windows x64，Python 3.14.5，已安装并注册 WebView2 Runtime。未在另一台干净电脑上测试，未发布 GitHub／Release。

## 最终成品

- 构建：`release-artifacts/v1.3.0-desktop-20261019-233305/dist/MusicDownloaderWeb-v1.3.0-desktop.exe`
- 干净分发目录：`release-artifacts/MusicDownloaderWeb-v1.3.0-desktop-Windows-x64/`
- EXE：114947868 bytes。
- EXE SHA-256：`6ce137d8d89079db1f032bae48cbb8186de79a3771bf8790e36edc575717456d`
- ZIP：114208974 bytes。
- ZIP SHA-256：`9df151acec1310e66039f7d75d17f84ecc241e6e657c612db62295a5c1f5bed7`
- 构建检查：Windows GUI PE 子系统；包含独立图标、WebView2 DLL、Loader、Python.Runtime；2708 条打包资源；无账号／缓存／output 等私有运行数据。

## 已通过

- Python 全量 407 测试。
- 前端 Node VM 1292 条断言。
- 真实源码 WebView2 窗口：四面板、同源 fetch、外链和主窗口外部导航拦截、关闭消息桥和宿主对象、真实“另存为”和“打开”文件对话框弹出及取消、关闭释放端口。记录：`portable-test-desktop-source-b1ucogk3/desktop-probe.json`。
- 最终 EXE 在新建中文空格目录、仅 System32 PATH、无账号环境中连续启动并关闭三次：独立可见窗口、无控制台、实际 WebView2 子进程、8899 占用回退、实例身份／隔离目录／匿名账号验证、正常关闭返回 0、全部后代退出、端口释放。记录：`portable-test-desktop-中文 空目录-wdcxe05x/desktop-acceptance.json`。
- Windows 原生 Job 测试验证：根进程及孙进程清理、外部无关进程存活、宿主退出时清理、关闭后拒绝创建新进程。
- 最终 EXE 业务验收 29 项中 28 项通过：启动和资源、账号隔离、FFmpeg 真实转换和完整解码、Node 依赖、NCM 固定哈希及授权样本副本真实转换／完整解码／原件不变、网易云／咪咕搜索、QQ 歌单、三个平台官方二维码创建／等待／取消。

## 未通过／未覆盖

最终业务验收 HiFiTi 搜索返回 HTTP 403，0 结果，记为失败；前一轮桌面构建的同项为 HTTP 200、24 结果。没有绕过官网挑战、连续重试或替换资源，不将官网访问失败解释为桌面测试全部通过。最终记录目录：`portable-test-1.3.0-desktop-中文 空目录-15s7pnzl`。该测试脚本的退出码仅由必需离线项决定，因此不能用退出码 0 宣称在线项全部通过。

本次没有真实手机扫码确认、账号密码登录、注册账号、购买／积分操作、全新 AI 下载／安装或目标歌曲权益下载验证。剪贴板 API 存在及前端降级逻辑有验证，不代表所有系统剪贴板策略。真实保存／上传对话框验证了弹出与取消，未模拟用户选择任意个人文件。缺失运行时流程通过模拟测试，未卸载本机运行时。界面截图未进行人工视觉审查。

使用方式、运行时要求及兼容模式见 `desktop-windows.md`。此更改保留已发布 v1.2.0，不包含安卓 APK。
