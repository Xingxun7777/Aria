# Aria v1.1 功能归档 - 摘要弹窗

**日期**: 2025-12-31
**分支**: dev/v1.1-voice-commands
**提交**: 本次提交（hash 见 git log）
**归档人**: Codex (GPT-5)

---

## 需求概述
新增唤醒词命令“瑶瑶总结一下”，对选中长文本进行高质量摘要：
- 一句话概括 + 要点列表
- 保留关键术语、数字、时间等信息
- 输出简洁清晰，不影响现有“润色”功能

---

## 实现方案
1. 增加 SummaryAction（动作驱动 UI），与翻译弹窗同一架构
2. 新增 SummaryWorker 执行摘要请求（独立提示词）
3. 复用 TranslationPopup 作为摘要弹窗，并支持自定义文案
4. 唤醒词命令映射到 summarize_popup，避免与“缩写/精简”混淆

---

## 提示词设计（摘要）
核心目标：中文输出、结构化摘要、覆盖关键内容、严禁编造。

输出格式：
```
一句话概括：...
要点：
- ...
- ...
```

---

## 交互流程
1. 用户选中文本
2. 说“瑶瑶总结一下/遥遥总结一下”
3. 弹窗显示“正在总结...”
4. 返回摘要后展示结果，点击复制

---

## 边界条件
- 最短 20 字，过短提示用户
- 最长 20000 字，过长提示用户
- 不影响润色提示词与润色流程

---

## 影响范围
- `core/action/types.py`：新增 ActionType.SHOW_SUMMARY + SummaryAction
- `core/wakeword/executor.py`：新增 summarize_popup 执行路径
- `config/wakeword.json`：新增“总结”命令触发词映射
- `ui/qt/main.py`：新增摘要分支与 SummaryWorker 启动
- `ui/qt/translation_popup.py`：支持自定义标题/文案
- `ui/qt/workers/summary_worker.py`：摘要提示词与请求逻辑

---

## 验收结果
用户已测试通过，弹窗输出与复制流程正常。
