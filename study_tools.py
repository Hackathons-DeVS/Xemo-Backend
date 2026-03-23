import json
import logging
import os
import re
import base64
from typing import List

from openai import APIStatusError, LengthFinishReasonError
from pydantic import BaseModel, Field

from gemini_config import call_gemini_with_rate_limit_retry, create_gemini_client
from mindmaps import clean_text, extract_pdf_page_images, extract_text
from model_routing import get_model_candidates, get_model_for
from openrouter_config import call_openrouter_with_rate_limit_retry, create_openrouter_client


MAX_CONTEXT_CHARS = 220000
MAX_DOC_EXCERPT_CHARS = 50000
EXAM_INSIGHTS_CHUNK_CONTEXT_CHARS = 70000
EXAM_INSIGHTS_DOC_EXCERPT_CHARS = 18000
EXAM_INSIGHTS_MAX_BATCHES = 6
EXAM_INSIGHTS_MAX_CHUNKS_PER_DOC = 2
VISION_SUMMARY_PAGE_LIMIT = 6
SOURCE_SUMMARY_MAX_CHARS = 12000
SOURCE_PRIORITY = {
    "previous_year_paper": 0,
    "textbook": 1,
    "teacher_notes": 2,
    "topic_pdf": 3,
    "student_notes": 4,
    "syllabus": 5,
    "other": 6,
}
SOURCE_EXCERPT_MULTIPLIER = {
    "previous_year_paper": 1.5,
    "textbook": 1.35,
    "teacher_notes": 1.0,
    "topic_pdf": 0.9,
    "student_notes": 0.75,
    "syllabus": 0.7,
    "other": 0.7,
}

logger = logging.getLogger(__name__)
DEBUG_RAW_EXAM_INSIGHTS = os.environ.get("EXAM_INSIGHTS_DEBUG_RAW", "").strip().lower() in {"1", "true", "yes", "on"}


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
    options: List[str] = Field(default_factory=list)
    correct_answer: str = ""
    question_type: str = "short_answer"


class MockSection(BaseModel):
    title: str
    suggested_time_minutes: int
    questions: List[MockQuestion] = Field(default_factory=list)


class MockPaperResponse(BaseModel):
    paper_title: str
    instructions: List[str] = Field(default_factory=list)
    sections: List[MockSection] = Field(default_factory=list)


class GradedQuestionResult(BaseModel):
    question_index: int
    marks_awarded: float
    max_marks: int
    verdict: str
    feedback: str
    strengths: List[str] = Field(default_factory=list)
    improvements: List[str] = Field(default_factory=list)
    transcribed_answer: str = ""


class GradedSectionResult(BaseModel):
    section_index: int
    title: str
    questions: List[GradedQuestionResult] = Field(default_factory=list)


class MockPaperGradeResponse(BaseModel):
    evaluation_title: str
    overall_summary: str
    total_marks_awarded: float
    total_marks_possible: int
    graded_sections: List[GradedSectionResult] = Field(default_factory=list)


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


def extract_message_text(message):
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("value")
                if text:
                    parts.append(str(text))
            else:
                text = getattr(item, "text", None) or getattr(item, "content", None)
                if text:
                    parts.append(str(text))
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


def log_exam_insights_raw(label, text):
    if not DEBUG_RAW_EXAM_INSIGHTS:
        return
    logger.info("RAW_EXAM_INSIGHTS_%s_START", label)
    logger.info("%s", text or "<empty>")
    logger.info("RAW_EXAM_INSIGHTS_%s_END", label)


def is_quota_or_rate_limit_error(exc):
    if not isinstance(exc, APIStatusError):
        return False
    if exc.status_code == 429:
        return True

    body = getattr(exc, "body", None)
    if isinstance(body, (dict, list)):
        body_text = json.dumps(body)
    else:
        body_text = str(body or "")
    body_text = body_text.lower()
    return "resource_exhausted" in body_text or "quota" in body_text or "rate limit" in body_text


def sort_documents_for_context(documents):
    return sorted(
        documents,
        key=lambda doc: (
            SOURCE_PRIORITY.get(doc.get("source_type", "other"), 99),
            doc.get("id", 0),
        ),
    )


def get_document_study_text(doc, prefer_summary=True):
    if prefer_summary:
        summary = clean_text(doc.get("content_summary", "") or "")
        if summary:
            return summary
    return clean_text(doc.get("content_text", "") or "")


def build_context_bundle(documents, max_context_chars=MAX_CONTEXT_CHARS, max_doc_excerpt_chars=MAX_DOC_EXCERPT_CHARS):
    ordered = sort_documents_for_context(documents)
    remaining = max_context_chars
    parts = []

    for doc in ordered:
        text = get_document_study_text(doc, prefer_summary=True)
        if not text:
            continue

        excerpt = text[: min(max_doc_excerpt_chars, remaining)]
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


def build_source_type_context(documents, source_type, max_chars, heading):
    remaining = max_chars
    parts = []

    for doc in sort_documents_for_context(documents):
        if doc.get("source_type") != source_type:
            continue

        text = get_document_study_text(doc, prefer_summary=True)
        if not text or remaining <= 0:
            continue

        excerpt = text[:remaining]
        if not excerpt:
            continue

        parts.append(
            "\n".join(
                [
                    f"[{heading}]",
                    f"Title: {doc.get('title', 'Untitled')}",
                    f"Topic: {doc.get('topic', '')}",
                    f"Institution: {doc.get('institution', '')}",
                    "Content:",
                    excerpt,
                ]
            )
        )
        remaining -= len(excerpt)

    return "\n\n".join(parts).strip()


def build_mock_paper_context(documents):
    pyq_context = build_source_type_context(
        documents,
        "previous_year_paper",
        max_chars=22000,
        heading="Previous Year Paper Pattern Reference",
    )
    textbook_context = build_source_type_context(
        documents,
        "textbook",
        max_chars=42000,
        heading="Textbook Concept Coverage",
    )
    teacher_notes_context = build_source_type_context(
        documents,
        "teacher_notes",
        max_chars=18000,
        heading="Teacher Notes Emphasis",
    )
    topic_pdf_context = build_source_type_context(
        documents,
        "topic_pdf",
        max_chars=16000,
        heading="Topic PDF Support",
    )
    student_notes_context = build_source_type_context(
        documents,
        "student_notes",
        max_chars=10000,
        heading="Student Notes Support",
    )
    general_context = build_context_bundle(
        [doc for doc in documents if doc.get("source_type") not in {"previous_year_paper", "textbook", "teacher_notes", "topic_pdf", "student_notes"}],
        max_context_chars=12000,
        max_doc_excerpt_chars=8000,
    ) if any(doc.get("source_type") not in {"previous_year_paper", "textbook", "teacher_notes", "topic_pdf", "student_notes"} for doc in documents) else ""

    blocks = [
        pyq_context,
        textbook_context,
        teacher_notes_context,
        topic_pdf_context,
        student_notes_context,
        general_context,
    ]
    combined = "\n\n".join(block for block in blocks if block).strip()
    if not combined:
        raise ValueError("No usable study-source text is available yet.")
    return combined


