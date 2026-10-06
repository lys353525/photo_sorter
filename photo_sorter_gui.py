from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageOps
from PySide6.QtCore import QDate, QObject, QSettings, QSize, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QSpinBox,
    QSplitter,
    QStyle,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from send2trash import send2trash


APP_TITLE = "사진·동영상 정리 도우미"
DESTINATION_FOLDER = "_정리완료"
RECOVERY_FOLDER = "_복구보관함"
HISTORY_FILE = ".photo_sorter_last_operation.json"
THUMBNAIL_BATCH_SIZE = 18
THUMBNAIL_SIZE = QSize(132, 100)

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif", ".dng", ".cr2", ".nef", ".arw",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mts",
    ".m2ts", ".3gp", ".webm", ".mpg", ".mpeg",
}


class OperationCancelled(Exception):
    pass


@dataclass
class MediaItem:
    path: Path
    kind: str  # "사진" or "동영상"
    year: str
    month: str
    date_source: str


@dataclass
class ScanResult:
    root: Path
    items: list[MediaItem]
    duplicates: list[Path]
    stats: dict[str, dict[str, dict[str, int]]]
    similar_groups: list[list[Path]]
    unsupported_count: int
    error_messages: list[str]

    @property
    def photo_count(self) -> int:
        return sum(1 for item in self.items if item.kind == "사진")

    @property
    def video_count(self) -> int:
        return sum(1 for item in self.items if item.kind == "동영상")

    @property
    def total_count(self) -> int:
        return len(self.items)

    @property
    def metadata_missing_count(self) -> int:
        return sum(1 for item in self.items if item.date_source in {"파일 날짜", "미확인"})

    @property
    def similar_photo_count(self) -> int:
        unique_paths = {path for group in self.similar_groups for path in group}
        return len(unique_paths)


@dataclass
class OperationRecord:
    root: Path
    run_id: str
    moves: list[tuple[Path, Path]] = field(default_factory=list)  # original -> destination
    quarantined: list[tuple[Path, Path]] = field(default_factory=list)  # original -> recovery
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "run_id": self.run_id,
            "created_at": self.created_at,
            "moves": [[str(a), str(b)] for a, b in self.moves],
            "quarantined": [[str(a), str(b)] for a, b in self.quarantined],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "OperationRecord":
        return cls(
            root=Path(data["root"]),
            run_id=str(data.get("run_id", "unknown")),
            created_at=str(data.get("created_at", "")),
            moves=[(Path(a), Path(b)) for a, b in data.get("moves", [])],
            quarantined=[(Path(a), Path(b)) for a, b in data.get("quarantined", [])],
        )


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
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            if not exif:
                return None
            for tag in (36867, 36868, 306):  # DateTimeOriginal, Digitized, DateTime
                dt = parse_exif_datetime(exif.get(tag))
                if dt:
                    return dt
    except Exception:
        return None
    return None


def video_embedded_datetime(path: Path) -> Optional[datetime]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format_tags=creation_time:stream_tags=creation_time",
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
        raw = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def file_creation_datetime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_ctime)


