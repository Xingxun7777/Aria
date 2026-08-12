"""
Layer 0 deterministic pre-normalization (v14 polish pipeline)
==============================================================
Pure-regex local conversions that run BEFORE the polish LLM call:
  N1 digit-run     中文逐位数字串 → 阿拉伯（"五幺二"→512）
  N2 decimal       夹心小数（"三点一四"→3.14）
  N3 bare decimal  裸小数（"点零零四"→0.004）
  N4 file dot      文件名/域名点（"Start 点 Bat"→Start.Bat，后缀白名单制）
  N5 stutter       白名单虚词 ≥3 连压缩（"就是就是就是"→"就是"）
  N6 latin-digit   拉丁邻接单数字（"V 四"→V4）——默认关
  N7 gang-dash     "杠"→"-"（"3 杠 2"→3-2）——默认关
Every conversion is tracked in an `applied` list for the debug log.

公理：宁漏不错。层 0 只转确定性高的形态；任何判为含糊的完整跨度整体
放弃交 LLM，且后续规则不得再处理其子串（见 _boundary_blocked 的
「点」邻接检查——它让 N2/N3 放弃的小数跨度对 N1 不可见）。
"""

import re

_D = {
    "零": "0",
    "〇": "0",
    "幺": "1",
    "一": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}
_DIGIT_CLS = "零〇幺一二三四五六七八九两"  # 逐位读法字符集
_PLACE_CLS = "十百千万亿"  # 位值读法信号（出现即整串放弃，留给 LLM）

# 时间读法信号（R1）：跨度右邻时间单位字，或整段含明确时间语境词。
_TIME_UNIT_CLS = "分刻钟"
_TIME_CONTEXT_RE = re.compile(r"上午|下午|晚上|早上|中午|凌晨|半夜|点半")

# 枚举序列后缀（R2）："一二三线城市"是枚举不是编号 123。
_ENUM_SUFFIXES = ("线城市", "等奖", "等座", "流")

# 枚举序列前缀（A1-B3）："周一三五"“第一二三名"里的逐位串是枚举不是编号
# （真要说 123 章会说"第一百二十三章"）。"初"为防御性顺手（初一初二实测
# 单字间隔不成串）。
_ENUM_PREFIXES = ("周", "星期", "礼拜", "第", "初")

# N2 单字小数部的成语右邻（A1-B2）："一点一滴"“三点一线"是高频惯用语，
# 转出 1.1 滴 / 3.1 线 必错。
_IDIOM_TAIL_CLS = "滴线横竖"


def _boundary_blocked(m) -> bool:
    """True when the match window touches a char that makes the span ambiguous.

    - 位值字（十百千万亿）："四四五十分" 是位值读法，转出 "445 十分" 即铸错。
    - "点"：残留的裸露「点+数字」必然来自 N2/N3 判含糊后的放弃跨度
      （确定形态早已被它们消费），N1 再转其子串就是制造 "5点 004" 式
      破碎中间态（R3 含糊跨度屏蔽）。
    - ASCII 数字/小数点紧邻（R5）：ASR 输出天然中阿混排，"8080幺五" 转出
      的 "15" 会与 "8080" 物理粘连成用户没说过的 808015；字母邻接不挡
      （"V五幺二"→V512 是合法型号场景）。

    用邻字检查而非正则否定前瞻：前瞻会让贪婪窗口回退（"五幺二十"→"五幺"
    →51），转出前缀数字比整串放弃更糟。
    """
    s = m.string
    for idx in (m.start() - 1, m.end()):
        if 0 <= idx < len(s):
            c = s[idx]
            if c in _PLACE_CLS or c == "点" or c.isdigit() or c == ".":
                return True
    return False


def _is_reduplicated(run: str) -> bool:
    """叠数/对称四字串（R2）："三三两两"“七七八八"是成语，"幺五五幺"这类
    仅两种字符的对称串更可能是口语重复或回文号码——歧义，交 LLM。"""
    return len(run) == 4 and len(set(run)) <= 2


def _looks_like_time(m, frac: str) -> bool:
    """时间读法检测（R1），用于 N2/N3 的小数转换前拦截。

    - 跨度右邻「分/刻/钟」："三点零五分" 是 3:05 不是 3.05。
    - 整段含时间语境词："下午三点一五开会" 的 3.15 是时刻。
    - 小数部全零："九点零零" 只会是 9:00——没人把小数念成 X 点零零。
    """
    s = m.string
    if m.end() < len(s) and s[m.end()] in _TIME_UNIT_CLS:
        return True
    if _TIME_CONTEXT_RE.search(s):
        return True
    if frac and all(c in "零〇" for c in frac):
        return True
    return False


