import base64
import json
import os
import re
from typing import List

import fitz
from pydantic import BaseModel, Field

from gemini_config import create_gemini_client
from model_routing import get_model_for


MAX_SOURCE_CHARS = 90000
VISION_PAGE_LIMIT = 5


class VisualizationTopic(BaseModel):
    title: str
    summary: str
    key_takeaways: List[str] = Field(default_factory=list)
    mindmap_code: str
    flowchart_code: str


class VisualizationResponse(BaseModel):
    topics: List[VisualizationTopic]


def extract_text(pdf_path):
    """Fast text extraction using PyMuPDF."""
    try:
        if not pdf_path or not os.path.exists(pdf_path):
            raise ValueError(f"PDF file not found or invalid path: {pdf_path}")

        doc = fitz.open(pdf_path)
        if doc.page_count == 0:
            raise ValueError("PDF file has no pages")

        return "\n".join(page.get_text() for page in doc)
    except Exception as e:
        print(f"Error extracting text from PDF: {e}")
        raise Exception(f"Failed to extract text from PDF: {str(e)}")


def clean_text(text):
    """Normalize whitespace and cap the source text size."""
    text = re.sub(r"\s+", " ", text or "")
    text = re.sub(r"\x0c", "", text)
    return text[:MAX_SOURCE_CHARS]


def extract_pdf_page_images(pdf_path, max_pages=VISION_PAGE_LIMIT):
    """Render a few PDF pages into PNG data URLs for Gemini vision fallback."""
    if not pdf_path or not os.path.exists(pdf_path):
        raise ValueError(f"PDF file not found or invalid path: {pdf_path}")

    images = []
    doc = fitz.open(pdf_path)
    try:
        for page_index in range(min(doc.page_count, max_pages)):
            page = doc.load_page(page_index)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.2, 1.2), alpha=False)
            image_bytes = pixmap.tobytes("png")
            encoded = base64.b64encode(image_bytes).decode("utf-8")
            images.append(f"data:image/png;base64,{encoded}")
    finally:
        doc.close()

    return images


def parse_json_from_response(response_content):
    """Extract and parse a JSON object from the model response."""
    if isinstance(response_content, dict):
        return response_content

    if hasattr(response_content, "model_dump"):
        return response_content.model_dump()

    if isinstance(response_content, list):
        return {"topics": response_content}

    try:
        return json.loads(response_content)
    except json.JSONDecodeError:
        array_match = re.search(r"\[\s*\{.*\}\s*\]", response_content, re.DOTALL)
        if array_match:
            try:
                return {"topics": json.loads(array_match.group(0))}
            except json.JSONDecodeError:
                pass

        match = re.search(r"\{.*\}", response_content, re.DOTALL)
        if not match:
            return None

        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def strip_code_fences(code):
    code = (code or "").strip()
    if not code.startswith("```"):
        return code

    lines = code.splitlines()
    if len(lines) <= 1:
        return ""

    if lines[-1].strip() == "```":
        lines = lines[1:-1]
    else:
        lines = lines[1:]

    return "\n".join(lines).strip()


def sanitize_mermaid_label(label, fallback="Concept"):
    label = (label or "").strip()
    label = re.sub(r"`+", "", label)
    label = re.sub(r"[\[\]{}<>|]", "", label)
    label = label.replace('"', "'")
    label = re.sub(r"\s+", " ", label)
    return label[:80] or fallback


def split_summary_points(summary):
    parts = re.split(r"[.;:]\s+|\s+-\s+", (summary or "").strip())
    return [sanitize_mermaid_label(part) for part in parts if part.strip()][:4]


def build_fallback_mindmap(title, key_takeaways=None, summary=""):
    title_label = sanitize_mermaid_label(title, "Topic")
    branches = [
        sanitize_mermaid_label(item)
        for item in (key_takeaways or [])
        if sanitize_mermaid_label(item)
    ][:4]

    if not branches:
        branches = split_summary_points(summary)

    if not branches:
        branches = ["Overview", "Core idea", "Key relationship"]

    lines = ["mindmap", f"  root(({title_label}))"]
    for branch in branches:
        lines.append(f"    {branch}")

    return "\n".join(lines)


