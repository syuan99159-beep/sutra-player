import json
import re
from pathlib import Path
from flask import Flask, render_template, abort, send_from_directory, request, jsonify
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from sutra_processor import safe_scan_and_process, start_sutra_watcher

app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
SOUND_DIR = PROJECT_DIR / "static" / "sound"
SUTRA_DATA_DIR = PROJECT_DIR / "data" / "sutras"
ALIGNMENT_DATA_DIR = PROJECT_DIR / "data" / "alignments"


def load_sutra(sutra_id):
    """依照經文 ID 載入對應 JSON。"""
    for json_file in SUTRA_DATA_DIR.glob("*.json"):
        try:
            with json_file.open("r", encoding="utf-8") as file:
                data = json.load(file)

            if data.get("id") == sutra_id:
                return data

        except (OSError, json.JSONDecodeError) as error:
            app.logger.warning(
                "讀取經文資料失敗 %s：%s",
                json_file.name,
                error
            )

    return None

# =========================
# AI 經文檢索：讀取所有已收錄經書
# =========================

def load_all_sutras():
    """讀取 data/sutras/ 中所有有效的經書 JSON。"""
    sutras = []

    if not SUTRA_DATA_DIR.exists():
        app.logger.warning("經書資料夾不存在：%s", SUTRA_DATA_DIR)
        return sutras

    for json_file in sorted(SUTRA_DATA_DIR.glob("*.json")):
        try:
            with json_file.open("r", encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, dict):
                continue

            paragraphs = data.get("paragraphs", [])
            if not isinstance(paragraphs, list):
                continue

            # 只保留確實存在的文字段落
            paragraphs = [
                paragraph.strip()
                for paragraph in paragraphs
                if isinstance(paragraph, str) and paragraph.strip()
            ]

            if not paragraphs:
                continue

            sutras.append({
                "id": str(data.get("id", json_file.stem)),
                "title": str(data.get("title", json_file.stem)),
                "paragraphs": paragraphs,
            })

        except (OSError, json.JSONDecodeError) as error:
            app.logger.warning(
                "經文檢索無法讀取 %s：%s",
                json_file.name,
                error,
            )

    return sutras


def _normalize_sutra_text(text):
    """移除標點與空白，方便比對中文經文。"""
    return re.sub(r"[\W_]+", "", text or "", flags=re.UNICODE).lower()


def _make_text_ngrams(text):
    """建立文字的二字詞集合，適用於沒有空格分詞的中文。"""
    normalized = _normalize_sutra_text(text)

    if len(normalized) <= 2:
        return {normalized} if normalized else set()

    return {
        normalized[index:index + 2]
        for index in range(len(normalized) - 1)
    }


def retrieve_sutra_passages(
    query,
    current_sutra_id=None,
    paragraph_index=None,
    max_results=6,
):
    """
    從所有已收錄經書中檢索真實段落。

    current_sutra_id：目前閱讀的經書 ID。
    paragraph_index：目前閱讀段落的索引，從 0 開始。
    回傳內容只取自實際 JSON，不生成或改寫原文。
    """
    sutras = load_all_sutras()
    if not sutras:
        return []

    query_normalized = _normalize_sutra_text(query)
    query_ngrams = _make_text_ngrams(query)
    results = []

    current_sutra = None
    if current_sutra_id:
        current_sutra = next(
            (
                sutra for sutra in sutras
                if sutra["id"] == str(current_sutra_id)
            ),
            None,
        )

    # 目前段落只能從伺服器載入的經書 JSON 取得，
    # 不接受前端自行傳入的經文文字。
    current_paragraph = None
    if (
        current_sutra
        and isinstance(paragraph_index, int)
        and not isinstance(paragraph_index, bool)
        and 0 <= paragraph_index < len(current_sutra["paragraphs"])
    ):
        current_paragraph = current_sutra["paragraphs"][paragraph_index]

    for sutra in sutras:
        title = sutra["title"]
        sutra_id = sutra["id"]

        for index, paragraph in enumerate(sutra["paragraphs"]):
            paragraph_normalized = _normalize_sutra_text(paragraph)
            paragraph_ngrams = _make_text_ngrams(paragraph)

            # 二字詞重疊是基礎分數；完整詞句命中給予額外加分。
            overlap = len(query_ngrams & paragraph_ngrams)
            score = float(overlap)

            if (
                query_normalized
                and len(query_normalized) >= 2
                and query_normalized in paragraph_normalized
            ):
                score += 8

            # 經書標題出現在問題中時，稍微提高該經書的排序。
            if title and title in query:
                score += 5

            # 閱讀指定經書時，讓該經書段落更容易進入候選結果。
            if current_sutra and sutra_id == current_sutra_id:
                score += 2

            is_current_paragraph = (
                current_sutra
                and sutra_id == current_sutra_id
                and current_paragraph is not None
                and index == paragraph_index
            )

            if is_current_paragraph:
                score += 10

            # 沒有文字重疊的段落不會被當成直接命中。
            if score <= 0:
                continue

            results.append({
                "sutra_id": sutra_id,
                "title": title,
                "paragraph_index": index,
                "text": paragraph,
                "score": score,
                "is_current_paragraph": bool(is_current_paragraph),
                "is_direct_match": bool(overlap > 0),
            })

    results.sort(
        key=lambda item: (
            item["is_current_paragraph"],
            item["score"],
        ),
        reverse=True,
    )

    # 限制傳給 Gemini 的原文數量，避免提示內容過長。
    return results[:max_results]

