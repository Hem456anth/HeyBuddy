"""Thread-safe "may I?" prompts for tool calls (run a command, write a file).

Tools run on worker threads; Qt widgets live on the UI thread. `approve()`
can be called from anywhere: it marshals a request to the UI thread, shows
a small always-on-top dialog, and blocks the calling worker until the user
answers (or 120 s pass, which counts as "no").
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QApplication, QMessageBox

from ..utils.logger import get_logger

log = get_logger(__name__)

APPROVAL_TIMEOUT_SECONDS = 120


class _Request:
    def __init__(self, title: str, detail: str) -> None:
        self.title = title
        self.detail = detail
        self.answer = False
        self.done = threading.Event()


class ApprovalBroker(QObject):
    _requested = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self._requested.connect(self._show, Qt.ConnectionType.QueuedConnection)
        # Tests / self-check can force an answer without a dialog.
        self.auto_answer: bool | None = None

    def approve(self, title: str, detail: str) -> bool:
        if self.auto_answer is not None:
            log.info("Approval %r auto-answered %s", title, self.auto_answer)
            return self.auto_answer
        request = _Request(title, detail)
        app = QApplication.instance()
        if app is not None and QThread.currentThread() == app.thread():
            self._show(request)
        else:
            self._requested.emit(request)
            request.done.wait(APPROVAL_TIMEOUT_SECONDS)
        log.info("Approval %r -> %s", title, request.answer)
        return request.answer

    @pyqtSlot(object)
    def _show(self, request: _Request) -> None:
        box = QMessageBox()
        box.setWindowTitle(f"HeyBuddy: {request.title}")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(request.title)
        box.setInformativeText(request.detail[:2000])
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        try:
            request.answer = box.exec() == QMessageBox.StandardButton.Yes
        finally:
            request.done.set()
