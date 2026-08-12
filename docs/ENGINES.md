# Aria 1.5 识别引擎指南

Aria 的主识别能力来自 Qwen3-ASR。1.5 版本把模型后端拆成三个运行时，并保留 FunASR 作为中文备用。它们共用热词、上下文、润色、历史和输出链路。

## 选择引擎

| `asr_engine` | 模型/运行时 | 默认场景 | 硬件 |
|---|---|---|---|
| `qwen3_sherpa` | Qwen3-ASR 0.6B int8 / sherpa-onnx | 标准版 | 纯 CPU |
| `qwen3_llamacpp` | Qwen3-ASR 1.7B Q8 / llama.cpp CUDA | GPU 形态 | NVIDIA CUDA，建议至少 6 GB 可用显存 |
| `qwen3` | Qwen3-ASR 0.6B/1.7B / PyTorch | 源码与 legacy | CUDA 或 CPU |
| `funasr` | Paraformer | 中文备用 | CPU 或 CUDA，取决于源码环境 |

简单建议：

- 优先兼容、无显卡：`qwen3_sherpa`。
- 有 NVIDIA 显卡并希望使用 1.7B 模型：`qwen3_llamacpp`。
- 已有旧 PyTorch 安装或需要源码调试：`qwen3`。
- Qwen3 运行时不可用且只处理中文：可尝试 `funasr`。

不要只依据“新/旧”选择。0.6B CPU 和 1.7B GPU 的准确率、延迟、内存取舍不同，应以自己的麦克风、说话方式和硬件实测。

## 切换方式

1. 悬浮球右键菜单中的“识别方式”卡可在 CPU 轻量与 GPU 加速之间热切换。
2. 设置 → 高级设置 → 语音识别引擎可选择全部已安装引擎。
3. 高级用户可修改 `config/hotwords.json` 顶层 `asr_engine`。

切换前会检查依赖和模型。加载失败时恢复原引擎；GPU 引擎启动失败时可回退到 CPU 引擎。

## 标准版：qwen3_sherpa

标准版已经包含 sherpa-onnx、Qwen3-ASR 0.6B int8 模型和本地 VAD，不需要显卡或 torch。

主要配置：

| 字段 | 默认 | 说明 |
|---|---|---|
| `model_dir` | `models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25` | 模型目录 |
| `num_threads` | 自动 | 未填写时根据 CPU 逻辑核数决定 |

环境变量 `ARIA_SHERPA_MODEL_DIR` 可覆盖模型目录。

CPU 引擎为了把计算资源留给最终转写，不提供 GPU 引擎那样的实时中间字幕。

## GPU 形态：qwen3_llamacpp

### 推荐安装

1. 下载并完整解压唯一的 `Aria-v<版本>-Windows.zip`，正常启动 Aria。
2. 在悬浮窗右键菜单点击“GPU 加速”；尚未安装时确认约 3.1 GB 的下载提示。
3. Aria 在后台下载并验证固定资产，悬浮窗持续显示进度；通过后自动切换到 GPU，无需重启，也不需要系统 Python、CUDA Toolkit 或开发环境。

应用内入口不可用时，可先退出 Aria，再双击包根的 `Install_GPU.cmd` 走同一套下载与验证流程。

安装器会准备：

- `llama-server.exe` 及 CUDA DLL；
- `Qwen3-ASR-1.7B-Q8_0.gguf`；
- `mmproj-Qwen3-ASR-1.7B-Q8_0.gguf`。

四个上游下载件都有固定字节数和 SHA256。下载后安装器还会实际启动
`llama-server`、加载 1.7B 模型、等待本机 `/health` 就绪并用 `nvidia-smi`
确认该精确进程已使用 NVIDIA GPU。只有全部通过才把运行配置切换到
`qwen3_llamacpp`；失败时保留原 CPU 引擎。下载中断可直接重跑续传。

手动只读复核可运行：

```powershell
.\_internal\python.exe .\fetch_gpu_pack.py --check
```

维护者构建可以用 `--gpu-pack` 生成包含以上资产的单目录 GPU 包；公开 GitHub
Release 使用一个统一 Windows 包，CPU 解压即用，GPU 由包内一键安装器补齐，避免
上传超过平台单资产限制的大型归档。

### 运行方式

Aria 启动一个上游 `llama-server.exe` 子进程，并通过本机 HTTP 调用：

- 监听地址固定为 `127.0.0.1`；
- 默认端口 `18539`；
- 子进程随引擎卸载或 Aria 退出而关闭；
- 端口被其他程序占用时不强制结束对方；
- 只回收路径和模型均与当前 Aria 资产匹配的残留子进程。

主要配置：

| 字段 | 出厂值 | 说明 |
|---|---|---|
| `server_path` | `llamacpp/llama-server.exe` | 包内构建会改成 bundled 相对路径 |
| `model_path` | `models/qwen3-asr-gguf/Qwen3-ASR-1.7B-Q8_0.gguf` | 主模型 |
| `mmproj_path` | 空 | 空值时按主模型文件名推断 |
| `port` | `18539` | 本机回环端口 |
| `ngl` | `99` | GPU layer 数 |
| `ctx` | `8192` | 上下文长度 |

相对路径先按 Aria 程序根解析，再检查 `models/llamacpp_runtime/`。环境变量 `ARIA_LLAMACPP_DIR` 可覆盖资产根。

## PyTorch 兼容引擎：qwen3

`qwen3` 是旧完整包和源码开发环境使用的 PyTorch 运行时。1.5 标准/GPU 发行包不携带 torch，因此在这些包中选择它会失败并回退。

`model_name = auto` 时会根据可用显存选择 0.6B 或 1.7B。GPU 压力备胎只适用于这个 torch-CUDA 引擎，不适用于 sherpa 或 llama.cpp。

## 本地对比数据

2026-07 的同批 125 段录音回放得到以下开发机参考值；基准文本来自 PyTorch 1.7B 输出，不是人工真值，因此只能用于相对比较：

| 指标 | `qwen3` 1.7B GPU | `qwen3_sherpa` CPU | `qwen3_llamacpp` GPU Q8 |
|---|---:|---:|---:|
| 3–8 秒语句转写耗时 p50 | ~0.8 s | ~0.5 s | ~0.1 s |
| 相对 1.7B 基准的字符偏离 | 基准 | ~6% | ~1.5% |
| 典型冷启动 | ~10 s | ~3 s | ~2–6 s |
| 典型显存 | 约 6–8 GB | 0 | 约 4.2 GB |

这些数字受 CPU、GPU、磁盘、驱动、模型版本和音频长度影响，不代表最低保证。

## 常见问题

### 切换 GPU 后立即回退

先查看 `DebugLog/llamacpp_server.log`，再检查：

1. `fetch_gpu_pack.py --check` 是否通过；
2. `nvidia-smi` 是否正常；
3. `18539` 是否被其他程序占用；
4. GGUF 和 mmproj 是否都存在；
5. 包内 `ggml-cuda.dll` 等 CUDA DLL 是否齐全。

### 退出后 llama-server 仍存在

先确认它的完整路径属于当前 Aria 安装。不要按进程名批量结束所有 `llama-server.exe`；其他软件也可能使用该上游程序。

### CPU 引擎没有实时字幕

这是当前计算策略，不是加载失败。CPU 运行时只提交最终转写。

### 标准版误选 qwen3

标准版没有 torch。切回 `qwen3_sherpa`；如需 1.7B GPU 模型，安装 GPU 资产后选择 `qwen3_llamacpp`。