# 照經文 ID 尋找對應檔案
def load_alignment(sutra_id):
    """依照經文 ID 載入 WhisperX 逐字對齊資料。"""
    alignment_file = (
        ALIGNMENT_DATA_DIR / f"{sutra_id}-alignment.json"
    )

    if not alignment_file.is_file():
        app.logger.warning(
            "找不到經文對齊資料：%s",
            alignment_file.name
        )
        return None

    try:
        with alignment_file.open("r", encoding="utf-8") as file:
            data = json.load(file)

        return data

    except (OSError, json.JSONDecodeError) as error:
        app.logger.warning(
            "讀取經文對齊資料失敗 %s：%s",
            alignment_file.name,
            error
        )
        return None
    
def find_audio(sutra):
    """依照經文資料尋找對應的 MP3 音檔。"""
    candidates = []

    # 優先使用經文 JSON 明確指定的音檔名稱
    for key in ("audio_file", "audio_filename"):
        filename = sutra.get(key)
        if isinstance(filename, str) and filename.strip():
            candidates.append(filename)

    # 依照來源文件名稱尋找，例如：大悲咒.docx -> 大悲咒.mp3
    source = sutra.get("source", {})
    if isinstance(source, dict):
        source_filename = source.get("filename", "")
        if source_filename:
            candidates.append(
                f"{Path(source_filename).stem}.mp3"
            )

    # 依照經文標題尋找，例如：大悲咒 -> 大悲咒.mp3
    title = sutra.get("title", "")
    if title:
        candidates.append(f"{title}.mp3")

    # 依序檢查音檔是否存在
    for filename in dict.fromkeys(candidates):
        audio_path = SOUND_DIR / filename

        if audio_path.is_file():
            return filename

    return None

@app.route("/")
def home():
    """顯示經文播放器首頁。"""
    sutras = []

    if SUTRA_DATA_DIR.exists():
        for json_file in sorted(SUTRA_DATA_DIR.glob("*.json")):
            try:
                with json_file.open("r", encoding="utf-8") as file:
                    data = json.load(file)

                sutras.append({
                    "id": data.get("id", json_file.stem),
                    "title": data.get("title", json_file.stem),
                    "url": f"/sutras/{data.get('id', json_file.stem)}"
                })

            except (OSError, json.JSONDecodeError) as error:
                app.logger.warning(
                    "讀取經文清單失敗 %s：%s",
                    json_file.name,
                    error
                )

    return render_template("index.html", sutras=sutras)

@app.route("/sutras/<sutra_id>")
def sutra_detail(sutra_id):
    """顯示指定經文的閱讀頁面。"""
    sutra = load_sutra(sutra_id)

    if sutra is None:
        abort(404)

    alignment = load_alignment(sutra_id)
    audio_filename = find_audio(sutra)

    if audio_filename is None:
        app.logger.warning(
            "找不到經文對應的 MP3：%s",
            sutra.get("title", sutra_id)
        )

    return render_template(
        "heart_sutra.html",
        sutra=sutra,
        alignment=alignment,
        audio_filename=audio_filename
    )

