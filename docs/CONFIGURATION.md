# Aria 配置参考

配置文件路径：`config/hotwords.json`

首次运行时自动从 `config/hotwords.template.json` 创建。保存后 **2 秒内自动热重载**，无需重启。

---

## 通用设置 (`general`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `hotkey` | string | `` ` `` | 全局热键（反引号）|
| `input_mode` | string | `"toggle"` | 输入模式：`"toggle"`（切换）/ `"ptt"`（按住说话）|
| `ptt_key` | string | `"right_ctrl"` | PTT 按键 |
| `audio_device` | string | `""` | 音频设备名称（空字符串 = 自动检测）|

## 语音识别

顶层字段 `asr_engine` 控制引擎选择（默认 `qwen3`）：`qwen3` / `funasr` / `whisper` / `fireredasr`

### FunASR (`funasr`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_name` | string | `"paraformer-zh"` | 模型名称 |

### Whisper (`whisper`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_size` | string | `"large-v3-turbo"` | 模型规格 |

### Qwen3-ASR (`qwen3`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_name` | string | `"auto"` | `"auto"` 自动选择：VRAM >= 4GB 用 1.7B，否则 0.6B |

## VAD 语音检测 (`vad`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `threshold` | float | `0.3` | 灵敏度 (0-1)，值越低越灵敏 |
| `min_silence_ms` | int | `1200` | 静默判定阈值（毫秒）|

## 热词系统

### 热词列表 (`hotwords`)

```json
{
  "hotwords": ["Claude", "PyTorch", "星巡"]
}
```

### 热词权重 (`hotword_weights`)

每个热词可独立设置权重，控制在各纠错层的参与程度：

```json
{
  "hotword_weights": {
    "Claude": 0.9,
    "PyTorch": 0.7
  }
}
```

**权重对照表：**

| 权重 | ASR 分数 | 正则替换 | 拼音匹配 | LLM 提示 |
|------|----------|----------|----------|----------|
| 0 | 跳过 | - | - | 禁用 |
| 0.3 | 15 (提示) | - | - | 仅提示 |
| 0.5 | 50 (标准) | Yes | - | 参考 |
| 0.7 | 70 (强) | Yes | - | 强参考 |
| 0.9 | 85 (很强) | Yes | Yes | 强参考 |
| 1.0 | 100 (锁定) | Yes | Yes | 必须 |

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

## API 润色 (`polish`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 启用 API 润色 |
| `api_url` | string | `"https://openrouter.ai/api"` | API 端点 |
| `api_key` | string | — | OpenRouter API Key |
| `model` | string | `"deepseek/deepseek-chat-v3.1:free"` | 润色模型 |
| `timeout` | int | `10` | 超时（秒）|

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
| `model_path` | string | `"models/qwen2.5-1.5b-instruct-q4_k_m.gguf"` | GGUF 模型路径 |

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
