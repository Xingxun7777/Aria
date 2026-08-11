# Aria 配置参考

配置文件路径：`config/hotwords.json`

首次运行时自动从 `config/hotwords.template.json` 创建。保存后 **2 秒内自动热重载**，无需重启。

屏幕采集、OCR 后端、缓存与上下文分流的完整说明见 [屏幕 OCR 上下文模块](OCR.md)。

---

## 通用设置 (`general`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `hotkey` | string | `"grave"` | 全局热键（`` ` `` 反引号键）|
| `audio_device` | string | `""` | 音频设备名称（空字符串 = 自动检测）|
| `auto_startup` | bool | `false` | 开机自启动 |
| `minimize_to_tray` | bool | `false` | 启动后最小化到托盘 |
| `start_active` | bool | `true` | 启动后自动进入监听状态 |
| `trigger_mode` | string | `"toggle"` | 热键触发模式：`"toggle"` / `"hold_to_talk"`，见下 |

### 热键触发模式 (`general.trigger_mode`)

- `"toggle"`（默认）：按一下开始录音，再按一下停止。行为与历史版本完全一致。
- `"hold_to_talk"`：按住说话，松开即上屏；**短按**（<300ms）或**双击**进入锁定持续听写，锁定后再按一下停止。适合习惯对讲机式输入的用户。

未知值或读取失败时自动回退 `"toggle"`。

## 语音识别

顶层字段 `asr_engine` 控制引擎选择：`qwen3_sherpa` / `qwen3_llamacpp` / `qwen3` / `funasr`（发行包按口味预设——标准版 `qwen3_sherpa`、GPU 版 `qwen3_llamacpp`；源码模板默认 `qwen3`）

| 引擎 | 运行时 | 适用场景 |
|------|--------|----------|
| `qwen3_sherpa` | sherpa-onnx int8（无 torch） | **标准版默认**。无显卡机器，纯 CPU |
| `qwen3_llamacpp` | llama.cpp CUDA + GGUF（无 torch） | **GPU 版默认**。NVIDIA 显卡 GPU 加速，无需 torch |
| `qwen3` | PyTorch（CUDA/CPU） | legacy 1.x 标配 / 源码环境，支持 GPU/CPU 设备切换 |
| `funasr` | FunASR Paraformer | 中文备用引擎 |

> 悬浮球菜单的「识别方式」卡在 sherpa / llamacpp 运行时下是**跨引擎两键热切换**（GPU 加速 ⇋ CPU 轻量，切换前资产预检、失败自动回滚）；在 torch `qwen3` / `funasr` 下保持原有 CUDA/CPU 设备切换语义。引擎启动加载失败时自动回退：llamacpp 先回退 sherpa 再回退 torch；v2 发行包（无 torch）走到无引擎可用时提示错误并保持托盘可用。

顶层字段 `enable_initial_prompt`（默认 `true`）控制是否启用 Layer 1 热词引导。

### Qwen3-ASR (`qwen3`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_name` | string | `"auto"` | `"auto"` 自动选择：VRAM >= 5GB 用 1.7B，否则 0.6B |
| `device` | string | `"cuda"` | 计算设备：`"cuda"` / `"cpu"` |
| `language` | string | `"Chinese"` | 识别语言 |
| `torch_dtype` | string | `"bfloat16"` | 计算精度：`"bfloat16"` / `"float16"` / `"float32"`（不兼容时自动降级）|

### Qwen3-ASR 轻量引擎 (`qwen3_sherpa`)