@app.route("/sound/<path:filename>")
def serve_sound(filename):
    """提供真人誦經音檔。"""
    if not filename.lower().endswith(".mp3"):
        abort(404)

    sound_path = SOUND_DIR / filename
    if not sound_path.is_file():
        abort(404)

    return send_from_directory(SOUND_DIR, filename)



# =========================
# AI 人生顧問：Gemini API
# =========================

# AI 共通回覆長度規則
AI_RESPONSE_LENGTH_RULE = (
    "【回覆長度與表達規範】"
    "1. 一般問題以 80～150 個中文字為目標，通常使用 3～5 句話。"
    "2. 開頭直接回答問題，不要使用冗長的寒暄或重述問題。"
    "3. 優先提供最重要的結論與 1～3 個重點，避免重複說明相同內容。"
    "4. 依照目前選定的模式回答，不要為了補充內容而延伸到無關領域。"
    "5. 除非使用者要求詳細說明，否則不要主動提供過多背景、案例或延伸知識。"
    "6. 使用者要求深入分析、比較或完整步驟時，可以適度增加篇幅。"
    "7. 不得為了縮短篇幅而省略重要資訊、必要步驟或關鍵風險。"
    "8. 使用繁體中文，語氣自然、清楚、容易理解。"
)

AI_MODES = {
    "sutra": {
        "name": "經文智慧",
        "instruction": (
            "你是「經文智慧」助理。請以佛教經典與佛法思想為主要參考，"
            "用溫和、尊重、不說教的繁體中文回答，並盡量連結到日常生活。"
            "若不確定經文出處，不要捏造經文或引用。你不是宗教權威，"
            "也不要把個人推論說成經典原文。"
            "回答經文問題時，請遵守以下順序："
            "1. 先直接回答使用者問的字句，不要急著延伸到人生道理。"
            "2. 解釋關鍵詞的意思，再說明整句經文的語意。"
            "3. 如果使用者問「為什麼」，必須解釋前後文的邏輯關係，不能只說兩句話彼此相關。"
            "4. 區分經文原文、佛學解釋，以及應用在現代生活的推論。"
            "5. 不確定的典故、出處或教義，不得當成確定事實。"   
            "6. 如果檢索資料不足，請明確說明，不要自行補造經文。"
            "7. 使用繁體中文，回答完整、自然，避免只說到一半。"
            "8. 不要每次都用「您好，很高興和您一起探討」等制式開場。"
            "9. 回答應先簡潔清楚，再視問題補充例子。"
            "10. 保持回答的客觀性和中立性，避免個人偏見影響。"
            "共通規則"
            "1. 必須優先處理目前選定模式的核心任務。"
            "2. 不得因為問題出現其他領域的詞彙，就直接改用其他模式的回答方式。"
            "3. 可以簡短提及其他領域，但不能讓其他領域取代目前模式的主要任務。"
            "4. 如果問題確實超出目前模式，先從目前模式的角度回應，必要時詢問使用者是否希望切換模式。"
            "5. 除非使用者明確要求切換，否則不要自行改變模式。"
            "特別提醒： 人際關係模式不能直接變成職涯規劃顧問；職涯發展模式也不能只用安慰或一般人生道理代替選項分析。"
            
            "跨領域問題處理規則："
            "1. 優先完成目前模式的核心任務，但不必拒絕所有其他領域的問題。"
            "2. 如果使用者提出簡單、明確的一般性問題，可以直接提供簡潔且有用的回答，"
            "不必強行將問題連結至目前模式。"
            "3. 如果問題屬於其他專業領域且需要深入分析，可以先簡短說明目前能提供的協助，"
            "再詢問使用者是否希望切換至更適合的模式。"
            "4. 不得為了維持角色而拒絕合理的一般性協助，也不得將無關問題硬套進目前模式。"
        
        ),
    },
    "relationship": {
        "name": "人際關係",
        "instruction": (
            "你是「人際關係」助理。"
            "你的首要任務是理解使用者與他人之間的互動、衝突、情緒與界線。"
            "先回應使用者當下的感受，再整理事情經過與雙方可能的立場。"
            "提供具體、自然、可以直接使用的溝通方式。"
            "不要在資訊不足時武斷判定誰對誰錯。"

            "除非使用者明確詢問經文或佛法，否則不要主動將回答轉為經文解釋、"
            "佛學名詞分析或宗教教義講解。"
            "如果佛法觀點確實有助於理解情緒、關係或衝突，可以適度補充，"
            "但不得為了引用經文而勉強連結，也不得捏造經文原文或出處。"

            "如果使用者提到工作，應先判斷問題核心是否為同事互動、"
            "主管溝通、職場衝突或人際界線；不要直接提供升學或職涯規劃。"
            "如果問題主要是升學、轉職或職涯選擇，應先從人際關係角度"
            "確認是否存在他人期待、家庭壓力或溝通問題，不直接替使用者"
            "進行完整的職涯方案分析。"

            "回答應自然、有同理心且具體。"
            "一般問題優先採用「理解感受、分析問題、具體建議」的結構，"
            "避免重複描述使用者的情緒或反覆表達同一個觀點。"
            "除非問題複雜或使用者要求詳細分析，否則提供適量的建議即可。"
            "資訊不足時，可以提出一個最重要的追問。"
            "使用繁體中文。"
            
            "跨領域問題處理規則："
            "1. 優先完成目前模式的核心任務，但不必拒絕所有其他領域的問題。"
            "2. 如果使用者提出簡單、明確的一般性問題，可以直接提供簡潔且有用的回答，"
            "不必強行將問題連結至目前模式。"
            "3. 如果問題屬於其他專業領域且需要深入分析，可以先簡短說明目前能提供的協助，"
            "再詢問使用者是否希望切換至更適合的模式。"
            "4. 不得為了維持角色而拒絕合理的一般性協助，也不得將無關問題硬套進目前模式。"

        ),
    },
    "career": {
        "name": "職涯發展",
        "instruction": (
            "你是「職涯發展」助理。"
            "你的首要任務是協助使用者釐清興趣、能力、目標、現實限制與職涯選項。"
            "回答應以實際的職涯分析為主，提供具體、合理且可執行的建議。"

            "遇到升學、就業、轉職或職涯選擇問題時，"
            "應先釐清使用者的目標與條件，再比較相關選項的優缺點、"
            "成本、收益、風險、機會成本及長期發展。"
            "應遵循「先分析、再追問」的原則。"
            "當使用者已提供部分背景時，必須先根據現有資訊提出有用的初步分析，"
            "不可因為仍有資訊未知，就只提出問題而不提供任何分析。"
            "資訊不足時，應說明目前可以判斷的部分，再詢問最關鍵的一至兩個問題。"

            "若使用者已提供學歷、科系及職涯目標，"
            "應立即比較升學與直接就業的可能優勢、成本、風險及適用情況。"
            "例如，資工系畢業生若以高薪為目標，"
            "可以先比較直接就業累積經驗與攻讀碩士爭取特定研發職位的差異，"
            "但不得在缺乏可靠資料時，將特定薪資數字或升學結果說成確定事實。"

            "每次追問前，必須先檢查使用者是否已在前文回答過相同問題。"
            "不得重複詢問已知的學歷、科系、目標或其他背景。"
            "當資訊已足以提出有意義的建議時，應直接給出初步結論、判斷依據與下一步行動，"
            "而不是持續收集資訊。"

            "除非使用者明確詢問經文或佛法，否則不要將回答轉為經文講解"
            "或佛學研究。"
            "如果佛法觀點確實有助於使用者思考價值觀、面對不確定性"
            "或釐清人生方向，可以適度補充，但不得取代實際的職涯分析，"
            "也不得為了連結佛法而勉強引用經文或捏造出處。"

            "不得只用安慰、鼓勵或一般人生道理代替選項分析。"
            "不得假裝知道使用者未曾提供的背景資訊。"
            "使用繁體中文，回答應清楚、客觀且有條理。"

            "跨領域問題處理規則："
            "1. 優先完成目前模式的核心任務，但不必拒絕所有其他領域的問題。"
            "2. 如果使用者提出簡單、明確的一般性問題，可以直接提供簡潔且有用的回答，"
            "不必強行將問題連結至目前模式。"
            "3. 如果問題屬於其他專業領域且需要深入分析，可以先簡短說明目前能提供的協助，"
            "再詢問使用者是否希望切換至更適合的模式。"
            "4. 不得為了維持角色而拒絕合理的一般性協助，也不得將無關問題硬套進目前模式。"

        ),
    },
}

