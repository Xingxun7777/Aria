# Aria 配置参考

配置文件路径：`config/hotwords.json`

首次运行时自动从 `config/hotwords.template.json` 创建。保存后 **2 秒内自动热重载**，无需重启。

---

## 通用设置 (`general`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `hotkey` | string | `"grave"` | 全局热键（`` ` `` 反引号键）|
| `audio_device` | string | `""` | 音频设备名称（空字符串 = 自动检测）|
| `auto_startup` | bool | `false` | 开机自启动 |
| `minimize_to_tray` | bool | `false` | 启动后最小化到托盘 |
| `start_active` | bool | `true` | 启动后自动进入监听状态 |

## 语音识别

顶层字段 `asr_engine` 控制引擎选择（默认 `qwen3`）：`qwen3` / `funasr`

顶层字段 `enable_initial_prompt`（默认 `true`）控制是否启用 Layer 1 热词引导。

### Qwen3-ASR (`qwen3`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_name` | string | `"auto"` | `"auto"` 自动选择：VRAM >= 5GB 用 1.7B，否则 0.6B |
| `device` | string | `"cuda"` | 计算设备：`"cuda"` / `"cpu"` |
| `language` | string | `"Chinese"` | 识别语言 |
| `torch_dtype` | string | `"bfloat16"` | 计算精度：`"bfloat16"` / `"float16"` / `"float32"`（不兼容时自动降级）|

### FunASR (`funasr`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_name` | string | `"paraformer-zh"` | 模型名称 |
| `device` | string | `"cuda"` | 计算设备：`"cuda"` / `"cpu"` |
| `enable_vad` | bool | `false` | 启用 FunASR 内置 VAD（Aria 已有独立 VAD，通常关闭）|
| `enable_punc` | bool | `false` | 启用 FunASR 内置标点恢复 |

## VAD 语音检测 (`vad`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `noise_filter` | bool | `true` | 噪声过滤：丢弃"嗯"、"啊"、"呃"等环境噪声产生的无意义文字 |
| `screen_ocr` | bool | `true` | 屏幕识别辅助：说话时 OCR 识别屏幕文字作为 ASR 上下文 |
| `threshold` | float | `0.2` | 语音检测灵敏度 (0.1-0.9)，值越低越灵敏 |
| `energy_threshold` | float | `0.003` | 能量门控阈值 (0.0005-0.02)，低于此值的音频直接丢弃 |
| `min_silence_ms` | int | `1200` | 静默判定阈值（毫秒），说完一句话后等多久认为说完了 |

## 热词系统

### 热词列表 (`hotwords`)

```json
{
  "hotwords": ["Claude", "PyTorch", "星巡"]
}
```

### 热词权重 (`hotword_weights`)

每个热词可独立设置权重，控制在各纠错层的参与程度。**未在此处配置的热词默认权重为 0.5**（参考级）。

```json
{
  "hotword_weights": {
    "Claude": 0.9,
    "PyTorch": 0.7
  }
}
```

**权重对照表：**

| 权重 | ASR 分数 | 正则替换 | 拼音匹配 | LLM 润色 |
|------|----------|----------|----------|----------|
| 0 | 跳过 | - | - | - |
| 0.3 | 30 (提示) | - | - | - |
| 0.5 | 60 (标准) | Yes | - | 参考 |
| 0.7 | 60 (标准) | Yes | - | 参考 |
| 0.9 | 100 (锁定) | Yes | - | 参考 |
| 1.0 | 100 (锁定) | Yes | Yes | 必须 |

> - 拼音模糊匹配（Layer 2.5）仅在权重 = 1.0 时激活
> - 权重 < 0.5 的热词不参与 LLM 润色（仅 ASR 层提示）
> - ASR 分数列仅对 FunASR 生效；Qwen3 使用独立的 context 机制
> - 两个引擎的 initial_prompt 和 context 阈值均为 0.5（权重 0.3 不进入 ASR 引导）

### 正则替换规则 (`replacements`)

```json
{
  "replacements": {
    "scale": "skill",
    "星循": "星巡"
  }
}
```

### 领域上下文 (`domain_context`)

```json
{
  "domain_context": "编程技术讨论，涉及 Python、CUDA、深度学习"
}
```

## 润色模式 (`polish_mode`)

顶层字段，控制 Layer 3 润色策略：

| 值 | 说明 |
|------|------|
| `"off"` | 禁用 Layer 3 润色（仅使用 L1-L2.5 纠错）|
| `"quality"` | 使用 API 润色（OpenRouter），效果最好 |
| `"fast"` | 使用本地 LLM 润色（llama.cpp），延迟最低 |

默认 `"quality"`。实际是否生效还取决于对应润色模块的 `enabled` 字段。

## 润色偏好

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `filter_filler_words` | bool | `true` | 口语过滤：去除"就是"、"嗯"、"呃"等无意义填充词 |
| `auto_structure` | bool | `false` | 自动结构化：长段口述整理为带换行、编号的文本 |
| `personalization_rules` | string | `""` | 个性化规则（每行一条自然语言指令）|
| `reply_style` | string | `""` | 回复风格偏好（"帮我回复"时使用）|
| `screen_context_enabled` | bool | `true` | 屏幕感知：根据前台应用类型调整润色风格 |
| `app_categories` | object | `{}` | 自定义应用类别映射（进程名 → 场景类型）|

**个性化规则示例：**
```json
{
  "personalization_rules": "不要把口语化的表达改成书面语\n英文专有名词保留原始大小写"
}
```

## API 润色 (`polish`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 启用 API 润色 |
| `api_url` | string | `"https://openrouter.ai/api"` | API 端点 |
| `api_key` | string | — | OpenRouter API Key |
| `model` | string | `"deepseek/deepseek-chat-v3.1:free"` | 润色模型 |
| `timeout` | int | `10` | 超时（秒）|
| `prompt_template` | string | *(内置模板)* | 自定义润色提示词。使用 `{text}` 占位符表示原文 |

**示例：**

```json
{
  "polish": {
    "enabled": true,
    "api_url": "https://openrouter.ai/api",
    "api_key": "sk-or-v1-xxxx",
    "model": "deepseek/deepseek-chat-v3.1:free",
    "timeout": 10
  }
}
```

## 本地润色 (`local_polish`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 启用本地 LLM 润色 |
| `model_path` | string | `""` | GGUF 模型路径（需自行下载配置）|
| `n_gpu_layers` | int | `-1` | GPU 加速层数（-1 = 全部层上 GPU）|
| `n_ctx` | int | `512` | 上下文窗口大小（token 数）|

## 文本输出 (`output`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `typewriter_mode` | bool | `false` | 逐字输入模式（兼容游戏 / 管理员应用）|
| `typewriter_delay_ms` | int | `15` | 逐字间隔（毫秒）|
| `check_elevation` | bool | `true` | 管理员权限检测 |

## 翻译 (`translation`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `output_mode` | string | `"popup"` | 翻译输出方式：`"popup"`（弹窗）/ `"clipboard"`（剪贴板）|

## 唤醒词与语音命令

唤醒词在 `config/wakeword.json` 中配置，键盘命令在 `config/commands.json` 中配置。

这两个文件可直接编辑，保存后自动生效。
