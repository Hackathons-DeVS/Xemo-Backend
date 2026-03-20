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

function renderMockPaper(paper) {
    mockPaperTitle.textContent = paper.paper_title || "Mock Paper";
    mockPaperOutput.innerHTML = `
        <section class="mock-paper-card">
            <h3>${escapeHtml(paper.paper_title || "Mock Paper")}</h3>
            <div class="mock-paper-instructions">
                <h4>Instructions</h4>
                <ul>${(paper.instructions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
            </div>
            <div class="mock-paper-sections">
                ${(paper.sections || []).map((section, index) => `
                    <article class="mock-section">
                        <header>
                            <strong>Section ${index + 1}: ${escapeHtml(section.title)}</strong>
                            <span>${escapeHtml(section.suggested_time_minutes)} min suggested</span>
                        </header>
                        <ol>
                            ${(section.questions || []).map((question) => `
                                <li>
                                    <p>${escapeHtml(question.question)}</p>
                                    <p class="question-meta">${escapeHtml(question.marks)} marks</p>
                                    <details>
                                        <summary>Answer outline</summary>
                                        <p>${escapeHtml(question.answer_outline)}</p>
                                    </details>
                                </li>
                            `).join("")}
                        </ol>
                    </article>
                `).join("")}
            </div>
        </section>
    `;
    mockPaperSection.classList.remove("hidden");
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
    const file = formData.get("file");
    if (!(file instanceof File) || !file.name) {
        setStatus("Choose a PDF before uploading.", "error");
        return;
    }

    uploadButton.disabled = true;
    studyPlanButton.disabled = true;
    setStatus("Generating visualizations from your PDF...", "loading");
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
        runMetadata.textContent = `Generated ${data.visualizations?.length || 0} topic visualizations in ${data.processing_time || 0}s.`;
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
