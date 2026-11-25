"""CoC7 命令与 BCDice 输入输出转换。"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class DiceError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RuleConfig:
    custom: bool = False
    critical_max: int = 5
    fumble_min: int = 95

    def validate(self) -> None:
        if self.custom and not 1 <= self.critical_max < self.fumble_min <= 100:
            raise ValueError("大成功上限必须小于大失败下限，两个数值都应在 1–100 之间。")


@dataclass(frozen=True)
class DiceCommand:
    secret: bool
    expression: str = ""
    check: str = ""
    modifier: int = 0
    difficulty: str = ""
    adjustment: int = 0
    subject: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CheckInput:
    actor: str
    label: str
    value: int


@dataclass(frozen=True)
class DiceResult:
    text: str
    details: dict


_COMMAND = re.compile(r"^[.。．](rah|rch|rh|ra|rc|r)(?=$|\s|[^\x00-\x7f]|\d|[bp](?:\d|\s|[^\x00-\x7f]|$)|d\d)", re.I)
_DIFFICULTIES = {"普通": "", "常规": "", "常規": "", "困难": "h", "困難": "h", "极难": "e", "極難": "e"}
_DIFFICULTY_NAME = {"": "普通", "h": "困难", "e": "极难"}
_DIVISOR = {"": 1, "h": 2, "e": 5}
_TERM = r"(?:\d{0,3}d\d{1,4}|\d{1,6})"
_FORMULA = re.compile(rf"{_TERM}(?:\s*[+-]\s*{_TERM})*(?=$|\s)", re.I | re.ASCII)


def parse_command(text: str) -> DiceCommand | None:
    # 粘贴命令常会带空行或 BOM。先识别前缀，暗骰不能因此落入公开聊天。
    text = text.lstrip().lstrip("\ufeff").lstrip()
    command = _COMMAND.match(text)
    if command is None:
        return None
    if len(text.splitlines()) != 1:
        raise DiceError("dice_syntax")
    name = command[1].lower()
    rest = text[command.end():].strip()
    if name in ("r", "rh"):
        if rest:
            formula = _FORMULA.match(rest)
            if formula is None or len(formula[0]) > 120:
                raise DiceError("dice_syntax")
            expression = "".join(formula[0].lower().split())
            reason = rest[formula.end():].strip()
            if reason.startswith(("+", "-", "*", "/", "%", "=")):
                raise DiceError("dice_syntax")
        else:
            expression, reason = "1d100", ""
        terms = [(int(count or "1"), int(sides)) for count, sides in re.findall(r"(\d*)d(\d+)", expression)]
        if any(count < 1 or sides < 1 for count, sides in terms) or sum(count for count, _ in terms) > 100:
            raise DiceError("dice_limit")
        return DiceCommand(name == "rh", expression=expression, reason=reason[:80])
    modifier = 0
    mode = re.match(r"^([bp])(\d*)(?=\s|[^\x00-\x7f]|$)", rest, re.I | re.ASCII)
    if mode:
        if mode[2] not in ("", "1", "2"):
            raise DiceError("dice_limit")
        modifier = int(mode[2] or 1) * (1 if mode[1].lower() == "b" else -1)
        rest = rest[mode.end():].lstrip()
    subject = ""
    leading_target = re.fullmatch(r'@(?:"([^"]+)"|([^\s"]+))\s+(.+)', rest)
    if leading_target:
        subject, rest = leading_target[1] or leading_target[2], leading_target[3]
    # 海豹写法为末尾 @角色名。支持引号或不带引号的含空格姓名。
    target = re.search(r"\s+@(.+)$", rest)
    if target:
        if subject:
            raise DiceError("dice_syntax")
        subject = target[1].strip()
        if subject.startswith('"') and subject.endswith('"'):
            subject = subject[1:-1]
        if not subject:
            raise DiceError("dice_syntax")
        rest = rest[:target.start()].strip()
    if len(subject) > 80:
        raise DiceError("dice_syntax")
    difficulty = ""
    for prefix, value in _DIFFICULTIES.items():
        if rest.startswith(prefix):
            difficulty = value
            rest = rest[len(prefix):].lstrip()
            break
    check = re.fullmatch(r"([^@+\-=\s]{1,40})(?:\s*([+-]\d{1,3}))?", rest)
    if check is None:
        raise DiceError("dice_syntax")
    return DiceCommand(name.endswith("h"), check=check[1], modifier=modifier, difficulty=difficulty,
                       adjustment=int(check[2] or 0), subject=subject)


def _worker(payload: dict) -> dict:
    directory = Path(__file__).resolve().parent
    if getattr(sys, "frozen", False):
        runtime = directory / "node.exe"
        executable = str(runtime) if runtime.is_file() else None
    else:
        executable = shutil.which("node")
    if not executable or not (directory / "node_modules" / "bcdice" / "package.json").is_file():
        raise DiceError("dice_engine_unavailable")
    try:
        result = subprocess.run(
            [executable, str(directory / "worker.cjs")],
            input=(json.dumps(payload) + "\n").encode("utf-8"),
            capture_output=True, timeout=5, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise DiceError("dice_engine_unavailable") from error
    if not isinstance(data, dict) or "error" in data:
        raise DiceError("dice_syntax")
    return data


def roll(command: DiceCommand, config: RuleConfig, check: CheckInput | None = None) -> DiceResult:
    if command.expression:
        output = _worker({"expression": command.expression})
        dice = ", ".join(str(item["value"]) for item in output["rands"])
        reason = f" · {command.reason}" if command.reason else ""
        trace = f" [{dice}]" if dice else ""
        return DiceResult(f"{command.expression.upper()}{trace} = {output['total']}{reason}", output)
    if check is None:
        raise DiceError("dice_check_missing")
    target = min(9999, max(0, check.value + command.adjustment))
    # BCDice 将普通 CC<=0 解释成未指定目标。用 1 取得同样的大成功／大失败边界和骰迹，
    # 再用人物真实的 0 点阈值判断普通、困难和极难成功。
    output = _worker({"target": max(1, target), "modifier": command.modifier, "difficulty": command.difficulty})
    units = [item["value"] % 10 for item in output["rands"] if item["kind"] == "normal"]
    tens = [item["value"] for item in output["rands"] if item["kind"] == "tens_d10"]
    if len(units) != 1 or len(tens) != 1 + abs(command.modifier):
        raise DiceError("dice_engine_unavailable")
    candidates = [(value + units[0]) or 100 for value in tens]
    selected = min(candidates) if command.modifier > 0 else max(candidates)
    threshold = target // _DIVISOR[command.difficulty]

    if config.custom:
        critical = selected <= config.critical_max
        fumble = selected >= config.fumble_min
    else:
        critical, fumble = output["critical"], output["fumble"]
    if critical:
        grade = "大成功"
    elif fumble:
        grade = "大失败"
    elif selected <= target // 5:
        grade = "极难成功"
    elif selected <= target // 2:
        grade = "困难成功"
    elif selected <= target:
        grade = "普通成功"
    else:
        grade = "失败"
    meets = critical or (not fumble and selected <= threshold)
    dice = f"（{'奖励' if command.modifier > 0 else '惩罚'} {abs(command.modifier)} 骰，候选 {' / '.join(map(str, candidates))}）" if command.modifier else ""
    adjustment = f"，技能 {check.value}{command.adjustment:+d}" if command.adjustment else ""
    failed_requirement = f"，未达到{_DIFFICULTY_NAME[command.difficulty]}要求" if command.difficulty and not meets else ""
    text = (f"{check.actor}的{check.label}检定{dice}：{selected} / {threshold}"
            f"{adjustment}，{grade}{failed_requirement}。")
    return DiceResult(text, {**output, "critical": critical, "fumble": fumble, "candidates": candidates,
                             "selected": selected, "grade": grade, "target": target,
                             "threshold": threshold, "meets_requirement": meets})
