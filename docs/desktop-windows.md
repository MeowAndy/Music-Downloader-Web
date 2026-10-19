# Windows 桌面窗口版

当前版本：`1.3.1-desktop`。本版增加图标启动动画和首屏滚动优化；下载见 [v1.3.1-desktop Release](https://github.com/MeowAndy/Music-Downloader-Web/releases/tag/v1.3.1-desktop)。保留旧版 v1.2.0，不覆盖其发布附件。

## 使用

双击 `MusicDownloaderWeb-v1.3.1-desktop.exe`，等待解包和本机服务自检，然后进入独立的音乐下载器窗口。默认不打开浏览器或控制台；原有四面板、平台账号、下载、歌词、转换及可选 AI 功能保持原来的网页业务实现。

界面使用 pywebview 6.2.1 和 Microsoft Edge WebView2，不使用旧 IE 内核。Windows 需要已安装的 WebView2 Runtime、.NET Framework 4.6.2 或更新版本。官方运行时入口：<https://developer.microsoft.com/microsoft-edge/webview2/>。缺失或初始化失败时显示原生提示，不自动下载安装运行时。无需安装 Python、Node 或 FFmpeg。

窗口有系统标题栏、图标、最小化、最大化和关闭按钮。歌曲仍保存在 EXE 旁的 `output`；目录不可写时按原有规则回退。文件列表的“保存”使用原生另存为，上传使用系统文件选择器。官网、网盘和 GitHub 外链由系统浏览器打开，不会占用主界面。

关闭窗口会停止本实例的 HTTP 服务并清理本程序启动的子进程。正在进行的转换、下载和 AI 任务会被中断；请先等待任务结束。已存在、并非由本程序启动的 AI 服务不会被接管或关闭。

## 启动与滚动

页面就绪前显示现有应用图标的淡入、光晕和进度动画，随后平滑退出。初始化失败或超时也会解除启动层，不会一直挡住操作。动画出现在单文件解包与服务自检之后，不代表 EXE 双击后立即显示。系统启用“减少动态效果”（`prefers-reduced-motion`）时，关闭旋转、缩放和光晕动画，并取消最短展示等待。

宽屏按工作区实际位置计算面板可用高度，尽量将四栏保留在首屏；列表较长时使用细圆角滚动条。两栏、单栏或较矮窗口改为页面整体滚动，避免多重内部滚动和内容裁切。登录卡由用户主动打开，不再在启动后自动弹出并挤压工作区。

## 兼容与诊断

- 浏览器兼容模式：`MusicDownloaderWeb-v1.3.1-desktop.exe --browser`。可在 Windows 快捷方式的目标路径后添加该参数。此模式没有控制台，请通过任务管理器结束实例，或使用默认桌面模式正常关闭。
- 无界面自动验收：环境变量 `MUSIC_DOWNLOADER_NO_BROWSER=1` 优先于所有窗口／浏览器模式，不导入 GUI、不弹出诊断对话框。
- 数据目录下的 `startup-report.json` 记录受控诊断和当前实例内部地址；早期导入失败的通用说明写入系统临时目录 `MusicDownloaderWeb-startup-error.txt`。
- 仍使用仅绑定 `127.0.0.1` 的内部 HTTP 服务。默认尝试 8899，占用则使用系统分配的端口；窗口没有地址栏，不会向局域网开放。
- 没有为 JavaScript 暴露文件、命令或账号桥接，WebView2 消息桥与宿主对象功能关闭。外部主窗口导航会被阻止；外链仅接受无账号密码的 HTTP(S) URL。
- WebView 使用私有模式，不复用用户的 Edge 浏览器资料；现有平台账号存储策略不变，HiFiTi／咪咕仍为内存会话。

## 实现与测试入口

- 源码桌面启动：`python desktop_bootstrap.py`。
- 窗口层：`desktop.py`；启动自检和 HTTP 生命周期复用 `startup.serve(app, presenter=...)`。
- 子进程：`owned_processes.py`，Windows Job Object 在子进程恢复执行前完成分配，关闭 Job 清理其后代；不会按进程名或端口杀进程。
- 构建：`python packaging/build_release.py`。使用无控制台 PE 子系统并检查 WebView2／CLR 资源及私密文件排除。
- 原生源码交互验收：`python packaging/probe_desktop.py`。隔离数据目录，验证四面板、同源请求、外链拦截、桥接关闭、真实另存为／上传对话框及关闭释放端口。
- 成品窗口验收：`python packaging/accept_desktop.py <exe>`。复制到新的中文空格目录，缩减 PATH，验证三次真实窗口创建和关闭、端口冲突回退、匿名状态、进程树退出和端口释放。
- 成品业务验收：`python packaging/accept_windows.py <exe> --online`。测试独立目录后端、工具、搜索及官方二维码创建／等待／取消，不执行真实扫码确认。

这不是 Electron，也没有重写为原生控件；它是带系统窗口的 WebView2 桌面应用。开发机验收不等于另一台缺少运行时的干净电脑已验证。当前测试不代表所有歌曲权益、真实手机扫码确认、真实账号登录、全新 AI 安装或所有用户剪贴板策略均已验证。界面截图未进行人工视觉审查。