sherpa-onnx int8 运行时，无需显卡和 torch。`asr_engine` 设为 `qwen3_sherpa` 启用。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_dir` | string | `"models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25"` | int8 模型目录；相对路径以程序根目录为基准，可用环境变量 `ARIA_SHERPA_MODEL_DIR` 覆盖 |
| `provider` | string | `"cpu"` | 推理后端（当前仅 CPU）|
| `num_threads` | int | 自动 | 推理线程数（1-64）。不配置时按 CPU 核数自动取 `min(16, max(4, 逻辑核数/2))`（约等于物理核数）；显式配置值优先 |
| `max_total_len` | int | `2048` | 单次解码 token 总长上限 |

> 块内不要写 `device` 键（运行时键是 `provider`；引擎会自行设置 device）。

### Qwen3-ASR GPU 加速引擎 (`qwen3_llamacpp`)

llama.cpp CUDA + GGUF，常驻 `llama-server` 子进程，无需 torch。`asr_engine` 设为 `qwen3_llamacpp` 启用。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `server_path` | string | `"llamacpp/llama-server.exe"` | llama-server 可执行文件路径（相对程序根目录）|
| `model_path` | string | `"models/qwen3-asr-gguf/Qwen3-ASR-1.7B-Q8_0.gguf"` | GGUF 模型路径 |
| `mmproj_path` | string | `""` | mmproj 文件路径；留空自动取 model_path 同目录的 `mmproj-<模型名>.gguf` |
| `port` | int | `18539` | llama-server 本地端口（1024-65535）|
| `ngl` | int | `99` | 上 GPU 的层数（99 = 全部上卡）|
| `ctx` | int | `8192` | KV 上下文长度 |
| `request_timeout_base` | float | `8` | 单请求最小超时秒数（实际按音频时长放大，1-120）|

> llama-server 进程意外死亡时，自愈重载会绕过常规冷却立即重启（进程死亡只有重载能恢复）。

### FunASR (`funasr`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model_name` | string | `"paraformer-zh"` | 模型名称 |
| `device` | string | `"cuda"` | 计算设备：`"cuda"` / `"cpu"` |
| `enable_vad` | bool | `false` | 启用 FunASR 内置 VAD（Aria 已有独立 VAD，通常关闭）|
| `enable_punc` | bool | `false` | 启用 FunASR 内置标点恢复 |

## 识别救援 (`asr_rescue`)

最终段转写超时 / 引擎异常 / 有语音却返空时的自动补救链：连续失败自动重建引擎（自愈重载），配置云端 Key 后还可将丢失段音频发云端二次转写。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `true` | 救援链总开关（含连续失败后的引擎自愈重载）|
| `cloud_enabled` | bool | `false` | 云端二次转写开关（需同时配置 `api_key`）|
| `api_key` | string | `""` | 阿里云百炼 API Key（本机 DPAPI 加密存储）|
| `api_url` | string | `"https://dashscope.aliyuncs.com/compatible-mode/v1"` | 云端 API 端点 |
| `model` | string | `"qwen3-asr-flash"` | 云端二次转写模型 |
| `timeout_s` | float | `15` | 云端请求超时（3-120 秒）|
| `max_audio_s` | float | `60` | 超过此时长的丢失段不上云（1-300 秒）|
| `beep` | bool | `true` | 丢句时低音提示音 |

救援行为：

- **自愈重载**：连续 2 次最终段失败触发引擎重建，两次重载间隔至少 600 秒（重载失败或中止时缩短为 60 秒重试窗）；llama-server 进程已死亡时绕过冷却立即重载。
- **云端补救**：丢失段音频发云端转写；迟到结果超过 20 秒、或期间已有新内容上屏时，存入历史记录而不直接上屏。
- **失败提示**：空白、停顿和无法确认的返空结果只记诊断，不打扰用户；只有明确超时或引擎异常才红闪，并提示重试或正在自动补救。

## VAD 语音检测 (`vad`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `noise_filter` | bool | `true` | 噪声过滤：丢弃"嗯"、"啊"、"呃"等环境噪声产生的无意义文字 |
| `screen_ocr` | bool | `false` | 屏幕感知总开关；在“自动学习的热词”中显式选择后才读取窗口标题和页面内容 |
| `screen_ocr_polish` | bool | `false` | 屏幕感知增强：将屏幕 OCR 摘要传给 AI 润色层，用于人名和专业术语纠错；出厂默认关，旧配置缺此键时也按 `false` 处理 |
| `screen_ocr_use_dml` | bool | `true` | 启用 DirectML OCR 加速：DML 在独立 OCR worker 中运行；worker native 崩溃会自动触发 CPU 回退，不会杀掉 Aria 主进程 |
| `screen_ocr_force_cpu` | bool | `false` | 强制 CPU OCR（诊断用）：即使开启 `screen_ocr_use_dml` 也跳过 DirectML GPU tier；保存后热重载会重建 OCR 后端 |
| `threshold` | float | `0.15` | 语音检测灵敏度 (0.1-0.9)，值越低越灵敏 |
| `energy_threshold` | float | `0.003` | 能量门控阈值 (0.0005-0.02)，低于此值的音频直接丢弃 |
| `min_silence_ms` | int | `1500` | 静默判定阈值（毫秒），说完一句话后等多久认为说完了 |

### 屏幕感知增强如何工作

