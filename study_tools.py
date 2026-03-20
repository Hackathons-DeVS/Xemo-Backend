import json
import re
from typing import List

from pydantic import BaseModel, Field

from gemini_config import create_gemini_client
from mindmaps import clean_text, extract_pdf_page_images, extract_text
from model_routing import get_model_for


MAX_CONTEXT_CHARS = 220000
MAX_DOC_EXCERPT_CHARS = 50000
VISION_SUMMARY_PAGE_LIMIT = 6
SOURCE_PRIORITY = {
    "previous_year_paper": 0,
    "teacher_notes": 1,
    "student_notes": 2,
    "topic_pdf": 3,
    "textbook": 4,
    "syllabus": 5,
    "other": 6,
}


class Flashcard(BaseModel):
    front: str
    back: str
    difficulty: str = "medium"
    source_type: str = ""


class FlashcardResponse(BaseModel):
    deck_title: str
    overview: str
    flashcards: List[Flashcard]


class ExamPattern(BaseModel):
    pattern: str
    evidence: str


class ExamInsightResponse(BaseModel):
    title: str
    overview: str
    repeated_themes: List[str] = Field(default_factory=list)
    probable_questions: List[str] = Field(default_factory=list)
    question_patterns: List[ExamPattern] = Field(default_factory=list)
    revision_priorities: List[str] = Field(default_factory=list)


class MockQuestion(BaseModel):
    question: str
    marks: int
    answer_outline: str


class MockSection(BaseModel):
    title: str
    suggested_time_minutes: int
    questions: List[MockQuestion] = Field(default_factory=list)


class MockPaperResponse(BaseModel):
    paper_title: str
    instructions: List[str] = Field(default_factory=list)
    sections: List[MockSection] = Field(default_factory=list)


def parse_json_fallback(content):
    try:
        return json.loads(content)
    except Exception:
        match = re.search(r"\{.*\}", content or "", re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except Exception:
            return None


def sort_documents_for_context(documents):
    return sorted(
        documents,
        key=lambda doc: (
            SOURCE_PRIORITY.get(doc.get("source_type", "other"), 99),
            doc.get("id", 0),
        ),
    )


def build_context_bundle(documents):
    ordered = sort_documents_for_context(documents)
    remaining = MAX_CONTEXT_CHARS
    parts = []

    for doc in ordered:
        text = clean_text(doc.get("content_text", "") or "")
        if not text:
            continue

        excerpt = text[: min(MAX_DOC_EXCERPT_CHARS, remaining)]
        if not excerpt:
            break

        parts.append(
            "\n".join(
                [
                    f"[Document #{doc.get('id', 'n/a')}]",
                    f"Title: {doc.get('title', 'Untitled')}",
                    f"Source type: {doc.get('source_type', 'other')}",
                    f"Subject: {doc.get('subject', '')}",
                    f"Topic: {doc.get('topic', '')}",
                    f"Institution: {doc.get('institution', '')}",
                    "Content:",
                    excerpt,
                ]
            )
        )
        remaining -= len(excerpt)
        if remaining <= 0:
            break

    if not parts:
        raise ValueError("No usable study-source text is available yet.")

    return "\n\n".join(parts)


def extract_document_content(file_path, source_type="other"):
    raw_text = extract_text(file_path)
    cleaned = clean_text(raw_text)
    if cleaned.strip():
        return cleaned, "text"

    page_images = extract_pdf_page_images(file_path, max_pages=VISION_SUMMARY_PAGE_LIMIT)
    if not page_images:
        return "", "empty"

    summary = generate_document_vision_summary(page_images, source_type=source_type)
    return clean_text(summary), "vision_summary"


def generate_document_vision_summary(page_images, source_type="other"):
    client = create_gemini_client(timeout=120)
    user_content = [
        {
            "type": "text",
            "text": (
                "Read these rendered PDF pages and extract the important study content in plain text. "
                "Preserve important headings, formulas, definitions, and question prompts if present. "
                f"The source type is {source_type}."
            ),
        }
    ]

    for index, image_url in enumerate(page_images, start=1):
        user_content.append({"type": "text", "text": f"Page {index}"})
        user_content.append({"type": "image_url", "image_url": {"url": image_url}})

    response = client.chat.completions.create(
        model=get_model_for("document_vision"),
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract useful educational text from scanned documents. "
                    "Return clean plain text without markdown fences."
                ),
            },
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=3000,
    )
    return response.choices[0].message.content or ""


