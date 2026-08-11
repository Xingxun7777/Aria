# Aria 屏幕 OCR 上下文模块

屏幕 OCR 是 Aria 1.5 的核心上下文模块。它不是截图工具，也不会把识别到的页面文字直接当成用户输入；它负责从当前前台窗口提取“此刻可能相关的专名、术语和场景证据”，再按运行模式安全地提供给 ASR、可选润色或可选热词学习链。

## 默认状态

| 能力 | 配置 | 出厂默认 | 默认行为 |
|---|---|---|---|
| 本地屏幕采集 | `vad.screen_ocr` | `false` | 在“自动学习的热词”中显式开启后，才在本机读取窗口标题、UI Automation 文本或窗口截图 |
| OCR 加速 | `vad.screen_ocr_use_dml` | `true` | 优先尝试独立进程中的 DirectML PP-OCRv5；失败自动回退 CPU |
| 强制 CPU | `vad.screen_ocr_force_cpu` | `false` | 仅用于兼容性诊断；开启后跳过 DirectML |
| 屏幕感知增强 | `vad.screen_ocr_polish` | `false` | 关闭时不会把 OCR 摘要发送给润色 API；缺少此字段同样按关闭处理 |
| 自动热词学习 | `auto_hotword.enabled` | `false` | 关闭时不持久化 OCR 候选词，也不调用热词审查 API；缺少此字段同样按关闭处理 |
| 完整屏幕文字日志 | `ARIA_DEBUG_SAVE_SCREEN_TEXT` | 未设置 | 默认不保存完整 OCR/页面文字；显式设为 `1` 才写诊断内容 |

`screen_context_enabled` 是“按前台应用类别调整润色语气”的开关，不是 OCR 总开关。它默认开启，但只有实际调用润色 API 时才会发送前台进程的应用名称和推断出的场景类别；关闭后可省略这部分应用上下文。它不发送窗口标题或截图。OCR 总开关是 `vad.screen_ocr`。

## 三层采集

```text
当前前台窗口
   ├─ Layer 0：窗口标题                    立即可用
   ├─ Layer 1：UI Automation 文档/编辑文本  后台、适用于原生应用
   └─ Layer 2：窗口截图 → 本地 OCR          后台、浏览器/终端/自绘 UI 兜底
                         │
                         ▼
              清洗、去重、限长、按 HWND 缓存
                         │
        ┌────────────────┼──────────────────┐
        ▼                ▼                  ▼
 fast 模式 ASR      显式开启的润色增强   显式开启的自动热词
 过滤后短关键词      最多 1200 字上下文    候选词学习/审查
```

### Layer 0：窗口标题

Aria 在说话开始和前台窗口切换时立即读取窗口标题，去掉浏览器名称等常见后缀并做基础清洗。标题是高置信来源；即使 OCR 后端不可用，标题层仍可工作。

### Layer 1：UI Automation

对于能暴露文档或编辑控件的原生 Windows 应用，Aria 先通过 UI Automation 读取最多约 500 个字符。浏览器、Windows Terminal、经典控制台和部分终端类窗口会跳过这一层，避免低质量或高延迟的可访问性树遍历。

### Layer 2：截图 OCR

UI Automation 没拿到正文时，Aria 截取当前前台窗口到内存，长边超过 1500 像素时先缩放，再运行本地 OCR。截图对象只用于本次推理，默认不写成图片文件。

## OCR 后端与故障隔离

默认探测顺序：

```text
PP-OCRv5 + DirectML
        ↓ 初始化、provider 或运行失败
PP-OCRv5 + CPU
        ↓ 失败
RapidOCR 内置 v4 + CPU
        ↓ 失败
Windows OCR
        ↓ 失败
窗口标题层
```

- **`v5_dml`**：检测、方向分类和识别三个 ONNX Runtime 阶段都必须实际挂载 `DmlExecutionProvider`，并通过带文字的小图冷启动测试，才算启用成功。
- **独立 worker**：DirectML 只在 `rapidocr_worker.py` 子进程中运行。驱动或 ONNX Runtime 在 native 层崩溃时，Aria 主进程仍在，父进程会把“worker 退出”当成普通失败并切到 CPU。
- **`v5_cpu` / `v4_cpu`**：CPU 回退在本机完成，不需要网络。
- **发行资产**：标准版和 GPU 版都包含 `models/rapidocr/v5/`。公开源码快照不携带模型二进制；源码环境缺少 v5 资产时会继续探测 RapidOCR 内置 v4、Windows OCR 或标题层。

