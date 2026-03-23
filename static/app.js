const uploadForm = document.getElementById("upload-form");
const uploadButton = document.getElementById("upload-button");
const studyPlanButton = document.getElementById("study-plan-button");
const statusBanner = document.getElementById("status-banner");
const runMetadata = document.getElementById("run-metadata");

const libraryForm = document.getElementById("library-form");
const libraryUploadButton = document.getElementById("library-upload-button");
const libraryDocuments = document.getElementById("library-documents");
const librarySummary = document.getElementById("library-summary");
const flashcardsButton = document.getElementById("flashcards-button");
const examInsightsButton = document.getElementById("exam-insights-button");
const mockPaperButton = document.getElementById("mock-paper-button");

const flashcardsSection = document.getElementById("flashcards-section");
const flashcardsTitle = document.getElementById("flashcards-title");
const flashcardsOverview = document.getElementById("flashcards-overview");
const flashcardsGrid = document.getElementById("flashcards-grid");

const examInsightsSection = document.getElementById("exam-insights-section");
const examInsightsTitle = document.getElementById("exam-insights-title");
const examInsightsOverview = document.getElementById("exam-insights-overview");
const examInsightsGrid = document.getElementById("exam-insights-grid");

const mockPaperSection = document.getElementById("mock-paper-section");
const mockPaperTitle = document.getElementById("mock-paper-title");
const mockPaperOutput = document.getElementById("mock-paper-output");

const visualSection = document.getElementById("visual-section");
const topicList = document.getElementById("topic-list");
const topicDetailEmpty = document.getElementById("topic-detail-empty");
const topicDetailPanel = document.getElementById("topic-detail-panel");
const studyPlanSection = document.getElementById("study-plan-section");
const studyPlanGrid = document.getElementById("study-plan-grid");
const studyPlanMeta = document.getElementById("study-plan-meta");

let latestVisualPayload = null;
let activeTopicIndex = 0;
let sourceLibrary = [];
let latestMockPaper = null;
let latestMockPaperGrading = null;
let latestMockPaperAttempt = {};
let wholePaperCaptureFiles = [];
let wholePaperStream = null;

if (window.mermaid) {
    window.mermaid.initialize({
        startOnLoad: false,
        theme: "neutral",
        securityLevel: "loose",
        themeVariables: {
            primaryColor: "#e8dcc2",
            primaryTextColor: "#1f2a2c",
            primaryBorderColor: "#0f766e",
            lineColor: "#115e59",
            tertiaryColor: "#fffaf0",
            fontFamily: "Trebuchet MS, Segoe UI, sans-serif"
        }
    });
}

function setStatus(message, tone = "idle") {
    statusBanner.textContent = message;
    statusBanner.className = `status-banner ${tone}`;
}

function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

function toTitleCase(value) {
    return String(value || "other")
        .replaceAll("_", " ")
        .replace(/\b\w/g, (char) => char.toUpperCase());
}

function getMockAttemptKey(sectionIndex, questionIndex) {
    return `${sectionIndex}:${questionIndex}`;
}

function getMockGradeForQuestion(grading, sectionIndex, questionIndex) {
    const section = (grading?.graded_sections || []).find((item) => Number(item.section_index) === Number(sectionIndex));
    return (section?.questions || []).find((item) => Number(item.question_index) === Number(questionIndex)) || null;
}

function revokeWholePaperCaptureUrls() {
    wholePaperCaptureFiles.forEach((file) => {
        if (file?.previewUrl) {
            URL.revokeObjectURL(file.previewUrl);
        }
    });
}

function resetWholePaperCaptureFiles() {
    revokeWholePaperCaptureUrls();
    wholePaperCaptureFiles = [];
}

function stopWholePaperCamera() {
    if (wholePaperStream) {
        wholePaperStream.getTracks().forEach((track) => track.stop());
        wholePaperStream = null;
    }
    const video = document.getElementById("whole-paper-camera-video");
    if (video) {
        video.srcObject = null;
    }
}

