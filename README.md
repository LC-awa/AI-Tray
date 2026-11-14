# AI-Tray — Windows 托盘 AI 快捷启动器

一个常驻右下角通知区域的小工具：

- **左键单击**托盘图标：用 Chromium 内核浏览器以 **PWA 风格独立窗口**打开默认 AI 站点（默认 DeepSeek）。
- **右键单击**托盘图标：弹出菜单，随时切换 ChatGPT、Claude、豆包、Kimi 等站点。
- 菜单最下方是 **退出程序**。
- 菜单项、默认站点、浏览器、图标颜色都写在 `config.json` 里，改完点一下“重新加载配置”即可生效。

图标是用 Pillow 代码画出来的（圆角渐变底 + 对话气泡），不需要准备图片；也可以换成自己的 `.png` / `.ico`。

---

## 1. 安装与运行

要求：Windows 10/11，Python 3.10+（64 位）。

```powershell
cd AI-Tray
python -m pip install -r requirements.txt
```

依赖只有两个：

- [pystray](https://pypi.org/project/pystray/)：系统托盘和菜单
- [Pillow](https://pypi.org/project/Pillow/)：生成托盘图标

`webbrowser`、`subprocess`、`json` 等都是 Python 标准库，不需要额外安装。

启动：

```powershell
python ai_tray.py
```

不想看到黑色控制台窗口，可以用：

```powershell
pythonw ai_tray.py
```

第一次运行会在 `ai_tray.py` 同目录生成 `config.json`（本仓库已经附带一份）。

### 自检命令

改完配置或怀疑环境有问题时，先跑：

```powershell
python ai_tray.py --check
```

它会打印：配置文件路径、识别到的浏览器、默认站点、菜单结构、实际会执行的启动命令。

其他命令行参数：

| 参数 | 作用 |
| --- | --- |
| `--check` | 只检查配置和浏览器，不启动托盘 |
| `--dry-run` | 真正启动托盘，但点击时只打印命令、不打开浏览器 |
| `--config PATH` | 使用指定配置文件 |
| `--export-icon PATH` | 生成 `icon.ico` / `.png` 后退出（打包 exe 时用） |
| `--version` | 显示版本号 |

---

## 2. 右键菜单长什么样

```text
DeepSeek          ← 默认项（菜单里显示为粗体）
ChatGPT
Claude
豆包
Kimi
─────────────
打开配置文件
重新加载配置
─────────────
退出程序
```

- 单击哪个 AI，就用 Chromium 的 `--app=网址` 打开哪个独立窗口。
- **左键单击**托盘图标 = 点击“默认项”，打开 `config.json` 中 `default` 指定的站点。
- “打开配置文件”会用系统默认编辑器打开 `config.json`。
- “重新加载配置”不用重启程序，改完网址、菜单、浏览器后点它即可。

---

## 3. 配置说明（config.json）

完整示例：

```json
{
  "title": "AI 快捷启动器",
  "default": "deepseek",
  "browser": "auto",
  "pwa": true,
  "extra_args": [],
  "icon": null,
  "accent": "#4F46E5",
  "sites": [
    {
      "id": "deepseek",
      "name": "DeepSeek",
      "url": "https://chat.deepseek.com/"
    },
    {
      "id": "chatgpt",
      "name": "ChatGPT",
      "url": "https://chatgpt.com/"
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `title` | 托盘图标悬停提示里显示的名字，也是通知标题 |
| `default` | 左键单击打开哪个站点，填某个站点的 `id` |
| `browser` | `auto`、`chrome`、`edge`、`brave`、`vivaldi`、`chromium`，或浏览器 exe 的完整路径 |
| `pwa` | `true`：`--app=网址` 独立窗口（PWA 风格）；`false`：普通标签页 |
| `extra_args` | 追加给浏览器的参数，例如 `["--profile-directory=Default"]` |
| `icon` | 自定义图标路径（`.png` / `.ico`），相对路径以 `config.json` 所在目录为准；`null` 表示自动生成 |
| `accent` | 自动生成图标的主色，格式 `#RRGGBB` |
| `sites` | 菜单项数组，每一项包含 `id`、`name`、`url` |

### 添加一个新站点

在 `sites` 数组里加一项，然后右键托盘 →“重新加载配置”：

```json
{
  "id": "gemini",
  "name": "Gemini",
  "url": "https://gemini.google.com/"
}
```

`id` 可以省略，程序会用 `name` 自动生成；但 `default` 引用的 `id` 必须存在。
`url` 不写 `https://` 也可以，程序会自动补上。

### 关于“系统默认浏览器”和 PWA

你的两条要求（“用系统默认浏览器”和“用 Chromium 内核”）在默认浏览器不是 Chromium 时是冲突的。程序的策略是：

1. `browser: "auto"` 时，先看 Windows 默认浏览器是不是 Chromium 系（Chrome / Edge / Brave / Vivaldi）；
2. 如果是，就用它；
3. 如果不是（例如默认是 Firefox），就按 Chrome → Edge → Brave → Vivaldi → Chromium 的顺序找一个已安装的 Chromium 浏览器；
4. 一个都找不到，才退回 `webbrowser` 的系统默认打开方式（普通标签页），并在日志里警告。

想固定用某个浏览器，把 `browser` 改成 `"chrome"`、`"edge"` 或 exe 的完整路径即可。

> 说明：`--app=URL` 是 Chromium 提供的“应用窗口”模式，效果接近 PWA：独立窗口、没有地址栏和标签栏。它和“在 Chrome 里正式安装的 PWA”不完全一样；如果你已经在浏览器里安装了某个 PWA，也可以把 `pwa` 设为 `false`，再用 `extra_args` 指定 `--app-id=...`，或直接让它打开普通页面。

---

## 4. 打包成 .exe

先安装 PyInstaller：

```powershell
python -m pip install pyinstaller
```

### 方法一：一键脚本（推荐）

```powershell
python build.py
```

脚本会先调用 `python ai_tray.py --export-icon icon.ico` 生成 exe 图标，再执行 PyInstaller。产物：

```text
dist\AI-Tray.exe
```

想要带控制台的调试版：

```powershell
python build.py --console
```

### 方法二：手动命令

```powershell
python ai_tray.py --export-icon icon.ico
python -m PyInstaller --noconfirm --clean --onefile --windowed --name AI-Tray --icon icon.ico --hidden-import pystray._win32 ai_tray.py
```

`--hidden-import pystray._win32` 不能省：pystray 在运行时才动态导入 Windows 后端，PyInstaller 的静态分析发现不了它，漏掉会在启动时报 `ImportError`。

打包完成后：

1. 把 `dist\AI-Tray.exe` 放到一个**可写**的普通文件夹（不要放 `C:\Program Files`，否则首次运行无法生成 `config.json`）；
2. 把 `config.json` 复制到 exe 旁边（不复制也行，首次运行会自动生成默认配置）；
3. 双击 exe 即可。

`--onefile` 体积小、只有一个文件，但每次启动要解压，冷启动约 1～2 秒。更在意启动速度可以去掉 `--onefile` 换成 `--onedir`，产物是一个文件夹，里面的 exe 启动更快。

> 杀毒软件误报：PyInstaller 单文件 exe 偶尔会被误判。可以改用 `--onedir`，或给 exe 做代码签名。

本仓库已经附带一个按上述命令打好的 `dist\AI-Tray.exe`，`dist\config.json` 也放在它旁边，双击即可试运行。修改配置时，改 `dist\config.json` 或重新打包时把根目录的 `config.json` 复制过去。

### 开机自启

不需要管理员权限：

1. `Win + R` 输入 `shell:startup` 回车；
2. 在打开的“启动”文件夹里新建一个指向 `AI-Tray.exe` 的快捷方式。

登录 Windows 后程序会自动出现在托盘里。

---

## 5. 实现要点

- **左键默认动作**：菜单里默认站点那一项使用 `pystray.MenuItem(..., default=True)`。pystray 的 Windows 后端收到 `WM_LBUTTONUP` 时会调用 `icon()`，进而执行默认项；这正是“注册在 default action 里”的要求，不需要额外安装鼠标钩子。
- **右键菜单**：pystray 在 `WM_RBUTTONUP` 时用原生 `TrackPopupMenuEx` 弹出菜单。
- **浏览器**：用 `webbrowser.register()` 注册一个自定义 controller，再通过 `webbrowser.get(...).open(...)` 调用。controller 内部用 `subprocess.Popen` 启动 Chromium，并加上 `--app=网址`。
- **图标**：Pillow 画 4 倍尺寸后缩放到 256×256，保存成多尺寸 ICO 交给 pystray；也支持加载自定义图片。
- **单实例**：用 Windows 命名互斥体（`CreateMutexW`）防止重复启动出多个托盘图标。
- **防重复点击**：同一站点 1 秒内只打开一次，避免双击时弹出两个窗口。
- **兼容性**：pystray 创建托盘窗口时会调用 `ChangeWindowMessageFilterEx`；在部分受限令牌或安全软件环境下这个调用会返回“拒绝访问”，pystray 默认会因此启动失败。程序对该调用做了容错，失败只记一条警告，不影响托盘启动。
- **日志**：`%LOCALAPPDATA%\AI-Tray\ai-tray.log`（自动轮转，最多约 3 MB）。因为打包成 `--windowed` 后没有控制台，排查问题请看这个文件。

---

## 6. 常见问题

**托盘里看不到图标？**
Windows 11 默认把新图标收进“^”溢出区。点任务栏右下角的 `^`，把 AI 图标拖到任务栏上即可常驻。

**左键点了没反应？**
先运行 `python ai_tray.py --check`，确认：
- 配置里的默认站点存在；
- 能找到 Chromium 浏览器；
- 打印出的“默认启动命令”是否正确。
然后看日志 `%LOCALAPPDATA%\AI-Tray\ai-tray.log`。

**右键菜单里没有我要的 AI？**
编辑 `config.json` 的 `sites` 数组，保存后右键托盘 →“重新加载配置”。

**配置改坏了，程序打不开？**
程序会弹窗提示具体是哪一行 JSON 出错，并继续使用旧配置（如果是启动时出错则退出）。也可以先备份再删除 `config.json`，重新启动会自动生成默认配置。

**默认浏览器是 Firefox，还能用吗？**
可以。`auto` 会跳过 Firefox，在已安装的 Chromium 浏览器里选一个。想强制指定就改 `browser`。

**双击托盘图标会开两个窗口吗？**
不会。同一站点有 1 秒防抖，双击只会打开一次。

---

## 7. 文件说明

```text
AI-Tray/
├─ ai_tray.py        主程序（单文件）
├─ config.json       菜单、网址、浏览器、图标配置
├─ requirements.txt  pystray + Pillow
├─ build.py          一键打包脚本
├─ icon.ico          由代码生成的 exe/托盘图标（可用 --export-icon 重新生成）
└─ README.md         本文件
```

卸载：退出程序，删除文件夹即可。程序不写注册表（除非你自己加了开机启动快捷方式），日志在 `%LOCALAPPDATA%\AI-Tray`。
