import os

from env_loader import load_env_file
from gemini_config import GEMINI_MODEL


load_env_file()

DEFAULT_ANALYSIS_MODEL = os.environ.get("GEMINI_ANALYSIS_MODEL", "gemini-2.5-pro")

FEATURE_MODELS = {
    "visuals": os.environ.get("MODEL_VISUALS", GEMINI_MODEL),
    "document_vision": os.environ.get("MODEL_DOCUMENT_VISION", GEMINI_MODEL),
    "source_summary": os.environ.get("MODEL_SOURCE_SUMMARY", "openai/gpt-oss-120b:free"),
    "study_plan": os.environ.get("MODEL_STUDY_PLAN", GEMINI_MODEL),
    "flashcards": os.environ.get("MODEL_FLASHCARDS", "openai/gpt-oss-120b:free"),
    "exam_insights": os.environ.get("MODEL_EXAM_INSIGHTS", "openai/gpt-oss-120b:free"),
    "mock_paper": os.environ.get("MODEL_MOCK_PAPER", "openai/gpt-oss-120b:free"),
}


def get_model_for(feature_name):
    return FEATURE_MODELS[feature_name]


def get_fallback_model_for(feature_name):
    env_key = f"MODEL_{feature_name.upper()}_FALLBACK"
    return os.environ.get(env_key, GEMINI_MODEL)


def get_model_candidates(feature_name):
    candidates = []
    for model_name in (get_model_for(feature_name), get_fallback_model_for(feature_name)):
        if model_name and model_name not in candidates:
            candidates.append(model_name)
    return candidates


def is_gemini_model(model_name):
    return model_name.startswith("gemini") or model_name.startswith("google/gemini")
