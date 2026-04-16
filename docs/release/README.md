# Aria 发布流程

## 一键发布（推荐）

```powershell
.venv\Scripts\python.exe build_portable\release_all.py
```

自动完成全部流程：构建 → 7z 压缩 → git tag → GitHub Release → 上传资产。

**安全保证**：只在 dist 副本上清理敏感数据，绝不修改源码目录。

### 可用参数

| 参数 | 说明 |
|------|------|
| `--dry-run` | 预览流程，不实际执行 |
| `--skip-build` | 跳过构建，使用已有 dist 目录 |
| `--skip-upload` | 只构建+压缩，不上传 GitHub |

### 示例

```powershell
# 预览
.venv\Scripts\python.exe build_portable\release_all.py --dry-run

# 只打包不上传
.venv\Scripts\python.exe build_portable\release_all.py --skip-upload

# 用已有 dist 重新压缩+上传
.venv\Scripts\python.exe build_portable\release_all.py --skip-build
```

## 内部流程

`release_all.py` 按顺序执行：

1. **Phase 0 — 安全检查**：检测 Aria 进程、7z/gh/python 工具、git 状态
2. **Phase 1 — 构建**：调用 `build.py`（内部完成源码复制+脱敏）→ 编译 Aria.exe → 验证无 API key
3. **Phase 2 — 压缩**：7z 多线程压缩为 `Aria-v{version}-lite.7z`
4. **Phase 3 — 发布**：创建 git tag → GitHub Release → 上传资产

## 脱敏内容

构建时 `build.py` 在 dist 副本上自动执行：

- 用 `hotwords.template.json` 覆盖 `hotwords.json`
- 清空 hotwords / replacements / domain_context / personalization_rules / reply_style
- 清空 polish.api_key、备用 API 配置、本地模型路径、音频设备名
- 将 wakeword.json 置为 `enabled=false` + 空唤醒词
- 将 commands.json 置为 `enabled=false` + 空前缀
- 清空 DebugLog/、data/history/、data/history_txt/、data/insights/
- 重置 reminders.json、highlights.txt
- 删除 *.log / *.bak / __pycache__

## 危险脚本（不要使用）

以下脚本会直接修改源码目录，已加交互确认保护：

- `release_prep.py` — 在源码树上运行 sanitize（会清空 API key、历史记录等）
- `release-prep.bat` — 调用 release_prep.py 的批处理包装

正常发布 **永远不需要** 运行这些脚本。
