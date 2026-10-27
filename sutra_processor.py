import json
import re
import hashlib
import logging
from pathlib import Path
from datetime import datetime

import threading
scan_lock = threading.Lock()

import subprocess
import time
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import pymupdf
from docx import Document

# =========================
# 路徑設定
# =========================

# sutra_processor.py 位於 Scripture/sutra_player/
PROJECT_DIR = Path(__file__).resolve().parent
SCRIPTURE_DIR = PROJECT_DIR.parent / "經文檔"
OUTPUT_DIR = PROJECT_DIR / "data" / "sutras"
SUPPORTED_EXTENSIONS = {".txt", ".docx", ".pdf"}
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =========================
# WhisperX 自動對齊設定
# =========================

SOUND_DIR = PROJECT_DIR / "static" / "sound"
ALIGNMENT_DIR = PROJECT_DIR / "data" / "alignments"
ALIGNER_SCRIPT = PROJECT_DIR / "audio_aligner.py"

# WhisperX 使用獨立的虛擬環境
ALIGN_PYTHON = PROJECT_DIR.parent / ".venv-align" / "Scripts" / "python.exe"
ALIGNMENT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)


# =========================
# 文字處理
# =========================

def read_txt(file_path):
    """讀取 TXT，依序嘗試常見的文字編碼。"""
    encodings = ["utf-8-sig", "utf-8", "cp950", "big5"]

    for encoding in encodings:
        try:
            return file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError("無法辨識 TXT 編碼")


def read_docx(file_path):
    """讀取 Word 段落。"""
    document = Document(file_path)

    paragraphs = [
        paragraph.text.strip()
        for paragraph in document.paragraphs
        if paragraph.text.strip()
    ]

    return "\n".join(paragraphs)


def read_pdf(file_path):
    """讀取 PDF 中可選取的文字。"""
    pages = []

    with pymupdf.open(file_path) as document:
        for page in document:
            text = page.get_text("text")
            if text.strip():
                pages.append(text)

    if not pages:
        raise ValueError(
            "PDF 沒有可擷取的文字，可能是掃描圖片，需要 OCR"
        )

    return "\n".join(pages)


def extract_text(file_path):
    """依檔案格式選擇讀取方式。"""
    extension = file_path.suffix.lower()

    if extension == ".txt":
        return read_txt(file_path)

    if extension == ".docx":
        return read_docx(file_path)

    if extension == ".pdf":
        return read_pdf(file_path)

    raise ValueError(f"不支援的檔案格式：{extension}")


