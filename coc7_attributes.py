"""CoC7 人物卡导入与属性表共用的字段定义。"""

import re
import unicodedata

MAX_IMPORT_LENGTH = 12_000

# 按人物卡布局排列。前八项是基础属性，幸运单独生成。
ATTRIBUTES = (
    ("str", "力量"), ("dex", "敏捷"), ("pow", "意志"),
    ("con", "体质"), ("app", "外貌"), ("edu", "教育"),
    ("siz", "体型"), ("int", "智力"), ("luck", "幸运"),
)
RESOURCES = (("hp", "生命值"), ("san", "理智"), ("mp", "魔法值"))

SKILL_COLUMNS = (
    (
        ("会计", 5), ("人类学", 1), ("估价", 5), ("考古学", 1), ("取悦", 15),
        ("攀爬", 20), ("计算机使用", 5), ("信用评级", 0), ("克苏鲁神话", 0),
        ("乔装", 5), ("闪避", None), ("汽车驾驶", 20), ("电气维修", 10), ("电子学", 1),
        ("话术", 5), ("斗殴", 25), ("手枪", 20), ("步枪／霰弹枪", 25), ("急救", 30),
        ("历史", 5), ("恐吓", 15), ("跳跃", 20), ("外语", 1), ("母语", None),
    ),
    (
        ("法律", 5), ("图书馆使用", 20), ("聆听", 20), ("锁匠", 1), ("机械维修", 10),
        ("医学", 1), ("博物学", 10), ("导航", 10), ("神秘学", 5), ("操作重型机械", 1),
        ("说服", 10), ("精神分析", 1), ("心理学", 10), ("骑术", 5), ("妙手", 10),
        ("侦查", 25), ("潜行", 20), ("生存", 10), ("游泳", 20), ("投掷", 20),
        ("追踪", 10), ("驯兽", 5), ("潜水", 1), ("爆破", 1), ("读唇", 1),
        ("催眠", 1), ("炮术", 1),
    ),
)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


_ALIASES = {
    **{alias: key for key, label in (*ATTRIBUTES, *RESOURCES) for alias in (key, label)},
    **{_normalize(name): name for column in SKILL_COLUMNS for name, _ in column},
    "灵感": "int", "意志力": "pow", "知识": "edu", "幸运值": "luck", "运气": "luck",
    "san值": "san", "理智值": "san", "体力": "hp", "血量": "hp", "魔法": "mp",
    "魅惑": "取悦", "计算机": "计算机使用", "电脑": "计算机使用",
    "信用": "信用评级", "信誉": "信用评级", "克苏鲁": "克苏鲁神话", "cm": "克苏鲁神话",
    "汽车": "汽车驾驶", "驾驶": "汽车驾驶", "汽车驾驭": "汽车驾驶",
    "图书馆": "图书馆使用", "图书馆利用": "图书馆使用", "开锁": "锁匠", "撬锁": "锁匠",
    "侦察": "侦查", "偵察": "侦查", "偵查": "侦查",
    "自然学": "博物学", "领航": "导航", "重型操作": "操作重型机械",
    "重型机械": "操作重型机械", "重型": "操作重型机械",
    "射击(手枪)": "手枪", "格斗(斗殴)": "斗殴",
    "步枪/散弹枪": "步枪／霰弹枪", "步枪": "步枪／霰弹枪", "霰弹枪": "步枪／霰弹枪",
}

_MARKDOWN_SEPARATOR = re.compile(r"[|:\-\s]+")
_FIELD = re.compile(r"([^\W\d_][^\d\s|:=,;_+\-]{0,39})\s*[:=]?\s*([0-9]{1,4})%?")
_SEPARATORS = re.compile(r"[\s,;、]+")
_FIRST_STAT = re.compile(r"(?:力量|str|敏捷|dex|意志|pow|体质|con|外貌|app|教育|edu|体型|siz|智力|int)\s*[:=]?\s*[0-9]")


def parse_attributes(text: str) -> dict[str, int]:
    if len(text) > MAX_IMPORT_LENGTH:
        raise ValueError("属性指令太长，请只复制人物卡的 .st 属性行。")
    text = _normalize(text)
    lines = [line.strip().strip("|").strip() for line in text.splitlines()
             if line.strip() and not _MARKDOWN_SEPARATOR.fullmatch(line)]
    body = " ".join(lines)
    if not body.startswith(".st") or len(body) == 3:
        raise ValueError("请粘贴以 .st 开头的人物卡属性指令。")
    body = body[3:].strip()
    # 卡表也会导出带角色名的格式。名字只用来定位属性正文。
    match = _FIRST_STAT.match(body)
    if match is None:
        named = re.match(r"[^?\r\n]{1,160}?[?\-]\s*(?=" + _FIRST_STAT.pattern + r")", body)
        if named:
            body = body[named.end():]
    values: dict[str, int] = {}
    offset = 0
    while offset < len(body):
        separator = _SEPARATORS.match(body, offset)
        if separator:
            offset = separator.end()
            continue
        match = _FIELD.match(body, offset)
        if match is None:
            raise ValueError(f"无法识别属性指令中的「{body[offset:offset + 18]}」。")
        name, amount = match.groups()
        key = attribute_key(name)
        value = int(amount)
        if key in values and values[key] != value:
            raise ValueError(f"「{name}」与同一属性的其他数值不一致。")
        values[key] = value
        offset = match.end()

    missing = [label for key, label in ATTRIBUTES[:8] if key not in values]
    if missing:
        raise ValueError("缺少基础属性：" + "、".join(missing) + "。")
    return values


def attribute_key(name: str) -> str:
    normalized = _normalize(name)
    return _ALIASES.get(normalized, normalized)


def skill_defaults(values: dict[str, int]) -> dict[str, int]:
    return {
        name: default if default is not None else (values["dex"] // 2 if name == "闪避" else values["edu"])
        for column in SKILL_COLUMNS for name, default in column
    }


def effective_attributes(values: dict[str, int]) -> dict[str, int]:
    # 旧卡可能曾把后来加入的别名当作独立技能保存。明确的标准字段优先。
    canonical = {}
    for name, value in values.items():
        key = attribute_key(name)
        if key == name or key not in canonical:
            canonical[key] = value
    return {
        **skill_defaults(canonical),
        "hp": (canonical["con"] + canonical["siz"]) // 10,
        "mp": canonical["pow"] // 5,
        "san": canonical["pow"],
        **canonical,
    }
