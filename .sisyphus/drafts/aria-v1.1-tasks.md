# Draft: Aria v1.1 多任务规划

## 任务概览

用户请求包含 4 个主要任务：
1. Git 提交 + 推送当前修改
2. 清理打包的 release 文件
3. 部署新的 Qwen3-ASR 模型
4. 研究热词系统适配

---

## 任务 1: Git 操作

### 当前状态 (confirmed)
- 分支: `dev/v1.1-voice-commands`
- 领先 origin 5 commits (未推送)
- 10 个已修改文件 (725 行增加)
- 5 个未跟踪文件需要添加

### 修改文件列表
已修改:
- `.agent/context/session.md` - AI 上下文
- `app.py` - 主应用入口
- `core/asr/funasr_engine.py` - FunASR 引擎
- `core/hotword/__init__.py` - 热词模块
- `core/hotword/manager.py` - 热词管理器
- `core/hotword/polish.py` - AI 润色模块
- `system/output.py` - 文本输出系统
- `ui/qt/floating_ball.py` - 浮动球 UI
- `ui/qt/main.py` - 主窗口
- `ui/qt/settings.py` - 设置面板

未跟踪:
- `.sisyphus/` - 工作计划目录 (可排除)
- `core/hotword/utils.py` - 热词工具函数 (新文件)
- `system/admin.py` - 管理员权限检测 (新文件)
- `test_v4_flow.py` - 测试脚本
- `ui/qt/elevation_dialog.py` - 权限提升对话框 (新文件)

### 最近提交风格
```
fix(output): address Round 2 three-way AI review findings
feat(output): add typewriter mode and permission detection
feat(hotword): optimize FunASR score mapping
```
遵循 Conventional Commits 规范

### 需要用户决策
- [ ] 是否提交 `.agent/context/session.md`? (AI 上下文文件)
- [ ] 是否提交 `test_v4_flow.py`? (测试脚本)
- [ ] 是否排除 `.sisyphus/` 目录?

---

## 任务 2: 清理工作

### 发现的文件
G:\AIBOX\ 目录下:
- `aria-release/` - 打包发布版本目录
- `aria-offline-*.zip` - 多个离线包 (CPU/CUDA 版本)
- `voicetype-v1.1-dev/` - 源码目录 (保留)

### aria-release 目录内容
包含完整的打包版本:
- Aria-CPU-v1.1.zip, Aria-CUDA-v1.1.zip
- 多个 .bat 启动脚本
- 完整的源码结构副本

### 需要用户决策
- [ ] aria-release/ 是否删除? (看起来是发布版本)
- [ ] aria-offline-*.zip 文件是否删除?
- [ ] 需要保留哪些打包文件?

---

## 任务 3: Qwen3-ASR 集成

### 现有 ASR 架构
```
core/asr/
├── base.py          # ASREngine 抽象基类
├── funasr_engine.py # FunASR 实现 (Paraformer)
├── whisper_engine.py # Whisper 实现
├── fireredasr_engine.py # FireRedASR 实现
└── __init__.py
```

### ASREngine 接口
```python
class ASREngine(ABC):
    @abstractmethod
    def transcribe(self, audio: bytes) -> ASRResult
    
    @abstractmethod
    def transcribe_stream(self, audio_generator) -> Generator[ASRResult]
    
    @property
    @abstractmethod
    def is_loaded(self) -> bool
    
    @abstractmethod
    def load(self) -> None
    
    @abstractmethod
    def unload(self) -> None
```

### Qwen3-ASR 调研结果 (COMPLETED)

**官方信息来源**: 
- GitHub: https://github.com/QwenLM/Qwen3-ASR
- 阿里云 DashScope API 文档

**模型特性**:
- **Qwen3-ASR-1.7B**: SOTA 性能，支持 52 种语言和方言
- **Qwen3-ASR-0.6B**: 速度优先，2000x 吞吐量
- 支持流式/离线两种推理模式
- 内置语言检测和时间戳预测

**安装方式**:
```bash
pip install -U qwen-asr         # transformers 后端
pip install -U qwen-asr[vllm]   # vLLM 后端 (推荐)
```

**基础用法**:
```python
from qwen_asr import Qwen3ASRModel

model = Qwen3ASRModel.from_pretrained(
    "Qwen/Qwen3-ASR-1.7B",
    dtype=torch.bfloat16,
    device_map="cuda:0",
)

results = model.transcribe(
    audio="path/to/audio.wav",
    language=None,  # 自动检测
)
```