def split_paragraphs(text):
    """
    先保留原始換行段落，再依中文標點切分較長的段落。
    不使用 AI，避免改寫經文原文。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 合併同一行中不必要的連續空白，但保留換行
    lines = [
        re.sub(r"[ \t]+", " ", line).strip()
        for line in text.split("\n")
    ]

    paragraphs = []
    sentence_pattern = re.compile(r".+?(?:[。！？；]+|$)")

    for line in lines:
        if not line:
            continue

        # 保留原有段落；僅在單行過長時依標點切句
        if len(line) <= 120:
            paragraphs.append(line)
            continue

        sentences = [
            match.group().strip()
            for match in sentence_pattern.finditer(line)
            if match.group().strip()
        ]

        paragraphs.extend(sentences)

    return paragraphs


# =========================
# 快取與背景處理
# =========================

def get_output_path(file_path):
    """產生穩定且不易重名的 JSON 檔名。"""
    relative_path = file_path.relative_to(SCRIPTURE_DIR)
    identity = str(relative_path).lower()

    digest = hashlib.sha256(
        identity.encode("utf-8")
    ).hexdigest()[:10]

    safe_name = re.sub(
        r'[<>:"/\\|?*]', "_", file_path.stem
    ).strip(" .")

    safe_name = safe_name or "sutra"

    return OUTPUT_DIR / f"{safe_name}-{digest}.json"


def is_cache_valid(file_path, output_path):
    """檢查原始檔案是否與已儲存的解析結果一致。"""
    if not output_path.exists():
        return False

    try:
        data = json.loads(
            output_path.read_text(encoding="utf-8")
        )

        stat = file_path.stat()
        source = data.get("source", {})

        return (
            source.get("mtime_ns") == stat.st_mtime_ns
            and source.get("size") == stat.st_size
        )

    except (OSError, ValueError, json.JSONDecodeError):
        return False


def find_audio_for_sutra(sutra):
    """依經文 JSON 的資訊尋找對應 MP3。"""
    candidates = []

    # 優先使用 JSON 中明確記錄的音檔名稱
    for key in ("audio_file", "audio_filename"):
        filename = sutra.get(key)
        if isinstance(filename, str) and filename.strip():
            candidates.append(filename.strip())

    # 依原始文件名稱尋找，例如 大悲咒.docx -> 大悲咒.mp3
    source = sutra.get("source", {})
    if isinstance(source, dict):
        source_filename = source.get("filename", "")
        if source_filename:
            candidates.append(f"{Path(source_filename).stem}.mp3")

    # 依經文標題尋找
    title = sutra.get("title", "")
    if title:
        candidates.append(f"{title}.mp3")

    # 去除重複候選名稱，並確認檔案存在
    for filename in dict.fromkeys(candidates):
        audio_path = SOUND_DIR / filename

        if audio_path.is_file():
            return audio_path

    return None

# 自動配對與對齊函式
def ensure_alignment(json_path):
    """確認經文是否需要 WhisperX 對齊；需要時才執行。"""
    try:
        if not json_path.is_file():
            return "missing_json"

        with json_path.open("r", encoding="utf-8") as file:
            sutra = json.load(file)

        sutra_id = sutra.get("id")
        if not sutra_id:
            logger.error("經文 JSON 缺少 id：%s", json_path.name)
            return "failed"

        audio_path = find_audio_for_sutra(sutra)

        if audio_path is None:
            logger.info(
                "尚無對應 MP3，暫不對齊：%s",
                sutra.get("title", json_path.stem)
            )
            return "waiting_audio"

        alignment_path = ALIGNMENT_DIR / (
            f"{json_path.stem}-alignment.json"
        )

        # 已有對齊檔，而且比經文 JSON、MP3 都新，就不重跑
        if alignment_path.is_file():
            alignment_mtime = alignment_path.stat().st_mtime
            latest_input_mtime = max(
                json_path.stat().st_mtime,
                audio_path.stat().st_mtime
            )

            if alignment_mtime >= latest_input_mtime:
                logger.info(
                    "對齊結果已是最新：%s",
                    alignment_path.name
                )
                return "aligned"

        if not ALIGN_PYTHON.is_file():
            logger.error(
                "找不到 WhisperX Python：%s",
                ALIGN_PYTHON
            )
            return "failed"

        if not ALIGNER_SCRIPT.is_file():
            logger.error(
                "找不到對齊程式：%s",
                ALIGNER_SCRIPT
            )
            return "failed"

        logger.info(
            "開始 WhisperX 對齊：%s（音檔：%s）",
            sutra.get("title", sutra_id),
            audio_path.name
        )

        result = subprocess.run(
            [
                str(ALIGN_PYTHON),
                str(ALIGNER_SCRIPT),
                "--sutra-id",
                str(sutra_id),
            ],
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False
        )

        if result.stdout.strip():
            logger.info("WhisperX 輸出：\n%s", result.stdout.strip())

        if result.returncode != 0:
            logger.error(
                "WhisperX 執行失敗（代碼 %s）：\n%s",
                result.returncode,
                result.stderr.strip()
            )
            return "failed"

        # 確認程式確實產生對齊檔
        if not alignment_path.is_file():
            logger.error(
                "WhisperX 已結束，但找不到對齊檔：%s",
                alignment_path
            )
            return "failed"

        logger.info("逐字對齊完成：%s", alignment_path.name)
        return "aligned"

    except Exception:
        logger.exception("自動對齊發生錯誤：%s", json_path.name)
        return "failed"

def process_file(file_path):
    """處理單一本經文，並確認逐字對齊狀態。"""
    output_path = get_output_path(file_path)

    if is_cache_valid(file_path, output_path):
        logger.info("略過未變更檔案：%s", file_path.name)

        alignment_status = ensure_alignment(output_path)
        logger.info(
            "經文對齊狀態：%s -> %s",
            file_path.name,
            alignment_status
        )
        return "skipped"

    try:
        text = extract_text(file_path)
        paragraphs = split_paragraphs(text)

        if not paragraphs:
            raise ValueError("文件沒有可用的經文文字")

        stat = file_path.stat()

        data = {
            "id": output_path.stem,
            "title": file_path.stem,
            "source": {
                "filename": file_path.name,
                "path": str(file_path.relative_to(SCRIPTURE_DIR)),
                "format": file_path.suffix.lower(),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size
            },
            "paragraphs": paragraphs,
            "paragraph_count": len(paragraphs),
            "processed_at": datetime.now().isoformat(
                timespec="seconds"
            )
        }

        # 先寫入暫存檔，再替換正式檔案
        temp_path = output_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        temp_path.replace(output_path)

        logger.info(
            "處理完成：%s，共 %s 段",
            file_path.name,
            len(paragraphs)
        )

        alignment_status = ensure_alignment(output_path)
        logger.info(
            "經文對齊狀態：%s -> %s",
            file_path.name,
            alignment_status
        )

        return "processed"

    except Exception:
        logger.exception("處理失敗：%s", file_path.name)
        return "failed"




def cleanup_deleted_sutras():
    """清除原始經文已不存在的 JSON 快取及對應的逐字對齊資料。"""
    if not SCRIPTURE_DIR.is_dir():
        logger.warning(
            "經文資料夾不存在，為避免誤刪，跳過 JSON 清理：%s",
            SCRIPTURE_DIR
        )
        return 0

    deleted_count = 0
    deleted_alignment_count = 0

    for json_file in OUTPUT_DIR.glob("*.json"):
        try:
            with json_file.open("r", encoding="utf-8") as file:
                data = json.load(file)

            source = data.get("source", {})
            relative_path = source.get("path")

            # 只清理由本處理器產生、具有原始路徑資訊的 JSON
            if not relative_path:
                continue

            source_path = (SCRIPTURE_DIR / relative_path).resolve()

            # 確認路徑仍在經文資料夾內，避免誤刪其他位置的資料
            try:
                source_path.relative_to(SCRIPTURE_DIR.resolve())
            except ValueError:
                logger.warning(
                    "原始路徑超出經文資料夾，跳過清理：%s",
                    json_file.name
                )
                continue

            # 原始經文不存在時，刪除 JSON 及對應的 alignment
            if not source_path.is_file():
                sutra_id = data.get("id", json_file.stem)

                # alignment 檔名格式：<經文 JSON 檔名>-alignment.json
                alignment_file = ALIGNMENT_DIR / (
                    f"{json_file.stem}-alignment.json"
                )

                # 先刪除對齊檔；若失敗，保留經文 JSON 供下次重試
                if alignment_file.exists():
                    try:
                        alignment_file.unlink()
                        deleted_alignment_count += 1
                        logger.info(
                            "已清除失效的逐字對齊 JSON：%s",
                            alignment_file.name
                        )
                    except OSError:
                        logger.exception(
                            "刪除逐字對齊 JSON 失敗：%s",
                            alignment_file.name
                        )
                        continue

                json_file.unlink()
                deleted_count += 1

                logger.info(
                    "已清除失效的經文 JSON：%s（ID：%s）",
                    json_file.name,
                    sutra_id
                )

        except (OSError, json.JSONDecodeError):
            logger.exception(
                "清理經文 JSON 時發生錯誤：%s",
                json_file.name
            )

    logger.info(
        "清理完成：經文 JSON 刪除 %s 個，逐字對齊 JSON 刪除 %s 個",
        deleted_count,
        deleted_alignment_count
    )
    return deleted_count

def scan_and_process():
    """掃描經文資料夾，預處理文件並清除失效快取。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not SCRIPTURE_DIR.is_dir():
        logger.error(
            "找不到經文資料夾：%s",
            SCRIPTURE_DIR
        )
        return {
            "found": 0,
            "processed": 0,
            "skipped": 0,
            "failed": 0,
            "deleted": 0
        }

    files = sorted(
        (
            path
            for path in SCRIPTURE_DIR.rglob("*")
            if path.is_file()
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
            and not path.name.startswith("~$")
        ),
        key=lambda path: str(path).lower()
    )

    results = {
        "found": len(files),
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "deleted": 0
    }

    for file_path in files:
        status = process_file(file_path)

        if status in results:
            results[status] += 1

    # 所有現存文件處理完畢後，再清除失效 JSON
    results["deleted"] = cleanup_deleted_sutras()

    logger.info("掃描結果：%s", results)
    return results


