# Aria 发布指南

## 快速发布（一键命令）

```powershell
build_portable\release-lite.bat
build_portable\release-full.bat
```

或手动分步执行：

```powershell
.venv\Scripts\python.exe build_portable\build.py --dist-name Aria_release_lite
.venv\Scripts\python.exe build_portable\build_launcher_exe.py --dist-name Aria_release_lite

.venv\Scripts\python.exe build_portable\build.py --full --dist-name Aria_release_full
.venv\Scripts\python.exe build_portable\build_launcher_exe.py --dist-name Aria_release_full
```

完成后：

- `dist_portable\Aria_release_lite\` = GitHub 发布用 Lite 包
- `dist_portable\Aria_release_full\` = 网盘/云盘离线 Full 包

`build_portable\release.bat lite/full` 仍可作为兼容入口使用。

---

## 完整发布流程

### 1. 开发完成后

```powershell
# 确保开发版能正常运行
.venv\Scripts\pythonw.exe -m aria.launcher

# 更新版本号
# - __init__.py: __version__ = "x.y.z"
# - aria/__init__.py: __version__ = "x.y.z"
# - CHANGELOG.md: 添加新版本条目

# 提交代码
git add <files>
git commit -m "feat: 新功能描述"
git push
```

### 2. 打包便携版

```powershell
# Lite（GitHub 发布，首次运行自动下载模型）
.venv\Scripts\python.exe build_portable\build.py --dist-name Aria_release_lite
.venv\Scripts\python.exe build_portable\build_launcher_exe.py --dist-name Aria_release_lite

# Full（网盘离线包，内置 Qwen3-ASR 0.6B + 1.7B）
.venv\Scripts\python.exe build_portable\build.py --full --dist-name Aria_release_full
.venv\Scripts\python.exe build_portable\build_launcher_exe.py --dist-name Aria_release_full
```

### 3. 验证便携版

```powershell
# Lite 测试启动
dist_portable\Aria_release_lite\Aria.exe

# 检查敏感数据已清理（应无输出）
findstr "sk-or-v1" dist_portable\Aria_release_lite\_internal\app\aria\config\hotwords.json

# Full 测试启动
dist_portable\Aria_release_full\Aria.exe

# Full 应内置两个 Qwen3-ASR 模型目录
dir dist_portable\Aria_release_full\_internal\app\aria\models

# 检查配置使用默认值
python -c "import json; c=json.load(open('dist_portable/Aria_release_lite/_internal/app/aria/config/hotwords.json','r',encoding='utf-8')); print('hotkey:', c['general']['hotkey']); print('vad:', c['vad']['threshold']); print('api_key:', c['polish']['api_key'][:10])"
```

### 4. 打包分发

```powershell
# 使用 7-Zip 压缩（推荐）
7z a -t7z -mx=9 Aria-v1.0.0-lite.7z dist_portable\Aria_release_lite\
7z a -t7z -mx=9 Aria-v1.0.0-full.7z dist_portable\Aria_release_full\

# 或 ZIP 格式
7z a -tzip Aria-v1.0.0-lite.zip dist_portable\Aria_release_lite\
```

---

## build.py 自动完成的事情

| 步骤 | 说明 |
|------|------|
| 1. 下载 Python | embedded Python 3.12.4 |
| 2. 复制代码 | core/, ui/, system/, config/ → _internal/app/aria/ |
| 3. 清理敏感数据 | 用 hotwords.template.json 替换用户配置，确保无 API key 泄露 |
| 4. 复制依赖 | site-packages (~8.3GB, 含 PyTorch + CUDA) |
| 5. 创建启动脚本 | .cmd, .vbs, .bat |
| 6. 配置 _pth | stdlib → site-packages → app → app\aria → import site |

## build_launcher_exe.py 做的事情

- 用 PyInstaller 编译 launcher_stub.py → Aria.exe
- 嵌入图标 (assets/aria.ico)
- 输出约 8MB

---

## 配置处理策略

### 分发配置（build.py 自动处理）

build.py 使用 `hotwords.template.json` 替换用户的 `hotwords.json`，确保：

| 项目 | 分发值 |
|------|--------|
| `asr_engine` | `qwen3` |
| `general.hotkey` | `` ` `` (反引号) |
| `vad.threshold` | `0.3` |
| `audio_device` | `""` (自动检测) |
| `polish.api_key` | `YOUR_OPENROUTER_API_KEY_HERE` |
| `local_polish.enabled` | `false` |
| `hotwords` | `[]` (用户自行配置) |
| `replacements` | `{}` |

### 其他清理

- `DebugLog/` → 删除
- `data/insights/` → 删除
- `*.log`, `*.bak`, `__pycache__` → 删除
- 绝对路径 (`[A-Z]:\`) → 清除

---

## Windows SmartScreen

EXE 未签名，首次运行会触发 SmartScreen 警告。用户需点击"更多信息"→"仍要运行"。

---

## 目录结构

```
Aria/
├── .venv/                      开发虚拟环境
├── core/                       核心模块 (ASR, VAD, HotWord)
├── ui/qt/                      Qt6 界面
├── system/                     系统集成 (热键, 输出, 权限)
├── config/                     配置文件（开发版，含用户 API key）
│   ├── hotwords.json           用户配置（不分发）
│   └── hotwords.template.json  模板配置（用于分发）
├── build_portable/             打包脚本
│   ├── build.py                主打包脚本
│   ├── build_launcher_exe.py   EXE 编译脚本
│   ├── launcher_stub.py        EXE 源码
│   ├── release-lite.bat        Lite 打包（GitHub）
│   ├── release-full.bat        Full 打包（网盘离线）
│   ├── release.bat             兼容入口（lite/full 分发）
│   └── RELEASE_GUIDE.md        本文件
├── dist_portable/              打包输出
│   ├── Aria_release_lite/      GitHub 发布用便携版
│   └── Aria_release_full/      网盘离线用便携版
└── assets/
    └── aria.ico                图标
```

---

## 版本发布 Checklist

详见 [docs/RELEASE_CHECKLIST.md](../docs/RELEASE_CHECKLIST.md)

快速检查：

- [ ] 开发版 `Aria_debug.bat` 测试通过
- [ ] 版本号已更新 (`__init__.py`, `aria/__init__.py`, `CHANGELOG.md`)
- [ ] git commit & push
- [ ] Lite / Full 构建脚本运行成功
- [ ] 对应的 `Aria.exe` 启动正常
- [ ] 敏感数据已清理（API key, 日志, 音频）
- [ ] 配置为默认值（热键=反引号, VAD=0.3）
- [ ] Lite / Full 压缩包创建成功
- [ ] GitHub 上传 Lite，网盘上传 Full