`DebugLog/ocr_debug.log` 会记录后端 tier、provider、耗时、字符数和失败原因，适合排查 DirectML 回退。遇到兼容性问题时，可先关闭 `screen_ocr_use_dml`；只有需要强制验证 CPU 路径时再开启 `screen_ocr_force_cpu`。

## 缓存与延迟策略

- OCR 在后台执行，不阻塞录音和 ASR 主链；并发触发会合并，最新窗口的请求会排队而不是静默丢弃。
- ASR 使用的缓存要求与当前窗口 HWND 匹配，且只读取新鲜缓存。
- 润色层可以使用同窗口缓存和最多 3 分钟的少量跨窗口短期上下文，以便用户切到输入窗口后仍能引用刚看到的专名。
- 开启屏幕感知增强的 `quality` 润色路径在没有当前窗口缓存时，只会对较长语句、专名/术语测试等更需要屏幕证据的场景做预测式有界等待；其余情况直接退回标题或已有缓存。
- DirectML 路径等待上限通常更宽，CPU 路径保持更保守的延迟预算。无论是否等到 OCR，输入链都会继续，不会因为屏幕识别失败而丢句。

## 如何进入识别与润色链

### `polish_mode = "quality"`（出厂模式）

OCR 原文不进入 ASR context，避免页面导航、按钮和无关正文过度影响语音识别。只有同时满足下面两项，最多约 1200 字的带来源标签摘要才会交给所配置的润色 API：

1. `polish.enabled = true`；
2. `vad.screen_ocr_polish = true`。

润色后的结果还会经过长度、内容新增和屏幕证据守卫；OCR 文本是纠错证据，不是允许模型续写页面内容的指令。

### `polish_mode = "fast"`

Aria 不等待 OCR，只从当前标题和已经存在的新鲜缓存中提取低风险短关键词，再经过噪声过滤后追加到 ASR context。英文 OCR 词不会因为只在页面中出现一次就获得强偏置。

### `polish_mode = "off"`

ASR 使用静态热词和常规近期上下文，不额外注入屏幕 OCR。`vad.screen_ocr` 若仍开启，后台本地缓存可以继续服务用户之后显式开启的 OCR 能力。

## 自动热词学习

`auto_hotword.enabled` 是独立的显式开关，出厂关闭。开启后，OCR 提取的候选专名会写入 `data/auto_hotwords.json`；达到阈值后，可使用用户配置或主润色 API 对“候选词、出现次数、有限的窗口标题样本”进行审查。`auto_hotword.sample_logging.enabled` 也是独立开关，只有开启后才把受限数量的 OCR 原文样本写入 `data/ocr_samples/`。

如果只需要本地 OCR，不要开启自动热词学习。更完整的数据出网与清理说明见 [数据与隐私说明](PRIVACY_DATA.md)。

## 本地数据与隐私

- 默认只在内存中保存当前 OCR 结果与短期缓存，不保存窗口截图。
- 默认调试日志不写前台应用名称、窗口标题或 OCR 正文，只写长度、哈希、通用场景类别、后端和耗时。
- `ARIA_DEBUG_SAVE_SCREEN_TEXT=1` 会开启应用名称、标题预览和 `DebugLog/screen_text_dump.log` 完整上下文诊断；这些内容可能含私人消息、文档和路径，排查结束后应关闭并删除。
- 只有屏幕感知增强与 API 润色同时显式开启时，OCR 文字摘要才会发往用户配置的兼容 API；发送的是文字上下文，不是截图。
- `screen_context_enabled` 与 OCR 开关相互独立：AI 润色开启时，它默认会发送当前应用名称和场景类别；关闭该项即可省略，且它不会发送窗口标题或截图。
- 公开源码同步和便携包构建都会排除 `DebugLog/`、`data/`、运行时配置和 OCR 样本；不要把这些目录直接上传到公开 Issue。

配置字段的完整定义见 [配置参考](CONFIGURATION.md)。
