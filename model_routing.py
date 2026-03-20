import os

from env_loader import load_env_file
from gemini_config import GEMINI_MODEL


load_env_file()


FEATURE_MODELS = {
    "visuals": os.environ.get("MODEL_VISUALS", GEMINI_MODEL),
    "document_vision": os.environ.get("MODEL_DOCUMENT_VISION", GEMINI_MODEL),
    "study_plan": os.environ.get("MODEL_STUDY_PLAN", GEMINI_MODEL),
    "flashcards": os.environ.get("MODEL_FLASHCARDS", GEMINI_MODEL),
    "exam_insights": os.environ.get("MODEL_EXAM_INSIGHTS", GEMINI_MODEL),
    "mock_paper": os.environ.get("MODEL_MOCK_PAPER", GEMINI_MODEL),
}


def get_model_for(feature_name):
    return FEATURE_MODELS[feature_name]


def is_gemini_model(model_name):
    return model_name.startswith("gemini") or model_name.startswith("google/gemini")
