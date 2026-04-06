# Aria 项目规范

## Git 提交规范
- **commit 只用 Xingxun 身份**，不加 Co-Authored-By Claude
- **每次推送必须 bump 版本号**：修改 `__init__.py` 的 `__version__`
- **必须双推两个 remote**：`git push origin main && git push release main`
  - `origin` = `Aria-dev.git`（开发仓库）
  - `release` = `Aria.git`（用户更新源）
- 忘记任何一步 → 用户拉不到更新

## 部署测试
- **不要手动复制文件到部署目录**
- 测试更新流程：推 GitHub → 部署目录运行 `update.bat`
- 部署目录：`G:/AriaV1.03/Aria-v1.0.3.2-full`

## 代码修改
- **NEVER use Edit tool on `config/hotwords.json`** — Chinese triggers Rust UTF-8 bug
- bat 文件不写中文（GBK 乱码），所有中文提示放 Python 脚本
- AriaApp 没有 `self.config` 属性，读配置用 `self._config_path` + json.load
