"""KP 的 CoC7 掷骰设置。"""

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QLabel, QLayout, QSpinBox, QVBoxLayout,
)

from dice_component import RuleConfig


class DiceManagementDialog(QDialog):
    def __init__(self, parent, config: RuleConfig, save: Callable[[RuleConfig], None]):
        super().__init__(parent)
        self.setWindowTitle("骰子")
        self.setMinimumWidth(470)
        self.save = save
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        layout.setVerticalSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        self.preset = QComboBox()
        self.preset.addItem("COC7 原版", False)
        self.preset.addItem("村规", True)
        self.preset.setCurrentIndex(int(config.custom))
        form = QFormLayout()
        form.addRow("规则预设", self.preset)
        layout.addLayout(form)

        self.custom_panel = QFrame()
        self.custom_panel.setObjectName("panel")
        options = QFormLayout(self.custom_panel)
        self.critical_max = self._limit(config.critical_max)
        self.fumble_min = self._limit(config.fumble_min)
        self.critical_max.setPrefix("1 至 ")
        self.fumble_min.setSuffix(" 至 100")
        options.addRow("大成功区间(右闭):", self.critical_max)
        options.addRow("大失败区间(左闭):", self.fumble_min)
        layout.addWidget(self.custom_panel)

        self.feedback = QLabel()
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setStyleSheet("color: #a33737;")
        self.feedback.setWordWrap(True)
        self.feedback.hide()
        layout.addWidget(self.feedback)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.preset.currentIndexChanged.connect(self._update_preset)
        self.critical_max.valueChanged.connect(self.feedback.hide)
        self.fumble_min.valueChanged.connect(self.feedback.hide)
        self._update_preset()

    @staticmethod
    def _limit(value: int) -> QSpinBox:
        field = QSpinBox()
        field.setRange(1, 100)
        field.setValue(value)
        return field

    def _update_preset(self) -> None:
        self.custom_panel.setVisible(bool(self.preset.currentData()))
        self.feedback.hide()

    def accept(self) -> None:
        config = RuleConfig(custom=bool(self.preset.currentData()), critical_max=self.critical_max.value(),
                            fumble_min=self.fumble_min.value())
        try:
            self.save(config)
        except (OSError, ValueError) as error:
            self.feedback.setText(str(error))
            self.feedback.show()
            return
        super().accept()