开启 `screen_ocr` + `screen_ocr_polish` 后，Aria 会在说话开始时后台读取当前窗口标题和 OCR 正文，并把这些文字作为**动态参考材料**传给 AI 润色层。它不是静态热词表，也不会把屏幕内容直接插入输出。

按润色模式分流：

- `quality`：屏幕 OCR **不进入 ASR context**，只进入 API Polish 层。这样可以让强模型做语义理解和柔性纠错，避免 ASR 先被脏 UI 文本带偏。
- `fast`：只把当前已经可用的窗口标题/新鲜 OCR 缓存抽成低风险短关键词送入 ASR context，**不等待 OCR**；新屏幕词以中文专名/术语为主，英文 OCR 词只有在已经存在于静态热词 context 时才用于加权。如果当前窗口还没有 OCR 缓存，就只用已有标题/静态热词继续识别。
- `off`：保持静态热词/近期上下文 ASR 逻辑，不额外注入屏幕 OCR。

OCR 后端默认走 `v5_dml → v5_cpu → v4_cpu`。关键点是：`v5_dml` 不在 Aria 主进程里跑，而是在独立 OCR worker 进程里跑；如果个别驱动/ONNX Runtime 组合在 native 层 access violation，只会杀掉 worker，主进程会记录失败并降级到 CPU。需要排查兼容性或临时牺牲速度时，可以关闭 `screen_ocr_use_dml` 或打开 `screen_ocr_force_cpu`。

若 RapidOCR 三个 tier 都不可用，运行时还会继续尝试 Windows OCR；再失败时保留窗口标题层。窗口截图只在内存中用于本地 OCR，不保存成图片。完整 OCR/页面文字日志也默认关闭，只有显式设置 `ARIA_DEBUG_SAVE_SCREEN_TEXT=1` 才写入诊断文件。

`auto_hotword.enabled`（默认 `false`）与 `screen_ocr_polish` 是两个独立的显式授权开关。前者开启后会持久化 OCR 候选词，并可用配置的 API 审查候选词和有限窗口标题样本；缺少该字段时按关闭处理。

适合场景：

- 当前窗口有罕见人名、角色名、英文产品名：ASR 读音接近但字错时，优先采用屏幕写法。
- 当前窗口能明确说明领域：例如 Blender/Weight Paint/Normal Map 场景下把“发现贴图”纠成“法线贴图”；医学/化学上下文下把音近药名、试剂名纠成专业写法。
- 同屏很脏：按钮、行号、历史聊天、路径会被当作低置信噪声；模型会优先找与当前 ASR 语义相关的一簇上下文。

安全边界：

- 屏幕与 ASR 无关时保持原句，只做基础标点和口语整理。
- API 失败、超时、输出过长或采用了明确负面示例时自动回退。
- 快速模式不为 OCR 增加等待；高质量模式在长句、人名/角色/术语等场景才会做有界短等。
- Typewriter 流式输出在有 OCR 上下文时自动走原子后处理路径，避免已经打出的文字无法回滚。

## 热词系统

### 热词列表 (`hotwords`)

```json
{
  "hotwords": ["DeepSeek", "PyTorch", "Qwen3"]
}
```

### 热词权重 (`hotword_weights`)

每个热词可独立设置权重，控制在各纠错层的参与程度。**未在此处配置的热词默认权重为 0.3**（提示级）。

```json
{
  "hotword_weights": {
    "DeepSeek": 0.9,
    "PyTorch": 0.7
  }
}
```

**权重对照表：**

| 权重 | ASR 引导 | 正则替换 | 拼音匹配 | LLM 润色 |
|------|----------|----------|----------|----------|
| 0 | 跳过 | - | - | - |
| 0.1 | 跳过 | Yes | - | 仅严格约束 |
| 0.3 | 提示 | Yes | - | 低优先参考 |
| 0.5 | 标准 | Yes | - | 参考 |
| 0.7 | 标准 | Yes | - | 参考 |
| 0.9 | 锁定 | Yes | - | 参考 |
| 1.0 | 锁定 | Yes | Yes | 必须 |