def generate_flashcards(documents, subject="", topic="", flashcard_count=12):
    context = build_context_bundle(documents)
    client = create_gemini_client(timeout=120)
    model_name = get_model_for("flashcards")
    prompt = f"""
Create a study flashcard deck from the supplied study sources.

Requirements:
- Focus on exam-useful concepts, definitions, formulas, laws, and common confusions.
- Use all sources together, but give extra weight to previous year papers and teacher notes.
- Keep cards concise and student-friendly.
- Generate exactly {flashcard_count} flashcards.
- Mention the most relevant source type for each card.

Subject: {subject or 'General'}
Topic focus: {topic or 'Use the strongest recurring topics from the sources'}

Sources:
{context}
""".strip()

    try:
        response = client.beta.chat.completions.parse(
            model=model_name,
            messages=[
                {"role": "system", "content": "You create concise exam-oriented study flashcards in strict JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=5000,
            response_format=FlashcardResponse,
        )
        parsed = response.choices[0].message.parsed
        return parsed.model_dump()
    except Exception:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You create concise exam-oriented study flashcards in JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=5000,
        )
        parsed = parse_json_fallback(response.choices[0].message.content)
        if not parsed:
            raise ValueError("Could not parse flashcards from the model response.")
        return parsed


def generate_exam_insights(documents, subject="", topic="", institution=""):
    context = build_context_bundle(documents)
    client = create_gemini_client(timeout=120)
    model_name = get_model_for("exam_insights")
    prompt = f"""
Analyze these study sources like an exam-prep coach.

Requirements:
- Use all sources together.
- Previous year papers should strongly influence likely themes and patterns.
- Teacher notes should influence emphasis.
- Output a compact, practical exam strategy.

Subject: {subject or 'General'}
Topic focus: {topic or 'Full provided scope'}
Institution or board: {institution or 'Not specified'}

Sources:
{context}
""".strip()

    try:
        response = client.beta.chat.completions.parse(
            model=model_name,
            messages=[
                {"role": "system", "content": "You analyze exam patterns and revision strategy in strict JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=4500,
            response_format=ExamInsightResponse,
        )
        parsed = response.choices[0].message.parsed
        return parsed.model_dump()
    except Exception:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You analyze exam patterns and revision strategy in JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=4500,
        )
        parsed = parse_json_fallback(response.choices[0].message.content)
        if not parsed:
            raise ValueError("Could not parse exam insights from the model response.")
        return parsed


def generate_mock_paper(documents, subject="", topic="", institution="", total_marks=50, duration_minutes=60):
    context = build_context_bundle(documents)
    client = create_gemini_client(timeout=120)
    model_name = get_model_for("mock_paper")
    prompt = f"""
Create a realistic mock test paper from these study sources.

Requirements:
- Mimic the style suggested by previous year papers if they exist.
- Cover the most exam-relevant parts of the provided material.
- Balance short-answer and longer-answer prompts where appropriate.
- The paper should total about {total_marks} marks.
- Suggested duration should be about {duration_minutes} minutes.
- Include concise answer outlines for evaluation.

Subject: {subject or 'General'}
Topic focus: {topic or 'Full provided scope'}
Institution or board: {institution or 'Not specified'}

Sources:
{context}
""".strip()

    try:
        response = client.beta.chat.completions.parse(
            model=model_name,
            messages=[
                {"role": "system", "content": "You create structured mock exam papers in strict JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=5500,
            response_format=MockPaperResponse,
        )
        parsed = response.choices[0].message.parsed
        return parsed.model_dump()
    except Exception:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You create structured mock exam papers in JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=5500,
        )
        parsed = parse_json_fallback(response.choices[0].message.content)
        if not parsed:
            raise ValueError("Could not parse the mock paper from the model response.")
        return parsed
