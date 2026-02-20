# 贡献指南

感谢你对 Aria 的关注！本文档说明如何参与开发。

## 开发环境搭建

### 前置要求

- Windows 10/11 64 位
- Python 3.12
- Git
- NVIDIA GPU（可选，无 GPU 自动使用 CPU 模式）

### 步骤

```bash
# 1. 克隆仓库
git clone https://github.com/Xingxun7777/Aria.git
cd Aria

# 2. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 复制配置模板（首次）
copy config\hotwords.template.json config\hotwords.json

# 5. 启动（调试模式，带控制台输出）
Aria_debug.bat
```

> `config/hotwords.json` 是你的本地配置，已被 `.gitignore` 排除。**不要提交此文件**——它包含个人设置和 API 密钥。

## 项目结构

详见 [README.md 项目结构](README.md#项目结构)，核心目录：

| 目录 | 说明 |
|------|------|
| `core/asr/` | 语音识别引擎（FunASR、Whisper、FireRedASR、Qwen3） |
| `core/audio/` | 音频采集 + VAD |
| `core/hotword/` | 四层热词纠错 + AI 润色 |
| `core/selection/` | 选区指令（润色、翻译等） |
| `core/wakeword/` | 唤醒词检测 + 命令执行 |
| `ui/qt/` | PySide6 界面 |
| `system/` | 热键、文本输出、权限检测 |
| `config/` | 配置文件和模板 |

主入口：`launcher.py` → `app.py`（状态机 + ASR 编排）

## 测试

```bash
# 调试模式启动，观察控制台日志
Aria_debug.bat

# 日志输出目录
DebugLog/
```

目前没有自动化测试套件。测试以手动功能验证为主——启动后测试语音输入、热词纠错、选区指令等功能。

## 提交 Pull Request

### 流程

1. Fork 仓库并创建功能分支
2. 开发 + 测试
3. 确保没有提交 `config/hotwords.json`
4. 更新 `CHANGELOG.md`（如有用户可见变更）
5. 如涉及配置项，同步更新 `config/hotwords.template.json`
6. 提交 PR，填写模板中的检查清单

### 提交信息规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 风格：

```
feat: 添加 XXX 功能
fix: 修复 XXX 问题
refactor: 重构 XXX 模块
docs: 更新 XXX 文档
build: 修改构建配置
```

### 代码风格

- 遵循项目现有风格
- 中文注释
- 线程安全：跨线程 UI 操作通过 `QtBridge` 的 `QMetaObject.invokeMethod`
- 配置写入使用原子操作（tmp + fsync + os.replace）

## 重要警告

- **永远不要提交 `config/hotwords.json`**：包含用户个人配置和 API 密钥
- **配置模板变更提交 `config/hotwords.template.json`**：模板中 `polish.enabled` 必须为 `false`
- **大型模型文件不进仓库**：`.gguf`、`.bin`、`.pt` 等已在 `.gitignore` 中排除

## 许可证

贡献的代码将以 [Apache License 2.0](LICENSE) 发布。提交 PR 即表示你同意此许可。