# N1 逐位数字串：3~4 位直接转；含"幺"降为 2 位即转（"幺"是最强的"这是数字"信号）。
#    ≥5 位不转（可能是两个量连读，如"七六八五幺二"=768+512，拼接即灾难，留给 LLM 按语境切）。
_RE_DIGIT_RUN = re.compile(rf"[{_DIGIT_CLS}]{{2,}}")


def _convert_run(m):
    run = m.group(0)
    if _boundary_blocked(m):
        return run
    if len(run) >= 5:  # 连读多量，交 LLM
        return run
    if len(run) == 2 and "幺" not in run:  # "一二线城市"类歧义，不动
        return run
    if _is_reduplicated(run):  # "三三两两"/"七七八八"/"幺五五幺"
        return run
    if m.string.startswith(_ENUM_SUFFIXES, m.end()):  # "一二三线城市"/"一二三等奖"
        return run
    if "幺" not in run and any(
        # A1-B3："周一三五"/"第一二三名"——前缀枚举整串弃转。含"幺"豁免：
        # "幺"是最强的"这是数字"信号，"第五幺二行"=第 512 行（枚举不会用幺）。
        m.string.endswith(prefix, 0, m.start())
        for prefix in _ENUM_PREFIXES
    ):
        return run
    return "".join(_D[c] for c in run)


# N2 夹心小数："三点一四"→3.14。两侧必须是纯逐位字（"十点零五"左侧"十"不在字符集→不匹配）。
_RE_DECIMAL = re.compile(rf"([{_DIGIT_CLS}]+)点([{_DIGIT_CLS}]+)")


def _convert_decimal(m):
    if _boundary_blocked(m):  # 位值（"三点二十"）/ ASCII 粘连（"三点一四5"）/ 邻"点"
        return m.group(0)
    if len(m.group(1)) >= 5:  # 整数部连读多量，与 N1 同一闸（防 768512.5）
        return m.group(0)
    if _looks_like_time(m, m.group(2)):  # "三点零五分"/"下午三点一五"/"九点零零"
        return m.group(0)
    if any(_is_reduplicated(g) for g in m.groups()):
        return m.group(0)
    if (  # A1-B2：单字小数部 + 成语右邻（"一点一滴"/"三点一线"）整跨度弃转
        len(m.group(2)) == 1
        and m.end() < len(m.string)
        and m.string[m.end()] in _IDIOM_TAIL_CLS
    ):
        return m.group(0)
    return (
        f'{"".join(_D[c] for c in m.group(1))}.'
        f'{"".join(_D[c] for c in m.group(2))}'
    )


# N3 裸小数："点零零四"→0.004。"点"前不得是任何数字/位值字/小数点——
#    "5点零零四"按 0.xxx 模板会铸出 50.004（紧邻由 _boundary_blocked 挡，
#    被 pad 空格隔开的数字前缀由 converter 内左跳空白检查挡）。
#    小数部分 ≥2 位（"点一下"不触发）。
_RE_BARE_DECIMAL = re.compile(
    rf"(?<![{_DIGIT_CLS}{_PLACE_CLS}])点([{_DIGIT_CLS}]{{2,}})"
)


def _convert_bare_decimal(m):
    if _boundary_blocked(m):
        return m.group(0)
    # 左侧跳过空白找实际前字符：紧邻的中文数字已被负后顾挡掉，这里补挡
    # 被 CJK pad 空格隔开的一切数字类前缀（"1024 点零零四"）。
    s = m.string
    i = m.start() - 1
    while i >= 0 and s[i].isspace():
        i -= 1
    if i >= 0 and (s[i].isdigit() or s[i] == "." or s[i] in _D or s[i] in _PLACE_CLS):
        return m.group(0)
    if _looks_like_time(m, m.group(1)):
        return m.group(0)
    if _is_reduplicated(m.group(1)):
        return m.group(0)
    if m.group(1)[0] not in "零〇":
        # A1-B1：真实裸小数口述几乎总以零打头（"点零零四"）；「点」后直跟
        # 非零数字串的形态几乎全是动词点菜概数（"点两三个菜"）、习语
        # （"指点一二"）或名词枚举（"知识点一二三"）——整类弃转。
        return m.group(0)
    return f'0.{"".join(_D[c] for c in m.group(1))}'


# N4 文件名/域名点（R4 白名单制）：匹配整条「拉丁段 点 拉丁段[ 点 拉丁段…]」链，
#    仅当链的最后一段是已知扩展名/域名后缀才整链转换——"A 点 B"、"第 1 点 Alpha"
#    保持原样，"www 点 example 点 com" 整链转 www.example.com。
_RE_FILE_DOT = re.compile(r"[A-Za-z0-9]+(?: ?点 ?[A-Za-z]+)+")
_RE_DOT_SPLIT = re.compile(r" ?点 ?")
_FILE_DOT_SUFFIXES = frozenset(
    (
        "json bat py txt md exe cmd log yaml yml toml ini csv zip "
        "png jpg wav onnx gguf com cn org net io dev"
    ).split()
)


