"""骰子部件的公共接口，与 Story 的界面和存储无关。"""

from .core import CheckInput, DiceCommand, DiceError, DiceResult, RuleConfig, parse_command, roll

__all__ = ["CheckInput", "DiceCommand", "DiceError", "DiceResult", "RuleConfig", "parse_command", "roll"]
