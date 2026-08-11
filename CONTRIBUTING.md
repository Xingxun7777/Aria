# 参与 Aria 开发

感谢你愿意改进 Aria。提交代码前，请先确认改动属于当前 **1.5 三运行时架构**，不要为已经归档的 legacy 1.x 新增功能。

## 开发目标

Aria 的核心目标是可靠的 Windows 语音输入链路，而不只是单次 ASR 推理：

1. 本地识别可独立使用；
2. 低音量、噪声、引擎失败不能静默丢句；
3. 热词、屏幕 OCR、上下文和润色只能提供纠错证据，不能改变用户原意；
4. 输出后必须恢复剪贴板和目标窗口现场；
5. 配置、历史、模型和密钥在升级时必须保留；
6. 标准版不依赖 torch，GPU 版运行时失败必须能回退 CPU。

## 环境

- Windows 10/11 x64
- Python 3.12
- Git
- 默认源码模板使用 `qwen3` PyTorch CUDA；只改文档或运行不加载 ASR 的单测时不需要 NVIDIA GPU

```powershell
git clone https://github.com/Xingxun7777/Aria.git
cd Aria
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy config\hotwords.template.json config\hotwords.json
.\.venv\Scripts\python.exe launcher.py
```

便携构建另使用 `.venv-slim` 作为 torch-free 依赖源；普通功能开发不要把依赖安装到全局 Python。

## 目录职责

| 路径 | 职责 |
|---|---|
| `app.py` | 应用状态机、录音生命周期、ASR 编排 |
| `core/asr/` | 引擎适配、声学策略、失败自愈与救援 |
| `core/audio/` | 采集、DSP、VAD、增益 |
| `core/context/` | 窗口标题/UIA/截图 OCR、DML worker、缓存与上下文路由 |
| `core/hotword/` | 热词、纠错、润色 |
| `system/output.py` | 键盘/剪贴板输出与恢复 |
| `ui/qt/` | PySide6 UI |
| `config/*.template.json` | 可提交的出厂配置 |
| `assets/` | 图标、VAD 和界面运行资产 |
| `docs/` | 配置、引擎、OCR、数据与隐私说明 |

接入新 ASR 引擎前，请先阅读公开的 [`docs/ENGINES.md`](docs/ENGINES.md)，并在提交中同时覆盖配置、运行时切换、打包和回退路径。修改窗口采集、OCR 后端、缓存、屏幕纠错或自动热词时，先阅读 [`docs/OCR.md`](docs/OCR.md)，同时验证本地/联网边界和 DirectML → CPU 回退。

OCR v5 模型位于本机 `models/rapidocr/`，不进入 Git；标准版和 GPU 包由构建器从明确 allowlist 捆入这些运行资产。不要把个人或实验模型目录加入公开包。

## 测试

公开快照可执行的基础检查：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app.py launcher.py aria core ui system
git diff --check
```

维护者开发树另含不随公开快照分发的自动化测试、构建编排和诊断工具。合并前会使用仓库 `.venv` 跑完整回归、源码/CPU/GPU 结构检查和发行扫描。不要用系统 conda Python 代替项目环境；测试不得启动完整 GUI，也不要结束正在运行的 Aria。

按改动类型补充定向验证：

- ASR / VAD：相关单测 + 失败/低音量路径；
- 输出：文字、图片、文件、空剪贴板与 Electron/终端恢复；
- 配置：模板加载、热重载、旧配置迁移；
- OCR：标题/UIA/截图路径、缓存一致性、DML worker 失败后的 CPU 回退、缺字段时的隐私开关；
- 构建：source/CPU/GPU 结构、PE 版本资源、package privacy scan；
- 发布：由维护者通过项目发布编排完成，不接受手工替换发行附件。

## 配置与隐私

以下内容不得提交：

- `config/hotwords.json`、`wakeword.json`、任何 `*.local.json`；
- API Key、DPAPI 密文、个人热词、个人唤醒词或本机路径；
- `DebugLog/`、OCR dump/样本、历史、录音、模型、dist、临时实验；
- 会话、交接、协作记录或自动生成的贡献署名。

要修改默认值，只改对应模板并同步文档与测试。`polish.enabled`、`asr_rescue.cloud_enabled` 必须继续默认关闭。

## 提交与 Pull Request

1. 一个 PR 解决一个清晰问题。
2. 说明用户可见行为、根因、测试和剩余风险。
3. 配置字段变化必须同步模板、`docs/CONFIGURATION.md` 和迁移逻辑。
4. 运行隐私扫描并检查 staged diff。
5. Git 元数据只描述技术变化；不要加入工具署名、协作过程或自动生成说明。

建议提交类型：

```text
feat: ...
fix: ...
refactor: ...
docs: ...
build: ...
test: ...
```

## 许可证

提交到本仓库的代码将按 [Apache License 2.0](LICENSE) 发布。
