#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI-Tray：Windows 系统托盘里的 AI 快捷启动器。

功能
----
* 左键单击托盘图标：用 Chromium 内核浏览器的“应用窗口”（PWA 风格）打开默认 AI 网址。
* 右键单击托盘图标：弹出菜单，选择任意已配置的 AI，或退出程序。
* 菜单项、默认站点、浏览器、图标颜色全部来自同目录下的 config.json。

只支持 Windows。依赖：pystray、Pillow。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import logging.handlers
import os
import platform
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError as exc:  # 延迟到 main() 里给出友好提示
    pystray = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None

__version__ = "1.0.0"

APP_NAME = "AI-Tray"
APP_TITLE = "AI 快捷启动器"
DEFAULT_ACCENT = "#4F46E5"          # 靛蓝
MUTEX_NAME = "AITray.SingleInstance.v1"
ERROR_ALREADY_EXISTS = 183
OPEN_DEBOUNCE_SECONDS = 1.0

logger = logging.getLogger("ai_tray")

# 单个实例的互斥体句柄，必须保持引用直到进程退出
_mutex_handle: int | None = None


def _patch_pystray_compat() -> None:
    """兼容受限令牌/安全软件环境。

    pystray 创建托盘窗口时会调用 ChangeWindowMessageFilterEx；该调用只用于
    Explorer 重启后恢复图标，在某些受限令牌下会返回 WinError 5。pystray 没有
    捕获这个异常，会导致托盘完全启动不了，因此这里包一层，失败时只记日志。
    """
    try:
        from pystray._util import win32 as pystray_win32

        original = pystray_win32.ChangeWindowMessageFilterEx
        if getattr(original, "_ai_tray_compat", False):
            return

        def safe_change_window_message_filter_ex(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except OSError:
                logger.warning(
                    "ChangeWindowMessageFilterEx 调用失败，已忽略（不影响托盘启动）",
                    exc_info=True,
                )
                return False

        safe_change_window_message_filter_ex._ai_tray_compat = True  # type: ignore[attr-defined]
        pystray_win32.ChangeWindowMessageFilterEx = safe_change_window_message_filter_ex
    except Exception:
        logger.debug("安装 pystray 兼容补丁失败", exc_info=True)


if pystray is not None:
    _patch_pystray_compat()

# ---------------------------------------------------------------------------
# 默认配置 & 数据结构
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "_help": (
        "sites 里的每一项都会成为右键菜单中的一项；default 填某一项的 id，"
        "决定左键单击打开哪个；browser 可填 auto、chrome、edge、brave、vivaldi、"
        "chromium，或浏览器 exe 的完整路径；pwa 为 true 时用 --app 独立窗口打开。"
    ),
    "title": APP_TITLE,
    "default": "deepseek",
    "browser": "auto",
    "pwa": True,
    "extra_args": [],
    "icon": None,
    "accent": DEFAULT_ACCENT,
    "sites": [
        {"id": "deepseek", "name": "DeepSeek", "url": "https://chat.deepseek.com/"},
        {"id": "chatgpt", "name": "ChatGPT", "url": "https://chatgpt.com/"},
        {"id": "claude", "name": "Claude", "url": "https://claude.ai/"},
        {"id": "doubao", "name": "豆包", "url": "https://www.doubao.com/chat/"},
        {"id": "kimi", "name": "Kimi", "url": "https://kimi.moonshot.cn/"},
    ],
}


class ConfigError(Exception):
    """config.json 无法读取或内容不合法。"""


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class Config:
    path: Path
    title: str
    default_id: str
    browser_pref: str
    pwa: bool
    extra_args: tuple[str, ...]
    icon: Path | None
    accent: str
    sites: tuple[Site, ...]
    warnings: tuple[str, ...] = ()

    @property
    def default_site(self) -> Site:
        for site in self.sites:
            if site.id == self.default_id:
                return site
        return self.sites[0]