def build_context_batches(
    documents,
    max_context_chars=EXAM_INSIGHTS_CHUNK_CONTEXT_CHARS,
    max_doc_excerpt_chars=EXAM_INSIGHTS_DOC_EXCERPT_CHARS,
    max_batches=EXAM_INSIGHTS_MAX_BATCHES,
):
    ordered = sort_documents_for_context(documents)
    batches = []
    current_parts = []
    current_chars = 0

    for doc in ordered:
        text = clean_text(doc.get("content_text", "") or "")
        if not text:
            continue

        excerpt = text[:max_doc_excerpt_chars]
        if not excerpt:
            continue

        block = "\n".join(
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

        block_len = len(block)
        if current_parts and current_chars + block_len > max_context_chars:
            batches.append("\n\n".join(current_parts))
            current_parts = []
            current_chars = 0
            if len(batches) >= max_batches:
                break

        if block_len > max_context_chars:
            block = block[:max_context_chars]
            block_len = len(block)

        current_parts.append(block)
        current_chars += block_len

    if current_parts and len(batches) < max_batches:
        batches.append("\n\n".join(current_parts))

    if not batches:
        raise ValueError("No usable study-source text is available yet.")

    return batches


def split_text_for_exam_analysis(text, chunk_size, max_chunks):
    text = clean_text(text or "")
    if not text:
        return []

    chunks = []
    start = 0
    while start < len(text) and len(chunks) < max_chunks:
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end
    return chunks


def build_document_analysis_units(
    documents,
    base_chunk_chars=EXAM_INSIGHTS_DOC_EXCERPT_CHARS,
    max_chunks_per_doc=EXAM_INSIGHTS_MAX_CHUNKS_PER_DOC,
):
    ordered = sort_documents_for_context(documents)
    units = []

    for doc in ordered:
        source_type = doc.get("source_type", "other")
        text = get_document_study_text(doc, prefer_summary=True)
        if not text:
            continue

        multiplier = SOURCE_EXCERPT_MULTIPLIER.get(source_type, 1.0)
        chunk_size = max(6000, int(base_chunk_chars * multiplier))
        chunk_limit = max_chunks_per_doc if source_type in {"previous_year_paper", "textbook"} else 1
        chunks = split_text_for_exam_analysis(text, chunk_size=chunk_size, max_chunks=chunk_limit)

        for chunk_index, chunk in enumerate(chunks, start=1):
            units.append(
                {
                    "id": doc.get("id", "n/a"),
                    "title": doc.get("title", "Untitled"),
                    "source_type": source_type,
                    "subject": doc.get("subject", ""),
                    "topic": doc.get("topic", ""),
                    "institution": doc.get("institution", ""),
                    "chunk_index": chunk_index,
                    "chunk_count": len(chunks),
                    "content": chunk,
                }
            )

    return units


def build_exam_insights_prompt(context, subject="", topic="", institution="", compact=False):
    compact_rules = """
- Keep the entire response compact.
- overview: maximum 80 words
- repeated_themes: exactly 4 items
- probable_questions: exactly 5 items
- question_patterns: exactly 4 items, each with one short evidence sentence
- revision_priorities: exactly 5 items
- title: maximum 8 words
""".strip()

    standard_rules = """
- Use all sources together.
- Previous year papers should strongly influence likely themes and patterns.
- Teacher notes should influence emphasis.
- Output a compact, practical exam strategy.
""".strip()

    return f"""
Analyze these study sources like an exam-prep coach.

Requirements:
{compact_rules if compact else standard_rules}

Subject: {subject or 'General'}
Topic focus: {topic or 'Full provided scope'}
Institution or board: {institution or 'Not specified'}

Sources:
{context}
""".strip()


def build_exam_insight_batch_prompt(context, batch_index, total_batches, subject="", topic="", institution=""):
    return f"""
Analyze this study-source batch for exam preparation.

Requirements:
- Focus on what is actually likely to matter in exams.
- Give extra weight to previous year papers and teacher notes.
- Stay concise and return only a valid JSON object.
- summary: maximum 90 words
- repeated_themes: 3 to 5 short items
- probable_questions: 3 to 5 short items
- question_patterns: 2 to 4 items with pattern and evidence
- revision_priorities: 3 to 5 short items

Batch: {batch_index} of {total_batches}
Subject: {subject or 'General'}
Topic focus: {topic or 'Full provided scope'}
Institution or board: {institution or 'Not specified'}

Sources:
{context}
""".strip()


def build_exam_insight_document_prompt(unit, unit_index, total_units, subject="", topic="", institution=""):
    source_type = unit.get("source_type", "other").replace("_", " ")
    emphasis = (
        "This is a highest-priority source. Treat exam pattern evidence from it as highly important."
        if unit.get("source_type") in {"previous_year_paper", "textbook"}
        else "Use this source as supporting material and align it to the higher-priority sources when possible."
    )

    return f"""
Analyze this single study source for exam preparation.

Requirements:
- Return only a valid JSON object.
- summary: maximum 90 words
- repeated_themes: 3 to 5 short items
- probable_questions: 3 to 5 short items
- question_patterns: 2 to 4 items with pattern and evidence
- revision_priorities: 3 to 5 short items
- Prioritize exam-relevant ideas, chapter weightage clues, repeated derivations, formulas, definitions, and likely question styles.

Global subject: {subject or unit.get('subject') or 'General'}
Global topic focus: {topic or unit.get('topic') or 'Full provided scope'}
Institution or board: {institution or unit.get('institution') or 'Not specified'}
Source {unit_index} of {total_units}
Source title: {unit.get('title', 'Untitled')}
Source type: {source_type}
Source chunk: {unit.get('chunk_index', 1)} of {unit.get('chunk_count', 1)}
Priority guidance: {emphasis}

Source content:
{unit.get('content', '')}
""".strip()


def build_exam_insight_synthesis_prompt(batch_analyses, subject="", topic="", institution=""):
    return f"""
Combine these intermediate exam-analysis notes into one final student-friendly exam insights report.

Requirements:
- Return only a valid JSON object.
- Use all batch analyses together.
- De-duplicate repeated points.
- repeated_themes: exactly 4 items
- probable_questions: exactly 5 items
- question_patterns: exactly 4 items with pattern and evidence
- revision_priorities: exactly 5 items
- overview: maximum 100 words
- title: maximum 8 words

Subject: {subject or 'General'}
Topic focus: {topic or 'Full provided scope'}
Institution or board: {institution or 'Not specified'}

Intermediate analyses:
{json.dumps(batch_analyses, ensure_ascii=True, indent=2)}
""".strip()


def repair_json_payload(client, model_name, raw_text, schema_name, schema_requirements):
    if not raw_text or not raw_text.strip():
        return None

    repair_prompt = f"""
Convert the following model output into a valid JSON object.

Schema name: {schema_name}
Requirements:
{schema_requirements}

Return only valid JSON and do not omit useful information that is already present.

Raw model output:
{raw_text}
""".strip()

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {
                "role": "system",
                "content": "You repair partially formatted AI output into strict JSON. Return only valid JSON.",
            },
            {"role": "user", "content": repair_prompt},
        ],
        temperature=0.1,
        max_tokens=2200,
        response_format={"type": "json_object"},
    )
    repaired_text = extract_message_text(response.choices[0].message)
    return parse_json_fallback(repaired_text)


