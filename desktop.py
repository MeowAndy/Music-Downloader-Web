"""WebView2 shell. No application API is exposed to JavaScript."""
import threading
import webbrowser
from urllib.parse import urlsplit

RUNTIME_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/'


def valid_external_url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme in ('http', 'https') and bool(parts.hostname)
                and parts.username is None and parts.password is None
                and parts.port != 0)
    except ValueError:
        return False


def local_navigation(value, base):
    if not valid_external_url(value):
        return False
    return urlsplit(value)[:2] == urlsplit(base)[:2]


def runtime_available():
    import winreg
    key = r'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(root, key, 0, winreg.KEY_READ | view) as handle:
                    version = winreg.QueryValueEx(handle, 'pv')[0]
                    if int(str(version).split('.')[0]) >= 86:
                        return True
            except (OSError, ValueError, TypeError):
                pass
    return False


def run(url):
    if not runtime_available():
        raise RuntimeError('WebView2 运行时不可用')
    import webview
    from paths import RES_DIR

    # This adapter deliberately targets the pinned Windows backend; do not fall back to MSHTML.
    webview.settings.update(ALLOW_DOWNLOADS=True, ALLOW_FILE_URLS=False,
                            OPEN_EXTERNAL_LINKS_IN_BROWSER=False,
                            IGNORE_SSL_ERRORS=False)
    from webview.guilib import initialize
    backend = initialize('edgechromium')
    if backend.renderer != 'edgechromium':
        raise RuntimeError('WebView2 运行时不可用')
    window = webview.create_window('音乐下载器', url, width=1500, height=920,
                                   min_size=(760, 560), background_color='#10121a')
    loaded = threading.Event()
    closed = threading.Event()
    failures = []

    def open_external(value):
        if valid_external_url(value) and not local_navigation(value, url):
            try:
                webbrowser.open(value)
            except Exception:
                pass

    def before_show():
        native = window.native
        browser = native.browser
        control = native.webview

        def navigation(sender, args):
            target = str(args.Uri)
            if not local_navigation(target, url):
                args.Cancel = True
                open_external(target)

        def new_window(sender, args):
            args.Handled = True
            open_external(str(args.Uri))

        def initialized(sender, args):
            if not args.IsSuccess:
                failures.append('WebView2 初始化失败')
                window.destroy()
                return
            core = control.CoreWebView2
            core.NewWindowRequested -= browser.on_new_window_request
            core.NewWindowRequested += new_window
            # Keep downloads/file inputs/clipboard web-native, but disable pywebview's message bridge.
            core.Settings.IsWebMessageEnabled = False
            core.Settings.AreHostObjectsAllowed = False

        control.NavigationStarting += navigation
        control.CoreWebView2InitializationCompleted += initialized

    def watchdog():
        import time
        deadline = time.monotonic() + 35
        while not loaded.is_set() and not closed.wait(.1):
            if time.monotonic() >= deadline:
                failures.append('桌面页面未能在限定时间内就绪')
                window.destroy()
                return

    window.events.before_show += before_show
    window.events.loaded += lambda: loaded.set()
    window.events.closed += lambda: closed.set()
    webview.start(watchdog, gui='edgechromium', debug=False,
                  private_mode=True, icon=str(RES_DIR / 'app.ico'))
    closed.set()
    if failures:
        raise RuntimeError(failures[0])