function renderWholeSubmissionPreview() {
    const preview = document.getElementById("whole-paper-capture-preview");
    if (!preview) {
        return;
    }
    if (!wholePaperCaptureFiles.length) {
        preview.innerHTML = `<p class="capture-empty">No camera captures yet.</p>`;
        return;
    }
    preview.innerHTML = wholePaperCaptureFiles.map((file, index) => `
        <figure class="capture-thumb">
            <img src="${escapeHtml(file.previewUrl)}" alt="Captured page ${index + 1}">
            <figcaption>Page ${index + 1}</figcaption>
        </figure>
    `).join("");
}

async function startWholePaperCamera() {
    const video = document.getElementById("whole-paper-camera-video");
    const panel = document.getElementById("whole-paper-camera-panel");
    if (!navigator.mediaDevices?.getUserMedia || !video || !panel) {
        setStatus("Live camera capture is not available in this browser.", "error");
        return;
    }

    try {
        stopWholePaperCamera();
        wholePaperStream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: "environment" },
            audio: false
        });
        video.srcObject = wholePaperStream;
        await video.play();
        panel.classList.remove("hidden");
        setStatus("Camera started. Capture each page when it is clearly in frame.", "success");
    } catch (error) {
        setStatus(`Could not start the camera: ${error.message}`, "error");
    }
}

async function captureWholePaperFrame() {
    const video = document.getElementById("whole-paper-camera-video");
    if (!video || !wholePaperStream) {
        setStatus("Start the camera first before capturing a page.", "error");
        return;
    }

    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth || 1280;
    canvas.height = video.videoHeight || 720;
    const context = canvas.getContext("2d");
    context.drawImage(video, 0, 0, canvas.width, canvas.height);

    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!blob) {
        setStatus("Could not capture the camera frame.", "error");
        return;
    }

    const file = new File([blob], `captured-page-${Date.now()}.png`, { type: "image/png" });
    file.previewUrl = URL.createObjectURL(blob);
    wholePaperCaptureFiles.push(file);
    renderWholeSubmissionPreview();
    setStatus(`Captured page ${wholePaperCaptureFiles.length}.`, "success");
}

function getGenerationPayload() {
    return {
        subject: document.getElementById("action-subject").value.trim(),
        topic: document.getElementById("action-topic").value.trim(),
        institution: document.getElementById("action-institution").value.trim(),
        flashcard_count: Number(document.getElementById("flashcard-count").value || 12),
        total_marks: Number(document.getElementById("mock-marks").value || 50),
        duration_minutes: Number(document.getElementById("mock-duration").value || 60)
    };
}

async function readResponsePayload(response) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
        return response.json();
    }

    const text = await response.text();
    return {
        error: text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim() || "Unexpected server response."
    };
}

function initPannableStage(stage) {
    if (!stage || stage.dataset.pannableReady === "true") {
        return;
    }

    let isDragging = false;
    let startX = 0;
    let startY = 0;
    let scrollLeft = 0;
    let scrollTop = 0;

    stage.dataset.pannableReady = "true";

    stage.addEventListener("pointerdown", (event) => {
        isDragging = true;
        stage.classList.add("dragging");
        startX = event.clientX;
        startY = event.clientY;
        scrollLeft = stage.scrollLeft;
        scrollTop = stage.scrollTop;
        stage.setPointerCapture(event.pointerId);
    });

    stage.addEventListener("pointermove", (event) => {
        if (!isDragging) {
            return;
        }
        const deltaX = event.clientX - startX;
        const deltaY = event.clientY - startY;
        stage.scrollLeft = scrollLeft - deltaX;
        stage.scrollTop = scrollTop - deltaY;
    });

    function stopDragging(event) {
        if (!isDragging) {
            return;
        }
        isDragging = false;
        stage.classList.remove("dragging");
        if (event?.pointerId !== undefined && stage.hasPointerCapture(event.pointerId)) {
            stage.releasePointerCapture(event.pointerId);
        }
    }

    stage.addEventListener("pointerup", stopDragging);
    stage.addEventListener("pointercancel", stopDragging);
    stage.addEventListener("pointerleave", stopDragging);
}

async function renderMermaidIn(element) {
    if (!window.mermaid || !element) {
        return;
    }

    const nodes = element.querySelectorAll(".mermaid");
    if (!nodes.length) {
        return;
    }

    for (const node of nodes) {
        node.removeAttribute("data-processed");
    }

    await window.mermaid.run({
        nodes: Array.from(nodes)
    });

    element.querySelectorAll(".diagram-stage").forEach(initPannableStage);
}

