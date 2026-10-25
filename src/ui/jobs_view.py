"""Jobs tab: background helpers, their progress, results and files.

HeyClicky shows running and finished helpers in its dock with their
progress lines, final reply and artifacts. HeyBuddy lists them here.
"""
from __future__ import annotations

import os
import sys
import time
import webbrowser
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core.jobs import Job, JobManager

_STATUS_LABEL = {
    "queued": "Queued", "running": "Working", "done": "Done", "failed": "Failed", "cancelled": "Stopped",
}


def open_in_shell(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        webbrowser.open(Path(path).as_uri())


class JobsView(QWidget):
    def __init__(self, jobs: JobManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.jobs = jobs
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.empty_label = QLabel("No jobs yet. Ask for something bigger, like\n\"research my competitors and make a PDF\".")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda *_: self._show_selected())
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(True)
        buttons = QHBoxLayout()
        self.open_result_btn = QPushButton("Open result")
        self.open_folder_btn = QPushButton("Open folder")
        self.stop_btn = QPushButton("Stop")
        self.remove_btn = QPushButton("Remove")
        for button in (self.open_result_btn, self.open_folder_btn, self.stop_btn, self.remove_btn):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(button)
        self.open_result_btn.clicked.connect(self._open_result)
        self.open_folder_btn.clicked.connect(self._open_folder)
        self.stop_btn.clicked.connect(self._stop)
        self.remove_btn.clicked.connect(self._remove)
        layout.addWidget(self.empty_label)
        layout.addWidget(self.list, 2)
        layout.addWidget(self.detail, 3)
        layout.addLayout(buttons)
        jobs.job_changed.connect(self.refresh, Qt.ConnectionType.QueuedConnection)
        self.refresh()

    def _selected(self) -> Job | None:
        item = self.list.currentItem()
        return self.jobs.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    @pyqtSlot()
    @pyqtSlot(object)
    def refresh(self, _job: object = None) -> None:
        selected = self._selected()
        selected_id = selected.id if selected else None
        self.list.blockSignals(True)
        self.list.clear()
        for job in self.jobs.jobs():
            item = QListWidgetItem(f"{_STATUS_LABEL.get(job.status, job.status)}  ·  {job.title}\n{job.summary_line}")
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            self.list.addItem(item)
            if job.id == selected_id:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        has_jobs = self.list.count() > 0
        self.empty_label.setVisible(not has_jobs)
        self.list.setVisible(has_jobs)
        self.detail.setVisible(has_jobs)
        self._show_selected()

    def _show_selected(self) -> None:
        job = self._selected()
        for button in (self.open_result_btn, self.open_folder_btn, self.stop_btn, self.remove_btn):
            button.setEnabled(job is not None)
        if job is None:
            self.detail.clear()
            return
        self.jobs.mark_seen(job.id)
        self.stop_btn.setEnabled(job.is_active)
        self.remove_btn.setEnabled(not job.is_active)
        self.open_result_btn.setEnabled(bool(job.artifacts))
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.created_at))
        lines = [f"<b>{job.title}</b> &nbsp; <span style='color:#8a93a3'>{started}</span>"]
        if job.reply:
            lines.append(job.reply.replace("\n", "<br>"))
        if job.error:
            lines.append(f"<span style='color:#ff6b6b'>{job.error}</span>")
        if job.artifacts:
            lines.append("<b>Files</b><br>" + "<br>".join(Path(a).name for a in job.artifacts))
        if job.progress:
            lines.append("<span style='color:#8a93a3'>" + "<br>".join(job.progress[-8:]) + "</span>")
        lines.append(f"<span style='color:#8a93a3'><b>Brief</b><br>{job.brief[:1500]}</span>")
        self.detail.setHtml("<br><br>".join(lines))

    def _open_result(self) -> None:
        job = self._selected()
        if job and job.artifacts:
            open_in_shell(job.artifacts[0])

    def _open_folder(self) -> None:
        job = self._selected()
        if job:
            open_in_shell(job.folder)

    def _stop(self) -> None:
        job = self._selected()
        if job:
            self.jobs.cancel(job.id)

    def _remove(self) -> None:
        job = self._selected()
        if job:
            self.jobs.remove(job.id)
            self.refresh()
