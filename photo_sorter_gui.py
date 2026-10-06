from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import Image
from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from send2trash import send2trash


APP_TITLE = "사진·동영상 정리 도우미"
DESTINATION_FOLDER = "_정리완료"

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".dng", ".cr2", ".nef", ".arw",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mts",
    ".m2ts", ".3gp", ".webm", ".mpg", ".mpeg",
}


@dataclass
class MediaItem:
    path: Path
    kind: str  # "사진" or "동영상"
    year: str
    month: str


@dataclass
class ScanResult:
    root: Path
    items: list[MediaItem]
    duplicates: list[Path]
    stats: dict[str, dict[str, dict[str, int]]]
    unsupported_count: int
    error_messages: list[str]

    @property
    def photo_count(self) -> int:
        return sum(
            count
            for year_data in self.stats.get("사진", {}).values()
            for count in year_data.values()
        )

    @property
    def video_count(self) -> int:
        return sum(
            count
            for year_data in self.stats.get("동영상", {}).values()
            for count in year_data.values()
        )

    @property
    def total_count(self) -> int:
        return self.photo_count + self.video_count


ProgressCallback = Callable[[int, str], None]


def classify_media(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "사진"
    if ext in VIDEO_EXTENSIONS:
        return "동영상"
    return None


def parse_exif_datetime(value: object) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def image_taken_datetime(path: Path) -> Optional[datetime]:
    """EXIF 촬영일(DateTimeOriginal 등)을 우선 사용한다."""
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None
            # 36867: DateTimeOriginal, 36868: DateTimeDigitized, 306: DateTime
            for tag in (36867, 36868, 306):
                dt = parse_exif_datetime(exif.get(tag))
                if dt:
                    return dt
    except Exception:
        return None
    return None


def video_embedded_datetime(path: Path) -> Optional[datetime]:
    """ffprobe가 설치되어 있으면 영상 컨테이너의 creation_time을 사용한다."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format_tags=creation_time",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        raw = result.stdout.strip()
        if not raw:
            return None
        # 예: 2025-07-16T04:12:30.000000Z
        normalized = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def file_creation_datetime(path: Path) -> datetime:
    """Windows에서는 생성 시각. 다른 OS에서는 ctime 기반 폴백."""
    return datetime.fromtimestamp(path.stat().st_ctime)


def media_datetime(path: Path, kind: str) -> datetime:
    if kind == "사진":
        dt = image_taken_datetime(path)
        if dt:
            return dt
    elif kind == "동영상":
        dt = video_embedded_datetime(path)
        if dt:
            return dt

    return file_creation_datetime(path)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def collect_candidate_files(root: Path, recursive: bool) -> tuple[list[Path], int]:
    candidates: list[Path] = []
    unsupported = 0
    destination = (root / DESTINATION_FOLDER).resolve()

    if recursive:
        for current_root, dirs, files in os.walk(root):
            current = Path(current_root)
            # 결과 폴더는 재검색하지 않는다.
            dirs[:] = [
                d for d in dirs
                if (current / d).resolve() != destination
            ]
            for filename in files:
                path = current / filename
                if classify_media(path):
                    candidates.append(path)
                else:
                    unsupported += 1
    else:
        for path in root.iterdir():
            if path.is_file():
                if classify_media(path):
                    candidates.append(path)
                else:
                    unsupported += 1

    candidates.sort(key=lambda p: str(p).lower())
    return candidates, unsupported


def scan_folder(
    root: Path,
    recursive: bool,
    remove_duplicates: bool,
    progress: Optional[ProgressCallback] = None,
) -> ScanResult:
    candidates, unsupported = collect_candidate_files(root, recursive)
    total = max(len(candidates), 1)
    seen_photo_hashes: dict[str, Path] = {}
    duplicates: list[Path] = []
    items: list[MediaItem] = []
    stats: dict[str, dict[str, dict[str, int]]] = {"사진": {}, "동영상": {}}
    errors: list[str] = []

    for index, path in enumerate(candidates, start=1):
        kind = classify_media(path)
        if kind is None:
            continue

        try:
            if remove_duplicates and kind == "사진":
                digest = sha256_file(path)
                if digest in seen_photo_hashes:
                    duplicates.append(path)
                    if progress:
                        pct = int(index / total * 100)
                        progress(pct, f"중복 사진 확인: {path.name}")
                    continue
                seen_photo_hashes[digest] = path

            dt = media_datetime(path, kind)
            year = f"{dt.year:04d}"
            month = f"{dt.month:02d}"
            items.append(MediaItem(path=path, kind=kind, year=year, month=month))
            stats.setdefault(kind, {}).setdefault(year, {}).setdefault(month, 0)
            stats[kind][year][month] += 1
        except Exception as exc:
            errors.append(f"{path}: {exc}")

        if progress:
            pct = int(index / total * 100)
            progress(pct, f"분석 중: {path.name}")

    if progress:
        progress(100, "미리보기 생성 완료")

    return ScanResult(
        root=root,
        items=items,
        duplicates=duplicates,
        stats=stats,
        unsupported_count=unsupported,
        error_messages=errors,
    )


def next_available_path(folder: Path, filename: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while True:
        candidate = folder / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def execute_plan(
    result: ScanResult,
    remove_duplicates: bool,
    progress: Optional[ProgressCallback] = None,
) -> tuple[int, int, list[str]]:
    errors: list[str] = []
    deleted_duplicates = 0
    moved = 0

    duplicate_total = len(result.duplicates) if remove_duplicates else 0
    total_steps = max(duplicate_total + len(result.items), 1)
    current_step = 0

    if remove_duplicates:
        for path in result.duplicates:
            try:
                if path.exists():
                    # 영구 삭제 대신 Windows 휴지통으로 보내 복구 가능하게 한다.
                    send2trash(str(path))
                    deleted_duplicates += 1
            except Exception as exc:
                errors.append(f"중복 파일 삭제 실패 - {path}: {exc}")
            current_step += 1
            if progress:
                progress(
                    int(current_step / total_steps * 100),
                    f"중복 사진 정리: {path.name}",
                )

    base = result.root / DESTINATION_FOLDER

    for item in result.items:
        try:
            if not item.path.exists():
                raise FileNotFoundError("미리보기 이후 파일이 이동되었거나 삭제되었습니다.")

            target_folder = base / item.kind / item.year / item.month
            target_folder.mkdir(parents=True, exist_ok=True)
            target_path = next_available_path(target_folder, item.path.name)
            shutil.move(str(item.path), str(target_path))
            moved += 1
        except Exception as exc:
            errors.append(f"이동 실패 - {item.path}: {exc}")

        current_step += 1
        if progress:
            progress(
                int(current_step / total_steps * 100),
                f"정리 중: {item.path.name}",
            )

    if progress:
        progress(100, "정리 완료")

    return moved, deleted_duplicates, errors


class ScanWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, root: Path, recursive: bool, remove_duplicates: bool):
        super().__init__()
        self.root = root
        self.recursive = recursive
        self.remove_duplicates = remove_duplicates

    @Slot()
    def run(self) -> None:
        try:
            result = scan_folder(
                self.root,
                self.recursive,
                self.remove_duplicates,
                lambda pct, msg: self.progress.emit(pct, msg),
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class ExecuteWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(int, int, object)
    failed = Signal(str)

    def __init__(self, result: ScanResult, remove_duplicates: bool):
        super().__init__()
        self.result = result
        self.remove_duplicates = remove_duplicates

    @Slot()
    def run(self) -> None:
        try:
            moved, deleted, errors = execute_plan(
                self.result,
                self.remove_duplicates,
                lambda pct, msg: self.progress.emit(pct, msg),
            )
            self.finished.emit(moved, deleted, errors)
        except Exception as exc:
            self.failed.emit(str(exc))


class StatCard(QFrame):
    def __init__(self, title: str, value: str = "0") -> None:
        super().__init__()
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(4)

        title_label = QLabel(title)
        title_label.setObjectName("statTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("statValue")

        layout.addWidget(title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.scan_result: Optional[ScanResult] = None
        self.scan_thread: Optional[QThread] = None
        self.scan_worker: Optional[ScanWorker] = None
        self.execute_thread: Optional[QThread] = None
        self.execute_worker: Optional[ExecuteWorker] = None

        self.setWindowTitle(APP_TITLE)
        self.resize(1000, 760)
        self.setMinimumSize(860, 650)
        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        subtitle = QLabel(
            "사진과 동영상을 날짜 기준으로 자동 분류하고, 실행 전에 결과를 미리 확인합니다."
        )
        subtitle.setObjectName("subtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        folder_card = QFrame()
        folder_card.setObjectName("card")
        folder_layout = QVBoxLayout(folder_card)
        folder_layout.setContentsMargins(18, 16, 18, 16)
        folder_layout.setSpacing(10)

        folder_label = QLabel("정리할 폴더")
        folder_label.setObjectName("sectionTitle")
        folder_layout.addWidget(folder_label)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setPlaceholderText("폴더를 선택하세요")
        browse_button = QPushButton("폴더 선택")
        browse_button.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse_button)
        folder_layout.addLayout(folder_row)

        option_row = QHBoxLayout()
        self.recursive_checkbox = QCheckBox("하위 폴더까지 포함")
        self.recursive_checkbox.setChecked(True)
        self.duplicate_checkbox = QCheckBox("중복 사진 제거 (완전히 동일한 파일만)")
        self.duplicate_checkbox.setChecked(False)
        self.recursive_checkbox.toggled.connect(self.invalidate_preview)
        self.duplicate_checkbox.toggled.connect(self.invalidate_preview)
        option_row.addWidget(self.recursive_checkbox)
        option_row.addWidget(self.duplicate_checkbox)
        option_row.addStretch(1)
        folder_layout.addLayout(option_row)

        root.addWidget(folder_card)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        self.photo_card = StatCard("사진", "0")
        self.video_card = StatCard("동영상", "0")
        self.duplicate_card = StatCard("중복 사진", "0")
        self.total_card = StatCard("정리 대상", "0")
        for card in (
            self.photo_card,
            self.video_card,
            self.duplicate_card,
            self.total_card,
        ):
            stats_row.addWidget(card)
        root.addLayout(stats_row)

        content_row = QHBoxLayout()
        content_row.setSpacing(14)

        tree_card = QFrame()
        tree_card.setObjectName("card")
        tree_layout = QVBoxLayout(tree_card)
        tree_layout.setContentsMargins(16, 14, 16, 16)
        tree_title = QLabel("정리 결과 미리보기")
        tree_title.setObjectName("sectionTitle")
        tree_layout.addWidget(tree_title)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["폴더", "파일 수"])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().resizeSection(0, 330)
        self.tree.header().resizeSection(1, 90)
        tree_layout.addWidget(self.tree)

        log_card = QFrame()
        log_card.setObjectName("card")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(16, 14, 16, 16)
        log_title = QLabel("작업 로그")
        log_title.setObjectName("sectionTitle")
        log_layout.addWidget(log_title)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("분석 및 정리 진행 상황이 여기에 표시됩니다.")
        log_layout.addWidget(self.log)

        content_row.addWidget(tree_card, 3)
        content_row.addWidget(log_card, 2)
        root.addLayout(content_row, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        root.addWidget(self.progress)

        bottom_row = QHBoxLayout()
        note = QLabel(
            "결과 폴더: _정리완료 / 사진·동영상 / YYYY / MM   ·   중복 사진은 휴지통으로 이동"
        )
        note.setObjectName("note")
        bottom_row.addWidget(note)
        bottom_row.addItem(QSpacerItem(20, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))

        self.preview_button = QPushButton("미리보기 생성")
        self.preview_button.setObjectName("secondaryButton")
        self.preview_button.clicked.connect(self.start_scan)

        self.execute_button = QPushButton("정리 실행")
        self.execute_button.setObjectName("primaryButton")
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self.start_execute)

        bottom_row.addWidget(self.preview_button)
        bottom_row.addWidget(self.execute_button)
        root.addLayout(bottom_row)

    def _apply_style(self) -> None:
        self.setFont(QFont("Malgun Gothic", 10))
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #F7F5FB;
                color: #3F4554;
            }
            QLabel#title {
                font-size: 25px;
                font-weight: 700;
                color: #3C4250;
            }
            QLabel#subtitle {
                color: #7A8190;
                margin-bottom: 2px;
            }
            QFrame#card, QFrame#statCard {
                background: #FFFFFF;
                border: 1px solid #E8E4F0;
                border-radius: 14px;
            }
            QLabel#sectionTitle {
                font-size: 13px;
                font-weight: 700;
                color: #555D6E;
            }
            QLabel#statTitle {
                color: #868C99;
                font-size: 11px;
            }
            QLabel#statValue {
                color: #465067;
                font-size: 22px;
                font-weight: 700;
            }
            QLabel#note {
                color: #8B91A0;
                font-size: 10px;
            }
            QLineEdit, QTextEdit, QTreeWidget {
                background: #FCFBFE;
                border: 1px solid #E3DFF0;
                border-radius: 9px;
                padding: 8px;
                selection-background-color: #DCCEF8;
            }
            QTreeWidget::item {
                height: 28px;
            }
            QTreeWidget::item:selected {
                background: #EAE3FA;
                color: #3F4554;
            }
            QHeaderView::section {
                background: #F0ECF8;
                color: #697083;
                padding: 7px;
                border: none;
                border-bottom: 1px solid #E1DDEC;
                font-weight: 700;
            }
            QPushButton {
                background: #EEEAF6;
                color: #51596A;
                border: none;
                border-radius: 9px;
                padding: 9px 15px;
                font-weight: 700;
            }
            QPushButton:hover {
                background: #E4DDF1;
            }
            QPushButton:disabled {
                background: #ECECF0;
                color: #A7AAB1;
            }
            QPushButton#primaryButton {
                background: #A9BCEB;
                color: #27334D;
                padding-left: 22px;
                padding-right: 22px;
            }
            QPushButton#primaryButton:hover {
                background: #98ADE3;
            }
            QPushButton#secondaryButton {
                background: #DDEEDB;
                color: #405A42;
            }
            QPushButton#secondaryButton:hover {
                background: #CEE7CB;
            }
            QCheckBox {
                spacing: 7px;
                color: #606777;
            }
            QCheckBox::indicator {
                width: 17px;
                height: 17px;
            }
            QProgressBar {
                background: #ECE8F3;
                border: none;
                border-radius: 7px;
                text-align: center;
                height: 14px;
                color: #5D6474;
            }
            QProgressBar::chunk {
                background: #B7DCC5;
                border-radius: 7px;
            }
            """
        )


    @Slot()
    def invalidate_preview(self) -> None:
        if self.scan_result is None:
            return
        self.scan_result = None
        self.execute_button.setEnabled(False)
        self.tree.clear()
        self._clear_stats()
        self.progress.setValue(0)
        self._append_log("옵션이 변경되어 미리보기를 다시 생성해야 합니다.")

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "정리할 폴더 선택")
        if not folder:
            return
        self.folder_edit.setText(folder)
        self.scan_result = None
        self.execute_button.setEnabled(False)
        self.tree.clear()
        self._clear_stats()
        self.log.clear()
        self.progress.setValue(0)
        self._append_log(f"폴더 선택: {folder}")

    def _clear_stats(self) -> None:
        self.photo_card.set_value("0")
        self.video_card.set_value("0")
        self.duplicate_card.set_value("0")
        self.total_card.set_value("0")

    def _append_log(self, text: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{timestamp}] {text}")

    def _set_busy(self, busy: bool) -> None:
        self.preview_button.setEnabled(not busy)
        self.execute_button.setEnabled((not busy) and self.scan_result is not None)

    def start_scan(self) -> None:
        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            QMessageBox.information(self, APP_TITLE, "먼저 정리할 폴더를 선택해 주세요.")
            return

        root = Path(folder_text)
        if not root.exists() or not root.is_dir():
            QMessageBox.warning(self, APP_TITLE, "선택한 폴더를 찾을 수 없습니다.")
            return

        self.scan_result = None
        self.tree.clear()
        self._clear_stats()
        self.progress.setValue(0)
        self._set_busy(True)
        self._append_log("미리보기 분석을 시작합니다.")

        self.scan_thread = QThread(self)
        self.scan_worker = ScanWorker(
            root=root,
            recursive=self.recursive_checkbox.isChecked(),
            remove_duplicates=self.duplicate_checkbox.isChecked(),
        )
        self.scan_worker.moveToThread(self.scan_thread)
        self.scan_thread.started.connect(self.scan_worker.run)
        self.scan_worker.progress.connect(self.on_progress)
        self.scan_worker.finished.connect(self.on_scan_finished)
        self.scan_worker.failed.connect(self.on_worker_failed)
        self.scan_worker.finished.connect(self.scan_thread.quit)
        self.scan_worker.failed.connect(self.scan_thread.quit)
        self.scan_thread.finished.connect(self.scan_worker.deleteLater)
        self.scan_thread.finished.connect(self.scan_thread.deleteLater)
        self.scan_thread.start()

    @Slot(int, str)
    def on_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(percent)
        if percent in (0, 100) or percent % 10 == 0:
            self.statusBar().showMessage(message)

    @Slot(object)
    def on_scan_finished(self, result: ScanResult) -> None:
        self.scan_result = result
        self._set_busy(False)
        self._populate_tree(result)
        self.photo_card.set_value(f"{result.photo_count:,}")
        self.video_card.set_value(f"{result.video_count:,}")
        self.duplicate_card.set_value(f"{len(result.duplicates):,}")
        self.total_card.set_value(f"{result.total_count:,}")

        self._append_log(
            f"미리보기 완료: 사진 {result.photo_count:,}개, "
            f"동영상 {result.video_count:,}개, 중복 사진 {len(result.duplicates):,}개"
        )
        if result.unsupported_count:
            self._append_log(f"지원하지 않는 파일 {result.unsupported_count:,}개는 제외했습니다.")
        if result.error_messages:
            self._append_log(f"분석 오류 {len(result.error_messages):,}건이 있습니다.")
            for msg in result.error_messages[:20]:
                self._append_log(f"  - {msg}")
            if len(result.error_messages) > 20:
                self._append_log("  - 그 외 오류는 생략되었습니다.")

        self.progress.setValue(100)
        self.statusBar().showMessage("미리보기 준비 완료")

    def _populate_tree(self, result: ScanResult) -> None:
        self.tree.clear()
        root_item = QTreeWidgetItem([DESTINATION_FOLDER, f"{result.total_count:,}"])
        root_item.setExpanded(True)
        self.tree.addTopLevelItem(root_item)

        for kind in ("사진", "동영상"):
            years = result.stats.get(kind, {})
            kind_total = sum(sum(months.values()) for months in years.values())
            kind_item = QTreeWidgetItem([kind, f"{kind_total:,}"])
            kind_item.setExpanded(True)
            root_item.addChild(kind_item)

            for year in sorted(years):
                months = years[year]
                year_total = sum(months.values())
                year_item = QTreeWidgetItem([year, f"{year_total:,}"])
                year_item.setExpanded(True)
                kind_item.addChild(year_item)

                for month in sorted(months):
                    month_item = QTreeWidgetItem([month, f"{months[month]:,}"])
                    year_item.addChild(month_item)

        self.tree.expandToDepth(2)

    def start_execute(self) -> None:
        if self.scan_result is None:
            QMessageBox.information(self, APP_TITLE, "먼저 미리보기를 생성해 주세요.")
            return

        duplicate_text = ""
        if self.duplicate_checkbox.isChecked() and self.scan_result.duplicates:
            duplicate_text = (
                f"\n중복 사진 {len(self.scan_result.duplicates):,}개는 "
                "휴지통으로 이동합니다."
            )

        answer = QMessageBox.question(
            self,
            "정리 실행 확인",
            f"총 {self.scan_result.total_count:,}개 파일을 정리합니다."
            f"{duplicate_text}\n\n계속하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.progress.setValue(0)
        self._set_busy(True)
        self._append_log("파일 정리를 시작합니다.")

        self.execute_thread = QThread(self)
        self.execute_worker = ExecuteWorker(
            result=self.scan_result,
            remove_duplicates=self.duplicate_checkbox.isChecked(),
        )
        self.execute_worker.moveToThread(self.execute_thread)
        self.execute_thread.started.connect(self.execute_worker.run)
        self.execute_worker.progress.connect(self.on_progress)
        self.execute_worker.finished.connect(self.on_execute_finished)
        self.execute_worker.failed.connect(self.on_worker_failed)
        self.execute_worker.finished.connect(self.execute_thread.quit)
        self.execute_worker.failed.connect(self.execute_thread.quit)
        self.execute_thread.finished.connect(self.execute_worker.deleteLater)
        self.execute_thread.finished.connect(self.execute_thread.deleteLater)
        self.execute_thread.start()

    @Slot(int, int, object)
    def on_execute_finished(self, moved: int, deleted: int, errors: list[str]) -> None:
        self._set_busy(False)
        self.progress.setValue(100)
        self._append_log(f"정리 완료: 이동 {moved:,}개, 중복 정리 {deleted:,}개")

        if errors:
            self._append_log(f"작업 오류 {len(errors):,}건")
            for msg in errors[:30]:
                self._append_log(f"  - {msg}")
            if len(errors) > 30:
                self._append_log("  - 그 외 오류는 생략되었습니다.")

        QMessageBox.information(
            self,
            APP_TITLE,
            f"정리가 완료되었습니다.\n\n"
            f"이동한 파일: {moved:,}개\n"
            f"중복 사진 정리: {deleted:,}개\n"
            f"오류: {len(errors):,}건",
        )

        # 실행 후 같은 계획을 재사용하지 못하도록 초기화한다.
        self.scan_result = None
        self.execute_button.setEnabled(False)
        self.statusBar().showMessage("정리 완료")

    @Slot(str)
    def on_worker_failed(self, message: str) -> None:
        self._set_busy(False)
        self._append_log(f"오류: {message}")
        QMessageBox.critical(self, APP_TITLE, f"작업 중 오류가 발생했습니다.\n\n{message}")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