def safe_scan_and_process():
    """避免同一時間重複執行掃描與 WhisperX。"""
    if not scan_lock.acquire(blocking=False):
        logger.info("已有掃描流程執行中，略過本次重複觸發")
        return None

    try:
        return scan_and_process()
    finally:
        scan_lock.release()

# ===== 經文資料夾即時監控 =====
class SutraFolderHandler(FileSystemEventHandler):
    """偵測經文資料夾的變動，延遲後重新掃描。"""

    def __init__(self):
        self.timer = None
        self.lock = threading.Lock()

    def on_created(self, event):
        self.schedule_scan()

    def on_modified(self, event):
        self.schedule_scan()

    def on_deleted(self, event):
        self.schedule_scan()

    def on_moved(self, event):
        self.schedule_scan()

    def schedule_scan(self):
        """合併短時間內連續發生的事件，避免重複掃描。"""
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()

            self.timer = threading.Timer(1.5, self.run_scan)
            self.timer.daemon = True
            self.timer.start()

    def run_scan(self):
        try:
            print("\n偵測到經文資料夾變動，開始更新……")
            result = safe_scan_and_process()
            print(f"經文資料更新完成：{result}")
        except Exception as error:
            print(f"經文資料更新失敗：{error}")

def start_sutra_watcher():
    """啟動經文資料夾的背景監控。"""
    if not SCRIPTURE_DIR.exists():
        print(f"找不到經文資料夾：{SCRIPTURE_DIR}")
        return None

    event_handler = SutraFolderHandler()
    observer = Observer()
    observer.schedule(
        event_handler,
        str(SCRIPTURE_DIR),
        recursive=True
    )
    observer.start()

    print(f"經文即時監控已啟動：{SCRIPTURE_DIR}")
    return observer


if __name__ == "__main__":
    scan_and_process()
