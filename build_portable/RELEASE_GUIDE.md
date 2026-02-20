# Aria 发布指南

## 快速发布（一键命令）

```powershell
cd G:\AIBOX\voicetype-v1.1-dev
build_portable\release.bat
```

或手动分步执行：

```powershell
.venv\Scripts\python.exe build_portable\build.py
.venv\Scripts\python.exe build_portable\build_launcher_exe.py
```

完成后，`dist_portable\Aria\` 就是可分发的便携版。

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
# 运行打包脚本（自动使用 template 配置，清理敏感数据）
.venv\Scripts\python.exe build_portable\build.py

# 编译 EXE 启动器
.venv\Scripts\python.exe build_portable\build_launcher_exe.py
```

### 3. 验证便携版

```powershell
# 测试启动
dist_portable\Aria\Aria.exe

# 检查敏感数据已清理（应无输出）
findstr "sk-or-v1" dist_portable\Aria\_internal\app\aria\config\hotwords.json

# 检查配置使用默认值
python -c "import json; c=json.load(open('dist_portable/Aria/_internal/app/aria/config/hotwords.json','r',encoding='utf-8')); print('hotkey:', c['general']['hotkey']); print('vad:', c['vad']['threshold']); print('api_key:', c['polish']['api_key'][:10])"
```

### 4. 打包分发

```powershell
# 使用 7-Zip 压缩（推荐）
7z a -t7z -mx=9 Aria-v1.1.2.7z dist_portable\Aria\

# 或 ZIP 格式
7z a -tzip Aria-v1.1.2.zip dist_portable\Aria\
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
| `asr_engine` | `funasr` |
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
voicetype-v1.1-dev/
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
│   ├── release.bat             一键打包
│   └── RELEASE_GUIDE.md        本文件
├── dist_portable/              打包输出
│   └── Aria/                   可分发的便携版
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
- [ ] `build.py` 运行成功
- [ ] `build_launcher_exe.py` 运行成功
- [ ] 便携版 `Aria.exe` 启动正常
- [ ] 敏感数据已清理（API key, 日志, 音频）
- [ ] 配置为默认值（热键=反引号, VAD=0.3）
- [ ] 7z 压缩
- [ ] 上传分发