MAX_AI_MESSAGE_LENGTH = 4000
MAX_AI_HISTORY_MESSAGES = 12
MAX_AI_HISTORY_ITEM_LENGTH = 2000


def _get_gemini_client():
    """建立 Gemini client；API Key 僅從環境變數讀取。"""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "尚未設定 GEMINI_API_KEY。請在專案環境變數或 .env 檔設定 API Key。"
        )

    from google import genai
    return genai.Client(api_key=api_key)


@app.route("/api/ai-assistant", methods=["POST"])
def ai_assistant():
    """接收 AI 人生顧問問題，保留本次對話上下文。"""
    print("=== AI API ROUTE ENTERED ===", flush=True)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({
            "success": False,
            "error": "請以 JSON 格式傳送問題。"
        }), 400

    mode = payload.get("mode", "sutra")
    if mode not in AI_MODES:
        return jsonify({
            "success": False,
            "error": "目前不支援這個 AI 模式。"
        }), 400

    message = payload.get("message", "")
    if not isinstance(message, str) or not message.strip():
        return jsonify({
            "success": False,
            "error": "請先輸入想詢問的內容。"
        }), 400

    message = message.strip()
    if len(message) > MAX_AI_MESSAGE_LENGTH:
        return jsonify({
            "success": False,
            "error": f"問題太長，請控制在 {MAX_AI_MESSAGE_LENGTH} 字以內。"
        }), 400

    raw_history = payload.get("history", [])
    if not isinstance(raw_history, list):
        raw_history = []

    # 只接收 user / assistant 的文字訊息，限制筆數及長度。
    history = []
    for item in raw_history[-MAX_AI_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        history.append({
            "role": role,
            "content": content[:MAX_AI_HISTORY_ITEM_LENGTH]
        })

    # 讀取目前閱讀情境；首頁提問時可以沒有 context。
    raw_context = payload.get("context", {})
    if not isinstance(raw_context, dict):
        raw_context = {}

    current_sutra_id = raw_context.get("sutra_id")
    if not isinstance(current_sutra_id, str):
        current_sutra_id = None

    paragraph_index = raw_context.get("paragraph_index")
    if (
        not isinstance(paragraph_index, int)
        or isinstance(paragraph_index, bool)
    ):
        paragraph_index = None

    # 只有「經文智慧」模式需要搜尋已收錄的經文。
    # 人際關係與職涯發展模式不執行經文檢索。
    if mode == "sutra":
        retrieved_passages = retrieve_sutra_passages(
            query=message,
            current_sutra_id=current_sutra_id,
            paragraph_index=paragraph_index,
            max_results=6,
        )
    else:
        retrieved_passages = []

    # 組成提供給 Gemini 的原文資料。
    if retrieved_passages:
        passage_lines = []

        for number, passage in enumerate(retrieved_passages, start=1):
            passage_lines.append(
                f"{number}. 經書：{passage['title']}\n"
                f"   段落索引：{passage['paragraph_index']}\n"
                f"   原文：{passage['text']}\n"
                f"   文字比對命中："
                f"{'是' if passage['is_direct_match'] else '否'}"
            )

        scripture_context = "\n\n".join(passage_lines)
    else:
        scripture_context = "本次文字檢索沒有找到可確認相關的經文段落。"


    if mode == "sutra":
        retrieval_instruction = f"""
    【本次經文檢索資料】
    {scripture_context}

    【回答規則】
    1. 先直接解釋使用者詢問的經文字句。
    2. 說明關鍵詞與前後文的關係。
    3. 區分經文原文、佛學解釋及生活應用。
    4. 只能引用檢索資料中實際存在的經文原文。
    5. 不得捏造經文、經書名稱或出處。
    6. 若沒有直接相關的經文，明確說明檢索限制。
    7. 使用繁體中文。
    """
    elif mode == "relationship":
        retrieval_instruction = """
    【目前模式：人際關係】
    本模式專注於家人、朋友、伴侶、同儕及其他人際互動。

    回答流程：
    1. 先理解使用者的處境與感受。
    2. 整理事情經過，分析雙方可能的立場與誤解。
    3. 若問題涉及工作或升學，先確認是否存在家人期待、
    他人意見、害怕讓人失望或其他人際壓力。
    4. 提供實際可使用的溝通方式與界線建議。
    5. 資訊不足時，優先提出一個有助於理解人際處境的問題。

    除非使用者明確詢問經文或佛法，否則不要解釋經文、
    五蘊或佛學名詞，也不要產生完整的職涯選項分析。
    不要提及背景經文檢索結果。
    使用繁體中文。
    """
    elif mode == "career":
        retrieval_instruction = """
    【目前模式：職涯發展】
    本模式專注於升學、就業、職涯方向、學業與工作選擇。

    回答流程：
    1. 釐清使用者的目標、興趣與現實限制。
    2. 比較相關選項的優缺點、風險與長期影響。
    3. 提供具體的評估方法與下一步行動。
    4. 資訊不足時，詢問最重要的必要問題。

    除非使用者明確詢問經文或佛法，否則不要將回答轉為
    經文講解或佛學研究。
    不要提及背景經文檢索結果。
    使用繁體中文。
    """

    try:
        from google.genai import types

        client = _get_gemini_client()
        contents = []
        for item in history:
            contents.append(types.Content(
                role="user" if item["role"] == "user" else "model",
                parts=[types.Part(text=item["content"])]
            ))
        contents.append(types.Content(
            role="user",
            parts=[
                types.Part(
                    text=(
                        f"{message}\n\n"
                        f"{retrieval_instruction}"
                    )
                )
            ]
        ))

        app.logger.warning("AI 診斷：已進入 Gemini 呼叫前")
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=(
                    AI_MODES[mode]["instruction"]
                    + "\n\n"
                    + AI_RESPONSE_LENGTH_RULE
                ),
                max_output_tokens=1024,
                temperature=0.7,
            ),
        )

        answer = getattr(response, "text", None)
        app.logger.warning("AI 診斷：已取得 Gemini 回覆，answer=%r", answer)
        candidates = getattr(response, "candidates", None) or []
        finish_reason = "UNKNOWN"

        if candidates:
            finish_reason = str(
                getattr(candidates[0], "finish_reason", "UNKNOWN")
            )

        app.logger.info(
            "Gemini 回覆狀態：mode=%s, finish_reason=%s, "
            "answer_length=%s",
            mode,
            finish_reason,
            len(answer) if isinstance(answer, str) else 0,
        )
        app.logger.info("Gemini 完整回覆：%r", answer)

        if not isinstance(answer, str) or not answer.strip():
            app.logger.warning("Gemini 回傳空白內容，模式：%s", mode)
            return jsonify({
                "success": False,
                "error": "AI 暫時沒有產生回覆，請稍後再試。"
            }), 502

        return jsonify({
            "success": True,
            "mode": mode,
            "reply": answer.strip()
        })

    except RuntimeError as error:
        app.logger.warning("AI 設定問題：%s", error)
        return jsonify({
            "success": False,
            "error": str(error)
        }), 500
    except ImportError:
        app.logger.exception("找不到 google-genai 套件")
        return jsonify({
            "success": False,
            "error": "尚未安裝 google-genai。請依照 requirements.txt 安裝套件後重啟系統。"
        }), 500
    except Exception:
        app.logger.exception("Gemini API 呼叫失敗")
        return jsonify({
            "success": False,
            "error": "AI 服務目前無法使用，請檢查網路、API Key 與 Gemini API 設定後再試。"
        }), 502


if __name__ == "__main__":
    observer = None

    # Debug 模式只在 Flask 重新載入後的子程序初始化
    safe_scan_and_process()
    observer = start_sutra_watcher()

    try:
        app.run(
            host="0.0.0.0",
            port=5005,
            debug=True,
            use_reloader=False,
        )
    finally:
        if observer is not None:
            observer.stop()
            observer.join()
