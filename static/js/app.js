
document.addEventListener("DOMContentLoaded", () => {

    // =========================
    // 字體大小控制
    // =========================
    const fontButtons = document.querySelectorAll(".font-button");

    const textAreas = document.querySelectorAll(
        ".sutra-content, .sutra-reading, .sutra-reading-panel"
    );

    const fontClasses = {
        small: "font-small",
        medium: "font-medium",
        large: "font-large"
    };

    const FONT_STORAGE_KEY = "sutraFontSize";

    // 判斷這次載入是否為重新整理
    const navigationEntry = performance.getEntriesByType("navigation")[0];
    const isReload = navigationEntry?.type === "reload";

    // 只有重新整理時，才恢復預設中字
    if (isReload) {
        sessionStorage.setItem(FONT_STORAGE_KEY, "medium");
    }

    // 一般進入其他頁面時，沿用上次選擇
    const savedFontSize =
        sessionStorage.getItem(FONT_STORAGE_KEY) || "medium";

    function setFontSize(size) {
        const targetClass = fontClasses[size];

        if (!targetClass) return;

        textAreas.forEach((area) => {
            Object.values(fontClasses).forEach((className) => {
                area.classList.remove(className);
            });

            area.classList.add(targetClass);
        });

        fontButtons.forEach((button) => {
            const isActive = button.dataset.size === size;

            button.classList.toggle("active", isActive);
            button.setAttribute("aria-pressed", String(isActive));
        });

        // 記住目前選擇，供其他頁面沿用
        sessionStorage.setItem(FONT_STORAGE_KEY, size);
    }

    fontButtons.forEach((button) => {
        button.addEventListener("click", () => {
            setFontSize(button.dataset.size);
        });
    });

    setFontSize(savedFontSize);


    // =========================
    // 真人誦經音訊播放器
    // =========================
    const audio = document.getElementById("sutra-audio");

    if (audio) {
        const previousButton = document.getElementById("previous-button");
        const nextButton = document.getElementById("next-button");
        const playButton = document.getElementById("play-button");
        const restartButton = document.getElementById("restart-button");
        const loopButton = document.getElementById("loop-button");
        const progress = document.getElementById("audio-progress");
        const volume = document.getElementById("audio-volume");
        const playerTime = document.getElementById("player-time");
        const playerStatus = document.getElementById("player-status");
        const speedButtons = document.querySelectorAll(".speed-button");

        let isSeeking = false;
        let highlightedSpan = null;
        let animationFrameId = null;

        // =========================
        // 載入逐字對齊資料
        // =========================
        const readingPanel = document.getElementById(
            "sutra-reading-panel"
        );

        const alignmentElement = document.getElementById(
            "sutra-alignment-data"
        );

        let alignmentData = null;
        let characterSpans = [];

        const ignoredChars = new Set([
            " ", "\t", "\n", "\r",
            "，", "。", "！", "？", "、", "；", "：",
            ",", ".", "!", "?", ";", ":",
            "「", "」", "『", "』",
            "（", "）", "(", ")",
            "《", "》", "〈", "〉",
            "…", "—", "-"
        ]);

        if (alignmentElement && readingPanel) {
            try {
                alignmentData = JSON.parse(
                    alignmentElement.textContent
                );

                if (
                    !alignmentData ||
                    !Array.isArray(alignmentData.paragraphs)
                ) {
                    alignmentData = null;
                }
            } catch (error) {
                console.error("讀取逐字對齊資料失敗：", error);
                alignmentData = null;
            }
        }

        // 將對齊時間套到原文，保留標點與空白。
        function initializeCharacterHighlight() {
            if (!readingPanel || !alignmentData) return;

            const paragraphs = alignmentData.paragraphs;

            paragraphs.forEach((alignedParagraph, paragraphIndex) => {
                const paragraph = readingPanel.querySelector(
                    `p[data-paragraph-index="${paragraphIndex}"]`
                );

                if (
                    !paragraph ||
                    typeof alignedParagraph.text !== "string" ||
                    !Array.isArray(alignedParagraph.characters)
                ) {
                    return;
                }

                const originalText = paragraph.textContent;
                const alignedChars = alignedParagraph.characters;

                // 對齊字元必須依序對應原文的非標點字元。
                let alignedIndex = 0;
                const fragment = document.createDocumentFragment();

                for (const char of originalText) {
                    if (ignoredChars.has(char)) {
                        fragment.appendChild(
                            document.createTextNode(char)
                        );
                        continue;
                    }

                    const timing = alignedChars[alignedIndex];

                    // 對齊資料不足或字元不一致時，保留原文字元，
                    // 不猜測時間，以免高亮錯位。
                    if (
                        !timing ||
                        timing.char !== char ||
                        !Number.isFinite(timing.start) ||
                        !Number.isFinite(timing.end) ||
                        timing.end <= timing.start
                    ) {
                        fragment.appendChild(
                            document.createTextNode(char)
                        );

                        // 字元不吻合時停止處理這段，避免後續錯位。
                        alignedIndex = alignedChars.length;
                        continue;
                    }

                    const span = document.createElement("span");
                    span.className = "sutra-character";
                    span.textContent = char;
                    span.dataset.start = String(timing.start);
                    span.dataset.end = String(timing.end);

                    fragment.appendChild(span);

                    characterSpans.push({
                        element: span,
                        start: timing.start,
                        end: timing.end
                    });

                    alignedIndex += 1;
                }

                // 只有全部對齊字元都成功使用，才套用高亮結構。
                if (alignedIndex === alignedChars.length) {
                    paragraph.replaceChildren(fragment);
                } else {
                    // 此段資料不完整或不匹配時，恢復原始文字，
                    // 並移除本段已加入的高亮候選資料。
                    characterSpans = characterSpans.filter(
                        (item) => !paragraph.contains(item.element)
                    );
                }
            });

            characterSpans.sort((a, b) => a.start - b.start);

            if (characterSpans.length > 0) {
                console.info(
                    `逐字對齊已載入：${characterSpans.length} 個字元`
                );
            } else {
                console.warn("沒有可使用的逐字對齊資料。");
            }
        }

        initializeCharacterHighlight();
        
        // =========================
        // 上一句／下一句控制
        // =========================
        const alignedParagraphs = (
            alignmentData &&
            Array.isArray(alignmentData.paragraphs)
        )
            ? alignmentData.paragraphs
                .filter((paragraph) =>
                    Number.isFinite(paragraph.start) &&
                    Number.isFinite(paragraph.end) &&
                    paragraph.end > paragraph.start
                )
                .sort((a, b) => a.index - b.index)
            : [];

        // 依目前播放時間，找出目前所在或最近已結束的句子。
        function getCurrentParagraphIndex() {
            if (alignedParagraphs.length === 0) {
                return -1;
            }

            const currentTime = audio.currentTime;

            // 優先找目前時間所在的句子。
            const activeIndex = alignedParagraphs.findIndex(
                (paragraph) =>
                    currentTime >= paragraph.start &&
                    currentTime < paragraph.end
            );

            if (activeIndex !== -1) {
                return activeIndex;
            }

            // 若位於兩句之間的停頓，使用最近一個已結束的句子。
            let previousIndex = -1;

            alignedParagraphs.forEach((paragraph, index) => {
                if (paragraph.end <= currentTime) {
                    previousIndex = index;
                }
            });

            // 若還沒到第一句，視為第一句。
            return previousIndex === -1 ? 0 : previousIndex;
        }

        function updateSentenceButtons() {
            if (!previousButton || !nextButton) return;

            if (alignedParagraphs.length === 0) {
                previousButton.disabled = true;
                nextButton.disabled = true;
                return;
            }

            const currentIndex = getCurrentParagraphIndex();

            previousButton.disabled = currentIndex <= 0;
            nextButton.disabled =
                currentIndex < 0 ||
                currentIndex >= alignedParagraphs.length - 1;
        }

        async function jumpToParagraph(index) {
            if (
                index < 0 ||
                index >= alignedParagraphs.length
            ) {
                return;
            }

            const paragraph = alignedParagraphs[index];

            audio.currentTime = paragraph.start;

            // 立即更新逐字高亮及播放時間。
            updateCharacterHighlight();
            updateProgress();
            updateSentenceButtons();

            // 如果原本正在播放，跳轉後繼續播放。
            if (!audio.paused) {
                try {
                    await audio.play();
                } catch (error) {
                    console.error("跳句後播放失敗：", error);
                    playerStatus.textContent = "跳句成功，請按播放繼續";
                }
            }
        }

        if (previousButton) {
            previousButton.addEventListener("click", () => {
                const currentIndex = getCurrentParagraphIndex();

                if (currentIndex > 0) {
                    jumpToParagraph(currentIndex - 1);
                }
            });
        }

        if (nextButton) {
            nextButton.addEventListener("click", () => {
                const currentIndex = getCurrentParagraphIndex();

                if (
                    currentIndex >= 0 &&
                    currentIndex < alignedParagraphs.length - 1
                ) {
                    jumpToParagraph(currentIndex + 1);
                }
            });
        }

        // 初始化按鈕狀態。
        updateSentenceButtons();

        // =========================
        // 時間與進度控制
        // =========================
        function formatTime(seconds) {
            if (!Number.isFinite(seconds) || seconds < 0) {
                return "00:00";
            }

            const minutes = Math.floor(seconds / 60);
            const remainingSeconds = Math.floor(seconds % 60);

            return (
                String(minutes).padStart(2, "0") +
                ":" +
                String(remainingSeconds).padStart(2, "0")
            );
        }

        function updateTime() {
            playerTime.textContent =
                `${formatTime(audio.currentTime)} / ` +
                `${formatTime(audio.duration)}`;
        }

        function updateProgress() {
            if (
                !isSeeking &&
                Number.isFinite(audio.duration) &&
                audio.duration > 0
            ) {
                progress.value = Math.round(
                    (audio.currentTime / audio.duration) * 1000
                );
            }

            updateTime();
        }

        function updatePlayButton() {
            playButton.textContent = audio.paused ? "播放" : "暫停";
        }

        // =========================
        // 逐字高亮
        // =========================
        function updateCharacterHighlight() {
            if (characterSpans.length === 0) return;

            const currentTime = audio.currentTime;

            // 找出目前播放時間對應的字元。
            // 使用二分搜尋，避免每一影格都掃描全部字元。
            let left = 0;
            let right = characterSpans.length - 1;
            let found = null;

            while (left <= right) {
                const middle = Math.floor((left + right) / 2);
                const item = characterSpans[middle];

                if (currentTime < item.start) {
                    right = middle - 1;
                } else if (currentTime >= item.end) {
                    left = middle + 1;
                } else {
                    found = item;
                    break;
                }
            }

            if (highlightedSpan !== (found ? found.element : null)) {
                if (highlightedSpan) {
                    highlightedSpan.classList.remove(
                        "character-highlight"
                    );
                }

                highlightedSpan = found ? found.element : null;

                if (highlightedSpan) {
                    highlightedSpan.classList.add(
                        "character-highlight"
                    );
                }
                
                // 目前高亮字超出閱讀區時，才自動捲動。
                if (highlightedSpan && readingPanel) {
                    const panelRect = readingPanel.getBoundingClientRect();
                    const charRect = highlightedSpan.getBoundingClientRect();

                    const topLimit = panelRect.top + 12;
                    const bottomLimit = panelRect.bottom - 12;

                    if (charRect.top < topLimit) {
                        readingPanel.scrollTop -= topLimit - charRect.top;
                    } else if (charRect.bottom > bottomLimit) {
                        readingPanel.scrollTop += charRect.bottom - bottomLimit;
                    }
                }
            }
        }

        function animationLoop() {
            updateCharacterHighlight();

            if (!audio.paused && !audio.ended) {
                animationFrameId = requestAnimationFrame(animationLoop);
            } else {
                animationFrameId = null;
            }
        }

        function startHighlightLoop() {
            if (animationFrameId === null) {
                animationFrameId = requestAnimationFrame(animationLoop);
            }
        }

        function stopHighlightLoop() {
            if (animationFrameId !== null) {
                cancelAnimationFrame(animationFrameId);
                animationFrameId = null;
            }

            // 暫停後仍依目前時間更新一次，讓拖曳定位也能正確顯示。
            updateCharacterHighlight();
        }

        // =========================
        // 音訊事件
        // =========================
        audio.addEventListener("loadedmetadata", () => {
            playButton.disabled = false;
            restartButton.disabled = false;

            playerStatus.textContent = "已載入真人誦經音檔";
            updateProgress();
            updateCharacterHighlight();
            updateSentenceButtons();
        });

        audio.addEventListener("canplay", () => {
            playerStatus.textContent = "準備播放";
        });

        playButton.addEventListener("click", async () => {
            if (audio.paused) {
                try {
                    await audio.play();
                } catch (error) {
                    console.error("音訊播放失敗：", error);
                    playerStatus.textContent = "播放失敗，請重新載入";
                }
            } else {
                audio.pause();
            }
        });

        audio.addEventListener("play", () => {
            updatePlayButton();
            playerStatus.textContent = "正在播放真人誦經";
            startHighlightLoop();
        });

        audio.addEventListener("pause", () => {
            updatePlayButton();

            if (!audio.ended) {
                playerStatus.textContent = "已暫停";
            }

            stopHighlightLoop();
        });

        audio.addEventListener("timeupdate", () => {
            updateProgress();
            updateCharacterHighlight();
            updateSentenceButtons();
        });

        audio.addEventListener("durationchange", updateProgress);

        audio.addEventListener("seeking", () => {
            updateCharacterHighlight();
        });

        audio.addEventListener("seeked", () => {
            updateProgress();
            updateCharacterHighlight();
            updateSentenceButtons();
        });

        progress.addEventListener("input", () => {
            isSeeking = true;

            if (
                Number.isFinite(audio.duration) &&
                audio.duration > 0
            ) {
                const targetTime =
                    (Number(progress.value) / 1000) * audio.duration;

                playerTime.textContent =
                    `${formatTime(targetTime)} / ` +
                    `${formatTime(audio.duration)}`;
            }
        });

        progress.addEventListener("change", () => {
            if (
                Number.isFinite(audio.duration) &&
audio.duration > 0
            ) {
                audio.currentTime =
                    (Number(progress.value) / 1000) * audio.duration;
            }

            isSeeking = false;
            updateProgress();
            updateCharacterHighlight();
            updateSentenceButtons();
        });

        restartButton.addEventListener("click", async () => {
            audio.currentTime = 0;
            updateCharacterHighlight();

            try {
                await audio.play();
            } catch (error) {
                console.error("重新播放失敗：", error);
                playerStatus.textContent = "無法播放，請再試一次";
            }
        });

        loopButton.addEventListener("click", () => {
            audio.loop = !audio.loop;

            loopButton.textContent = audio.loop
                ? "循環：開"
                : "循環：關";

            loopButton.setAttribute(
                "aria-pressed",
                String(audio.loop)
            );
        });

        speedButtons.forEach((button) => {
            button.addEventListener("click", () => {
                const speed = Number(button.dataset.speed);

                if (!Number.isFinite(speed) || speed <= 0) return;

                audio.playbackRate = speed;

                speedButtons.forEach((item) => {
                    item.classList.toggle("active", item === button);
                });
            });
        });

        volume.addEventListener("input", () => {
            audio.volume = Number(volume.value);
        });

        audio.volume = Number(volume.value);

        audio.addEventListener("ended", () => {
            updatePlayButton();
            updateProgress();
            updateCharacterHighlight();
            stopHighlightLoop();
            playerStatus.textContent = "誦經播放完畢";
        });

        audio.addEventListener("error", () => {
            playButton.disabled = true;
            restartButton.disabled = true;
            playerStatus.textContent = "音檔載入失敗，請檢查音檔";
        });

        if (audio.readyState >= 1) {
            playButton.disabled = false;
            restartButton.disabled = false;
            playerStatus.textContent = "已載入真人誦經音檔";
            updateProgress();
            updateCharacterHighlight();
        }

        // 進入經文播放頁面後，延遲 1 秒自動嘗試播放。
        // 部分瀏覽器會阻擋未經使用者手勢觸發的有聲自動播放，
        // 因此若被阻擋，保留原本播放按鈕並提示使用者手動播放。
        window.setTimeout(async () => {
            if (!audio || !audio.paused) return;

            try {
                await audio.play();
                playerStatus.textContent = "正在播放真人誦經";
            } catch (error) {
                console.info("瀏覽器阻擋自動播放，請使用播放按鈕：", error);
                if (playerStatus) {
                    playerStatus.textContent = "請按播放開始誦經（瀏覽器限制自動播放）";
                }
            }
        }, 1000);
    }

    // =========================
    // AI 人生顧問按鈕
    // =========================
    const aiButton = document.getElementById("ai-assistant-button");
    const aiPanel = document.getElementById("ai-assistant-panel");
    const aiCloseButton = document.getElementById("ai-panel-close");
    const aiChatMessages = document.getElementById("ai-chat-messages");
    const aiChatForm = document.getElementById("ai-chat-form");
    const aiChatInput = document.getElementById("ai-chat-input");
    const aiModeButtons = document.querySelectorAll(".ai-mode-button");

    let currentAIMode = "sutra";

    const aiModeNames = {
        sutra: "經文智慧",
        relationship: "人際關係",
        career: "職涯發展"
    };

    function setAIPanelOpen(open) {
        if (!aiButton || !aiPanel) return;

        aiPanel.classList.toggle("is-open", open);

        // 只有電腦版啟用左右分割；手機／平板維持原本面板模式。
        if (window.matchMedia("(min-width: 1024px)").matches) {
            document.body.classList.toggle("ai-layout-open", open);
        } else {
            document.body.classList.remove("ai-layout-open");
        }

        aiButton.setAttribute("aria-expanded", String(open));
        aiButton.setAttribute(
            "aria-label",
            open ? "關閉 AI 人生顧問" : "開啟 AI 人生顧問"
        );

        aiPanel.setAttribute("aria-hidden", String(!open));
        aiPanel.inert = !open;

        if (open && aiChatInput) {
            aiChatInput.focus();
        }
    }

    // 電腦版拖曳分隔線調整左右比例；不改變手機／平板操作。
    const aiResizeHandle = document.getElementById("ai-panel-resize-handle");
    const desktopSplitQuery = window.matchMedia("(min-width: 1024px)");
    const AI_PANEL_WIDTH_KEY = "sutraAiPanelWidth";

    function applyAIPanelWidth(width) {
        const minWidth = 320;
        const maxWidth = Math.min(640, window.innerWidth - 480);
        const safeMax = Math.max(minWidth, maxWidth);
        const clampedWidth = Math.min(safeMax, Math.max(minWidth, width));

        document.documentElement.style.setProperty(
            "--ai-panel-width",
            `${clampedWidth}px`
        );

        if (aiResizeHandle) {
            aiResizeHandle.setAttribute("aria-valuenow", String(Math.round(clampedWidth)));
        }
        return clampedWidth;
    }

    if (aiPanel && aiResizeHandle && desktopSplitQuery.matches) {
        const savedWidth = Number(localStorage.getItem(AI_PANEL_WIDTH_KEY));
        if (Number.isFinite(savedWidth) && savedWidth >= 320) {
            applyAIPanelWidth(savedWidth);
        } else {
            applyAIPanelWidth(window.innerWidth * 0.35);
        }

        let isDraggingPanel = false;

        aiResizeHandle.addEventListener("pointerdown", (event) => {
            if (!desktopSplitQuery.matches || !aiPanel.classList.contains("is-open")) return;

            isDraggingPanel = true;
            document.body.classList.add("ai-layout-resizing");
            aiResizeHandle.setPointerCapture(event.pointerId);
            event.preventDefault();
        });

        aiResizeHandle.addEventListener("pointermove", (event) => {
            if (!isDraggingPanel || !desktopSplitQuery.matches) return;

            const width = window.innerWidth - event.clientX;
            applyAIPanelWidth(width);
        });

        function finishPanelResize() {
            if (!isDraggingPanel) return;

            isDraggingPanel = false;
            document.body.classList.remove("ai-layout-resizing");

            const width = parseFloat(
                getComputedStyle(document.documentElement).getPropertyValue("--ai-panel-width")
            );
            if (Number.isFinite(width)) {
                localStorage.setItem(AI_PANEL_WIDTH_KEY, String(width));
            }
        }

        aiResizeHandle.addEventListener("pointerup", finishPanelResize);
        aiResizeHandle.addEventListener("pointercancel", finishPanelResize);

        aiResizeHandle.addEventListener("keydown", (event) => {
            if (!desktopSplitQuery.matches || !aiPanel.classList.contains("is-open")) return;

            const currentWidth = aiPanel.getBoundingClientRect().width;
            if (event.key === "ArrowLeft") {
                applyAIPanelWidth(currentWidth + 16);
                event.preventDefault();
            } else if (event.key === "ArrowRight") {
                applyAIPanelWidth(currentWidth - 16);
                event.preventDefault();
            } else {
                return;
            }

            const width = parseFloat(
                getComputedStyle(document.documentElement).getPropertyValue("--ai-panel-width")
            );
            if (Number.isFinite(width)) {
                localStorage.setItem(AI_PANEL_WIDTH_KEY, String(width));
            }
        });

        window.addEventListener("resize", () => {
            if (!desktopSplitQuery.matches) {
                document.body.classList.remove("ai-layout-open", "ai-layout-resizing");
                isDraggingPanel = false;
                return;
            }

            if (aiPanel.classList.contains("is-open")) {
                const currentWidth = aiPanel.getBoundingClientRect().width;
                applyAIPanelWidth(currentWidth);
            }
        });
    }

    function addAIMessage(speaker, message, type) {
        if (!aiChatMessages) return;
        const item = document.createElement("div");
        item.className = `ai-message ai-message-${type}`;
        const label = document.createElement("span");
        label.className = "ai-message-speaker";
        label.textContent = speaker;
        const paragraph = document.createElement("p");
        paragraph.textContent = message;

        item.append(label, paragraph);
        aiChatMessages.appendChild(item);
        aiChatMessages.scrollTop = aiChatMessages.scrollHeight;
    }

    if (aiButton && aiPanel) {
        aiButton.addEventListener("click", () => {
            setAIPanelOpen(!aiPanel.classList.contains("is-open"));
        });
    }

    if (aiCloseButton) {
        aiCloseButton.addEventListener("click", () => {
            setAIPanelOpen(false);
            aiButton?.focus();
        });
    }

    aiModeButtons.forEach((button) => {
        button.addEventListener("click", () => {
            currentAIMode = button.dataset.mode;
            if (!aiModeNames[currentAIMode]) return;

            aiModeButtons.forEach((item) => {
                const active = item === button;
                item.classList.toggle("active", active);
                item.setAttribute("aria-pressed", String(active));
            });

            // 切換模式時保留同一場對話紀錄，讓後端依新模式繼續回答。
            addAIMessage(
                "系統",
                `已切換到「${aiModeNames[currentAIMode]}」模式。`,
                "assistant"
            );
        });
    });


    // =========================
    // AI Agent：播放器指令（前端白名單）
    // =========================
    function tryHandlePlayerCommand(rawMessage) {
        const message = String(rawMessage || "").trim().replace(/[，。！!？?、]/g, "");
        const normalized = message.replace(/\s+/g, "").toLowerCase();
        const hasPlayer = Boolean(audio);
        const reply = (value) => ({ handled: true, reply: value });

        // 只在看起來像播放器控制時攔截，其他問題仍交給 Gemini。
        const playerCommandPattern =
            /播放|聽|暫停|停止播放|先停|從頭|重播|重新播放|上一句|前一句|下一句|下一段|循環|倍速|速度|快一點|慢一點|字體|字放大|字縮小|字大一點|字小一點|放大字|縮小字/;

        if (!playerCommandPattern.test(normalized)) {
            return { handled: false };
        }

        // 先判斷是否指定了經文名稱。
        const sutraAliases = [
            { name: "般若波羅蜜多心經", aliases: ["般若波羅蜜多心經", "般若心經", "心經"], id: "heart_sutra" },
            { name: "大悲咒", aliases: ["大悲咒", "大悲心陀羅尼"], id: "great_compassion_mantra" },
            { name: "準提神咒", aliases: ["準提神咒", "準提咒"], id: "cundi_dharani" }
        ];
        const requestedSutra = sutraAliases.find((sutra) =>
            sutra.aliases.some((alias) => normalized.includes(alias))
        );

        if (requestedSutra) {
            // 優先使用頁面上實際存在的經文連結，避免猜測網址。
            const sutraLinks = Array.from(document.querySelectorAll('a[href]'));
            const targetLink = sutraLinks.find((link) => {
                const label = `${link.textContent || ""} ${link.getAttribute("aria-label") || ""}`.replace(/\s+/g, "");
                return requestedSutra.aliases.some((alias) => label.includes(alias)) &&
                    /\/sutras\//.test(link.getAttribute("href") || "");
            });

            const currentTitle = document.querySelector("h2")?.textContent ||
                document.title || "";
            const alreadyOnRequestedSutra = requestedSutra.aliases.some((alias) =>
                currentTitle.replace(/\s+/g, "").includes(alias)
            );

            if (!alreadyOnRequestedSutra && targetLink) {
                window.location.href = targetLink.href;
                return reply(`好的，正在切換到「${requestedSutra.name}」播放頁面。`);
            }

            // 若頁面本身就是指定經文，接著執行播放；若沒有可用連結，避免自行猜路由。
            if (!alreadyOnRequestedSutra && !targetLink) {
                return reply(`我知道你想播放「${requestedSutra.name}」，但目前頁面找不到這部經文的切換連結。請先返回經書選單選擇它，再告訴我播放。`);
            }
        }

        const playIntent = /播放|開始播放|繼續播放|想聽|我要聽|播放誦經/.test(normalized);
        const pauseIntent = /暫停|先停|停止播放|停一下/.test(normalized);

        // 如果只是「播放心經／我想聽心經」等，指定經文判斷完成後就播放。
        if (playIntent && !pauseIntent &&
            (requestedSutra || /^(播放|開始播放|繼續播放|播放誦經|幫我播放|請播放|我想聽經文|我要聽經文)$/.test(normalized))) {
            if (!hasPlayer) {
                return reply("請先進入一部經文的播放頁面，我才能替你開始播放。");
            }
            if (!audio.paused) return reply("經文目前已經在播放了。");
            const playButton = document.getElementById("play-button");
            if (!playButton) return reply("找不到播放按鈕，請確認播放器頁面是否正常。");
            playButton.click();
            return reply(requestedSutra
                ? `好的，開始播放「${requestedSutra.name}」。`
                : "好的，開始播放經文。");
        }

        // 其他播放器指令需要在播放頁面執行。
        if (!hasPlayer) {
            return reply("播放器控制需要在經文播放頁面使用，請先進入一部經文的播放頁面。");
        }

        const click = (id) => {
            const element = document.getElementById(id);
            if (!element) return false;
            element.click();
            return true;
        };

        if (/^(暫停|暫停一下|先停|停止播放|幫我暫停|請暫停|先暫停一下)$/.test(normalized)) {
            if (!audio.paused) {
                return click("play-button") ? reply("好的，已暫停播放。") : reply("找不到播放按鈕。");
            }
            return reply("經文目前已經是暫停狀態。");
        }

        if (/^(從頭開始|從頭播放|重新播放|重播|重新開始|從頭來|幫我從頭開始)$/.test(normalized)) {
            return click("restart-button") ? reply("好的，從經文開頭重新播放。") : reply("找不到重新播放按鈕。");
        }

        if (/^(上一句|前一句|上一段)$/.test(normalized)) {
            return click("previous-button") ? reply("好的，已切換到上一句。") : reply("找不到上一句按鈕。");
        }

        if (/^(下一句|下一段)$/.test(normalized)) {
            return click("next-button") ? reply("好的，已切換到下一句。") : reply("找不到下一句按鈕。");
        }

        // 循環播放：接受自然語句及常見同音錯字「撥放」。
        const loopButton = document.getElementById("loop-button");
        const asksToEnableLoop =
            /循環(播放|撥放)/.test(normalized) ||
            /(開啟|打開|啟用|開|幫我開啟|幫我打開).{0,5}循環/.test(normalized) ||
            /(我要|我想要|我想|幫我|請幫我).{0,5}循環(播放|撥放)/.test(normalized);
        const asksToDisableLoop =
            /(關閉|取消|停止|不要).{0,5}循環/.test(normalized) ||
            /(循環播放|循環撥放).{0,5}(關閉|取消|停止|不要)/.test(normalized);

        if (asksToDisableLoop) {
            if (!loopButton) return reply("找不到循環播放按鈕。");
            if (!audio.loop) return reply("循環播放目前已經關閉。");
            loopButton.click();
            return reply("好的，已關閉循環播放。");
        }

        if (asksToEnableLoop) {
            if (!loopButton) return reply("找不到循環播放按鈕。");
            if (audio.loop) return reply("循環播放已經開啟了。");
            loopButton.click();
            return reply("好的，已開啟循環播放。");
        }

        const speedMatch = normalized.match(/(?:調成|調到|設定為|設定成|改成|切換到|播放速度)?(0\.5|0\.75|1\.0|1\.5|2\.0|1|1\.5|2)倍(?:速)?/);
        if (speedMatch) {
            const requested = String(Number(speedMatch[1]));
            const speedButton = Array.from(document.querySelectorAll(".speed-button"))
                .find((button) => String(Number(button.dataset.speed)) === requested);
            if (!speedButton) return reply(`目前播放器沒有 ${requested} 倍速選項，請選擇畫面上提供的速度。`);
            speedButton.click();
            return reply(`好的，已調整為 ${requested} 倍速。`);
        }

        if (/快一點|加快|速度快一點|播放快一點|再快一點/.test(normalized)) {
            const buttons = Array.from(document.querySelectorAll(".speed-button"))
                .map((button) => ({ button, speed: Number(button.dataset.speed) }))
                .filter((item) => Number.isFinite(item.speed))
                .sort((a, b) => a.speed - b.speed);
            const next = buttons.find((item) => item.speed > Number(audio.playbackRate) + 0.001);
            if (!next) return reply("目前已經是播放器提供的最快速度了。");
            next.button.click();
            return reply(`好的，已調整為 ${next.speed} 倍速。`);
        }

        if (/慢一點|放慢|速度慢一點|播放慢一點|再慢一點/.test(normalized)) {
            const buttons = Array.from(document.querySelectorAll(".speed-button"))
                .map((button) => ({ button, speed: Number(button.dataset.speed) }))
                .filter((item) => Number.isFinite(item.speed))
                .sort((a, b) => a.speed - b.speed);
            const next = buttons.filter((item) => item.speed < Number(audio.playbackRate) - 0.001).pop();
            if (!next) return reply("目前已經是播放器提供的最慢速度了。");
            next.button.click();
            return reply(`好的，已調整為 ${next.speed} 倍速。`);
        }

        if (/^(小字|小字體|字體小)$/.test(normalized)) {
            setFontSize("small");
            return reply("好的，已將經文字體調整為小字。");
        }
        if (/^(中字|中字體|字體中)$/.test(normalized)) {
            setFontSize("medium");
            return reply("好的，已將經文字體調整為中字。");
        }
        if (/^(大字|大字體|字體大)$/.test(normalized)) {
            setFontSize("large");
            return reply("好的，已將經文字體調整為大字。");
        }

        if (/放大|大一點|加大/.test(normalized) && /字|字體|字幕|經文/.test(normalized)) {
            const currentClass = ["small", "medium", "large"].find((size) =>
                document.querySelector(".sutra-content, .sutra-reading, .sutra-reading-panel")
                    ?.classList.contains(fontClasses[size])
            ) || "medium";
            const next = currentClass === "small" ? "medium" : currentClass === "medium" ? "large" : null;
            if (!next) return reply("字體目前已經是最大尺寸了。");
            setFontSize(next);
            return reply(`好的，已將經文字體調整為${next === "medium" ? "中" : "大"}字。`);
        }

        if (/縮小|小一點|減小/.test(normalized) && /字|字體|字幕|經文/.test(normalized)) {
            const currentClass = ["small", "medium", "large"].find((size) =>
                document.querySelector(".sutra-content, .sutra-reading, .sutra-reading-panel")
                    ?.classList.contains(fontClasses[size])
            ) || "medium";
            const next = currentClass === "large" ? "medium" : currentClass === "medium" ? "small" : null;
            if (!next) return reply("字體目前已經是最小尺寸了。");
            setFontSize(next);
            return reply(`好的，已將經文字體調整為${next === "medium" ? "中" : "小"}字。`);
        }

        // 像「播放心經時，這段在說什麼？」這類問題不應被誤當成控制指令。
        return { handled: false };
    }

    const aiConversationHistory = {
        sutra: [],
        relationship: [],
        career: []
    };
    let aiRequestInProgress = false;

    if (aiChatForm && aiChatInput) {
        aiChatForm.addEventListener("submit", async (event) => {
            event.preventDefault();

            const message = aiChatInput.value.trim();
            if (!message || aiRequestInProgress) return;

            addAIMessage("你", message, "user");
            aiConversationHistory[currentAIMode].push({ role: "user",content: message });
            aiChatInput.value = "";

            const commandResult = tryHandlePlayerCommand(message);
            if (commandResult.handled) {
                addAIMessage("AI", commandResult.reply, "assistant");
                aiConversationHistory[currentAIMode].push({
                    role: "assistant",
                    content: commandResult.reply
                });
                return;
            }

            aiRequestInProgress = true;
            const submitButton = aiChatForm.querySelector(
                'button[type="submit"], input[type="submit"]'
            );
            if (submitButton) submitButton.disabled = true;
            aiChatInput.disabled = true;
            addAIMessage("AI", "正在思考，請稍候……", "loading");

            try {
                const response = await fetch("/api/ai-assistant", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    },
                    body: JSON.stringify({
                        mode: currentAIMode,
                        message,
                        // 不把本次最新問題重複放進 history，後端會另行加入。
                        history: aiConversationHistory[currentAIMode].slice(0, -1)
                    })
                });

                let data;
                try {
                    data = await response.json();
                } catch {
                    throw new Error("伺服器回傳格式不正確，請確認 Flask 後端是否正常。");
                }

                if (!response.ok || !data.success) {
                    throw new Error(
                        typeof data.error === "string"
                            ? data.error
                            : "AI 暫時無法回覆，請稍後再試。"
                    );
                }

                // 將載入提示換成真正的 AI 回覆。
                const loadingMessage = aiChatMessages?.querySelector(".ai-message-loading:last-child");
                if (loadingMessage) loadingMessage.remove();

                const answer = String(data.reply || "").trim();
                if (!answer) throw new Error("AI 回傳了空白內容，請再試一次。");

                addAIMessage("AI", answer, "assistant");
                aiConversationHistory[currentAIMode].push({ role: "assistant", content: answer });
            } catch (error) {
                const loadingMessage = aiChatMessages?.querySelector(".ai-message-loading:last-child");
                if (loadingMessage) loadingMessage.remove();

                addAIMessage(
                    "系統",
                    error instanceof Error
                        ? error.message
                        : "連線失敗，請檢查網路與後端設定後再試。",
                    "assistant"
                );
            } finally {
                aiRequestInProgress = false;
                if (submitButton) submitButton.disabled = false;
                aiChatInput.disabled = false;
                aiChatInput.focus();
            }
        });
    }
});