function renderLibraryDocuments() {
    librarySummary.textContent = sourceLibrary.length
        ? `${sourceLibrary.length} study source${sourceLibrary.length === 1 ? "" : "s"} saved`
        : "No study sources yet.";

    if (!sourceLibrary.length) {
        libraryDocuments.innerHTML = `
            <div class="empty-card">
                Upload a textbook, notes, or previous year paper to start building your study context.
            </div>
        `;
        return;
    }

    libraryDocuments.innerHTML = sourceLibrary.map((doc) => `
        <article class="library-doc-card">
            <div class="doc-badges">
                <span class="source-badge">${escapeHtml(toTitleCase(doc.source_type))}</span>
                <span class="source-badge soft">${escapeHtml(doc.content_origin || "text")}</span>
            </div>
            <h4>${escapeHtml(doc.title)}</h4>
            <p class="doc-meta">
                ${escapeHtml(doc.subject || "No subject")} · ${escapeHtml(doc.topic || "No topic")} · ${escapeHtml(doc.institution || "No institution")}
            </p>
            <p class="doc-preview">${escapeHtml(doc.preview || "No preview available.")}</p>
        </article>
    `).join("");
}

async function loadSourceLibrary() {
    const response = await fetch("/api/library/documents");
    const data = await readResponsePayload(response);
    if (!response.ok) {
        throw new Error(data.error || "Could not load source documents.");
    }
    sourceLibrary = data.documents || [];
    renderLibraryDocuments();
}

function renderFlashcards(deck) {
    flashcardsTitle.textContent = deck.deck_title || "Flashcard Deck";
    flashcardsOverview.textContent = deck.overview || "";
    flashcardsGrid.innerHTML = (deck.flashcards || []).map((card, index) => `
        <button type="button" class="flashcard" data-flashcard-index="${index}">
            <span class="flashcard-label">Flashcard ${index + 1}</span>
            <strong>${escapeHtml(card.front)}</strong>
            <span class="flashcard-answer hidden">${escapeHtml(card.back)}</span>
            <span class="flashcard-meta">${escapeHtml(toTitleCase(card.difficulty || "medium"))} · ${escapeHtml(toTitleCase(card.source_type || "source"))}</span>
        </button>
    `).join("");

    flashcardsGrid.querySelectorAll(".flashcard").forEach((card) => {
        card.addEventListener("click", () => {
            card.classList.toggle("revealed");
            const answer = card.querySelector(".flashcard-answer");
            if (answer) {
                answer.classList.toggle("hidden");
            }
        });
    });

    flashcardsSection.classList.remove("hidden");
}

function renderExamInsights(data) {
    examInsightsTitle.textContent = data.title || "Exam Insights";
    examInsightsOverview.textContent = data.overview || "";
    examInsightsGrid.innerHTML = `
        <article class="insight-card">
            <h4>Repeated Themes</h4>
            <ul>${(data.repeated_themes || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </article>
        <article class="insight-card">
            <h4>Probable Questions</h4>
            <ul>${(data.probable_questions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </article>
        <article class="insight-card">
            <h4>Question Patterns</h4>
            <ul>${(data.question_patterns || []).map((item) => `<li><strong>${escapeHtml(item.pattern)}</strong><span>${escapeHtml(item.evidence)}</span></li>`).join("")}</ul>
        </article>
        <article class="insight-card">
            <h4>Revision Priorities</h4>
            <ul>${(data.revision_priorities || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </article>
    `;
    examInsightsSection.classList.remove("hidden");
}

