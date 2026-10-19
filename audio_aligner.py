
import argparse
import json
import re
from pathlib import Path
from datetime import datetime

import torch
import whisperx


PROJECT_DIR = Path(__file__).resolve().parent
ROOT_DIR = PROJECT_DIR.parent

SOUND_DIR = ROOT_DIR / "sound"
SUTRA_DIR = PROJECT_DIR / "data" / "sutras"
ALIGNMENT_DIR = PROJECT_DIR / "data" / "alignments"

DEVICE = "cpu"
LANGUAGE_CODE = "zh"

IGNORED_CHARS = set(
    " \t\n\r"
    "，。！？、；：,.!?;:"
    "「」『』（）()《》〈》…—-"
)


def normalize_text(text):
    """移除標點與空白，供對齊結果比對使用。"""
    return re.sub(
        r"""[\s，。！？、；：,.!?;:「」『』（）()《》〈〉…—-]""",
        "",
        text,
    )


def find_sutra_json(sutra_id):
    """依經文 ID 尋找 JSON，避免固定讀取心經。"""
    if not SUTRA_DIR.exists():
        raise FileNotFoundError(f"找不到經文資料夾：{SUTRA_DIR}")

    matches = []

    for path in sorted(SUTRA_DIR.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as file:
                data = json.load(file)

            if data.get("id") == sutra_id:
                matches.append(path)

        except (OSError, json.JSONDecodeError) as error:
            print(f"略過無法讀取的 JSON：{path.name}：{error}")

    if not matches:
        raise FileNotFoundError(f"找不到經文 ID：{sutra_id}")

    if len(matches) > 1:
        raise RuntimeError(f"同一個經文 ID 找到多份 JSON：{matches}")

    return matches[0]


def load_sutra(sutra_id):
    json_path = find_sutra_json(sutra_id)

    with json_path.open("r", encoding="utf-8") as file:
        sutra = json.load(file)

    paragraphs = sutra.get("paragraphs", [])

    if not paragraphs:
        raise ValueError("經文 JSON 沒有 paragraphs 資料")

    if not all(isinstance(p, str) for p in paragraphs):
        raise ValueError("經文 paragraphs 必須是文字陣列")

    return json_path, sutra, paragraphs


def find_audio(sutra, json_path):
    """依 JSON 資料尋找對應 MP3。"""
    candidates = []

    for key in ("audio_file", "audio_filename"):
        filename = sutra.get(key)
        if isinstance(filename, str) and filename.strip():
            candidates.append(SOUND_DIR / filename)

    source = sutra.get("source", {})
    source_filename = (
        source.get("filename", "")
        if isinstance(source, dict)
        else ""
    )

    if source_filename:
        candidates.append(
            SOUND_DIR / f"{Path(source_filename).stem}.mp3"
        )

    title = sutra.get("title", "")
    if title:
        candidates.append(SOUND_DIR / f"{title}.mp3")

    candidates.append(SOUND_DIR / f"{json_path.stem}.mp3")

    for audio_path in dict.fromkeys(candidates):
        if audio_path.is_file():
            return audio_path

    checked = "\n".join(str(p) for p in dict.fromkeys(candidates))
    raise FileNotFoundError(
        "找不到對應的 MP3 音檔。\n"
        f"已檢查：\n{checked}\n"
        "請確認音檔位於專案根目錄的 sound 資料夾。"
    )


def build_alignment(sutra_id):
    json_path, sutra, paragraphs = load_sutra(sutra_id)
    audio_path = find_audio(sutra, json_path)

    print(f"經文：{sutra.get('title', '未命名經文')}")
    print(f"經文 ID：{sutra.get('id')}")
    print(f"來源 JSON：{json_path.name}")
    print(f"音檔：{audio_path.name}")
    print(f"段落數：{len(paragraphs)}")

    if not torch.cuda.is_available():
        print("目前使用 CPU，對齊可能需要較長時間。")

    print("正在載入中文對齊模型……")
    model, metadata = whisperx.load_align_model(
        language_code=LANGUAGE_CODE,
        device=DEVICE,
    )

    print("正在讀取音檔……")
    audio = whisperx.load_audio(str(audio_path))
    audio_duration = len(audio) / 16000
    print(f"音訊實際長度：{audio_duration:.2f} 秒")

    full_text = " ".join(paragraphs)
    alignment_input = [{
        "text": full_text,
        "start": 0.0,
        "end": audio_duration,
    }]

    print("正在進行強制對齊……")
    result = whisperx.align(
        alignment_input,
        model,
        metadata,
        audio,
        DEVICE,
        return_char_alignments=True,
    )

    aligned_chars = []
    for segment in result.get("segments", []):
        aligned_chars.extend(segment.get("chars", []))

    timed_chars = [
        {
            "char": item["char"],
            "start": float(item["start"]),
            "end": float(item["end"]),
        }
        for item in aligned_chars
        if item.get("char")
        and item["char"] not in IGNORED_CHARS
        and item.get("start") is not None
        and item.get("end") is not None
    ]

    expected = normalize_text(full_text)
    actual = normalize_text("".join(item["char"] for item in timed_chars))

    print(f"原文非標點字數：{len(expected)}")
    print(f"有時間的字元數：{len(timed_chars)}")

    if expected != actual:
        raise RuntimeError(
            "對齊輸出的字元與原文不完全一致，已停止輸出，"
            "避免逐字高亮錯位。\n"
            f"原文開頭：{expected[:60]}\n"
            f"對齊開頭：{actual[:60]}"
        )

    paragraph_results = []
    cursor = 0

    for paragraph_index, paragraph in enumerate(paragraphs):
        paragraph_chars = []

        for char in paragraph:
            if char.isspace() or char in IGNORED_CHARS:
                continue

            if cursor >= len(timed_chars):
                raise RuntimeError("對齊字元數不足，已停止輸出。")

            item = timed_chars[cursor]
            paragraph_chars.append({
                "char": char,
                "start": item["start"],
                "end": item["end"],
            })
            cursor += 1

        paragraph_results.append({
            "index": paragraph_index,
            "text": paragraph,
            "start": (
                paragraph_chars[0]["start"] if paragraph_chars else None
            ),
            "end": (
                paragraph_chars[-1]["end"] if paragraph_chars else None
            ),
            "characters": paragraph_chars,
        })

    ALIGNMENT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = ALIGNMENT_DIR / f"{json_path.stem}-alignment.json"

    output = {
        "title": sutra.get("title"),
        "sutra_id": sutra.get("id"),
        "source_json": json_path.name,
        "audio_file": audio_path.name,
        "language": LANGUAGE_CODE,
        "device": DEVICE,
        "character_alignment": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "paragraph_count": len(paragraph_results),
        "paragraphs": paragraph_results,
    }

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print(f"\n對齊資料已輸出：{output_path}")
    print("請抽查前、中、後段，確認時間與實際誦經聲音相符。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="依經文 ID 產生 WhisperX 逐字對齊 JSON"
    )
    parser.add_argument(
        "--sutra-id",
        required=True,
        help="經文 JSON 中的 id，例如：大悲咒-19c12290f4",
    )

    args = parser.parse_args()
    build_alignment(args.sutra_id)
