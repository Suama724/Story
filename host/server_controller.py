"""Qt 窗口拥有的独立主机进程，停止操作只影响该子进程。"""

import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal


class ServerController(QObject):
    stateChanged = Signal(str)
    messageReceived = Signal(object)
    logReceived = Signal(str)
    errorOccurred = Signal(str)
    sendConfirmed = Signal(str)
    sendFailed = Signal(str)
    passwordChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.state = "stopped"
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(Path(__file__).resolve().parents[1]))
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.remove("STORY_CAMPAIGN_DIR")
        self.process.setProcessEnvironment(environment)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.readyReadStandardError.connect(self._read_errors)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._finished)
        self.stop_timer = QTimer(self)
        self.stop_timer.setSingleShot(True)
        self.stop_timer.timeout.connect(self._terminate)
        self._output = b""
        self._reported_error = False

    @property
    def active(self) -> bool:
        return self.state != "stopped"

    def _set_state(self, state: str) -> None:
        self.state = state
        self.stateChanged.emit(state)

    def start(self, directory: Path, host: str, port: int) -> None:
        if self.active:
            return
        self._output = b""
        self._reported_error = False
        self._set_state("starting")
        entry = ["--serve"] if getattr(sys, "frozen", False) else ["-m", "host", "serve"]
        self.process.start(sys.executable, [*entry, str(directory), "--host", host, "--port", str(port)])

    def command(self, command: str, **values: object) -> bool:
        if self.process.state() != QProcess.ProcessState.Running:
            return False
        payload = json.dumps({"command": command, **values}, ensure_ascii=False) + "\n"
        return self.process.write(payload.encode("utf-8")) >= 0

    def stop(self) -> None:
        if not self.active or self.state == "stopping":
            return
        self._set_state("stopping")
        self.command("stop")
        self.stop_timer.start(5000)

    def _terminate(self) -> None:
        pid = self.process.processId()
        self.process.terminate()

        def kill_owned_process() -> None:
            if pid and self.process.processId() == pid:
                self.process.kill()

        QTimer.singleShot(1500, kill_owned_process)

    def _read_output(self) -> None:
        self._output += bytes(self.process.readAllStandardOutput())
        while b"\n" in self._output:
            line, self._output = self._output.split(b"\n", 1)
            try:
                event = json.loads(line)
            except ValueError:
                self.logReceived.emit(line.decode("utf-8", errors="replace"))
                continue
            kind = event.get("event")
            if kind == "ready":
                if self.state != "stopping":
                    self._set_state("running")
                self.logReceived.emit(f"开始监听 {event['host']}:{event['port']}")
            elif kind == "message":
                self.messageReceived.emit(event["message"])
            elif kind == "log":
                self.logReceived.emit(event["message"])
            elif kind == "sent":
                self.sendConfirmed.emit(str(event["request_id"]))
            elif kind == "password_changed":
                self.passwordChanged.emit()
            elif kind == "error":
                self._reported_error = True
                if event.get("request_id") is not None:
                    self.sendFailed.emit(str(event["request_id"]))
                self.errorOccurred.emit(event["message"])

    def _read_errors(self) -> None:
        text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace").strip()
        if text:
            self.logReceived.emit(text)

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if self.state != "stopping":
            self.errorOccurred.emit(f"监听进程异常，{self.process.errorString()}")
        if error == QProcess.ProcessError.FailedToStart:
            self._set_state("stopped")

    def _finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        stopping = self.state == "stopping"
        self.stop_timer.stop()
        self._read_output()
        self._read_errors()
        self._set_state("stopped")
        self.logReceived.emit("监听已停止")
        if exit_code != 0 and not stopping and not self._reported_error:
            self.errorOccurred.emit(f"监听进程已退出，退出码 {exit_code}。请查看运行日志。")