function renderMockPaper(paper, grading = null, attempt = {}) {
    const sections = paper.sections || [];
    const paperTotalMarks = Number(
        paper.total_marks
        || sections.reduce((sum, section) => sum + (section.questions || []).reduce((sectionSum, question) => sectionSum + Number(question.marks || 0), 0), 0)
    );
    const paperTargetMarks = Number(paper.target_total_marks || paperTotalMarks || 0);
    const scorePercent = grading?.total_marks_possible
        ? Math.round((Number(grading.total_marks_awarded || 0) / Number(grading.total_marks_possible || 1)) * 100)
        : null;
    mockPaperTitle.textContent = paper.paper_title || "Mock Paper";
    mockPaperOutput.innerHTML = `
        <section class="mock-paper-card">
            <h3>${escapeHtml(paper.paper_title || "Mock Paper")}</h3>
            <p class="mock-paper-summary">Total marks: ${escapeHtml(paperTotalMarks)}${paperTargetMarks && paperTargetMarks !== paperTotalMarks ? ` (target ${escapeHtml(paperTargetMarks)})` : ""}</p>
            <div class="mock-paper-instructions">
                <h4>Instructions</h4>
                <ul>${(paper.instructions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
            </div>
            ${grading ? `
                <section class="mock-grading-summary">
                    <div>
                        <h4>${escapeHtml(grading.evaluation_title || "Evaluation Summary")}</h4>
                        <p>${escapeHtml(grading.overall_summary || "Your paper has been graded.")}</p>
                    </div>
                    <div class="mock-score-pill">${escapeHtml(grading.total_marks_awarded)} / ${escapeHtml(grading.total_marks_possible)} marks${scorePercent !== null ? ` · ${escapeHtml(scorePercent)}%` : ""}</div>
                </section>
            ` : ""}
            <div class="mock-paper-sections">
                ${sections.map((section, index) => {
                    const sectionTotalMarks = (section.questions || []).reduce((sum, question) => sum + Number(question.marks || 0), 0);
                    return `
                    <article class="mock-section">
                        <header>
                            <strong>Section ${index + 1}: ${escapeHtml(section.title)}</strong>
                            <span>${escapeHtml(sectionTotalMarks)} marks · ${escapeHtml(section.suggested_time_minutes)} min suggested</span>
                        </header>
                        <ol>
                            ${(section.questions || []).map((question, questionIndex) => {
                                const attemptKey = getMockAttemptKey(index, questionIndex);
                                const attemptValue = attempt[attemptKey] || {};
                                const grade = getMockGradeForQuestion(grading, index, questionIndex);
                                return `
                                <li>
                                    <p>${escapeHtml(question.question || question.prompt || question.text || question.body || "Question text unavailable")}</p>
                                    <p class="question-meta">${escapeHtml(question.marks ?? question.marks_allocated ?? 0)} marks · ${escapeHtml(toTitleCase(question.question_type || "question"))}</p>
                                    ${question.question_type === "mcq" && (question.options || []).length ? `
                                        <ul class="mcq-options">
                                            ${(question.options || []).map((option, optionIndex) => `<li>${escapeHtml(String.fromCharCode(65 + optionIndex))}. ${escapeHtml(option)}</li>`).join("")}
                                        </ul>
                                    ` : ""}
                                    <div class="answer-entry">
                                        <label>
                                            <span>Your typed answer</span>
                                            <textarea id="answer-text-${index}-${questionIndex}" rows="5" placeholder="Write your answer here...">${escapeHtml(attemptValue.answer_text || "")}</textarea>
                                        </label>
                                        <label>
                                            <span>Or upload answer-sheet images</span>
                                            <input id="answer-images-${index}-${questionIndex}" type="file" accept="image/*" multiple>
                                        </label>
                                    </div>
                                    ${grade ? `
                                        <div class="grade-card">
                                            <div class="grade-card-header">
                                                <strong>${escapeHtml(grade.marks_awarded)} / ${escapeHtml(grade.max_marks)} marks</strong>
                                                <span>${escapeHtml(grade.verdict || "Reviewed")}</span>
                                            </div>
                                            <p>${escapeHtml(grade.feedback || "No feedback returned.")}</p>
                                            ${(grade.strengths || []).length ? `<p><strong>Strengths:</strong> ${escapeHtml(grade.strengths.join(" | "))}</p>` : ""}
                                            ${(grade.improvements || []).length ? `<p><strong>Improve:</strong> ${escapeHtml(grade.improvements.join(" | "))}</p>` : ""}
                                            ${grade.transcribed_answer ? `<details><summary>Transcribed answer</summary><p>${escapeHtml(grade.transcribed_answer)}</p></details>` : ""}
                                        </div>
                                    ` : ""}
                                </li>
                            `;
                            }).join("")}
                        </ol>
                    </article>
                `;
                }).join("")}
            </div>
            <section class="whole-submission-panel">
                <h4>Submit The Full Paper At The End</h4>
                <p class="section-note">You can upload the whole answer script as a PDF, multiple images, or capture pages live from your camera. The grader will use this along with any question-wise answers you entered.</p>
                <div class="answer-entry">
                    <label>
                        <span>Whole paper text note</span>
                        <textarea id="whole-paper-text" rows="4" placeholder="Optional: add any final note about page order or question numbering...">${escapeHtml(attempt.whole_submission_text || "")}</textarea>
                    </label>
                    <label>
                        <span>Upload full answer script</span>
                        <input id="whole-paper-files" type="file" accept=".pdf,image/*" multiple>
                    </label>
                </div>
                <div class="button-row camera-actions">
                    <button type="button" id="start-whole-paper-camera">Open Camera</button>
                    <button type="button" id="capture-whole-paper-frame">Capture Page</button>
                    <button type="button" id="stop-whole-paper-camera">Stop Camera</button>
                    <button type="button" id="clear-whole-paper-captures">Clear Captures</button>
                </div>
                <div id="whole-paper-camera-panel" class="camera-panel hidden">
                    <video id="whole-paper-camera-video" autoplay playsinline muted></video>
                </div>
                <div id="whole-paper-capture-preview" class="capture-preview"></div>
            </section>
            <div class="button-row mock-paper-actions">
                <button type="button" id="grade-mock-paper-button">Grade My Attempt</button>
            </div>
        </section>
    `;
    mockPaperSection.classList.remove("hidden");

    const gradeButton = document.getElementById("grade-mock-paper-button");
    if (gradeButton) {
        gradeButton.addEventListener("click", submitMockPaperForGrading);
    }

    document.getElementById("start-whole-paper-camera")?.addEventListener("click", startWholePaperCamera);
    document.getElementById("capture-whole-paper-frame")?.addEventListener("click", captureWholePaperFrame);
    document.getElementById("stop-whole-paper-camera")?.addEventListener("click", () => {
        stopWholePaperCamera();
        setStatus("Camera stopped.", "idle");
    });
    document.getElementById("clear-whole-paper-captures")?.addEventListener("click", () => {
        resetWholePaperCaptureFiles();
        renderWholeSubmissionPreview();
        setStatus("Camera captures cleared.", "idle");
    });
    renderWholeSubmissionPreview();
}