> - 拼音模糊匹配（Layer 2.5）仅在权重 = 1.0 时激活
> - 正则替换在权重 >= 0.1 时即参与
> - 权重 0.1 的热词不进入 ASR 引导，仅在 LLM 润色层以严格约束形式参与
> - 权重 0.3 的热词在 LLM 润色层以低优先级参考形式参与
> - 高质量模式 ASR 引导阈值为 0.5（权重 < 0.5 不进入 initial_prompt / context）
> - 快速模式会在运行时把 0.3 热词临时当作 0.5 ASR 参考词，0.1 仍不进入 ASR；这个提升只影响运行时 prompt/context/score，不会改写 `hotwords.json`

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
| `"quality"` | 使用 API 润色（推荐 DeepSeek），效果最好；屏幕 OCR 只进 Polish 层，不进 ASR |
| `"fast"` | 使用本地 LLM 润色（llama.cpp），延迟最低；已缓存屏幕关键词可进 ASR，但不等待 OCR |

默认 `"quality"`。实际是否生效还取决于对应润色模块的 `enabled` 字段。

## 润色偏好

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `filter_filler_words` | bool | `true` | 口语过滤：去除"就是"、"嗯"、"呃"等无意义填充词（出厂 `polish_style: smooth` 下为 true，切换润色风格时自动调整） |
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
| `api_url` | string | `"https://api.deepseek.com"` | API 端点 |
| `api_key` | string | `""` | API Key |
| `model` | string | `"deepseek-v4-flash"` | 润色模型 |
| `timeout` | int | `20` | 超时（秒）|
| `pinyin_hint` | bool | `false` | 在润色 prompt 中给原始转写附一行拼音标注（TONE3 数字声调，最多 200 字），辅助 LLM 修同音字（的地得、人名近音）。仅影响云 API 润色路径，`local_polish` 不受影响 |
| `prewarm` | bool | `true` | 启动 / 深睡唤醒后自动发一次 max_tokens=1 的预热请求，焐热 TLS 连接与服务端 prompt 前缀缓存，消除闲置后第一句的全冷调用惩罚（约 +1.4s）。失败静默，不影响正常润色；记账 call_type 为 `polish_warmup` |
| `skip_short_text` | bool | `true` | 短文本快路径：最终文本 <10 个有效字、且无句首口水词（呃/嗯/然后/就是 等）、无口语数字时跳过云润色直接上屏（本地热词替换照走），每句省约 760ms。实测该层改动率仅 11% 且均为低危微修 |
| `prompt_template` | string | *(内置模板)* | 自定义润色提示词（留空使用内置高级模板，含音译对照表）|
| `api_url_backup` | string | `""` | 备用 API 端点（为空则不启用轮询）|
| `api_key_backup` | string | `""` | 备用 API Key（为空则复用主 Key）|
| `model_backup` | string | `""` | 备用模型（为空则复用主模型）|
| `slow_threshold_ms` | float | `3000` | 慢响应判定阈值（毫秒）|
| `switch_after_slow_count` | int | `2` | 连续慢 N 次后切换到备用 API |

**示例：**

```json
{
  "polish": {
    "enabled": true,
    "api_url": "https://api.deepseek.com",
    "api_key": "sk-xxxx",
    "model": "deepseek-v4-flash",
    "timeout": 20
  }
}
```

普通用户推荐在界面里配置：**设置 → API → 一键填入 DeepSeek 推荐配置 → 粘贴 API Key → 测试主 API → 保存 API 设置**。配置会保存在本机 `config/hotwords.json`，不会上传。

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
| `game_chat_profiles` | object | `{}` | 高级实验功能：按可执行文件精确启用游戏聊天投递；默认不配置、不猜测游戏 |

### 游戏聊天 profile（高级实验功能）

游戏聊天不是普通文本框。请只在无反作弊风险的测试环境中，为已经验证过的目标逐个添加 profile。Aria 不接受可执行文件路径，只接受精确的 `.exe` 文件名；配置保存后可随现有配置热重载生效。

```json
{
  "output": {
    "game_chat_profiles": {
      "samplegame.exe": {
        "enabled": true,
        "transport": "typewriter",
        "open_chat_key": "t",
        "chat_already_open": false,
        "allow_same_focus_after_open": false,
        "open_delay_ms": 120,
        "max_chars": 256,
        "auto_submit": false
      }
    }
  }
}
```