def normalize_exam_insights_payload(parsed, subject="", topic=""):
    if not isinstance(parsed, dict):
        return None

    result = {
        "title": parsed.get("title") or f"{topic or subject or 'Study'} Exam Insights",
        "overview": parsed.get("overview") or "Review the repeated themes and probable questions first.",
        "repeated_themes": parsed.get("repeated_themes") or [],
        "probable_questions": parsed.get("probable_questions") or [],
        "question_patterns": parsed.get("question_patterns") or [],
        "revision_priorities": parsed.get("revision_priorities") or [],
    }

    if not isinstance(result["question_patterns"], list):
        result["question_patterns"] = []

    normalized_patterns = []
    for item in result["question_patterns"][:4]:
        if isinstance(item, dict):
            normalized_patterns.append(
                {
                    "pattern": item.get("pattern", "Recurring question style"),
                    "evidence": item.get("evidence", "Observed in the supplied sources."),
                }
            )
        elif isinstance(item, str):
            normalized_patterns.append(
                {
                    "pattern": item,
                    "evidence": "Observed in the supplied sources.",
                }
            )
    result["question_patterns"] = normalized_patterns

    for key in ("repeated_themes", "probable_questions", "revision_priorities"):
        if not isinstance(result[key], list):
            result[key] = []
        result[key] = [str(item).strip() for item in result[key] if str(item).strip()][:6]

    return result


def normalize_exam_batch_payload(parsed):
    if not isinstance(parsed, dict):
        return None

    result = {
        "summary": str(parsed.get("summary") or "").strip(),
        "repeated_themes": parsed.get("repeated_themes") or [],
        "probable_questions": parsed.get("probable_questions") or [],
        "question_patterns": parsed.get("question_patterns") or [],
        "revision_priorities": parsed.get("revision_priorities") or [],
    }

    for key in ("repeated_themes", "probable_questions", "revision_priorities"):
        if not isinstance(result[key], list):
            result[key] = []
        result[key] = [str(item).strip() for item in result[key] if str(item).strip()][:5]

    if not isinstance(result["question_patterns"], list):
        result["question_patterns"] = []

    normalized_patterns = []
    for item in result["question_patterns"][:4]:
        if isinstance(item, dict):
            normalized_patterns.append(
                {
                    "pattern": str(item.get("pattern") or "Recurring question style").strip(),
                    "evidence": str(item.get("evidence") or "Observed in the supplied sources.").strip(),
                }
            )
        elif isinstance(item, str):
            normalized_patterns.append(
                {
                    "pattern": item.strip(),
                    "evidence": "Observed in the supplied sources.",
                }
            )
    result["question_patterns"] = normalized_patterns
    return result


