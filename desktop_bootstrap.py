"""Windowed entry point; even import/bootstrap failures have a native explanation."""
import ctypes
import importlib
import logging
import os
from pathlib import Path
import sys
import tempfile


def mode(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    argv = sys.argv[1:] if argv is None else argv
    if environ.get('MUSIC_DOWNLOADER_NO_BROWSER') == '1':
        return 'headless'
    return 'browser' if '--browser' in argv else 'desktop'


def report_failure(kind):
    text = ('音乐下载器启动失败（%s）。\n未记录账号或异常正文。\n'
            '桌面模式需要 Microsoft Edge WebView2 和 .NET Framework 4.6.2 或更新版本。\n'
            '可重新运行 MusicDownloaderWeb-v1.3.0-desktop.exe --browser 使用浏览器兼容模式。\n'
            'WebView2 官方安装页：https://developer.microsoft.com/microsoft-edge/webview2/' % kind)
    target = Path(tempfile.gettempdir()) / 'MusicDownloaderWeb-startup-error.txt'
    try:
        target.write_text(text, encoding='utf-8')
        text += '\n诊断说明：' + str(target)
    except OSError:
        pass
    if os.name == 'nt':
        ctypes.windll.user32.MessageBoxW(None, text, '音乐下载器 — 启动失败', 0x10)
    return text


def main(argv=None):
    selected = mode(argv)
    owned = None
    try:
        # Third-party exception logging may contain URLs or HTTP bodies.
        logging.getLogger('pywebview').disabled = True
        owned = importlib.import_module('owned_processes')
        owned.initialize()
        application = importlib.import_module('app').app
        startup = importlib.import_module('startup')
        if selected == 'desktop':
            presenter = importlib.import_module('desktop').run
            result = startup.serve(application, presenter=presenter)
        else:
            result = startup.serve(application)
        if result and selected != 'headless':
            report_failure('StartupCheckFailed')
        return result
    except (Exception, SystemExit) as exc:
        kind = ''.join(c for c in type(exc).__name__ if c.isalnum() or c == '_')[:60]
        if selected != 'headless':
            report_failure(kind)
        return 1
    finally:
        vocal = sys.modules.get('vocal_api')
        try:
            if vocal is not None:
                vocal.shutdown()
        finally:
            if owned is not None:
                owned.close()


if __name__ == '__main__':
    raise SystemExit(main())