async function submitMockPaperForGrading() {
    if (!latestMockPaper?.sections?.length) {
        setStatus("Generate a mock paper before grading an attempt.", "error");
        return;
    }

    const formData = new FormData();
    const answers = [];
    const wholePaperInput = document.getElementById("whole-paper-files");
    const wholePaperText = document.getElementById("whole-paper-text")?.value?.trim() || "";

    (latestMockPaper.sections || []).forEach((section, sectionIndex) => {
        (section.questions || []).forEach((question, questionIndex) => {
            const answerText = document.getElementById(`answer-text-${sectionIndex}-${questionIndex}`)?.value?.trim() || "";
            const imageInput = document.getElementById(`answer-images-${sectionIndex}-${questionIndex}`);
            const answerKey = getMockAttemptKey(sectionIndex, questionIndex);
            latestMockPaperAttempt[answerKey] = { answer_text: answerText };

            answers.push({
                section_index: sectionIndex,
                question_index: questionIndex,
                question: question.question || question.prompt || question.text || question.body || "",
                answer_text: answerText,
            });

            Array.from(imageInput?.files || []).forEach((file) => {
                formData.append(`answer_images_${sectionIndex}_${questionIndex}`, file);
            });
        });
    });

    latestMockPaperAttempt.whole_submission_text = wholePaperText;

    Array.from(wholePaperInput?.files || []).forEach((file) => {
        formData.append("whole_submission_files", file);
    });
    wholePaperCaptureFiles.forEach((file) => {
        formData.append("whole_submission_files", file);
    });

    formData.append("paper", JSON.stringify(latestMockPaper));
    formData.append("answers", JSON.stringify(answers));
    formData.append("subject", document.getElementById("action-subject").value.trim());
    formData.append("topic", document.getElementById("action-topic").value.trim());
    formData.append("institution", document.getElementById("action-institution").value.trim());
    formData.append("whole_submission_text", wholePaperText);

    setStatus("Grading your mock-paper attempt...", "loading");

    try {
        const response = await fetch("/api/library/mock-paper/grade", {
            method: "POST",
            body: formData
        });
        const data = await readResponsePayload(response);
        if (!response.ok) {
            throw new Error(data.error || "Mock paper grading failed.");
        }

        latestMockPaperGrading = data;
        renderMockPaper(latestMockPaper, latestMockPaperGrading, latestMockPaperAttempt);
        setStatus("Your mock paper was graded successfully.", "success");
    } catch (error) {
        setStatus(error.message || "Mock paper grading failed.", "error");
    }
}