@dataclass(frozen=True)
class Browser:
    key: str
    name: str
    path: Path


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def app_dir() -> Path:
    """程序所在目录：打包后是 exe 所在目录，源码运行时是 .py 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def log_file_path() -> Path:
    override = os.environ.get("AI_TRAY_LOG")
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_NAME / "ai-tray.log"


def configure_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    try:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError:
        # 日志文件写不了不应该阻止程序运行
        root.addHandler(logging.NullHandler())


def _emit(text: str) -> None:
    """有控制台时打印，--windowed 打包后 stdout 为 None 也不会报错。"""
    try:
        print(text, flush=True)
    except Exception:
        pass


def _msgbox(text: str, title: str = APP_NAME, flags: int = 0x40) -> None:
    """Windows 消息框；失败时静默。flags: 0x10=错误, 0x40=信息。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags | 0x10000)
    except Exception:
        pass


def _set_dpi_awareness() -> None:
    """让右键菜单在高 DPI 屏幕上保持清晰。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def acquire_single_instance() -> bool:
    """成功获取互斥体返回 True；已有实例在运行返回 False。"""
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        )
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            logger.warning("CreateMutexW 失败，跳过单实例检查")
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception:
        logger.exception("单实例检查失败，继续启动")
        return True


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------


def resolve_config_path(explicit: str | None) -> Path:
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser().resolve()
    env = os.environ.get("AI_TRAY_CONFIG")
    if env:
        return Path(os.path.expandvars(env)).expanduser().resolve()
    return app_dir() / "config.json"


def write_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def normalize_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("网址为空")
    url = value.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("只支持 http / https 网址")
    if not parsed.netloc:
        raise ValueError("缺少域名")
    return url


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def parse_config(raw: Any, path: Path) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("配置文件的最外层必须是一个 JSON 对象（{ ... }）。")

    warnings: list[str] = []

    def warn(message: str) -> None:
        warnings.append(message)
        logger.warning(message)

    title_raw = raw.get("title", APP_TITLE)
    if isinstance(title_raw, str) and title_raw.strip():
        title = title_raw.strip()
    else:
        warn("title 必须是非空字符串，已使用默认标题。")
        title = APP_TITLE

    browser_raw = raw.get("browser", "auto")
    if isinstance(browser_raw, str) and browser_raw.strip():
        browser_pref = browser_raw.strip()
    else:
        warn("browser 必须是字符串，已使用 auto。")
        browser_pref = "auto"

    pwa = raw.get("pwa", True)
    if not isinstance(pwa, bool):
        warn("pwa 必须是 true 或 false，已按 true 处理。")
        pwa = True

    extra_raw = raw.get("extra_args", [])
    if extra_raw is None:
        extra_raw = []
    if not isinstance(extra_raw, list):
        warn("extra_args 必须是数组，已忽略。")
        extra_raw = []
    extra_args = tuple(
        item for item in extra_raw if isinstance(item, str) and item.strip()
    )

    icon: Path | None = None
    icon_raw = raw.get("icon")
    if isinstance(icon_raw, str) and icon_raw.strip():
        candidate = Path(os.path.expandvars(icon_raw.strip())).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        candidate = candidate.resolve()
        if candidate.is_file():
            icon = candidate
        else:
            warn(f"icon 指向的文件不存在：{candidate}，将使用自动生成的图标。")
    elif icon_raw not in (None, ""):
        warn("icon 必须是图片路径或 null。")

    accent = raw.get("accent", DEFAULT_ACCENT)
    if not isinstance(accent, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        warn("accent 必须是 #RRGGBB 格式，已使用默认颜色。")
        accent = DEFAULT_ACCENT

    raw_sites = raw.get("sites")
    if not isinstance(raw_sites, list) or not raw_sites:
        raise ConfigError(
            "sites 必须是非空数组，例如：\n"
            '[{"id": "deepseek", "name": "DeepSeek", "url": "https://chat.deepseek.com/"}]'
        )

    sites: list[Site] = []
    used_ids: set[str] = set()
    for index, entry in enumerate(raw_sites, start=1):
        if isinstance(entry, str):
            entry = {"name": entry, "url": entry}
        if not isinstance(entry, dict):
            warn(f"第 {index} 个站点不是对象，已跳过。")
            continue

        try:
            url = normalize_url(entry.get("url"))
        except ValueError as exc:
            warn(f"第 {index} 个站点的网址无效（{exc}），已跳过。")
            continue

        name_raw = entry.get("name")
        name = str(name_raw).strip() if name_raw is not None else ""
        if not name:
            name = urlparse(url).netloc
            warn(f"第 {index} 个站点缺少 name，已用域名“{name}”代替。")

        id_raw = entry.get("id")
        site_id = str(id_raw).strip() if id_raw is not None else ""
        if not site_id:
            site_id = _slugify(name) or f"site{index}"
        if site_id in used_ids:
            base = site_id
            counter = 2
            while f"{base}-{counter}" in used_ids:
                counter += 1
            site_id = f"{base}-{counter}"
            warn(f"站点 id“{base}”重复，已改为“{site_id}”。")
        used_ids.add(site_id)
        sites.append(Site(site_id, name, url))

    if not sites:
        raise ConfigError("sites 里没有任何有效站点，请检查 name 和 url。")

    default_raw = raw.get("default")
    if default_raw is None:
        default_id = sites[0].id
    else:
        default_id = str(default_raw).strip()
        if default_id not in used_ids:
            match = next(
                (s.id for s in sites if s.id.casefold() == default_id.casefold()),
                None,
            )
            if match is not None:
                default_id = match
            else:
                warn(
                    f"default 指定的站点“{default_id}”不存在，"
                    f"左键将打开 {sites[0].name}。"
                )
                default_id = sites[0].id

    return Config(
        path=path,
        title=title,
        default_id=default_id,
        browser_pref=browser_pref,
        pwa=pwa,
        extra_args=extra_args,
        icon=icon,
        accent=accent,
        sites=tuple(sites),
        warnings=tuple(warnings),
    )


def load_config(path: Path, *, create: bool = True) -> Config:
    if not path.exists():
        if not create:
            raise ConfigError(f"找不到配置文件：{path}")
        try:
            write_default_config(path)
            logger.info("已生成默认配置文件：%s", path)
        except OSError as exc:
            raise ConfigError(
                f"无法创建配置文件：{path}\n{exc}\n\n"
                "如果程序放在只读目录（例如 Program Files），请把程序移到普通文件夹。"
            ) from exc

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{path}\n{exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"配置文件不是 UTF-8 编码：{path}\n{exc}") from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件不是合法的 JSON：\n第 {exc.lineno} 行，第 {exc.colno} 列附近\n"
            f"{exc.msg}"
        ) from exc

    return parse_config(raw, path)


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------


def _hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def make_icon(accent: str = DEFAULT_ACCENT, size: int = 256) -> "Image.Image":
    """用 Pillow 画一个圆角渐变底 + 白色对话气泡的托盘图标。"""
    if Image is None or ImageDraw is None:
        raise RuntimeError("缺少 Pillow，无法生成图标")

    scale = 4
    s = size * scale
    top = _hex_rgb(accent)
    bottom = tuple(max(0, int(round(c * 0.62))) for c in top)
    lum = _luminance(top)

    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # 竖向渐变
    for y in range(s):
        t = y / max(1, s - 1)
        color = tuple(
            int(round(top[i] + (bottom[i] - top[i]) * t)) for i in range(3)
        )
        draw.line([(0, y), (s, y)], fill=color + (255,))

    # 圆角裁切
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=255
    )
    canvas.putalpha(mask)

    # 对话气泡：深色底用白气泡，浅色底用深气泡
    if lum < 0.72:
        bubble = (255, 255, 255, 255)
        dot = top + (255,)
    else:
        bubble = (17, 24, 39, 255)
        dot = (255, 255, 255, 255)

    left, top_y = int(s * 0.17), int(s * 0.21)
    right, bottom_y = int(s * 0.83), int(s * 0.63)
    radius = int(s * 0.13)
    draw.rounded_rectangle(
        (left, top_y, right, bottom_y), radius=radius, fill=bubble
    )
    # 左下角的小尾巴
    draw.polygon(
        [
            (int(s * 0.30), bottom_y - int(s * 0.015)),
            (int(s * 0.30), int(s * 0.77)),
            (int(s * 0.46), bottom_y - int(s * 0.015)),
        ],
        fill=bubble,
    )

    # 气泡里的三个点
    dot_radius = int(s * 0.048)
    dot_y = int(s * 0.42)
    for x_ratio in (0.36, 0.50, 0.64):
        cx = int(s * x_ratio)
        draw.ellipse(
            (cx - dot_radius, dot_y - dot_radius, cx + dot_radius, dot_y + dot_radius),
            fill=dot,
        )

    resample = getattr(Image, "Resampling", Image).LANCZOS
    return canvas.resize((size, size), resample)


def load_icon(config: Config) -> "Image.Image":
    if config.icon is not None:
        try:
            with Image.open(config.icon) as image:
                return image.convert("RGBA").copy()
        except Exception:
            logger.exception("无法加载自定义图标 %s，改用自动生成的图标", config.icon)
    return make_icon(config.accent)


def save_icon_file(path: Path) -> None:
    image = make_icon(DEFAULT_ACCENT)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".ico":
        image.save(
            path,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    else:
        image.save(path)


# ---------------------------------------------------------------------------
# Chromium 浏览器探测与 PWA 启动
# ---------------------------------------------------------------------------

# key, 显示名, 进程名, 常见安装路径（支持环境变量）
BROWSER_SPECS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "chrome",
        "Google Chrome",
        "chrome.exe",
        (
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    (
        "edge",
        "Microsoft Edge",
        "msedge.exe",
        (
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    (
        "brave",
        "Brave",
        "brave.exe",
        (
            r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
        ),
    ),
    (
        "vivaldi",
        "Vivaldi",
        "vivaldi.exe",
        (
            r"%ProgramFiles%\Vivaldi\Application\vivaldi.exe",
            r"%LocalAppData%\Vivaldi\Application\vivaldi.exe",
        ),
    ),
    (
        "chromium",
        "Chromium",
        "chrome.exe",
        (
            r"%LocalAppData%\Chromium\Application\chrome.exe",
            r"%ProgramFiles%\Chromium\Application\chrome.exe",
        ),
    ),
)

_BROWSER_BY_KEY = {spec[0]: spec for spec in BROWSER_SPECS}

_DEFAULT_BROWSER_PROGIDS = {
    "chromehtml": "chrome",
    "msedgehtm": "edge",
    "bravehtml": "brave",
    "vivaldihtml": "vivaldi",
}


def _reg_app_path(exe_name: str) -> Path | None:
    """从注册表 App Paths 里找浏览器 exe（Chrome/Edge 安装器都会写）。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    sub_key = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
    lookups = [
        (winreg.HKEY_CURRENT_USER, sub_key),
        (winreg.HKEY_LOCAL_MACHINE, sub_key),
        (
            winreg.HKEY_LOCAL_MACHINE,
            rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
        ),
    ]
    for hive, key_name in lookups:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value.strip().strip('"'))
        if path.is_file():
            return path
    return None