| 字段 | 允许值 / 默认值 | 安全合同 |
|------|-----------------|----------|
| `enabled` | 仅字面量 `true` 才启用 | `1`、字符串和缺失值都不会启用 |
| `transport` | `manual` / `clipboard` / `typewriter`；默认 `manual` | `manual` 直接进入 Draft Box；自动方式不会跨策略回退 |
| `open_chat_key` | 字母、数字、F1–F12、Enter/Return/Escape/Tab/Space/Slash | 自动方式必须与 `chat_already_open` 二选一；不支持鼠标键、驱动或任意数字 VK |
| `chat_already_open` | 默认 `false` | 设为 `true` 表示用户明确保证聊天框已经打开；此时不得再配 `open_chat_key` |
| `allow_same_focus_after_open` | 默认 `false` | 仅供已经验证的自绘聊天框；默认必须观察到打开聊天后的原生焦点变化 |
| `open_delay_ms` | 默认 `120`，限制 `0–2000` | 打开聊天后等待目标稳定，等待结束仍会复核目标/profile |
| `max_chars` | 默认 `256`，限制 `32–2000` | 是保守安全上限，不代表游戏官方限制；超限保留全文转 Draft Box，不截断 |
| `auto_submit` | 默认 `false` | 与全局自动发送隔离；正文失败时绝不提交 |
| `submit_key` | 与安全按键集合相同 | 只有 `auto_submit: true` 时必须显式提供 |
| `submit_delay_ms` | 默认 `80`，限制 `0–2000` | 提交前等待并重新复核目标和 profile |

多行语音会被转成单行空格文本。即使状态显示 `sent`，也只代表 Windows 输入事件已被接受，不代表 Aria 能从游戏内读回并确认正文。任何真实游戏 profile 都应先保持 `auto_submit: false` 完成逐项验证；不要为受保护或高风险游戏启用自动化。

## 翻译 (`translation`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `output_mode` | string | `"popup"` | 翻译输出方式：`"popup"`（弹窗）/ `"clipboard"`（剪贴板）|

## 唤醒词与语音指令

唤醒词在 `config/wakeword.json` 中配置，键盘语音指令在 `config/commands.json` 中配置。

这两个文件可直接编辑，保存后自动生效。

### 语音指令 (`wakeword.json`)

语音指令是“唤醒词之后的指令短语”。其中内置指令用于控制 Aria、处理选区和发送常用快捷键；`commands.json` 保留键盘快捷键的白名单定义；`custom_instructions` 保存用户自己的启动/打开类指令，适合把软件、文件夹、网页或一段固定命令绑定到自然口令。

推荐在界面里配置：**设置 → 语音指令**。界面里可以设置语音唤醒词、查看内置语音指令，并导入“我的语音指令”常用预设（我的电脑、资源管理器、下载文件夹、系统/声音设置、计算器、记事本、截图工具等）。预设只会导入到表格，点击保存后才会写入本机配置并生效。

```json
{
  "wakeword": "小助手",
  "custom_instructions": [
    {
      "enabled": true,
      "phrase": "打开我的电脑",
      "aliases": ["打开此电脑", "打开这台电脑"],
      "command": "shell:MyComputerFolder",
      "mode": "open",
      "phonetic": true
    }
  ]
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `phrase` | string | `""` | 指令短语主写法，例如 `"打开我的电脑"`；至少 3 个字才会启用 |
| `aliases` | list | `[]` | 额外叫法或缩写；拼音近音匹配已默认覆盖同音字，通常可为空 |
| `command` | string | `""` | 启动目标：exe/lnk/文件夹/URL、Windows shell 入口（如 `shell:Downloads`）、应用协议（如 `ms-settings:`），或高级指令 |
| `mode` | string | `"open"` | `"open"` = 打开路径/URL；`"command"` = 带参数启动（不隐式打开 shell） |
| `phonetic` | bool | `true` | 是否启用拼音近音匹配；仅作用于此指令短语本身 |
| `enabled` | bool | `true` | 是否启用此条个人语音指令 |

内置常用预设的文件夹目标会优先使用 Windows shell namespace，例如 `shell:Downloads`、`shell:Desktop`、`shell:Personal`。这样不会保存用户目录绝对路径，也能适配 OneDrive、用户目录迁移、非 C 盘用户目录和不同语言系统。

安全边界：

- 我的语音指令不会写入 `hotwords`，不会影响普通语音输入的人名/术语验证。
- 只有预先配置的指令会被执行；ASR 识别出的自由文本不会被拼接进 shell。
- 界面会拦截重复的指令短语/近音别名，避免两条个人语音指令同时争抢同一句口令。
- 键盘快捷键仍来自 `commands.json` 的固定白名单，但运行时会和唤醒词指令走同一套唤醒词/拼音前缀检测链路。
- 发布构建会清空 `custom_instructions`，避免携带用户本机路径或私人指令。

发布态会把唤醒词重置为通用默认值，并清空我的语音指令等个人化路径/指令，避免携带本机隐私配置。