def detected_media_datetime(path: Path, kind: str) -> tuple[Optional[datetime], str]:
    if kind == "사진":
        dt = image_taken_datetime(path)
        if dt:
            return dt, "EXIF"
    else:
        dt = video_embedded_datetime(path)
        if dt:
            return dt, "영상 메타데이터"
    return None, "미확인"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def difference_hash(path: Path) -> int:
    """64-bit dHash. 가까운 값일수록 시각적으로 비슷한 이미지다."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("L")
        image = image.resize((9, 8), Image.Resampling.LANCZOS)
        pixels = list(image.getdata())
    result = 0
    bit = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            if pixels[offset + col] > pixels[offset + col + 1]:
                result |= 1 << bit
            bit += 1
    return result


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class BKTree:
    class Node:
        def __init__(self, value: int, index: int):
            self.value = value
            self.indices = [index]
            self.children: dict[int, "BKTree.Node"] = {}

    def __init__(self) -> None:
        self.root: Optional[BKTree.Node] = None

    def insert(self, value: int, index: int) -> None:
        if self.root is None:
            self.root = self.Node(value, index)
            return
        node = self.root
        while True:
            distance = hamming_distance(value, node.value)
            if distance == 0:
                node.indices.append(index)
                return
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = self.Node(value, index)
                return
            node = child

    def search(self, value: int, max_distance: int) -> list[int]:
        if self.root is None:
            return []
        found: list[int] = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            distance = hamming_distance(value, node.value)
            if distance <= max_distance:
                found.extend(node.indices)
            low = distance - max_distance
            high = distance + max_distance
            for edge, child in node.children.items():
                if low <= edge <= high:
                    stack.append(child)
        return found


class DisjointSet:
    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def find_similar_groups(hash_items: list[tuple[Path, int]], threshold: int) -> list[list[Path]]:
    if len(hash_items) < 2:
        return []
    dsu = DisjointSet(len(hash_items))
    tree = BKTree()
    for index, (_, value) in enumerate(hash_items):
        for other_index in tree.search(value, threshold):
            dsu.union(index, other_index)
        tree.insert(value, index)

    groups: dict[int, list[Path]] = {}
    for index, (path, _) in enumerate(hash_items):
        groups.setdefault(dsu.find(index), []).append(path)
    result = [sorted(group, key=lambda p: str(p).lower()) for group in groups.values() if len(group) >= 2]
    result.sort(key=lambda group: (-len(group), str(group[0]).lower()))
    return result


def collect_candidate_files(root: Path, recursive: bool) -> tuple[list[Path], int]:
    candidates: list[Path] = []
    unsupported = 0
    destination = (root / DESTINATION_FOLDER).resolve()

    if recursive:
        for current_root, dirs, files in os.walk(root):
            current = Path(current_root)
            dirs[:] = [d for d in dirs if (current / d).resolve() != destination]
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


def recalculate_stats(items: list[MediaItem]) -> dict[str, dict[str, dict[str, int]]]:
    stats: dict[str, dict[str, dict[str, int]]] = {"사진": {}, "동영상": {}}
    for item in items:
        stats.setdefault(item.kind, {}).setdefault(item.year, {}).setdefault(item.month, 0)
        stats[item.kind][item.year][item.month] += 1
    return stats


def scan_folder(
    root: Path,
    recursive: bool,
    remove_duplicates: bool,
    find_similar: bool,
    similarity_threshold: int,
    use_file_date_fallback: bool,
    cancel_event: threading.Event,
    progress: Optional[ProgressCallback] = None,
) -> ScanResult:
    candidates, unsupported = collect_candidate_files(root, recursive)
    total = max(len(candidates), 1)
    seen_photo_hashes: dict[str, Path] = {}
    duplicates: list[Path] = []
    items: list[MediaItem] = []
    similar_hashes: list[tuple[Path, int]] = []
    errors: list[str] = []

    for index, path in enumerate(candidates, start=1):
        if cancel_event.is_set():
            raise OperationCancelled("사용자가 분석을 취소했습니다.")

        kind = classify_media(path)
        if kind is None:
            continue

        try:
            if remove_duplicates and kind == "사진":
                digest = sha256_file(path)
                if digest in seen_photo_hashes:
                    duplicates.append(path)
                    if progress:
                        progress(int(index / total * 90), f"중복 사진 확인: {path.name}")
                    continue
                seen_photo_hashes[digest] = path

            dt, date_source = detected_media_datetime(path, kind)
            if dt is None and use_file_date_fallback:
                dt = file_creation_datetime(path)
                date_source = "파일 날짜"

            if dt is None:
                year, month = "날짜 미확인", "--"
            else:
                year, month = f"{dt.year:04d}", f"{dt.month:02d}"

            items.append(MediaItem(path=path, kind=kind, year=year, month=month, date_source=date_source))

            if find_similar and kind == "사진":
                try:
                    similar_hashes.append((path, difference_hash(path)))
                except Exception:
                    # RAW/HEIC 등 Pillow가 디코딩하지 못하는 형식은 정리 대상에는 남기되 유사도 분석만 제외한다.
                    pass
        except Exception as exc:
            errors.append(f"{path}: {exc}")

        if progress:
            progress(int(index / total * 90), f"분석 중: {path.name}")

    if cancel_event.is_set():
        raise OperationCancelled("사용자가 분석을 취소했습니다.")

    similar_groups: list[list[Path]] = []
    if find_similar and similar_hashes:
        if progress:
            progress(92, "유사 사진 그룹을 계산하는 중...")
        similar_groups = find_similar_groups(similar_hashes, similarity_threshold)

    if progress:
        progress(100, "미리보기 생성 완료")

    return ScanResult(
        root=root,
        items=items,
        duplicates=duplicates,
        stats=recalculate_stats(items),
        similar_groups=similar_groups,
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


def history_path(root: Path) -> Path:
    return root / DESTINATION_FOLDER / HISTORY_FILE


def save_operation_record(record: OperationRecord) -> None:
    path = history_path(record.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def load_operation_record(root: Path) -> Optional[OperationRecord]:
    path = history_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return OperationRecord.from_dict(data)
    except Exception:
        return None


def remove_operation_record(root: Path) -> None:
    try:
        history_path(root).unlink(missing_ok=True)
    except Exception:
        pass


def execute_plan(
    result: ScanResult,
    remove_duplicates: bool,
    cancel_event: threading.Event,
    progress: Optional[ProgressCallback] = None,
) -> tuple[OperationRecord, int, int, list[str], bool]:
    errors: list[str] = []
    quarantined_count = 0
    moved = 0
    cancelled = False
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    record = OperationRecord(root=result.root, run_id=run_id)

    duplicate_total = len(result.duplicates) if remove_duplicates else 0
    total_steps = max(duplicate_total + len(result.items), 1)
    current_step = 0

    if remove_duplicates:
        recovery_base = result.root / DESTINATION_FOLDER / RECOVERY_FOLDER / run_id / "duplicates"
        for path in result.duplicates:
            if cancel_event.is_set():
                cancelled = True
                break
            try:
                if path.exists():
                    try:
                        relative = path.relative_to(result.root)
                    except ValueError:
                        relative = Path(path.name)
                    stash_folder = recovery_base / relative.parent
                    stash_folder.mkdir(parents=True, exist_ok=True)
                    stash_path = next_available_path(stash_folder, relative.name)
                    shutil.move(str(path), str(stash_path))
                    record.quarantined.append((path, stash_path))
                    quarantined_count += 1
            except Exception as exc:
                errors.append(f"중복 파일 보관 실패 - {path}: {exc}")
            current_step += 1
            if progress:
                progress(int(current_step / total_steps * 100), f"중복 사진 정리: {path.name}")

    base = result.root / DESTINATION_FOLDER

    if not cancelled:
        for item in result.items:
            if cancel_event.is_set():
                cancelled = True
                break
            try:
                if not item.path.exists():
                    raise FileNotFoundError("미리보기 이후 파일이 이동되었거나 삭제되었습니다.")
                target_folder = base / item.kind / item.year / item.month
                target_folder.mkdir(parents=True, exist_ok=True)
                target_path = next_available_path(target_folder, item.path.name)
                original_path = item.path
                shutil.move(str(original_path), str(target_path))
                record.moves.append((original_path, target_path))
                moved += 1
            except Exception as exc:
                errors.append(f"이동 실패 - {item.path}: {exc}")

            current_step += 1
            if progress:
                progress(int(current_step / total_steps * 100), f"정리 중: {item.path.name}")

    save_operation_record(record)
    if progress:
        progress(100 if not cancelled else int(current_step / total_steps * 100), "정리 완료" if not cancelled else "정리 취소됨")
    return record, moved, quarantined_count, errors, cancelled


def undo_operation(
    record: OperationRecord,
    cancel_event: threading.Event,
    progress: Optional[ProgressCallback] = None,
) -> tuple[int, int, list[str], bool]:
    errors: list[str] = []
    restored_moves = 0
    restored_duplicates = 0
    cancelled = False
    operations = [("move", a, b) for a, b in reversed(record.moves)] + [
        ("duplicate", a, b) for a, b in reversed(record.quarantined)
    ]
    total = max(len(operations), 1)

    for index, (kind, original, current) in enumerate(operations, start=1):
        if cancel_event.is_set():
            cancelled = True
            break
        try:
            if not current.exists():
                raise FileNotFoundError(f"현재 파일을 찾을 수 없습니다: {current}")
            if original.exists():
                raise FileExistsError(f"원래 위치에 같은 이름의 파일이 이미 있습니다: {original}")
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current), str(original))
            if kind == "move":
                restored_moves += 1
                try:
                    record.moves.remove((original, current))
                except ValueError:
                    pass
            else:
                restored_duplicates += 1
                try:
                    record.quarantined.remove((original, current))
                except ValueError:
                    pass
        except Exception as exc:
            errors.append(f"되돌리기 실패 - {current}: {exc}")
        if progress:
            progress(int(index / total * 100), f"되돌리는 중: {original.name}")

    # 성공한 항목은 작업 기록에서 즉시 제거한다. 취소되거나 일부 실패해도 남은 항목은 다시 되돌릴 수 있다.
    if record.moves or record.quarantined:
        save_operation_record(record)
    else:
        remove_operation_record(record.root)
        recovery_run = record.root / DESTINATION_FOLDER / RECOVERY_FOLDER / record.run_id
        try:
            if recovery_run.exists():
                shutil.rmtree(recovery_run)
        except Exception:
            pass

    if progress:
        progress(100 if not cancelled else 0, "되돌리기 완료" if not cancelled else "되돌리기 취소됨")
    return restored_moves, restored_duplicates, errors, cancelled


def purge_recovery(root: Path) -> tuple[int, list[str]]:
    recovery = root / DESTINATION_FOLDER / RECOVERY_FOLDER
    if not recovery.exists():
        return 0, []
    count = 0
    errors: list[str] = []
    for child in list(recovery.iterdir()):
        try:
            send2trash(str(child))
            count += 1
        except Exception as exc:
            errors.append(f"{child}: {exc}")
    return count, errors


class ScanWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)

    def __init__(
        self,
        root: Path,
        recursive: bool,
        remove_duplicates: bool,
        find_similar: bool,
        similarity_threshold: int,
        use_file_date_fallback: bool,
    ):
        super().__init__()
        self.root = root
        self.recursive = recursive
        self.remove_duplicates = remove_duplicates
        self.find_similar = find_similar
        self.similarity_threshold = similarity_threshold
        self.use_file_date_fallback = use_file_date_fallback
        self.cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            result = scan_folder(
                self.root,
                self.recursive,
                self.remove_duplicates,
                self.find_similar,
                self.similarity_threshold,
                self.use_file_date_fallback,
                self.cancel_event,
                lambda pct, msg: self.progress.emit(pct, msg),
            )
            self.finished.emit(result)
        except OperationCancelled as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class ExecuteWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object, int, int, object, bool)
    failed = Signal(str)

    def __init__(self, result: ScanResult, remove_duplicates: bool):
        super().__init__()
        self.result = result
        self.remove_duplicates = remove_duplicates
        self.cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            record, moved, quarantined, errors, cancelled = execute_plan(
                self.result,
                self.remove_duplicates,
                self.cancel_event,
                lambda pct, msg: self.progress.emit(pct, msg),
            )
            self.finished.emit(record, moved, quarantined, errors, cancelled)
        except Exception as exc:
            self.failed.emit(str(exc))


class UndoWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(int, int, object, bool)
    failed = Signal(str)

    def __init__(self, record: OperationRecord):
        super().__init__()
        self.record = record
        self.cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            moved, duplicates, errors, cancelled = undo_operation(
                self.record,
                self.cancel_event,
                lambda pct, msg: self.progress.emit(pct, msg),
            )
            self.finished.emit(moved, duplicates, errors, cancelled)
        except Exception as exc:
            self.failed.emit(str(exc))


class StatCard(QFrame):
    def __init__(self, title: str, value: str = "0") -> None:
        super().__init__()
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(3)
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
        self.last_operation: Optional[OperationRecord] = None
        self.busy_worker: Optional[object] = None
        self.scan_thread: Optional[QThread] = None
        self.scan_worker: Optional[ScanWorker] = None
        self.execute_thread: Optional[QThread] = None
        self.execute_worker: Optional[ExecuteWorker] = None
        self.undo_thread: Optional[QThread] = None
        self.undo_worker: Optional[UndoWorker] = None
        self.thumbnail_queue: list[QListWidgetItem] = []
        self.thumbnail_timer = QTimer(self)
        self.thumbnail_timer.setInterval(30)
        self.thumbnail_timer.timeout.connect(self._load_thumbnail_batch)
        self.settings = QSettings("OpenAIExamples", "PhotoVideoSorter")

        self.setWindowTitle(APP_TITLE)
        self.resize(1280, 850)
        self.setMinimumSize(1040, 720)
        self.setAcceptDrops(True)
        self._build_ui()
        saved_theme = self.settings.value("theme", "파스텔")
        index = self.theme_combo.findText(str(saved_theme))
        if index >= 0:
            self.theme_combo.setCurrentIndex(index)
        self._apply_style()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(13)

        title_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        subtitle = QLabel("날짜별 자동 정리 · 썸네일 미리보기 · 유사/중복 탐지 · 수동 날짜 보정 · 안전한 되돌리기")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        title_row.addLayout(title_box, 1)
        theme_label = QLabel("테마")
        theme_label.setObjectName("note")
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["파스텔", "다크 파스텔"])
        self.theme_combo.currentTextChanged.connect(self.change_theme)
        title_row.addWidget(theme_label)
        title_row.addWidget(self.theme_combo)
        root.addLayout(title_row)

        folder_card = QFrame()
        folder_card.setObjectName("card")
        folder_layout = QVBoxLayout(folder_card)
        folder_layout.setContentsMargins(16, 14, 16, 14)
        folder_layout.setSpacing(9)
        folder_label = QLabel("정리할 폴더  ·  폴더를 창으로 드래그해 놓아도 됩니다")
        folder_label.setObjectName("sectionTitle")
        folder_layout.addWidget(folder_label)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        self.folder_edit.setPlaceholderText("폴더를 선택하거나 이 창으로 드래그하세요")
        browse_button = QPushButton("폴더 선택")
        browse_button.clicked.connect(self.choose_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse_button)
        folder_layout.addLayout(folder_row)

        option_row = QHBoxLayout()
        self.recursive_checkbox = QCheckBox("하위 폴더 포함")
        self.recursive_checkbox.setChecked(True)
        self.duplicate_checkbox = QCheckBox("완전 중복 사진 정리")
        self.similar_checkbox = QCheckBox("유사 사진 탐지")
        self.similar_checkbox.setChecked(True)
        self.filedate_checkbox = QCheckBox("메타데이터 없으면 파일 날짜 사용")
        self.filedate_checkbox.setChecked(True)
        self.similarity_spin = QSpinBox()
        self.similarity_spin.setRange(2, 16)
        self.similarity_spin.setValue(7)
        self.similarity_spin.setSuffix(" 단계")
        self.similarity_spin.setToolTip("낮을수록 거의 같은 사진만, 높을수록 더 넓게 유사 사진으로 묶습니다.")
        for widget in (self.recursive_checkbox, self.duplicate_checkbox, self.similar_checkbox, self.filedate_checkbox):
            widget.toggled.connect(self.invalidate_preview)
        self.similarity_spin.valueChanged.connect(self.invalidate_preview)
        option_row.addWidget(self.recursive_checkbox)
        option_row.addWidget(self.duplicate_checkbox)
        option_row.addWidget(self.similar_checkbox)
        option_row.addWidget(QLabel("유사도"))
        option_row.addWidget(self.similarity_spin)
        option_row.addWidget(self.filedate_checkbox)
        option_row.addStretch(1)
        folder_layout.addLayout(option_row)
        root.addWidget(folder_card)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(9)
        self.photo_card = StatCard("사진")
        self.video_card = StatCard("동영상")
        self.duplicate_card = StatCard("완전 중복")
        self.similar_card = StatCard("유사 사진")
        self.missing_card = StatCard("날짜 보정 필요")
        self.total_card = StatCard("정리 대상")
        for card in (self.photo_card, self.video_card, self.duplicate_card, self.similar_card, self.missing_card, self.total_card):
            stats_row.addWidget(card)
        root.addLayout(stats_row)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        tabs_card = QFrame()
        tabs_card.setObjectName("card")
        tabs_layout = QVBoxLayout(tabs_card)
        tabs_layout.setContentsMargins(12, 12, 12, 12)
        self.tabs = QTabWidget()
        tabs_layout.addWidget(self.tabs)

        tree_page = QWidget()
        tree_layout = QVBoxLayout(tree_page)
        tree_layout.setContentsMargins(5, 5, 5, 5)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["폴더", "파일 수"])
        self.tree.header().resizeSection(0, 400)
        self.tree.header().resizeSection(1, 90)
        tree_layout.addWidget(self.tree)
        self.tabs.addTab(tree_page, "폴더 트리")

        gallery_page = QWidget()
        gallery_layout = QVBoxLayout(gallery_page)
        gallery_layout.setContentsMargins(5, 5, 5, 5)
        gallery_controls = QHBoxLayout()
        self.gallery_filter = QComboBox()
        self.gallery_filter.addItems(["전체", "사진", "동영상", "날짜 보정 필요", "유사 사진"])
        self.gallery_filter.currentTextChanged.connect(self.refresh_gallery)
        self.gallery_info = QLabel("미리보기를 생성하면 썸네일이 표시됩니다.")
        self.gallery_info.setObjectName("note")
        self.manual_date = QDateEdit(QDate.currentDate())
        self.manual_date.setCalendarPopup(True)
        self.manual_date.setDisplayFormat("yyyy-MM-dd")
        self.manual_date_button = QPushButton("선택 파일 날짜 지정")
        self.manual_date_button.clicked.connect(self.apply_manual_date)
        gallery_controls.addWidget(QLabel("표시"))
        gallery_controls.addWidget(self.gallery_filter)
        gallery_controls.addWidget(self.gallery_info, 1)
        gallery_controls.addWidget(self.manual_date)
        gallery_controls.addWidget(self.manual_date_button)
        gallery_layout.addLayout(gallery_controls)

        self.gallery = QListWidget()
        self.gallery.setViewMode(QListWidget.IconMode)
        self.gallery.setIconSize(THUMBNAIL_SIZE)
        self.gallery.setResizeMode(QListWidget.Adjust)
        self.gallery.setMovement(QListWidget.Static)
        self.gallery.setSpacing(8)
        self.gallery.setSelectionMode(QListWidget.ExtendedSelection)
        self.gallery.setWordWrap(True)
        gallery_layout.addWidget(self.gallery)
        self.tabs.addTab(gallery_page, "썸네일")

        similar_page = QWidget()
        similar_layout = QVBoxLayout(similar_page)
        similar_layout.setContentsMargins(5, 5, 5, 5)
        similar_note = QLabel("유사 사진은 자동 삭제하지 않습니다. 촬영 장면이 비슷한 파일을 검토용으로만 묶습니다.")
        similar_note.setObjectName("note")
        similar_layout.addWidget(similar_note)
        self.similar_tree = QTreeWidget()
        self.similar_tree.setHeaderLabels(["그룹 / 파일", "위치"])
        self.similar_tree.header().resizeSection(0, 260)
        similar_layout.addWidget(self.similar_tree)
        self.tabs.addTab(similar_page, "유사 사진")

        splitter.addWidget(tabs_card)

        log_card = QFrame()
        log_card.setObjectName("card")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(14, 12, 14, 14)
        log_title = QLabel("작업 로그")
        log_title.setObjectName("sectionTitle")
        log_layout.addWidget(log_title)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("분석/정리/되돌리기 진행 상황이 표시됩니다.")
        log_layout.addWidget(self.log)
        splitter.addWidget(log_card)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)

        bottom_row = QHBoxLayout()
        note = QLabel("정리 위치: _정리완료 / 사진·동영상 / YYYY / MM  ·  중복은 _복구보관함에 보관 후 필요 시 비우기")
        note.setObjectName("note")
        bottom_row.addWidget(note)
        bottom_row.addItem(QSpacerItem(18, 18, QSizePolicy.Expanding, QSizePolicy.Minimum))

        self.purge_button = QPushButton("복구보관함 비우기")
        self.purge_button.clicked.connect(self.purge_recovery_bin)
        self.undo_button = QPushButton("마지막 작업 되돌리기")
        self.undo_button.clicked.connect(self.start_undo)
        self.cancel_button = QPushButton("작업 취소")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_current_work)
        self.preview_button = QPushButton("미리보기 생성")
        self.preview_button.setObjectName("secondaryButton")
        self.preview_button.clicked.connect(self.start_scan)
        self.execute_button = QPushButton("정리 실행")
        self.execute_button.setObjectName("primaryButton")
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self.start_execute)
        bottom_row.addWidget(self.purge_button)
        bottom_row.addWidget(self.undo_button)
        bottom_row.addWidget(self.cancel_button)
        bottom_row.addWidget(self.preview_button)
        bottom_row.addWidget(self.execute_button)
        root.addLayout(bottom_row)
        self._update_history_buttons()

    def _apply_style(self) -> None:
        self.setFont(QFont("Malgun Gothic", 10))
        dark = self.theme_combo.currentText() == "다크 파스텔"
        if dark:
            css = """
            QMainWindow, QWidget { background: #252833; color: #E7E8EE; }
            QLabel#title { font-size: 25px; font-weight: 700; color: #F4F1FF; }
            QLabel#subtitle, QLabel#note { color: #AAB0C0; }
            QFrame#card, QFrame#statCard { background: #303440; border: 1px solid #424857; border-radius: 14px; }
            QLabel#sectionTitle { font-size: 13px; font-weight: 700; color: #E2E5EF; }
            QLabel#statTitle { color: #AEB5C4; font-size: 11px; }
            QLabel#statValue { color: #E9E1FF; font-size: 20px; font-weight: 700; }
            QLineEdit, QTextEdit, QTreeWidget, QListWidget, QComboBox, QSpinBox, QDateEdit {
                background: #292D37; color: #E6E8F0; border: 1px solid #484E5D; border-radius: 9px; padding: 7px;
            }
            QTabWidget::pane { border: 1px solid #454B59; border-radius: 9px; }
            QTabBar::tab { background: #343946; color: #BFC5D2; padding: 8px 14px; margin-right: 2px; border-radius: 7px; }
            QTabBar::tab:selected { background: #5B5572; color: #FFFFFF; }
            QTreeWidget::item:selected, QListWidget::item:selected { background: #5A5570; color: #FFFFFF; }
            QHeaderView::section { background: #3A3F4C; color: #D6DAE5; padding: 7px; border: none; font-weight: 700; }
            QPushButton { background: #444A59; color: #ECEEF4; border: none; border-radius: 9px; padding: 9px 14px; font-weight: 700; }
            QPushButton:hover { background: #51586A; }
            QPushButton:disabled { background: #353944; color: #737987; }
            QPushButton#primaryButton { background: #788BBE; color: #101522; }
            QPushButton#secondaryButton { background: #6E9B82; color: #101A14; }
            QPushButton#dangerButton { background: #9B6F78; color: #1E1013; }
            QProgressBar { background: #3A3E49; border: none; border-radius: 7px; text-align: center; height: 14px; color: #E8EAF1; }
            QProgressBar::chunk { background: #769985; border-radius: 7px; }
            QCheckBox { spacing: 6px; color: #D1D5DF; }
            """
        else:
            css = """
            QMainWindow, QWidget { background: #F7F5FB; color: #3F4554; }
            QLabel#title { font-size: 25px; font-weight: 700; color: #3C4250; }
            QLabel#subtitle, QLabel#note { color: #7F8695; }
            QFrame#card, QFrame#statCard { background: #FFFFFF; border: 1px solid #E8E4F0; border-radius: 14px; }
            QLabel#sectionTitle { font-size: 13px; font-weight: 700; color: #555D6E; }
            QLabel#statTitle { color: #868C99; font-size: 11px; }
            QLabel#statValue { color: #465067; font-size: 20px; font-weight: 700; }
            QLineEdit, QTextEdit, QTreeWidget, QListWidget, QComboBox, QSpinBox, QDateEdit {
                background: #FCFBFE; color: #3F4554; border: 1px solid #E3DFF0; border-radius: 9px; padding: 7px;
            }
            QTabWidget::pane { border: 1px solid #E5E0ED; border-radius: 9px; }
            QTabBar::tab { background: #F0ECF8; color: #697083; padding: 8px 14px; margin-right: 2px; border-radius: 7px; }
            QTabBar::tab:selected { background: #DED3F1; color: #424957; }
            QTreeWidget::item:selected, QListWidget::item:selected { background: #EAE3FA; color: #3F4554; }
            QHeaderView::section { background: #F0ECF8; color: #697083; padding: 7px; border: none; font-weight: 700; }
            QPushButton { background: #EEEAF6; color: #51596A; border: none; border-radius: 9px; padding: 9px 14px; font-weight: 700; }
            QPushButton:hover { background: #E4DDF1; }
            QPushButton:disabled { background: #ECECF0; color: #A7AAB1; }
            QPushButton#primaryButton { background: #A9BCEB; color: #27334D; }
            QPushButton#secondaryButton { background: #DDEEDB; color: #405A42; }
            QPushButton#dangerButton { background: #F0D3D9; color: #74434D; }
            QProgressBar { background: #ECE8F3; border: none; border-radius: 7px; text-align: center; height: 14px; color: #5D6474; }
            QProgressBar::chunk { background: #B7DCC5; border-radius: 7px; }
            QCheckBox { spacing: 6px; color: #606777; }
            """
        self.setStyleSheet(css)

    @Slot(str)
    def change_theme(self, theme: str) -> None:
        self.settings.setValue("theme", theme)
        self._apply_style()

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(Path(url.toLocalFile()).is_dir() for url in urls):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_dir():
                self.set_folder(path)
                event.acceptProposedAction()
                return
        event.ignore()

    @Slot()
    def invalidate_preview(self) -> None:
        if self.scan_result is None:
            return
        self.scan_result = None
        self.execute_button.setEnabled(False)
        self.tree.clear()
        self.similar_tree.clear()
        self.gallery.clear()
        self.thumbnail_queue.clear()
        self.thumbnail_timer.stop()
        self._clear_stats()
        self.progress.setValue(0)
        self._append_log("옵션이 변경되어 미리보기를 다시 생성해야 합니다.")

    def choose_folder(self) -> None:
        if self.busy_worker is not None:
            QMessageBox.information(self, APP_TITLE, "현재 작업이 끝난 뒤 폴더를 변경해 주세요.")
            return
        folder = QFileDialog.getExistingDirectory(self, "정리할 폴더 선택")
        if folder:
            self.set_folder(Path(folder))

    def set_folder(self, folder: Path) -> None:
        if self.busy_worker is not None:
            self.statusBar().showMessage("작업 중에는 폴더를 변경할 수 없습니다.")
            return
        folder = folder.resolve()
        self.folder_edit.setText(str(folder))
        self.scan_result = None
        self.execute_button.setEnabled(False)
        self.tree.clear()
        self.similar_tree.clear()
        self.gallery.clear()
        self.thumbnail_queue.clear()
        self.thumbnail_timer.stop()
        self._clear_stats()
        self.log.clear()
        self.progress.setValue(0)
        self.last_operation = load_operation_record(folder)
        self._update_history_buttons()
        self._append_log(f"폴더 선택: {folder}")
        if self.last_operation:
            self._append_log("이 폴더에서 되돌릴 수 있는 마지막 작업 기록을 찾았습니다.")

    def _clear_stats(self) -> None:
        for card in (self.photo_card, self.video_card, self.duplicate_card, self.similar_card, self.missing_card, self.total_card):
            card.set_value("0")

    def _append_log(self, text: str) -> None:
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    def _set_busy(self, busy: bool, worker: Optional[object] = None) -> None:
        self.busy_worker = worker if busy else None
        self.preview_button.setEnabled(not busy)
        self.execute_button.setEnabled((not busy) and self.scan_result is not None)
        self.undo_button.setEnabled((not busy) and self.last_operation is not None)
        self.purge_button.setEnabled(not busy and self._recovery_has_items())
        self.cancel_button.setEnabled(busy)
        self.manual_date_button.setEnabled(not busy)

    def _recovery_has_items(self) -> bool:
        text = self.folder_edit.text().strip()
        if not text:
            return False
        recovery = Path(text) / DESTINATION_FOLDER / RECOVERY_FOLDER
        try:
            return recovery.exists() and any(recovery.iterdir())
        except Exception:
            return False

    def _update_history_buttons(self) -> None:
        self.undo_button.setEnabled(self.last_operation is not None and self.busy_worker is None)
        self.purge_button.setEnabled(self._recovery_has_items() and self.busy_worker is None)

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
        self.similar_tree.clear()
        self.gallery.clear()
        self.thumbnail_queue.clear()
        self.thumbnail_timer.stop()
        self._clear_stats()
        self.progress.setValue(0)
        self._append_log("미리보기 분석을 시작합니다.")

        thread = QThread(self)
        worker = ScanWorker(
            root=root,
            recursive=self.recursive_checkbox.isChecked(),
            remove_duplicates=self.duplicate_checkbox.isChecked(),
            find_similar=self.similar_checkbox.isChecked(),
            similarity_threshold=self.similarity_spin.value(),
            use_file_date_fallback=self.filedate_checkbox.isChecked(),
        )
        self.scan_thread = thread
        self.scan_worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_scan_finished)
        worker.failed.connect(self.on_worker_failed)
        worker.cancelled.connect(self.on_worker_cancelled)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_scan_worker)
        self._set_busy(True, worker)
        thread.start()

    @Slot(int, str)
    def on_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(max(0, min(100, percent)))
        self.statusBar().showMessage(message)

    @Slot(object)
    def on_scan_finished(self, result: ScanResult) -> None:
        self.scan_result = result
        self._set_busy(False)
        self._populate_tree(result)
        self._populate_similar_tree(result)
        self.photo_card.set_value(f"{result.photo_count:,}")
        self.video_card.set_value(f"{result.video_count:,}")
        self.duplicate_card.set_value(f"{len(result.duplicates):,}")
        self.similar_card.set_value(f"{result.similar_photo_count:,}")
        self.missing_card.set_value(f"{result.metadata_missing_count:,}")
        self.total_card.set_value(f"{result.total_count:,}")
        self.refresh_gallery()

        self._append_log(
            f"미리보기 완료: 사진 {result.photo_count:,}개, 동영상 {result.video_count:,}개, "
            f"완전 중복 {len(result.duplicates):,}개, 유사 사진 {result.similar_photo_count:,}개"
        )
        if result.metadata_missing_count:
            self._append_log(f"촬영 메타데이터가 없는 파일 {result.metadata_missing_count:,}개는 썸네일 탭에서 날짜를 수동 지정할 수 있습니다.")
        if result.unsupported_count:
            self._append_log(f"지원하지 않는 파일 {result.unsupported_count:,}개는 제외했습니다.")
        if result.error_messages:
            self._append_log(f"분석 오류 {len(result.error_messages):,}건")
            for msg in result.error_messages[:15]:
                self._append_log(f"  - {msg}")
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
            for year in sorted(years, key=lambda y: (y == "날짜 미확인", y)):
                months = years[year]
                year_item = QTreeWidgetItem([year, f"{sum(months.values()):,}"])
                year_item.setExpanded(True)
                kind_item.addChild(year_item)
                for month in sorted(months):
                    year_item.addChild(QTreeWidgetItem([month, f"{months[month]:,}"]))
        self.tree.expandToDepth(2)

    def _populate_similar_tree(self, result: ScanResult) -> None:
        self.similar_tree.clear()
        for index, group in enumerate(result.similar_groups, start=1):
            parent = QTreeWidgetItem([f"그룹 {index} · {len(group)}개", ""])
            parent.setExpanded(index <= 5)
            self.similar_tree.addTopLevelItem(parent)
            for path in group:
                try:
                    relative = path.relative_to(result.root)
                except ValueError:
                    relative = path
                child = QTreeWidgetItem([path.name, str(relative.parent)])
                child.setData(0, Qt.UserRole, str(path))
                parent.addChild(child)

    def refresh_gallery(self) -> None:
        self.thumbnail_timer.stop()
        self.thumbnail_queue.clear()
        self.gallery.clear()
        result = self.scan_result
        if result is None:
            self.gallery_info.setText("미리보기를 생성하면 썸네일이 표시됩니다.")
            return

        filter_name = self.gallery_filter.currentText()
        similar_paths = {path for group in result.similar_groups for path in group}
        selected_items: list[MediaItem] = []
        for item in result.items:
            include = (
                filter_name == "전체"
                or filter_name == item.kind
                or (filter_name == "날짜 보정 필요" and item.date_source in {"파일 날짜", "미확인"})
                or (filter_name == "유사 사진" and item.path in similar_paths)
            )
            if include:
                selected_items.append(item)

        placeholder = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        for media in selected_items:
            text = f"{media.path.name}\n{media.year}/{media.month} · {media.date_source}"
            item = QListWidgetItem(placeholder, text)
            item.setData(Qt.UserRole, str(media.path))
            item.setData(Qt.UserRole + 1, media.kind)
            item.setToolTip(str(media.path))
            item.setSizeHint(QSize(172, 145))
            self.gallery.addItem(item)
            if media.kind == "사진":
                self.thumbnail_queue.append(item)

        self.gallery_info.setText(f"{len(selected_items):,}개 표시 · 여러 장 선택 후 날짜를 한 번에 지정할 수 있습니다.")
        if self.thumbnail_queue:
            self.thumbnail_timer.start()

    def _load_thumbnail_batch(self) -> None:
        if not self.thumbnail_queue:
            self.thumbnail_timer.stop()
            return
        for _ in range(min(THUMBNAIL_BATCH_SIZE, len(self.thumbnail_queue))):
            list_item = self.thumbnail_queue.pop(0)
            path = Path(list_item.data(Qt.UserRole))
            if not path.exists():
                continue
            try:
                reader = QImageReader(str(path))
                reader.setAutoTransform(True)
                image = reader.read()
                if image.isNull():
                    continue
                pixmap = QPixmap.fromImage(image).scaled(
                    THUMBNAIL_SIZE,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                list_item.setIcon(QIcon(pixmap))
            except Exception:
                pass
        if not self.thumbnail_queue:
            self.thumbnail_timer.stop()

    def apply_manual_date(self) -> None:
        if self.scan_result is None:
            QMessageBox.information(self, APP_TITLE, "먼저 미리보기를 생성해 주세요.")
            return
        selected = self.gallery.selectedItems()
        if not selected:
            QMessageBox.information(self, APP_TITLE, "썸네일에서 날짜를 지정할 파일을 하나 이상 선택해 주세요.")
            return
        paths = {Path(item.data(Qt.UserRole)) for item in selected}
        qdate = self.manual_date.date()
        year, month = f"{qdate.year():04d}", f"{qdate.month():02d}"
        changed = 0
        for media in self.scan_result.items:
            if media.path in paths:
                media.year = year
                media.month = month
                media.date_source = "수동"
                changed += 1
        self.scan_result.stats = recalculate_stats(self.scan_result.items)
        self._populate_tree(self.scan_result)
        self.missing_card.set_value(f"{self.scan_result.metadata_missing_count:,}")
        self.refresh_gallery()
        self._append_log(f"수동 날짜 지정: {changed:,}개 파일 → {year}-{month}")

    def cancel_current_work(self) -> None:
        worker = self.busy_worker
        if worker is None:
            return
        request_cancel = getattr(worker, "request_cancel", None)
        if callable(request_cancel):
            request_cancel()
            self.cancel_button.setEnabled(False)
            self.statusBar().showMessage("현재 파일 처리가 끝나는 즉시 취소합니다...")
            self._append_log("작업 취소를 요청했습니다.")

    def start_execute(self) -> None:
        if self.scan_result is None:
            QMessageBox.information(self, APP_TITLE, "먼저 미리보기를 생성해 주세요.")
            return
        duplicate_text = ""
        if self.duplicate_checkbox.isChecked() and self.scan_result.duplicates:
            duplicate_text = (
                f"\n완전 중복 사진 {len(self.scan_result.duplicates):,}개는 _복구보관함으로 이동합니다. "
                "되돌리기 후 또는 확인 후 보관함을 비울 수 있습니다."
            )
        answer = QMessageBox.question(
            self,
            "정리 실행 확인",
            f"총 {self.scan_result.total_count:,}개 파일을 정리합니다.{duplicate_text}\n\n계속하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.progress.setValue(0)
        self._append_log("파일 정리를 시작합니다.")
        thread = QThread(self)
        worker = ExecuteWorker(self.scan_result, self.duplicate_checkbox.isChecked())
        self.execute_thread = thread
        self.execute_worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_execute_finished)
        worker.failed.connect(self.on_worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_execute_worker)
        self._set_busy(True, worker)
        thread.start()

    @Slot(object, int, int, object, bool)
    def on_execute_finished(self, record: OperationRecord, moved: int, quarantined: int, errors: list[str], cancelled: bool) -> None:
        self.last_operation = record
        self.scan_result = None
        self._set_busy(False)
        self._update_history_buttons()
        self.progress.setValue(100 if not cancelled else self.progress.value())
        state = "취소됨" if cancelled else "완료"
        self._append_log(f"정리 {state}: 이동 {moved:,}개, 중복 보관 {quarantined:,}개")
        if errors:
            self._append_log(f"작업 오류 {len(errors):,}건")
            for msg in errors[:20]:
                self._append_log(f"  - {msg}")
        message = (
            f"정리가 {'중간에 취소되었습니다' if cancelled else '완료되었습니다'}.\n\n"
            f"이동한 파일: {moved:,}개\n중복 보관: {quarantined:,}개\n오류: {len(errors):,}건\n\n"
            "처리된 파일은 '마지막 작업 되돌리기'로 원래 위치에 복원할 수 있습니다."
        )
        QMessageBox.information(self, APP_TITLE, message)
        self.execute_button.setEnabled(False)
        self.statusBar().showMessage(f"정리 {state}")

    def start_undo(self) -> None:
        record = self.last_operation
        if record is None:
            QMessageBox.information(self, APP_TITLE, "되돌릴 작업 기록이 없습니다.")
            return
        answer = QMessageBox.question(
            self,
            "마지막 작업 되돌리기",
            "마지막 정리 작업에서 이동한 파일과 복구보관함의 중복 파일을 원래 위치로 되돌립니다.\n\n계속하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.progress.setValue(0)
        self._append_log("마지막 작업 되돌리기를 시작합니다.")
        thread = QThread(self)
        worker = UndoWorker(record)
        self.undo_thread = thread
        self.undo_worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.on_progress)
        worker.finished.connect(self.on_undo_finished)
        worker.failed.connect(self.on_worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_undo_worker)
        self._set_busy(True, worker)
        thread.start()

    @Slot(int, int, object, bool)
    def on_undo_finished(self, moved: int, duplicates: int, errors: list[str], cancelled: bool) -> None:
        if self.last_operation is not None and not self.last_operation.moves and not self.last_operation.quarantined:
            self.last_operation = None
        self._set_busy(False)
        self._update_history_buttons()
        self._append_log(
            f"되돌리기 {'취소됨' if cancelled else '완료'}: 정리 파일 {moved:,}개, 중복 파일 {duplicates:,}개 복원"
        )
        if errors:
            for msg in errors[:20]:
                self._append_log(f"  - {msg}")
        QMessageBox.information(
            self,
            APP_TITLE,
            f"되돌리기 {'중간에 취소되었습니다' if cancelled else '완료되었습니다'}.\n\n"
            f"복원한 정리 파일: {moved:,}개\n복원한 중복 파일: {duplicates:,}개\n오류: {len(errors):,}건",
        )

    def purge_recovery_bin(self) -> None:
        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            return
        answer = QMessageBox.warning(
            self,
            "복구보관함 비우기",
            "중복 파일 복구보관함을 Windows 휴지통으로 보냅니다.\n앱의 자동 되돌리기로는 해당 중복 파일을 복원할 수 없게 됩니다.\n\n계속하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        root = Path(folder_text)
        count, errors = purge_recovery(root)
        if self.last_operation is not None:
            self.last_operation.quarantined = []
            save_operation_record(self.last_operation)
        self._update_history_buttons()
        self._append_log(f"복구보관함 비우기: {count:,}개 작업 폴더를 Windows 휴지통으로 이동")
        if errors:
            for msg in errors[:10]:
                self._append_log(f"  - {msg}")
        QMessageBox.information(self, APP_TITLE, f"복구보관함 정리가 끝났습니다.\n오류: {len(errors):,}건")

    @Slot()
    def _clear_scan_worker(self) -> None:
        self.scan_worker = None
        self.scan_thread = None

    @Slot()
    def _clear_execute_worker(self) -> None:
        self.execute_worker = None
        self.execute_thread = None

    @Slot()
    def _clear_undo_worker(self) -> None:
        self.undo_worker = None
        self.undo_thread = None

    @Slot(str)
    def on_worker_cancelled(self, message: str) -> None:
        self._set_busy(False)
        self.progress.setValue(0)
        self._append_log(message)
        self.statusBar().showMessage("작업 취소됨")

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