function renderTopicList(visualizations) {
    topicList.innerHTML = visualizations.map((item, index) => `
        <button
            type="button"
            class="topic-card ${index === activeTopicIndex ? "active" : ""}"
            data-topic-index="${index}"
        >
            <span class="topic-card-eyebrow">Topic ${index + 1}</span>
            <strong>${escapeHtml(item.title)}</strong>
            <span>${escapeHtml((item.summary || "No summary returned.").slice(0, 110))}${item.summary && item.summary.length > 110 ? "..." : ""}</span>
        </button>
    `).join("");

    topicList.querySelectorAll("[data-topic-index]").forEach((button) => {
        button.addEventListener("click", () => {
            activeTopicIndex = Number(button.dataset.topicIndex);
            renderTopicBrowser(visualizations);
        });
    });
}

function renderTopicDetail(topic, index) {
    topicDetailEmpty.classList.add("hidden");
    topicDetailPanel.classList.remove("hidden");
    topicDetailPanel.innerHTML = `
        <article class="topic-workspace" id="topic-workspace">
            <header class="topic-workspace-header">
                <div>
                    <p class="eyebrow">Active Topic</p>
                    <h3>${escapeHtml(topic.title)}</h3>
                    <p class="card-summary">${escapeHtml(topic.summary || "No summary returned.")}</p>
                </div>
                <div class="topic-actions">
                    <button type="button" id="download-topic-pdf">Download Topic PDF</button>
                </div>
            </header>

            <section id="topic-export-surface" class="topic-export-surface">
                <div class="topic-info-grid">
                    <section class="detail-card">
                        <h4>Key Takeaways</h4>
                        <ul class="takeaways large">
                            ${(topic.key_takeaways || []).map((takeaway) => `<li>${escapeHtml(takeaway)}</li>`).join("")}
                        </ul>
                    </section>
                    <section class="detail-card">
                        <h4>Reading Note</h4>
                        <p class="detail-note">
                            Drag inside each diagram to pan around. The canvases are intentionally larger so the branches are easier to inspect.
                        </p>
                    </section>
                </div>

                <section class="diagram-panel">
                    <div class="diagram-panel-header">
                        <div>
                            <p class="eyebrow">Mindmap</p>
                            <h4>${escapeHtml(topic.title)}</h4>
                        </div>
                    </div>
                    <div class="diagram-stage">
                        <div class="diagram-inner">
                            <div class="mermaid" id="mindmap-active-${index}">${escapeHtml(topic.mindmap_code)}</div>
                        </div>
                    </div>
                </section>

                <section class="diagram-panel">
                    <div class="diagram-panel-header">
                        <div>
                            <p class="eyebrow">Flowchart</p>
                            <h4>${escapeHtml(topic.title)}</h4>
                        </div>
                    </div>
                    <div class="diagram-stage">
                        <div class="diagram-inner">
                            <div class="mermaid" id="flowchart-active-${index}">${escapeHtml(topic.flowchart_code)}</div>
                        </div>
                    </div>
                </section>
            </section>
        </article>
    `;

    renderMermaidIn(topicDetailPanel);

    const downloadButton = document.getElementById("download-topic-pdf");
    if (downloadButton) {
        downloadButton.addEventListener("click", () => downloadTopicPdf(topic));
    }
}

function renderTopicBrowser(visualizations) {
    renderTopicList(visualizations);

    if (!visualizations.length) {
        topicDetailPanel.classList.add("hidden");
        topicDetailEmpty.classList.remove("hidden");
        topicDetailEmpty.textContent = "No topic visualizations were returned.";
        return;
    }

    const safeIndex = Math.min(activeTopicIndex, visualizations.length - 1);
    activeTopicIndex = Math.max(0, safeIndex);
    renderTopicDetail(visualizations[activeTopicIndex], activeTopicIndex);
}

