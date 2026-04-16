# Aria 发布检查清单

## 发布前

- [ ] `__init__.py` 版本号已更新
- [ ] `CHANGELOG.md` 已添加当前版本条目
- [ ] 代码已提交并推送到 main 分支
- [ ] 功能已在开发环境测试通过

## 一键发布

```powershell
.venv\Scripts\python.exe build_portable\release_all.py
```

脚本自动完成以下所有步骤，无需手动操作。

## 自动验证项（release_all.py 内置）

- [x] 无 Aria 进程运行
- [x] 7z / gh CLI / .venv Python 可用
- [x] build.py 构建成功（含 import smoke test + Unicode path 测试）
- [x] dist 内 hotwords.json 无 API key（正则 + 字段检查）
- [x] 7z 压缩完成
- [x] git tag 创建并推送
- [x] GitHub Release 创建
- [x] 资产上传完成

## 手动验证（可选，推荐首次发布时做）

- [ ] 解压 7z → 运行 Aria.exe → 正常启动
- [ ] 热键可触发录音
- [ ] 设置面板可打开
- [ ] Lite 版首次启动可自动下载模型