def normalize_mindmap_code(code, title, key_takeaways=None, summary=""):
    code = strip_code_fences(code)
    lines = [line.rstrip() for line in code.splitlines() if line.strip()]

    if not lines:
        return build_fallback_mindmap(title, key_takeaways=key_takeaways, summary=summary)

    if not lines[0].strip().startswith("mindmap"):
        lines.insert(0, "mindmap")

    normalized = ["mindmap"]
    body_lines = lines[1:]
    root_found = False
    branch_count = 0

    for line in body_lines:
        raw = line.replace("\t", "  ").rstrip()
        content = raw.strip()
        if not content:
            continue

        indent_level = max(1, (len(raw) - len(raw.lstrip(" "))) // 2)
        indent = "  " * indent_level
        content = re.sub(r"^[-*]\s*", "", content)
        content = re.sub(r"^#+\s*", "", content)
        content = sanitize_mermaid_label(content, "Concept")

        if content.lower() == "mindmap":
            continue

        normalized.append(f"{indent}{content}")
        if content.startswith("root"):
            root_found = True
        if indent_level == 2:
            branch_count += 1

    if not root_found:
        normalized.insert(1, f"  root(({sanitize_mermaid_label(title, 'Topic')}))")

    if branch_count == 0:
        return build_fallback_mindmap(title, key_takeaways=key_takeaways, summary=summary)

    return "\n".join(normalized)


def build_safe_flowchart(title, key_takeaways=None, summary=""):
    title_label = sanitize_mermaid_label(title, "Topic")
    steps = [
        sanitize_mermaid_label(item)
        for item in (key_takeaways or [])
        if sanitize_mermaid_label(item)
    ][:4]

    if not steps:
        steps = split_summary_points(summary)

    if not steps:
        steps = ["Overview", "Core idea", "Outcome"]

    lines = ["flowchart TD", f'  N0["{title_label}"]']
    for index, step in enumerate(steps, start=1):
        lines.append(f'  N{index}["{step}"]')
        source = f"N{index - 1}" if index > 1 else "N0"
        lines.append(f"  {source} --> N{index}")

    return "\n".join(lines)


def normalize_flowchart_code(code, title, key_takeaways=None, summary=""):
    return build_safe_flowchart(title, key_takeaways=key_takeaways, summary=summary)


def build_visual_generation_prompt(text=None, using_images=False):
    source_block = (
        f"Source:\n{text[:MAX_SOURCE_CHARS]}"
        if text and text.strip()
        else (
            "Source:\nThe source material is provided as rendered PDF page images. "
            "Read the pages carefully and infer the educational structure directly from them."
        )
    )

    extra_rule = (
        "- If the PDF appears to be a textbook or notes, group adjacent pages into concept clusters rather than page order.\n"
        if using_images
        else ""
    )

    return f"""
Create a visually useful study aid from the source material.

Return exactly one JSON object with this structure:
{{
  "topics": [
    {{
      "title": "short topic title",
      "summary": "2 sentence concept-first explanation",
      "key_takeaways": ["takeaway 1", "takeaway 2", "takeaway 3"],
      "mindmap_code": "Mermaid mindmap code only",
      "flowchart_code": "Mermaid flowchart code only"
    }}
  ]
}}

Rules:
- Create 3 to 6 topics only.
- Prefer concept clusters over page-order summaries.
- Mindmaps must feel rich, not bland: include hierarchy, examples, contrasts, conditions, or cause/effect where relevant.
- Each mindmap should have a clear root, 3 to 5 strong branches, and useful lower-level detail.
- Each flowchart should explain how the concept works, evolves, or is applied.
- If the concept is not procedural, turn the flowchart into a causal chain, comparison path, or decision path.
- Use concise node text.
- Use only facts present in the source material.
- Do not wrap the JSON in markdown fences.
{extra_rule}

{source_block}
""".strip()


def generate_visual_learning_assets(text=None, page_images=None):
    if (not text or not text.strip()) and not page_images:
        raise ValueError("Input text is empty or None")

    prompt = build_visual_generation_prompt(text=text, using_images=bool(page_images))

    try:
        print("Generating visual learning assets...")
        client = create_gemini_client()
        user_content = [{"type": "text", "text": prompt}]
        for index, image_url in enumerate(page_images or [], start=1):
            user_content.append({"type": "text", "text": f"PDF page {index}"})
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                }
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "You create polished educational visual structures in strict JSON "
                    "for Mermaid mindmaps and Mermaid flowcharts."
                ),
            },
            {"role": "user", "content": user_content},
        ]

        try:
            response = client.beta.chat.completions.parse(
                model=get_model_for("visuals"),
                messages=messages,
                temperature=0.5,
                max_tokens=5000,
                response_format=VisualizationResponse,
            )
            parsed = response.choices[0].message.parsed
            if parsed is not None:
                print("Visual asset generation done with structured output.")
                return parsed.model_dump()
        except Exception as parse_error:
            print(f"Structured output parse failed, falling back to plain completion: {parse_error}")

        response = client.chat.completions.create(
            model=get_model_for("visuals"),
            messages=messages,
            temperature=0.5,
            max_tokens=5000,
        )
        print("Visual asset generation done.")
        return response.choices[0].message.content
    except Exception as e:
        print(f"Error during visual asset generation: {e}")
        raise Exception(f"Failed to generate visual learning assets: {str(e)}")


def process_visual_learning_assets(ai_output):
    parsed = parse_json_from_response(ai_output)
    if not parsed or not isinstance(parsed.get("topics"), list):
        snippet = ai_output if isinstance(ai_output, str) else json.dumps(ai_output)[:1200]
        print(f"Invalid visualization payload snippet: {snippet[:1200]}")
        raise ValueError("Model response did not contain a valid topics array.")

    visualizations = []
    for item in parsed["topics"]:
        if not isinstance(item, dict):
            continue

        title = (item.get("title") or "Untitled Topic").strip()
        summary = (item.get("summary") or "").strip()
        key_takeaways = [
            takeaway.strip()
            for takeaway in item.get("key_takeaways", [])
            if isinstance(takeaway, str) and takeaway.strip()
        ][:5]

        mindmap_code = normalize_mindmap_code(
            item.get("mindmap_code", ""),
            title,
            key_takeaways=key_takeaways,
            summary=summary,
        )
        flowchart_code = normalize_flowchart_code(
            item.get("flowchart_code", ""),
            title,
            key_takeaways=key_takeaways,
            summary=summary,
        )

        visualizations.append(
            {
                "title": title,
                "summary": summary,
                "key_takeaways": key_takeaways,
                "mindmap_code": mindmap_code,
                "flowchart_code": flowchart_code,
            }
        )

    if not visualizations:
        raise ValueError("No valid visualizations could be extracted from the model response.")

    return visualizations


def process_mindmaps(ai_output):
    """Backward-compatible helper for older callers."""
    return [
        {"title": item["title"], "code": item["mindmap_code"]}
        for item in process_visual_learning_assets(ai_output)
    ]


def generate_mindmaps(text):
    """Backward-compatible helper for older callers."""
    return generate_visual_learning_assets(text)