async function downloadTopicPdf(topic) {
    const exportSurface = document.getElementById("topic-export-surface");
    if (!exportSurface || !window.html2canvas || !window.jspdf?.jsPDF) {
        setStatus("PDF export tools are not available in this browser session.", "error");
        return;
    }

    setStatus(`Creating a PDF for ${topic.title}...`, "loading");

    try {
        const canvas = await window.html2canvas(exportSurface, {
            backgroundColor: "#fffaf0",
            scale: 2,
            useCORS: true
        });

        const imageData = canvas.toDataURL("image/png");
        const { jsPDF } = window.jspdf;
        const pdf = new jsPDF("p", "mm", "a4");
        const pageWidth = pdf.internal.pageSize.getWidth();
        const pageHeight = pdf.internal.pageSize.getHeight();
        const imgWidth = pageWidth - 20;
        const imgHeight = canvas.height * imgWidth / canvas.width;

        let heightLeft = imgHeight;
        let position = 10;

        pdf.addImage(imageData, "PNG", 10, position, imgWidth, imgHeight);
        heightLeft -= (pageHeight - 20);

        while (heightLeft > 0) {
            position = heightLeft - imgHeight + 10;
            pdf.addPage();
            pdf.addImage(imageData, "PNG", 10, position, imgWidth, imgHeight);
            heightLeft -= (pageHeight - 20);
        }

        const filename = `${topic.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "topic"}.pdf`;
        pdf.save(filename);
        setStatus(`Downloaded ${topic.title} as a PDF.`, "success");
    } catch (error) {
        setStatus(`Could not create the topic PDF: ${error.message}`, "error");
    }
}

function renderStudyPlan(data) {
    const planItems = data.study_plan || [];
    studyPlanGrid.innerHTML = planItems.map((topic) => `
        <article class="plan-card">
            <h3>${escapeHtml(topic.topic)}</h3>
            <p class="plan-meta">${escapeHtml(topic.duration_minutes)} minutes total</p>
            <ul class="subtopic-list">
                ${(topic.subtopics || []).map((subtopic) => `
                    <li class="subtopic-item">
                        <div class="subtopic-header">
                            <strong>${escapeHtml(subtopic.name)}</strong>
                            <span class="quiz-count">${escapeHtml(subtopic.duration_minutes)} min - ${escapeHtml((subtopic.quiz || []).length)} questions</span>
                        </div>
                    </li>
                `).join("")}
            </ul>
        </article>
    `).join("");

    studyPlanMeta.textContent = `Topics: ${planItems.length} - Tokens: ${data.tokens ?? 0}`;
    studyPlanSection.classList.remove("hidden");
}

uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    const formData = new FormData(uploadForm);
    const files = Array.from(document.getElementById("pdf-file").files || []).filter((file) => file instanceof File && file.name);
    if (!files.length) {
        setStatus("Choose at least one PDF before uploading.", "error");
        return;
    }

    uploadButton.disabled = true;
    studyPlanButton.disabled = true;
    setStatus(`Generating visualizations from ${files.length} PDF${files.length === 1 ? "" : "s"}...`, "loading");
    runMetadata.textContent = "";
    visualSection.classList.add("hidden");
    studyPlanSection.classList.add("hidden");

    try {
        const response = await fetch("/api/mindmap/upload", {
            method: "POST",
            body: formData
        });
        const data = await readResponsePayload(response);
        if (!response.ok) {
            throw new Error(data.error || "Upload failed.");
        }

        latestVisualPayload = data;
        activeTopicIndex = 0;
        renderTopicBrowser(data.visualizations || []);
        visualSection.classList.remove("hidden");
        runMetadata.textContent = `Generated ${data.visualizations?.length || 0} topic visualizations from ${data.source_count || files.length} PDF${(data.source_count || files.length) === 1 ? "" : "s"} in ${data.processing_time || 0}s.`;
        setStatus("Visualizations are ready. Open a topic card to inspect the larger diagrams.", "success");
        studyPlanButton.disabled = !(data.mindmaps && data.mindmaps.length);
    } catch (error) {
        latestVisualPayload = null;
        setStatus(error.message || "Something went wrong while generating visualizations.", "error");
    } finally {
        uploadButton.disabled = false;
    }
});