def extract_marks_from_text(text):
    if not text:
        return 0
    match = re.search(r"(\d+)\s*marks?", str(text), re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except Exception:
            return 0
    return 0


def normalize_marks_value(value, fallback=0):
    if value is None:
        return fallback
    if isinstance(value, (int, float)):
        try:
            return max(int(value), 0)
        except Exception:
            return fallback

    text = str(value).strip()
    if not text:
        return fallback
    if text.isdigit():
        return max(int(text), 0)
    extracted = extract_marks_from_text(text)
    return extracted if extracted else fallback


def dominant_positive_mark(values):
    counts = {}
    for value in values or []:
        normalized = normalize_marks_value(value, fallback=0)
        if normalized <= 0:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
    if not counts:
        return 0
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def estimate_section_time_minutes(section_marks, total_marks, duration_minutes, question_count):
    section_marks = max(int(section_marks or 0), 0)
    question_count = max(int(question_count or 0), 0)
    if section_marks <= 0 and question_count <= 0:
        return 0
    if total_marks and duration_minutes and section_marks > 0:
        proportional = round((section_marks / max(total_marks, 1)) * duration_minutes)
        return max(proportional, max(question_count * 2, 4))
    return max(question_count * 3, round(section_marks * 1.4), 4)


def infer_question_type(question_text, marks):
    text = str(question_text or "").lower()
    marks = normalize_marks_value(marks, fallback=0)
    if any(keyword in text for keyword in ["mcq", "choose the correct", "select the correct", "option"]):
        return "mcq"
    if any(keyword in text for keyword in ["derive", "prove", "show that", "deduce"]):
        return "derivation"
    if any(keyword in text for keyword in ["calculate", "find", "determine", "solve", "numerical"]):
        return "numerical"
    if any(keyword in text for keyword in ["explain", "describe", "discuss"]):
        return "explanation"
    if any(keyword in text for keyword in ["differentiate", "distinguish", "compare"]):
        return "comparison"
    if marks <= 1:
        return "recall"
    if marks <= 2:
        return "short_answer"
    if marks >= 5:
        return "long_answer"
    return "application"


def collect_focus_areas(documents, limit=8):
    areas = []
    for doc in sort_documents_for_context(documents):
        label = (
            (doc.get("topic") or "").strip()
            or (doc.get("title") or "").strip()
        )
        if not label:
            continue
        normalized = re.sub(r"\s+", " ", label)
        if normalized in areas:
            continue
        areas.append(normalized)
        if len(areas) >= limit:
            break
    return areas


def build_mock_paper_blueprint(total_marks, duration_minutes, use_mcq_opening=False):
    section_templates = [
        (
            "Section A - Objective MCQ" if use_mcq_opening else "Section A - Quick Recall",
            1,
            "multiple-choice questions with four options covering definitions, one-line facts, units, and direct formulas"
            if use_mcq_opening
            else "definitions, one-line facts, units, direct formulas",
        ),
        ("Section B - Core Application", 2, "short explanations, conversions, standard applications"),
        ("Section C - Worked Concepts", 3, "reasoning, short derivations, compact numericals"),
        ("Section D - Deep Reasoning", 4, "multi-step explanations, reactions, structured solutions"),
        ("Section E - Long Answer", 5, "full derivations, extended explanations, mixed-part answers"),
    ]
    active_templates = [
        item for item in section_templates
        if total_marks >= item[1] * 2
    ] or section_templates[:3]

    total_ratio = sum(item[1] for item in active_templates)
    lines = []
    for title, marks_each, focus in active_templates:
        target_marks = max(marks_each * 2, round(total_marks * (marks_each / total_ratio)))
        estimated_questions = max(1, round(target_marks / marks_each))
        estimated_time = estimate_section_time_minutes(
            estimated_questions * marks_each,
            total_marks,
            duration_minutes,
            estimated_questions,
        )
        lines.append(
            f"- {title}: around {estimated_questions} question(s) x {marks_each} mark(s), about {estimated_time} minutes, focus on {focus}."
        )
    return "\n".join(lines)


def normalize_mock_paper_payload(parsed, subject="", topic="", total_marks=None, duration_minutes=None):
    if not isinstance(parsed, dict):
        return None

    sections_source = parsed.get("sections") or parsed.get("paper_sections") or []
    if isinstance(sections_source, dict):
        sections_source = list(sections_source.values())
    if not isinstance(sections_source, list):
        sections_source = []

    normalized_sections = []
    overall_marks = 0
    for section_index, section in enumerate(sections_source, start=1):
        if isinstance(section, str):
            section = {"title": f"Section {section_index}", "questions": [section]}
        if not isinstance(section, dict):
            continue

        section_default_marks = normalize_marks_value(
            section.get("marks_each") or section.get("marks_per_question") or extract_marks_from_text(section.get("title", "")),
            fallback=0,
        )

        raw_questions = section.get("questions") or section.get("items") or section.get("prompts") or []
        if isinstance(raw_questions, dict):
            raw_questions = list(raw_questions.values())
        if not isinstance(raw_questions, list):
            raw_questions = []

        normalized_questions = []
        inferred_section_marks = []
        for question_index, item in enumerate(raw_questions, start=1):
            if isinstance(item, str):
                item = {"question": item}
            if not isinstance(item, dict):
                continue

            question_text = (
                item.get("question")
                or item.get("prompt")
                or item.get("stem")
                or item.get("question_text")
                or item.get("text")
                or item.get("body")
                or item.get("title")
                or item.get("name")
                or item.get("query")
                or ""
            )
            answer_outline = (
                item.get("answer_outline")
                or item.get("answer")
                or item.get("expected_answer")
                or item.get("explanation")
                or item.get("solution")
                or ""
            )
            options = item.get("options") or item.get("choices") or item.get("mcq_options") or []
            if isinstance(options, dict):
                options = list(options.values())
            if not isinstance(options, list):
                options = []
            options = [str(option).strip() for option in options if str(option).strip()][:4]
            marks = normalize_marks_value(
                item.get("marks") or item.get("mark") or item.get("points") or item.get("question_marks") or extract_marks_from_text(question_text),
                fallback=section_default_marks,
            )
            if marks > 0:
                inferred_section_marks.append(marks)

            question_type = str(
                item.get("question_type")
                or ("mcq" if options else infer_question_type(question_text, marks))
            ).strip()
            correct_answer = str(
                item.get("correct_answer")
                or item.get("answer_key")
                or item.get("correct_option")
                or ""
            ).strip()

            normalized_questions.append(
                {
                    "question": str(question_text).strip() or f"Question {question_index}",
                    "marks": marks,
                    "answer_outline": str(answer_outline).strip(),
                    "question_type": question_type,
                    "options": options,
                    "correct_answer": correct_answer,
                }
            )

        fallback_marks = section_default_marks or dominant_positive_mark(inferred_section_marks)
        if fallback_marks <= 0:
            fallback_marks = dominant_positive_mark(
                normalize_marks_value(question.get("marks"), fallback=0)
                for question in normalized_questions
            ) or max(1, min(5, section_index))

        for question in normalized_questions:
            question["marks"] = normalize_marks_value(question.get("marks"), fallback=fallback_marks)

        section_marks_total = sum(question.get("marks", 0) for question in normalized_questions)
        overall_marks += section_marks_total
        section_time = normalize_marks_value(
            section.get("suggested_time_minutes") or section.get("duration_minutes") or section.get("time_minutes"),
            fallback=estimate_section_time_minutes(
                section_marks_total,
                total_marks or overall_marks,
                duration_minutes or 0,
                len(normalized_questions),
            ),
        )

        section_title = str(section.get("title") or section.get("name") or section.get("section") or f"Section {section_index}").strip()
        if section_title.lower() in {f"section {section_index}".lower(), "section"} and normalized_questions:
            first_type = normalized_questions[0].get("question_type", "")
            if first_type == "mcq":
                section_title = "Objective MCQ"
            elif any(question.get("marks", 0) >= 4 for question in normalized_questions):
                section_title = "Long Answer"
            else:
                section_title = f"Section {section_index}"

        normalized_sections.append(
            {
                "title": section_title,
                "suggested_time_minutes": section_time,
                "questions": normalized_questions,
            }
        )

    instructions = parsed.get("instructions") or parsed.get("exam_instructions") or []
    if isinstance(instructions, str):
        instructions = [instructions]
    if not isinstance(instructions, list):
        instructions = []

    result = {
        "paper_title": str(parsed.get("paper_title") or parsed.get("title") or parsed.get("paper_name") or f"{topic or subject or 'Study'} Mock Paper").strip(),
        "instructions": [str(item).strip() for item in instructions if str(item).strip()],
        "sections": normalized_sections,
        "total_marks": overall_marks,
        "target_total_marks": normalize_marks_value(total_marks, fallback=overall_marks),
    }

    return result if result["sections"] else None


def normalize_mock_paper_grade_payload(parsed):
    if not isinstance(parsed, dict):
        return None

    sections_source = parsed.get("graded_sections") or parsed.get("sections") or []
    if isinstance(sections_source, dict):
        sections_source = list(sections_source.values())
    if not isinstance(sections_source, list):
        sections_source = []

    graded_sections = []
    total_possible_running = 0
    total_awarded_running = 0.0

    for section_index, section in enumerate(sections_source):
        if not isinstance(section, dict):
            continue
        raw_questions = section.get("questions") or section.get("graded_questions") or []
        if isinstance(raw_questions, dict):
            raw_questions = list(raw_questions.values())
        if not isinstance(raw_questions, list):
            raw_questions = []

        normalized_questions = []
        for question_index, item in enumerate(raw_questions):
            if not isinstance(item, dict):
                continue
            max_marks = normalize_marks_value(item.get("max_marks") or item.get("marks") or item.get("possible_marks"), fallback=0)
            marks_awarded_raw = item.get("marks_awarded") if item.get("marks_awarded") is not None else item.get("score")
            try:
                marks_awarded = float(marks_awarded_raw if marks_awarded_raw is not None else 0)
            except Exception:
                marks_awarded = 0.0
            marks_awarded = min(max(marks_awarded, 0.0), float(max_marks))
            if max_marks > 0:
                marks_awarded = round(marks_awarded * 2) / 2

            total_possible_running += max_marks
            total_awarded_running += marks_awarded

            strengths = item.get("strengths") or []
            improvements = item.get("improvements") or item.get("next_steps") or []
            if isinstance(strengths, str):
                strengths = [strengths]
            if isinstance(improvements, str):
                improvements = [improvements]

            normalized_questions.append(
                {
                    "question_index": int(item.get("question_index", question_index)),
                    "marks_awarded": marks_awarded,
                    "max_marks": max_marks,
                    "verdict": str(item.get("verdict") or item.get("band") or "Needs review").strip(),
                    "feedback": str(item.get("feedback") or item.get("comment") or item.get("reasoning") or "").strip(),
                    "strengths": [str(value).strip() for value in strengths if str(value).strip()],
                    "improvements": [str(value).strip() for value in improvements if str(value).strip()],
                    "transcribed_answer": str(item.get("transcribed_answer") or item.get("student_answer") or "").strip(),
                }
            )

        graded_sections.append(
            {
                "section_index": int(section.get("section_index", section_index)),
                "title": str(section.get("title") or f"Section {section_index + 1}").strip(),
                "questions": normalized_questions,
            }
        )

    total_possible = normalize_marks_value(parsed.get("total_marks_possible"), fallback=total_possible_running)
    total_awarded_raw = parsed.get("total_marks_awarded")
    try:
        total_awarded = float(total_awarded_raw if total_awarded_raw is not None else total_awarded_running)
    except Exception:
        total_awarded = total_awarded_running
    total_awarded = min(max(round(total_awarded, 2), 0.0), float(total_possible or total_possible_running or 0))

    result = {
        "evaluation_title": str(parsed.get("evaluation_title") or parsed.get("title") or "Paper Evaluation").strip(),
        "overall_summary": str(parsed.get("overall_summary") or parsed.get("summary") or "").strip(),
        "total_marks_awarded": round(total_awarded_running, 2),
        "total_marks_possible": total_possible_running or total_possible,
        "graded_sections": graded_sections,
    }

    return result if graded_sections else None


def build_fallback_exam_insights(documents, subject="", topic="", institution=""):
    ordered = sort_documents_for_context(documents)
    title_seed = topic or subject or "Study"
    repeated_themes = []
    revision_priorities = []

    for doc in ordered:
        label = doc.get("topic") or doc.get("title") or doc.get("source_type", "Source")
        clean_label = re.sub(r"\s+", " ", str(label)).strip()
        if clean_label and clean_label not in repeated_themes:
            repeated_themes.append(clean_label)
        source_hint = f"Review {clean_label} from {doc.get('source_type', 'source').replace('_', ' ')}"
        if source_hint not in revision_priorities:
            revision_priorities.append(source_hint)
        if len(repeated_themes) >= 4 and len(revision_priorities) >= 5:
            break

    probable_questions = [
        f"Explain the core idea behind {item}."
        for item in repeated_themes[:3]
    ]
    if institution:
        probable_questions.append(f"Write an institution-style long answer on {topic or subject or 'the main topic'}.")

    return {
        "title": f"{title_seed} Exam Insights",
        "overview": "Generated a fallback exam strategy because the model response was incomplete. Focus on the recurring source themes first.",
        "repeated_themes": repeated_themes[:4],
        "probable_questions": probable_questions[:5],
        "question_patterns": [
            {
                "pattern": "Repeated conceptual explanation questions",
                "evidence": "Common themes appear across the supplied study sources.",
            }
        ],
        "revision_priorities": revision_priorities[:5],
    }


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


def summarize_study_source(content_text, title="", source_type="other", subject="", topic="", institution=""):
    cleaned = clean_text(content_text or "")
    if not cleaned:
        return "", "empty"

    excerpt = cleaned[:SOURCE_SUMMARY_MAX_CHARS]
    if len(cleaned) <= 2500:
        return excerpt, "direct_excerpt"

    client = create_openrouter_client(timeout=120)
    prompt = f"""
Create a reusable study summary for this uploaded source.

Requirements:
- Focus on exam-relevant concepts, formulas, derivations, definitions, and recurring question styles.
- Preserve chapter/topic signals that will help later flashcard, exam-insight, and mock-paper generation.
- Prioritize signal over prose.
- Keep the summary under 900 words.
- Use plain text with short headings and bullet-style lines.
- If the source is a previous year paper, emphasize pattern, marks distribution, and repeated question styles.
- If the source is a textbook, emphasize core concepts, formulas, definitions, and standard derivations.

Title: {title or 'Untitled Source'}
Source type: {source_type}
Subject: {subject or 'General'}
Topic: {topic or 'Not specified'}
Institution: {institution or 'Not specified'}

Source content:
{excerpt}
""".strip()

    try:
        response = call_openrouter_with_rate_limit_retry(
            client.chat.completions.create,
            operation_name=f"Source summarization {title or source_type or 'source'}",
            model=get_model_for("source_summary"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You compress educational material into reusable study summaries. "
                        "Return plain text only."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=1800,
        )
        summary_text = clean_text(extract_message_text(response.choices[0].message))
        if summary_text:
            return summary_text, "ai_summary"
    except Exception as exc:
        logger.warning("Source summarization failed for '%s': %s", title or source_type or 'source', exc)

    return excerpt, "direct_excerpt"


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

    response = call_gemini_with_rate_limit_retry(
        client.chat.completions.create,
        operation_name="Document vision summary",
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


def image_file_to_data_url(uploaded_file):
    mime_type = getattr(uploaded_file, "mimetype", None) or "image/png"
    raw_bytes = uploaded_file.read()
    if hasattr(uploaded_file, "stream"):
        uploaded_file.stream.seek(0)
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def transcribe_answer_images(answer_files, question_text=""):
    if not answer_files:
        return ""

    client = create_gemini_client(timeout=120)
    user_content = [
        {
            "type": "text",
            "text": (
                "Transcribe this student's handwritten or typed answer sheet into clean plain text. "
                "Preserve equations, steps, bullet points, and chemistry notation as faithfully as possible. "
                "Do not summarize."
            ),
        }
    ]
    if question_text:
        user_content.append({"type": "text", "text": f"Question context: {question_text}"})

    for index, answer_file in enumerate(answer_files, start=1):
        user_content.append({"type": "text", "text": f"Answer page {index}"})
        user_content.append({"type": "image_url", "image_url": {"url": image_file_to_data_url(answer_file)}})

    response = call_gemini_with_rate_limit_retry(
        client.chat.completions.create,
        operation_name="Answer sheet transcription",
        model=get_model_for("document_vision"),
        messages=[
            {
                "role": "system",
                "content": "You transcribe answer sheets into clean plain text. Return only the transcription.",
            },
            {"role": "user", "content": user_content},
        ],
        temperature=0.1,
        max_tokens=2500,
    )
    return clean_text(extract_message_text(response.choices[0].message))


def transcribe_submission_files(submission_files, upload_dir=None):
    if not submission_files:
        return "", []

    image_files = []
    extracted_parts = []
    temp_paths = []
    accepted_files = []

    try:
        for upload in submission_files:
            if not upload or not upload.filename:
                continue
            accepted_files.append(upload.filename)
            extension = os.path.splitext(upload.filename)[1].lower()
            if extension == ".pdf":
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(upload.filename)) or "submission.pdf"
                target_dir = upload_dir or os.getcwd()
                os.makedirs(target_dir, exist_ok=True)
                temp_path = os.path.join(target_dir, f"submission_{int(time.time() * 1000)}_{safe_name}")
                upload.save(temp_path)
                temp_paths.append(temp_path)
                extracted_text, _origin = extract_document_content(temp_path, source_type="student_notes")
                if extracted_text.strip():
                    extracted_parts.append(
                        f"[Submitted PDF: {upload.filename}]\n{extracted_text}"
                    )
            elif extension in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
                image_files.append(upload)

        if image_files:
            image_text = transcribe_answer_images(
                image_files,
                question_text="Full mock-paper student submission",
            )
            if image_text.strip():
                extracted_parts.append(
                    f"[Submitted answer images]\n{image_text}"
                )
    finally:
        for temp_path in temp_paths:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

    return clean_text("\n\n".join(part for part in extracted_parts if part)), accepted_files


def generate_flashcards(documents, subject="", topic="", flashcard_count=12):
    context = build_context_bundle(documents)
    client = create_openrouter_client(timeout=120)
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
        response = call_openrouter_with_rate_limit_retry(
            client.beta.chat.completions.parse,
            operation_name="Flashcard generation structured",
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
        response = call_openrouter_with_rate_limit_retry(
            client.chat.completions.create,
            operation_name="Flashcard generation compact",
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
    client = create_openrouter_client(timeout=120)
    for model_name in get_model_candidates("exam_insights"):
        logger.info("Generating exam insights with model: %s", model_name)
        batch_analyses = []

        try:
            analysis_units = build_document_analysis_units(documents)
            if not analysis_units:
                raise ValueError("No usable study-source text is available yet.")

            for index, unit in enumerate(analysis_units, start=1):
                batch_prompt = build_exam_insight_document_prompt(
                    unit,
                    unit_index=index,
                    total_units=len(analysis_units),
                    subject=subject,
                    topic=topic,
                    institution=institution,
                )
                response = call_openrouter_with_rate_limit_retry(
                    client.chat.completions.create,
                    operation_name=f"Exam insights batch analysis {index}",
                    model=model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You analyze educational material for exam preparation. "
                                "Return only a valid JSON object."
                            ),
                        },
                        {"role": "user", "content": batch_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=1600,
                    response_format={"type": "json_object"},
                )
                raw_batch_text = extract_message_text(response.choices[0].message)
                batch_label = re.sub(r"[^A-Z0-9]+", "_", unit.get("title", "UNTITLED").upper()).strip("_")[:60] or "UNTITLED"
                log_exam_insights_raw(f"BATCH_{index}_{batch_label}", raw_batch_text)
                parsed_batch = parse_json_fallback(raw_batch_text)
                if not parsed_batch and raw_batch_text:
                    logger.info(
                        "Repairing non-JSON batch exam analysis for source '%s' with model %s.",
                        unit.get("title", "Untitled"),
                        model_name,
                    )
                    parsed_batch = repair_json_payload(
                        client,
                        model_name,
                        raw_batch_text,
                        "exam_source_analysis",
                        """
- summary: string
- repeated_themes: array of short strings
- probable_questions: array of short strings
- question_patterns: array of objects with pattern and evidence
- revision_priorities: array of short strings
""".strip(),
                    )
                parsed_batch = normalize_exam_batch_payload(parsed_batch)
                if parsed_batch:
                    parsed_batch["source_type"] = unit.get("source_type", "other")
                    parsed_batch["source_title"] = unit.get("title", "Untitled")
                    batch_analyses.append(parsed_batch)
                else:
                    logger.warning(
                        "Could not parse batch exam analysis for source '%s'. Raw snippet: %s",
                        unit.get("title", "Untitled"),
                        raw_batch_text[:300],
                    )
        except Exception as exc:
            if is_quota_or_rate_limit_error(exc):
                logger.warning(
                    "Model %s hit quota/rate limits during batch exam-insights analysis. Trying fallback model.",
                    model_name,
                )
                continue
            logger.warning(
                "Batch exam-insights analysis failed for model %s, falling back to single-pass mode: %s",
                model_name,
                exc,
            )

        if batch_analyses:
            synthesis_prompt = build_exam_insight_synthesis_prompt(
                batch_analyses,
                subject=subject,
                topic=topic,
                institution=institution,
            )
            try:
                response = call_openrouter_with_rate_limit_retry(
                    client.beta.chat.completions.parse,
                    operation_name="Exam insights synthesis structured",
                    model=model_name,
                    messages=[
                        {"role": "system", "content": "You analyze exam patterns and revision strategy in strict JSON."},
                        {"role": "user", "content": synthesis_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=3200,
                    response_format=ExamInsightResponse,
                )
                log_exam_insights_raw("SYNTHESIS_STRUCTURED", extract_message_text(response.choices[0].message))
                parsed = response.choices[0].message.parsed
                return parsed.model_dump()
            except LengthFinishReasonError:
                logger.warning(
                    "Structured final exam-insights synthesis hit the length limit for model %s. Retrying as compact JSON.",
                    model_name,
                )
            except Exception as exc:
                if is_quota_or_rate_limit_error(exc):
                    logger.warning(
                        "Model %s hit quota/rate limits during final exam-insights synthesis. Trying fallback model.",
                        model_name,
                    )
                    continue
                logger.warning(
                    "Structured final exam-insights synthesis failed for model %s. Retrying as compact JSON: %s",
                    model_name,
                    exc,
                )

            try:
                response = call_openrouter_with_rate_limit_retry(
                    client.chat.completions.create,
                    operation_name="Exam insights synthesis compact",
                    model=model_name,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You analyze exam patterns and revision strategy in concise JSON. "
                                "Return only a valid JSON object."
                            ),
                        },
                        {"role": "user", "content": synthesis_prompt},
                    ],
                    temperature=0.3,
                    max_tokens=2600,
                    response_format={"type": "json_object"},
                )
                raw_synthesis_text = extract_message_text(response.choices[0].message)
                log_exam_insights_raw("SYNTHESIS_COMPACT", raw_synthesis_text)
                parsed = normalize_exam_insights_payload(
                    parse_json_fallback(raw_synthesis_text),
                    subject=subject,
                    topic=topic,
                )
                if not parsed and raw_synthesis_text:
                    logger.info("Repairing non-JSON compact exam-insights synthesis with model %s.", model_name)
                    repaired = repair_json_payload(
                        client,
                        model_name,
                        raw_synthesis_text,
                        "exam_insights",
                        """
- title: string
- overview: string
- repeated_themes: array of 4 short strings
- probable_questions: array of 5 short strings
- question_patterns: array of 4 objects with pattern and evidence
- revision_priorities: array of 5 short strings
""".strip(),
                    )
                    parsed = normalize_exam_insights_payload(
                        repaired,
                        subject=subject,
                        topic=topic,
                    )
                if parsed:
                    return parsed
            except Exception as exc:
                if is_quota_or_rate_limit_error(exc):
                    logger.warning(
                        "Model %s hit quota/rate limits during compact exam-insights synthesis. Trying fallback model.",
                        model_name,
                    )
                    continue
                logger.warning("Compact final exam-insights synthesis failed for model %s: %s", model_name, exc)

        context = build_context_bundle(documents)
        prompt = build_exam_insights_prompt(
            context,
            subject=subject,
            topic=topic,
            institution=institution,
        )

        try:
            response = call_openrouter_with_rate_limit_retry(
                client.beta.chat.completions.parse,
                operation_name="Exam insights single-pass structured",
                model=model_name,
                messages=[
                    {"role": "system", "content": "You analyze exam patterns and revision strategy in strict JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=2200,
                response_format=ExamInsightResponse,
            )
            log_exam_insights_raw("SINGLE_PASS_STRUCTURED", extract_message_text(response.choices[0].message))
            parsed = response.choices[0].message.parsed
            return parsed.model_dump()
        except LengthFinishReasonError:
            logger.warning(
                "Single-pass structured exam-insights generation hit the length limit for model %s. Retrying compact mode.",
                model_name,
            )
        except Exception as exc:
            if is_quota_or_rate_limit_error(exc):
                logger.warning(
                    "Model %s hit quota/rate limits during single-pass structured exam-insights generation. Trying fallback model.",
                    model_name,
                )
                continue
            logger.warning(
                "Single-pass structured exam-insights generation failed for model %s. Retrying compact mode: %s",
                model_name,
                exc,
            )

        try:
            compact_context = build_context_bundle(
                documents,
                max_context_chars=90000,
                max_doc_excerpt_chars=20000,
            )
            compact_prompt = build_exam_insights_prompt(
                compact_context,
                subject=subject,
                topic=topic,
                institution=institution,
                compact=True,
            )
            response = call_openrouter_with_rate_limit_retry(
                client.chat.completions.create,
                operation_name="Exam insights single-pass compact",
                model=model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You analyze exam patterns and revision strategy in concise JSON. "
                            "Return only a valid JSON object."
                        ),
                    },
                    {"role": "user", "content": compact_prompt},
                ],
                temperature=0.3,
                max_tokens=1800,
                response_format={"type": "json_object"},
            )
            raw_compact_text = extract_message_text(response.choices[0].message)
            log_exam_insights_raw("SINGLE_PASS_COMPACT", raw_compact_text)
            parsed = normalize_exam_insights_payload(
                parse_json_fallback(raw_compact_text),
                subject=subject,
                topic=topic,
            )
            if not parsed and raw_compact_text:
                logger.info("Repairing non-JSON single-pass compact exam insights with model %s.", model_name)
                repaired = repair_json_payload(
                    client,
                    model_name,
                    raw_compact_text,
                    "exam_insights",
                    """
- title: string
- overview: string
- repeated_themes: array of 4 short strings
- probable_questions: array of 5 short strings
- question_patterns: array of 4 objects with pattern and evidence
- revision_priorities: array of 5 short strings
""".strip(),
                )
                parsed = normalize_exam_insights_payload(
                    repaired,
                    subject=subject,
                    topic=topic,
                )
            if parsed:
                return parsed
        except Exception as exc:
            if is_quota_or_rate_limit_error(exc):
                logger.warning(
                    "Model %s hit quota/rate limits during single-pass compact exam-insights generation. Trying fallback model.",
                    model_name,
                )
                continue
            logger.warning("Single-pass compact exam-insights generation failed for model %s: %s", model_name, exc)

    return build_fallback_exam_insights(
        documents,
        subject=subject,
        topic=topic,
        institution=institution,
    )


def generate_mock_paper(documents, subject="", topic="", institution="", total_marks=50, duration_minutes=60):
    context = build_mock_paper_context(documents)
    client = create_openrouter_client(timeout=120)
    focus_areas = collect_focus_areas(documents)
    has_pyq = any(doc.get("source_type") == "previous_year_paper" for doc in documents)
    blueprint = build_mock_paper_blueprint(total_marks, duration_minutes, use_mcq_opening=has_pyq)
    prompt = f"""
Create a realistic mock test paper from these study sources.

Requirements:
- Use previous year papers only to infer exam style, section structure, difficulty, and topic weightage.
- Use textbooks and notes to decide the actual concept coverage and source material for the questions.
- Do not copy or closely paraphrase previous year paper questions.
- Do not reuse distinctive numerical values, chemical names, or wording from PYQs unless absolutely necessary.
- The paper must feel fresh and original while still matching the institution's style.
- Cover the most exam-relevant parts of the provided material across the textbook and notes, not just PYQs.
- At least 70 percent of the paper should test textbook-and-notes coverage rather than direct PYQ rewording.
- Balance short-answer and longer-answer prompts where appropriate.
- Ensure good chapter spread; avoid overconcentrating on only one chapter unless the sources strongly justify it.
- The paper should total about {total_marks} marks.
- Suggested duration should be about {duration_minutes} minutes.
- Include concise answer outlines for evaluation.
- Every question must include a nonzero integer `marks` value.
- Every section should have a realistic `suggested_time_minutes`.
- Prefer clean board-paper phrasing over chatty language.
- Keep answer outlines practical: core points, steps, formulas, or reactions expected for marks.
- If previous year papers suggest an objective opening section, make Section 1 a proper MCQ section with four options per question.
- For MCQ questions, include `options` and `correct_answer`.
- For non-MCQ questions, leave `options` empty.
- Never leave the question text blank. Every question must contain a complete question stem.
- Use specific chapter concepts from the textbook and notes, not generic placeholders like "Question 1".

Priority concept areas to cover:
{json.dumps(focus_areas, ensure_ascii=True)}

Suggested paper blueprint:
{blueprint}

Subject: {subject or 'General'}
Topic focus: {topic or 'Full provided scope'}
Institution or board: {institution or 'Not specified'}

Sources:
{context}
""".strip()

    last_error = None
    for model_name in get_model_candidates("mock_paper"):
        logger.info("Generating mock paper with model: %s", model_name)
        try:
            response = call_openrouter_with_rate_limit_retry(
                client.beta.chat.completions.parse,
                operation_name="Mock paper generation structured",
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
            return normalize_mock_paper_payload(
                parsed.model_dump(),
                subject=subject,
                topic=topic,
                total_marks=total_marks,
                duration_minutes=duration_minutes,
            ) or parsed.model_dump()
        except Exception as exc:
            if is_quota_or_rate_limit_error(exc):
                logger.warning(
                    "Model %s hit quota/rate limits for mock paper generation. Trying fallback model.",
                    model_name,
                )
                last_error = exc
                continue

        try:
            response = call_openrouter_with_rate_limit_retry(
                client.chat.completions.create,
                operation_name="Mock paper generation compact",
                model=model_name,
                messages=[
                    {"role": "system", "content": "You create structured mock exam papers in JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
                max_tokens=5500,
            )
            parsed = normalize_mock_paper_payload(
                parse_json_fallback(extract_message_text(response.choices[0].message)),
                subject=subject,
                topic=topic,
                total_marks=total_marks,
                duration_minutes=duration_minutes,
            )
            if not parsed:
                raise ValueError("Could not parse the mock paper from the model response.")
            return parsed
        except Exception as exc:
            last_error = exc
            if is_quota_or_rate_limit_error(exc):
                logger.warning(
                    "Model %s hit quota/rate limits during compact mock paper generation. Trying fallback model.",
                    model_name,
                )
                continue

    if last_error:
        raise last_error
    raise ValueError("Could not generate the mock paper from the model response.")


def grade_mock_paper_attempt(paper, answers, subject="", topic="", institution="", whole_submission_text=""):
    if not isinstance(paper, dict) or not (paper.get("sections") or []):
        raise ValueError("A valid mock paper is required for grading.")

    client = create_openrouter_client(timeout=180)
    model_name = get_model_for("mock_paper")

    grade_sections = []
    total_possible = 0
    for section_index, section in enumerate(paper.get("sections", [])):
        questions = []
        for question_index, question in enumerate(section.get("questions", [])):
            max_marks = normalize_marks_value(question.get("marks"), fallback=0)
            total_possible += max_marks

            answer_key = f"{section_index}:{question_index}"
            student_answer = answers.get(answer_key, {}) if isinstance(answers, dict) else {}
            combined_answer = clean_text(
                "\n\n".join(
                    part for part in [
                        student_answer.get("answer_text", ""),
                        student_answer.get("image_text", ""),
                    ] if part
                )
            )

            questions.append(
                {
                    "question_index": question_index,
                    "question": question.get("question", ""),
                    "max_marks": max_marks,
                    "answer_outline": question.get("answer_outline", ""),
                    "student_answer": combined_answer,
                    "question_type": question.get("question_type") or infer_question_type(question.get("question", ""), max_marks),
                }
            )

        grade_sections.append(
            {
                "section_index": section_index,
                "title": section.get("title", f"Section {section_index + 1}"),
                "questions": questions,
            }
        )

    prompt = f"""
You are grading a student's board-style mock paper attempt.

Grading standard:
- Grade like a fair but realistic school board paper checker.
- Award partial credit for correct method, key points, equations, and reasoning.
- Do not be overly strict about wording if the concept is correct.
- Do not be overly lenient when key steps, definitions, or conclusions are missing.
- Penalize major conceptual mistakes, unsupported claims, missing steps, and unanswered parts.
- Keep feedback short, specific, and useful.
- If a student left the answer blank, award 0.
- Respect the mark value strictly and award marks in 0.5 increments only.
- For 1-mark questions, expect a precise fact / definition / result.
- For 2-3 mark questions, reward the right key points and compact method.
- For 4+ mark questions, reward structure, working, derivation, reasoning, and completeness.
- If the answer is partly correct, explicitly award method marks where justified.
- Use verdict labels like: Excellent, Good, Partial, Weak, Unanswered.
- If a full-paper submission transcript is provided, align it to the most likely question before grading.
- Prefer the student's explicit per-question answer when present, but use the full-paper transcript to recover omitted answers.

Return only valid JSON with:
- evaluation_title
- overall_summary
- total_marks_awarded
- total_marks_possible
- graded_sections: array of sections
Each graded question must include:
- question_index
- marks_awarded
- max_marks
- verdict
- feedback
- strengths
- improvements
- transcribed_answer

Subject: {subject or 'General'}
Topic focus: {topic or paper.get('paper_title') or 'Full paper'}
Institution or board: {institution or 'Not specified'}

Paper and answers:
{json.dumps(grade_sections, ensure_ascii=True, indent=2)}

Full-paper submission transcript:
{whole_submission_text or "No full-paper transcript provided."}
""".strip()

    response = call_openrouter_with_rate_limit_retry(
        client.chat.completions.create,
        operation_name="Mock paper grading",
        model=model_name,
        messages=[
            {
                "role": "system",
                "content": "You grade exam answers and return only valid JSON.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=5000,
        response_format={"type": "json_object"},
    )

    parsed = normalize_mock_paper_grade_payload(
        parse_json_fallback(extract_message_text(response.choices[0].message))
    )
    if not parsed:
        raise ValueError("Could not parse the mock paper grading response.")
    question_lookup = {
        f"{section_index}:{question_index}": clean_text(
            "\n\n".join(
                part for part in [
                    (answers.get(f"{section_index}:{question_index}", {}) if isinstance(answers, dict) else {}).get("answer_text", ""),
                    (answers.get(f"{section_index}:{question_index}", {}) if isinstance(answers, dict) else {}).get("image_text", ""),
                ] if part
            )
        )
        for section_index, section in enumerate(paper.get("sections", []))
        for question_index, _question in enumerate(section.get("questions", []))
    }
    has_whole_submission = bool(clean_text(whole_submission_text or ""))

    awarded_total = 0.0
    possible_total = 0
    for section in parsed.get("graded_sections", []):
        section_index = int(section.get("section_index", 0))
        for question in section.get("questions", []):
            question_index = int(question.get("question_index", 0))
            answer_key = f"{section_index}:{question_index}"
            student_answer = question_lookup.get(answer_key, "")
            max_marks = normalize_marks_value(question.get("max_marks"), fallback=0)
            if not student_answer.strip() and not has_whole_submission:
                question["marks_awarded"] = 0.0
                question["verdict"] = "Unanswered"
                question["feedback"] = "No answer was submitted for this question."
                question["strengths"] = []
                question["improvements"] = ["Attempt this question and cover the core key points."]
            awarded_total += float(question.get("marks_awarded") or 0.0)
            possible_total += max_marks

    parsed["total_marks_awarded"] = round(awarded_total, 2)
    parsed["total_marks_possible"] = possible_total or total_possible
    return parsed
