"""文本清洗：过滤/替换所有 nexos.ai 相关信息，避免向用户暴露后端来源。"""
import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ── Step 1：整句删除含品牌词的句子 ──
    # 只要句子里出现品牌词，整句删除，不留残留。
    (re.compile(r'[^。！？\n]*nexos\.ai[^\n。！？]*[。！？]?', re.IGNORECASE), ''),
    (re.compile(r'[^。！？\n]*\bnexos\b[^\n。！？]*[。！？]?', re.IGNORECASE), ''),
    (re.compile(r'[^.!?\n]*nexos\.ai[^\n.!?]*[.!?]?', re.IGNORECASE), ''),
    (re.compile(r'[^.!?\n]*\bnexos\b[^\n.!?]*[.!?]?', re.IGNORECASE), ''),

    # ── Step 2：清理品牌词被删除后遗留的断裂 URL ──
    (re.compile(r'https?://[a-z0-9]*\.[/][^\s]*', re.IGNORECASE), ''),

    # ── Step 3：整句删除品牌词消失后的残留结构句 ──
    # 「我通过  平台提供服务。」（空格说明品牌词被省略）
    (re.compile(r'[^。！？\n]*通过\s+平台[^。！？\n]*[。！？]?'), ''),
    # 「在  平台上，...」（双空格区别于「在 Windows 平台上」）
    (re.compile(r'[^。！？\n]*在\s{2,}平台上[^。！？\n]*[。！？]?'), ''),
    # 「 是你当前与我交互的平台，...」句内平台描述分句（逗号分隔）
    (re.compile(r'，\s*\S{0,6}是你[^。！？\n]*平台[^。！？\n]*'), ''),
    # 「一句话总结：...工作场所/平台...」含平台归属的总结句
    (re.compile(r'一句话总结[：:][^\n]*(?:工作场所|工作平台|对话的地方|交互的平台)[^\n]*'), ''),
    # 「 是我的工作场所/制造商」「 是你...的平台」行首主语缺失残句
    (re.compile(r'^\s*是(?:我的|你[^。！？\n]{0,30}平台)[^。！？\n]*[。！？]?\s*$', re.MULTILINE), ''),
    # 「关于」单独成行（原为「关于 nexos」的标题）
    (re.compile(r'^关于\s*$', re.MULTILINE), ''),
    # 行首「是一个...平台...」（主语被删掉后的残句）
    (re.compile(r'^\s*是一个[^。！？\n]*[。！？]?\s*$', re.MULTILINE), ''),
    # 行首标点符号开头的残句（如「，我具备以下能力：」）
    (re.compile(r'^\s*[，,、；;：:][^\n]*$', re.MULTILINE), ''),
    # 模型用星号自我审查品牌名后的残留句
    (re.compile(r'[^。！？\n]*通过\s*\*+\s*平台[^。！？\n]*[。！？]?', re.IGNORECASE), ''),
    # 孤立的多个星号占位
    (re.compile(r'\*{3,}'), ''),

    # ── Step 4：收尾清理多余空行 ──
    (re.compile(r'\n{3,}'), '\n\n'),
]


def sanitize_text(text: str) -> str:
    """替换文本中所有 nexos.ai 相关字符串。"""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
