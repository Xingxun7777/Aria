# Security Policy | 安全策略

## Supported Versions | 支持版本

| Version | Supported |
|---------|-----------|
| 1.1.x   | Yes       |
| < 1.1   | No        |

## Reporting a Vulnerability | 报告漏洞

**请勿在公开 Issue 中报告安全漏洞。**
**Do NOT report security vulnerabilities in public Issues.**

### 联系方式 | Contact

请通过以下方式私密报告：

- **GitHub Private Vulnerability Reporting**: 在本仓库的 Security 标签页点击 "Report a vulnerability"
- **邮件 | Email**: 在仓库主页查看维护者联系方式

### 报告内容 | What to Include

- 漏洞描述和影响范围
- 复现步骤
- 受影响的版本
- 可能的修复建议（如有）

### 响应时间 | Response Timeline

- **确认收到**: 3 个工作日内
- **初步评估**: 7 个工作日内
- **修复发布**: 视严重程度，严重漏洞优先处理

## 安全相关说明 | Security Considerations

### 本地处理

Aria 的所有语音数据在本地处理，不上传任何服务器。ASR 引擎（FunASR、Whisper、Qwen3-ASR）均在本地运行。

### API 密钥

- `config/hotwords.json` 可能包含 AI 润色功能的 API 密钥
- 此文件已在 `.gitignore` 中排除，**不应被提交到仓库**
- 便携版构建使用 `config/hotwords.template.json`（不含密钥）

### 音频权限

- Aria 使用麦克风进行语音输入，仅在用户主动触发时录音
- 录音数据仅用于本地 ASR 处理，处理后不保留原始音频
- Typewriter 模式需要模拟键盘输入，部分场景需管理员权限

### 已知安全边界

- **本地网络**: 不监听任何网络端口
- **文件访问**: 仅读写 `config/`、`DebugLog/`、`data/` 目录
- **进程通信**: 仅通过 Windows Named Mutex 实现单例检查