def _convert_file_dot(m):
    segments = _RE_DOT_SPLIT.split(m.group(0))
    if segments[0].isdigit():
        # A1-B4：纯数字首段几乎总是钟点（"每天8点dev环境"——ASR 对钟点常出
        # 阿拉伯数字，dev/bat/log/io 又是技术口语高频词）；口述 "404 点 log"
        # 极罕见，整链弃转。
        return m.group(0)
    if segments[-1].lower() in _FILE_DOT_SUFFIXES:
        return ".".join(segments)
    return m.group(0)


# N5 白名单虚词 ≥3 连压缩（语义强调如"很重要很重要"是 2 连且非白名单，永不触发）。
_STUTTER_WORDS = ("就是说", "就是", "然后", "那个", "这个", "所以", "反正")
_RE_STUTTER = {
    w: re.compile(rf"(?:{re.escape(w)}[，、,\s]*){{3,}}") for w in _STUTTER_WORDS
}

# N6 拉丁邻接单数字："DeepSeek V 四 Pro"→"DeepSeek V4 Pro"。吞"字母与数字字间的单个空格"。
#    后面不能紧跟逐位字或"点"（那是 N1/N2 的领地）。默认关（误伤面未实测，留开关）。
_RE_LATIN_DIGIT = re.compile(
    rf"([A-Za-z]) ?([{_DIGIT_CLS}])(?![{_DIGIT_CLS}点])"
)

# N7 "杠"→"-"：两侧为 ASCII 字母/数字时才转（"3 杠 2"→"3-2"）。默认关（3/2 vs 3-2 歧义）。
_RE_GANG = re.compile(r"([0-9A-Za-z]) ?杠 ?([0-9A-Za-z])")


def _with_cjk_padding(m, converted):
    left_pad = (
        " "
        if m.start() > 0 and "\u4e00" <= m.string[m.start() - 1] <= "\u9fff"
        else ""
    )
    right_pad = (
        " "
        if m.end() < len(m.string)
        and "\u4e00" <= m.string[m.end()] <= "\u9fff"
        else ""
    )
    return f"{left_pad}{converted}{right_pad}"


def pre_normalize(
    text: str, *, latin_digit: bool = False, gang_dash: bool = False
) -> tuple[str, list[dict]]:
    """确定性预转换。返回 (转换后文本, 应用清单)。应用清单进 debug log。"""
    if not text:
        return text or "", []

    applied = []

    def tracked_sub(pattern, source, rule, converter, *, cjk_pad=False, ascii_pad=False):
        def replace(m):
            original = m.group(0)
            converted = converter(m)
            if converted == original:
                return original
            applied.append({"rule": rule, "from": original, "to": converted})
            if cjk_pad:
                return _with_cjk_padding(m, converted)
            if ascii_pad:
                # N5 压缩窗口的分隔符类会吞掉 N1/N2/N3 刚补的 pad 空格
                # （"就是就是就是 512"→"就是512"），后邻是拉丁/数字时补回一个。
                # pad 与 cjk_pad 同样不进 applied 的 to。
                nxt = m.string[m.end()] if m.end() < len(m.string) else ""
                if nxt.isascii() and nxt.isalnum():
                    return converted + " "
            return converted

        return pattern.sub(replace, source)

    out = text
    out = tracked_sub(
        _RE_DECIMAL,
        out,
        "decimal",
        _convert_decimal,
        cjk_pad=True,
    )
    out = tracked_sub(
        _RE_BARE_DECIMAL,
        out,
        "bare_decimal",
        _convert_bare_decimal,
        cjk_pad=True,
    )
    out = tracked_sub(
        _RE_DIGIT_RUN,
        out,
        "digit_run",
        _convert_run,
        cjk_pad=True,
    )
    out = tracked_sub(
        _RE_FILE_DOT,
        out,
        "file_dot",
        _convert_file_dot,
    )

    for word in _STUTTER_WORDS:
        out = tracked_sub(
            _RE_STUTTER[word],
            out,
            "stutter",
            lambda _m, replacement=word: replacement,
            ascii_pad=True,
        )
    if latin_digit:
        out = tracked_sub(
            _RE_LATIN_DIGIT,
            out,
            "latin_digit",
            lambda m: f"{m.group(1)}{_D[m.group(2)]}",
        )
    if gang_dash:
        out = tracked_sub(
            _RE_GANG,
            out,
            "gang_dash",
            lambda m: m.expand(r"\1-\2"),
        )

    return out, applied
