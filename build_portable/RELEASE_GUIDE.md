# Aria 发布指南

## 快速发布（一键命令）

```powershell
cd G:\AIBOX\aria-v1.1-dev
.venv\Scripts\python.exe build_portable\build.py && .venv\Scripts\python.exe build_portable\build_launcher_exe.py
```

完成后，`dist_portable\Aria\` 就是可分发的便携版。

---

## 完整发布流程

### 1. 开发完成后

```powershell
# 确保开发版能正常运行
.venv\Scripts\pythonw.exe -m aria.launcher

# 提交代码
git add -A
git commit -m "feat: 新功能描述"
git push
```

### 2. 打包便携版

```powershell
# 运行打包脚本（自动清理敏感数据）
.venv\Scripts\python.exe build_portable\build.py

# 编译 EXE 启动器
.venv\Scripts\python.exe build_portable\build_launcher_exe.py
```

### 3. 验证便携版

```powershell
# 测试启动
dist_portable\Aria\Aria.exe

# 检查敏感数据已清理
findstr "sk-or-v1" dist_portable\Aria\_internal\app\aria\config\hotwords.json
# 应该无输出
```

### 4. 打包分发

```powershell
# 使用 7-Zip 压缩（推荐）
7z a -t7z -mx=9 Aria-v1.1-portable.7z dist_portable\Aria\

# 或 ZIP 格式
7z a -tzip Aria-v1.1-portable.zip dist_portable\Aria\
```

---

## build.py 自动完成的事情

| 步骤 | 说明 |
|------|------|
| 1. 下载 Python | embedded Python 3.10.11 |
| 2. 复制代码 | aria/ → _internal/app/aria/ |
| 3. 复制依赖 | site-packages (~5.7GB) |
| 4. **清理敏感数据** | API key → 占位符, 日志/录音/缓存 → 删除 |
| 5. 创建启动脚本 | .cmd, .vbs, .bat |

## build_launcher_exe.py 做的事情

- 用 PyInstaller 编译 launcher_stub.py → Aria.exe
- 嵌入图标 (assets/aria.ico)
- 输出约 6MB

---

## 注意事项

### 敏感数据清理（build.py 自动处理）

- `config/hotwords.json` → API key 替换为 `YOUR_API_KEY_HERE`
- `DebugLog/` → 清空
- `data/insights/` → 清空
- `*.log`, `*.bak`, `__pycache__` → 删除

### 配置重置（build.py 自动处理）

- `hotwords` → 清空（用户自己配置）
- `polish_mode` → `local`（不需要 API）
- `wakeword` → `小助手`

### Windows SmartScreen

EXE 未签名，首次运行会警告。README.txt 已说明绕过方法。

---

## 目录结构

```
aria-v1.1-dev/
├── .venv/                  开发虚拟环境
├── aria/              源代码（开发版）
├── config/                 配置文件（开发版，含 API key）
├── build_portable/         打包脚本
│   ├── build.py            主打包脚本
│   ├── build_launcher_exe.py  EXE 编译脚本
│   ├── launcher_stub.py    EXE 源码
│   └── RELEASE_GUIDE.md    本文件
├── dist_portable/          打包输出
│   └── Aria/          可分发的便携版
└── assets/
    └── aria.ico       图标
```

---

## 版本发布 Checklist

- [ ] 开发版测试通过
- [ ] git commit & push
- [ ] 运行 build.py
- [ ] 运行 build_launcher_exe.py
- [ ] 测试便携版启动
- [ ] 检查敏感数据已清理
- [ ] 7z 压缩
- [ ] 上传分发