studyPlanButton.addEventListener("click", async () => {
    if (!latestVisualPayload?.mindmaps?.length) {
        setStatus("Generate visuals first so the study plan has topic structure to work from.", "error");
        return;
    }

    studyPlanButton.disabled = true;
    setStatus("Generating the study plan and quizzes...", "loading");

    try {
        const response = await fetch("/api/streaks/initialize", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({ mindmaps: latestVisualPayload.mindmaps })
        });
        const data = await readResponsePayload(response);
        if (!response.ok) {
            throw new Error(data.error || "Study plan generation failed.");
        }

        renderStudyPlan(data);
        setStatus("Study plan generated successfully.", "success");
    } catch (error) {
        setStatus(error.message || "Something went wrong while generating the study plan.", "error");
    } finally {
        studyPlanButton.disabled = false;
    }
});

libraryForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(libraryForm);
    const hasText = (formData.get("pasted_text") || "").toString().trim();
    const hasFiles = Array.from(document.getElementById("library-files").files || []).length > 0;

    if (!hasText && !hasFiles) {
        setStatus("Add pasted text or at least one file before saving to the library.", "error");
        return;
    }

    libraryUploadButton.disabled = true;
    setStatus("Saving study sources to the library...", "loading");

    try {
        const response = await fetch("/api/library/upload", {
            method: "POST",
            body: formData
        });
        const data = await readResponsePayload(response);
        if (!response.ok) {
            throw new Error(data.error || "Library upload failed.");
        }

        libraryForm.reset();
        await loadSourceLibrary();
        setStatus(data.message || "Study sources saved.", "success");
    } catch (error) {
        setStatus(error.message || "Something went wrong while saving the study sources.", "error");
    } finally {
        libraryUploadButton.disabled = false;
    }
});

flashcardsButton.addEventListener("click", async () => {
    if (!sourceLibrary.length) {
        setStatus("Save some study sources first so flashcards have context.", "error");
        return;
    }

    flashcardsButton.disabled = true;
    setStatus("Generating flashcards from the study library...", "loading");

    try {
        const response = await fetch("/api/library/flashcards", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(getGenerationPayload())
        });
        const data = await readResponsePayload(response);
        if (!response.ok) {
            throw new Error(data.error || "Flashcard generation failed.");
        }

        renderFlashcards(data);
        setStatus("Flashcards generated successfully.", "success");
    } catch (error) {
        setStatus(error.message || "Flashcard generation failed.", "error");
    } finally {
        flashcardsButton.disabled = false;
    }
});

examInsightsButton.addEventListener("click", async () => {
    if (!sourceLibrary.length) {
        setStatus("Save some study sources first so exam analysis has context.", "error");
        return;
    }

    examInsightsButton.disabled = true;
    setStatus("Analyzing previous papers and notes...", "loading");

    try {
        const response = await fetch("/api/library/exam-insights", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(getGenerationPayload())
        });
        const data = await readResponsePayload(response);
        if (!response.ok) {
            throw new Error(data.error || "Exam analysis failed.");
        }

        renderExamInsights(data);
        setStatus("Exam analysis generated successfully.", "success");
    } catch (error) {
        setStatus(error.message || "Exam analysis failed.", "error");
    } finally {
        examInsightsButton.disabled = false;
    }
});

mockPaperButton.addEventListener("click", async () => {
    if (!sourceLibrary.length) {
        setStatus("Save some study sources first so the mock paper has context.", "error");
        return;
    }

    mockPaperButton.disabled = true;
    setStatus("Generating a mock paper from the study library...", "loading");

    try {
        const response = await fetch("/api/library/mock-paper", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(getGenerationPayload())
        });
        const data = await readResponsePayload(response);
        if (!response.ok) {
            throw new Error(data.error || "Mock paper generation failed.");
        }

        latestMockPaper = data;
        latestMockPaperGrading = null;
        latestMockPaperAttempt = {};
        stopWholePaperCamera();
        resetWholePaperCaptureFiles();
        renderMockPaper(data);
        setStatus("Mock paper generated successfully.", "success");
    } catch (error) {
        setStatus(error.message || "Mock paper generation failed.", "error");
    } finally {
        mockPaperButton.disabled = false;
    }
});

loadSourceLibrary().catch((error) => {
    setStatus(error.message || "Could not load the source library.", "error");
});
