import os

from openai import OpenAI

from env_loader import load_env_file


load_env_file()

GEMINI_BASE_URL = os.environ.get(
    "GEMINI_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta/openai/",
)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def get_gemini_api_key():
    return os.environ.get("GEMINI_API_KEY")


def create_gemini_client(timeout=None):
    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError(
            "Missing Gemini API key. Set GEMINI_API_KEY in your .env file."
        )

    client_kwargs = {
        "api_key": api_key,
        "base_url": GEMINI_BASE_URL,
        "default_headers": {
            "x-goog-api-client": "xemo-ai-openai-compat/1.0",
        },
    }
    if timeout is not None:
        client_kwargs["timeout"] = timeout

    return OpenAI(**client_kwargs)