def _system_default_browser_key() -> str | None:
    """读取 Windows 默认浏览器的 ProgId；只关心 Chromium 系。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key_name = (
            r"Software\Microsoft\Windows\Shell\Associations"
            r"\UrlAssociations\http\UserChoice"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        return None
    if not isinstance(prog_id, str):
        return None
    return _DEFAULT_BROWSER_PROGIDS.get(prog_id.strip().lower())


def _find_browser_path(key: str) -> Path | None:
    spec = _BROWSER_BY_KEY.get(key)
    if spec is None:
        return None
    _, _, exe_name, common_paths = spec

    # chromium 的进程名也是 chrome.exe，不能查 Chrome 的注册表项
    if key != "chromium":
        path = _reg_app_path(exe_name)
        if path is not None:
            return path

    for pattern in common_paths:
        path = Path(os.path.expandvars(pattern))
        if path.is_file():
            return path
    return None


def detect_browser(preference: str) -> Browser | None:
    """按配置寻找 Chromium 内核浏览器。

    auto：优先系统默认浏览器（仅当它是 Chromium 系），然后 Chrome、Edge、
    Brave、Vivaldi、Chromium。也可以直接指定 key 或 exe 完整路径。
    """
    pref = (preference or "auto").strip()
    if pref.lower() not in ("auto", ""):
        candidate = Path(os.path.expandvars(pref)).expanduser()
        if candidate.is_file() and candidate.suffix.lower() == ".exe":
            return Browser("custom", candidate.stem, candidate)

        key = pref.lower()
        if key not in _BROWSER_BY_KEY:
            logger.warning("未知的 browser 配置“%s”，回退到 auto。", pref)
        else:
            path = _find_browser_path(key)
            if path is not None:
                name = _BROWSER_BY_KEY[key][1]
                return Browser(key, name, path)
            logger.warning("配置指定的 %s 没有找到，回退到 auto。", key)

    order: list[str] = []
    default_key = _system_default_browser_key()
    if default_key is not None:
        order.append(default_key)
    for spec in BROWSER_SPECS:
        if spec[0] not in order:
            order.append(spec[0])

    for key in order:
        path = _find_browser_path(key)
        if path is not None:
            name = _BROWSER_BY_KEY[key][1]
            if default_key == key:
                name += "（系统默认）"
            return Browser(key, name, path)
    return None


def build_browser_command(
    browser: Browser,
    url: str,
    pwa: bool,
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """Chromium 的 --app=URL 会打开无地址栏的独立窗口，即 PWA 风格。"""
    args = [str(browser.path), *extra_args]
    args.append(f"--app={url}" if pwa else url)
    return args


def _creation_flags() -> int:
    if sys.platform != "win32":
        return 0
    return subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP


class ChromiumController:
    """注册到 webbrowser 模块里的自定义控制器。"""

    REGISTERED_NAME = "ai-tray-chromium"

    def __init__(
        self,
        browser: Browser,
        pwa: bool,
        extra_args: tuple[str, ...] = (),
        *,
        dry_run: bool = False,
    ) -> None:
        self.browser = browser
        self.pwa = pwa
        self.extra_args = extra_args
        self.dry_run = dry_run
        self.last_command: list[str] | None = None

    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        command = build_browser_command(
            self.browser, url, self.pwa, self.extra_args
        )
        self.last_command = command
        pretty = subprocess.list2cmdline(command)

        if self.dry_run:
            logger.info("dry-run: %s", pretty)
            _emit(f"[DRY-RUN] {pretty}")
            return True

        logger.info("启动浏览器: %s", pretty)
        try:
            subprocess.Popen(
                command,
                close_fds=True,
                creationflags=_creation_flags(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            logger.exception("启动浏览器失败: %s", pretty)
            return False
        return True

    def open_new(self, url: str) -> bool:
        return self.open(url, 1)

    def open_new_tab(self, url: str) -> bool:
        return self.open(url, 2)


# ---------------------------------------------------------------------------
# 托盘应用
# ---------------------------------------------------------------------------


class TrayApp:
    def __init__(self, config: Config, *, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.browser: Browser | None = None
        self.controller: ChromiumController | None = None

        self._lock = threading.Lock()
        self._last_open: dict[str, float] = {}

        self._configure_browser(config)
        self.icon = pystray.Icon(
            "ai_tray",
            load_icon(config),
            self._tooltip(),
            self._build_menu(),
        )

    # -- 配置与菜单 ---------------------------------------------------------

    def _configure_browser(self, config: Config) -> None:
        self.browser = detect_browser(config.browser_pref)
        if self.browser is None:
            self.controller = None
            logger.warning(
                "没有找到 Chromium 内核浏览器，将退回系统默认浏览器打开普通标签页。"
            )
            return

        self.controller = ChromiumController(
            self.browser,
            config.pwa,
            config.extra_args,
            dry_run=self.dry_run,
        )
        webbrowser.register(
            ChromiumController.REGISTERED_NAME,
            None,
            self.controller,
        )
        logger.info(
            "使用浏览器：%s (%s)，PWA=%s",
            self.browser.name,
            self.browser.path,
            config.pwa,
        )

    def _tooltip(self) -> str:
        text = f"{self.config.title} · 左键 {self.config.default_site.name} · 右键菜单"
        return text[:120]

    def _build_menu(self) -> "pystray.Menu":
        items: list[Any] = []
        for site in self.config.sites:
            items.append(
                pystray.MenuItem(
                    site.name,
                    self._site_action(site),
                    default=(site.id == self.config.default_site.id),
                )
            )

        items.extend(
            [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("打开配置文件", self._on_open_config),
                pystray.MenuItem("重新加载配置", self._on_reload),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出程序", self._on_quit),
            ]
        )
        return pystray.Menu(*items)

    def _site_action(self, site: Site):
        def action(icon, item) -> None:
            self.open_site(site)

        return action

    # -- 打开网址 -----------------------------------------------------------

    def _allow_open(self, site_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last_open.get(site_id, 0.0)
            if now - last < OPEN_DEBOUNCE_SECONDS:
                return False
            self._last_open[site_id] = now
            return True

    def open_site(self, site: Site) -> None:
        if not self._allow_open(site.id):
            logger.info("忽略重复点击：%s", site.name)
            return
        logger.info("打开 %s -> %s", site.name, site.url)
        threading.Thread(
            target=self._open_worker,
            args=(site,),
            name=f"open-{site.id}",
            daemon=True,
        ).start()

    def _open_worker(self, site: Site) -> None:
        try:
            if self.controller is not None:
                opener = webbrowser.get(ChromiumController.REGISTERED_NAME)
                ok = opener.open(site.url, new=2, autoraise=True)
            elif self.dry_run:
                logger.info("dry-run: webbrowser.open(%s)", site.url)
                _emit(f"[DRY-RUN] webbrowser.open({site.url})")
                return
            else:
                ok = webbrowser.open(site.url, new=2, autoraise=True)

            if not ok:
                raise RuntimeError("浏览器没有接受打开请求")
        except Exception as exc:
            logger.exception("打开 %s 失败", site.url)
            self._notify(f"打开 {site.name} 失败：{exc}")

    def _notify(self, message: str) -> None:
        try:
            self.icon.notify(message, self.config.title)
        except Exception:
            logger.debug("显示通知失败", exc_info=True)

    # -- 菜单动作 -----------------------------------------------------------

    def _on_open_config(self, icon, item) -> None:
        path = self.config.path
        if not path.exists():
            try:
                write_default_config(path)
            except OSError as exc:
                self._notify(f"无法创建配置文件：{exc}")
                return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError:
            try:
                subprocess.Popen(["notepad.exe", str(path)])
            except OSError as exc:
                self._notify(f"无法打开配置文件：{exc}")

    def _on_reload(self, icon, item) -> None:
        try:
            config = load_config(self.config.path)
        except ConfigError as exc:
            logger.error("重新加载配置失败：%s", exc)
            _msgbox(f"配置加载失败：\n\n{exc}", "配置有误", 0x10)
            return

        self.config = config
        self._configure_browser(config)
        self.icon.icon = load_icon(config)
        self.icon.title = self._tooltip()
        self.icon.menu = self._build_menu()
        logger.info("配置已重新加载：%s", config.path)
        self._notify("配置已重新加载")

    def _on_quit(self, icon, item) -> None:
        logger.info("收到退出请求")
        self.icon.stop()

    # -- 运行 ---------------------------------------------------------------

    def _setup(self, icon) -> None:
        try:
            icon.visible = True
        except Exception:
            logger.exception("显示托盘图标失败")
            _msgbox("无法显示托盘图标，请查看日志。", APP_NAME, 0x10)
            icon.stop()
            return
        logger.info("托盘图标已显示，等待操作")

    def run(self) -> None:
        logger.info("AI-Tray %s 启动，配置：%s", __version__, self.config.path)
        self.icon.run(setup=self._setup)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def run_check(config_path: Path) -> int:
    config = load_config(config_path)
    browser = detect_browser(config.browser_pref)

    lines = [
        f"AI-Tray {__version__}",
        f"Python {platform.python_version()} ({platform.architecture()[0]})",
        f"配置文件: {config.path}",
    ]
    if config.warnings:
        lines.append("配置警告:")
        lines.extend(f"  - {warning}" for warning in config.warnings)
    if browser is not None:
        lines.append(f"浏览器: {browser.name} — {browser.path}")
    else:
        lines.append("浏览器: 未找到 Chromium 内核浏览器（将使用系统默认浏览器）")
    lines.append(
        "PWA 模式: " + ("开启（--app 独立窗口）" if config.pwa else "关闭（普通标签页）")
    )
    lines.append(f"左键默认: {config.default_site.name} — {config.default_site.url}")
    lines.append("右键菜单:")
    for site in config.sites:
        marker = "  [默认]" if site.id == config.default_site.id else ""
        lines.append(f"  {site.name}{marker}  ->  {site.url}")
    if browser is not None:
        command = build_browser_command(
            browser, config.default_site.url, config.pwa, config.extra_args
        )
        lines.append("默认启动命令: " + subprocess.list2cmdline(command))
    lines.append(f"日志文件: {log_file_path()}")

    text = "\n".join(lines)
    logger.info("自检结果:\n%s", text)
    _emit(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Windows 托盘 AI 快捷启动器（左键默认站点，右键菜单选择）。",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="指定 config.json 路径（默认使用程序同目录下的 config.json）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做配置/浏览器自检并打印结果，不启动托盘",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要执行的浏览器命令，不真正打开浏览器",
    )
    parser.add_argument(
        "--export-icon",
        metavar="PATH",
        help="生成 icon.ico（或 .png）后退出，用于打包 exe 的图标",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.export_icon:
        try:
            target = Path(os.path.expandvars(args.export_icon)).expanduser()
            if not target.is_absolute():
                target = (Path.cwd() / target).resolve()
            save_icon_file(target)
        except Exception as exc:
            _emit(f"生成图标失败：{exc}")
            _msgbox(f"生成图标失败：\n\n{exc}", APP_NAME, 0x10)
            return 1
        _emit(f"已生成图标：{target}")
        return 0

    configure_logging()

    if _IMPORT_ERROR is not None:
        message = (
            f"缺少依赖：{_IMPORT_ERROR.name}\n\n"
            "请在命令行运行：\n"
            "    python -m pip install -r requirements.txt\n\n"
            "或者：\n"
            "    python -m pip install pystray pillow"
        )
        logger.error(message)
        _emit(message)
        _msgbox(message, "缺少依赖", 0x10)
        return 1

    if sys.platform != "win32":
        message = "AI-Tray 只支持 Windows。"
        logger.error(message)
        _emit(message)
        return 2

    config_path = resolve_config_path(args.config)

    if args.check:
        try:
            return run_check(config_path)
        except ConfigError as exc:
            logger.error("配置错误：%s", exc)
            _emit(f"配置错误：\n{exc}")
            return 1

    _set_dpi_awareness()

    if not acquire_single_instance():
        message = "AI-Tray 已经在运行，请查看任务栏右下角的通知区域。"
        logger.info(message)
        _msgbox(message, APP_NAME, 0x40)
        return 0

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        logger.error("配置错误：%s", exc)
        _msgbox(f"配置加载失败：\n\n{exc}", "配置有误", 0x10)
        return 1

    def thread_excepthook(hook_args) -> None:
        logger.error(
            "线程 %s 未处理异常",
            hook_args.thread.name if hook_args.thread else "?",
            exc_info=(hook_args.exc_type, hook_args.exc_value, hook_args.exc_traceback),
        )

    threading.excepthook = thread_excepthook

    app = TrayApp(config, dry_run=args.dry_run)
    try:
        app.run()
    except Exception as exc:
        logger.exception("主循环异常")
        _msgbox(f"程序异常退出：\n\n{exc}\n\n详情见日志：\n{log_file_path()}", APP_NAME, 0x10)
        return 1

    logger.info("程序已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