**Context Biasing (热词偏置) - 关键发现**:

根据阿里云文档，Context Biasing 是 Qwen3-ASR 的核心功能：

1. **比传统热词更强大**: 使用语义理解而非精确匹配
2. **更灵活的格式**: 支持多种文本类型
   - 热词列表: "Hotword 1, Hotword 2, Hotword 3"
   - 段落文本
   - 混合格式
3. **限制**: context 内容不超过 10,000 tokens
4. **无分数权重**: 不支持 FunASR 的 "word score" 格式

**API 参数**:
- WebSocket: `session.input_audio_transcription.corpus.text`
- Python SDK: `corpus_text` 参数
- Java SDK: `corpusText` 参数

**对比 FunASR**:
| 特性 | FunASR | Qwen3-ASR |
|------|--------|-----------|
| 热词格式 | "word score" 逐行 | 纯文本字符串 |
| 权重支持 | 支持 (0-100) | 不支持 |
| 偏置方式 | 声学匹配 | 语义理解 |
| 灵活性 | 结构化 | 自由文本 |

---

## 任务 4: 热词系统适配

### 当前热词系统 (FunASR)

**权重映射**:
| UI 权重 | FunASR Score | 含义 |
|---------|--------------|------|
| 0.3     | 30           | hint (仅 ASR 提示) |
| 0.5     | 60           | reference (标准) |
| 1.0     | 100          | critical (锁定) |

**格式**: `"word score"` 逐行
```
阿里巴巴 60
Claude 100
```

### Polish 层分层
- `critical`: weight = 1.0, 必须使用
- `reference`: weight = 0.5, 中文参考
- `english_reference`: weight = 0.5, 英文参考 (更严格)

### Qwen3-ASR Context Biasing 适配方案 (DECIDED)

**关键差异**: Qwen3-ASR 使用语义理解，不支持分数权重

**推荐适配策略**:
1. **分层格式化**:
   ```
   【必须使用的专有名词】Claude, ComfyUI, DeepSeek
   【参考词汇】阿里巴巴, 淘宝, 天猫
   【英文术语】GitHub, Python, TypeScript
   ```

2. **权重到结构的映射**:
   | 权重 | 处理方式 |
   |------|----------|
   | 1.0 (critical) | 放入 "必须使用" 区块 |
   | 0.5 (reference) | 放入 "参考词汇" 区块 |
   | 0.3 (hint) | 不传入 context (省 tokens) |

3. **格式**: 逗号分隔的自然语言列表，分区块组织

**HotWordManager 需要的修改**:
- 新增 `get_qwen_context()` 方法
- 返回格式化的 context 字符串
- 复用现有 `_load_weights()` 逻辑

---

## 用户决策记录

**Git 提交**:
- [x] 提交所有源码修改
- [x] 排除 .agent/context/session.md
- [x] 排除 test_v4_flow.py
- [x] 添加 .sisyphus/ 到 .gitignore
- [x] 分多次提交 (按功能拆分)

**清理**:
- [x] 删除 aria-release/ 目录
- [ ] zip 文件暂不处理 (用户未明确)

**Qwen3-ASR**:
- [x] 完整集成 + 热词适配
- [x] 与现有 FunASR/Whisper 并存

---

## 依赖关系与并行化

```
Wave 1 (可并行):
├── Git 提交 (独立)
└── 清理 aria-release (独立)

Wave 2 (依赖 Wave 1 完成后):
├── 安装 qwen-asr 包
└── 创建 Qwen3ASREngine 基础结构

Wave 3:
├── 实现 Qwen3ASREngine.transcribe()
└── 实现 Qwen3ASREngine.load/unload

Wave 4:
├── HotWordManager.get_qwen_context()
└── 集成热词到 Qwen3ASREngine

Wave 5:
├── app.py 引擎选择逻辑
└── UI 设置面板更新

Wave 6:
└── 集成测试
```

---

## 技术决策

**Qwen3-ASR 后端选择**: vLLM (推荐)
- 更快的推理速度
- 支持流式
- 更好的批处理

**热词适配策略**: 分层格式化
- 利用 Qwen3 的语义理解能力
- 保持与现有权重系统兼容

**引擎切换方式**: 配置文件
- `asr_engine: "qwen3"` (新增选项)
- 与现有 funasr/whisper 并列
