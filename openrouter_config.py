import logging
import os
import time

from openai import APIStatusError, OpenAI

from env_loader import load_env_file


load_env_file()
logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL",
    "https://openrouter.ai/api/v1/",
)
OPENROUTER_RATE_LIMIT_COOLDOWN_SECONDS = int(os.environ.get("OPENROUTER_RATE_LIMIT_COOLDOWN_SECONDS", "60"))
OPENROUTER_RATE_LIMIT_MAX_ATTEMPTS = int(os.environ.get("OPENROUTER_RATE_LIMIT_MAX_ATTEMPTS", "2"))
OPENROUTER_LOG_USAGE = os.environ.get("OPENROUTER_LOG_USAGE", "").strip().lower() in {"1", "true", "yes", "on"}


def get_openrouter_api_key():
    return os.environ.get("OPENROUTER_API_KEY")


def create_openrouter_client(timeout=None):
    api_key = get_openrouter_api_key()
    if not api_key:
        raise RuntimeError(
            "Missing OpenRouter API key. Set OPENROUTER_API_KEY in your .env file."
        )

    client_kwargs = {
        "api_key": api_key,
        "base_url": OPENROUTER_BASE_URL,
        "max_retries": 0,
        "default_headers": {
            "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "http://localhost:5000"),
            "X-Title": os.environ.get("OPENROUTER_APP_NAME", "Xemo Visual Study Lab"),
        },
    }
    if timeout is not None:
        client_kwargs["timeout"] = timeout

    return OpenAI(**client_kwargs)


def log_openrouter_usage(response, operation_name, model_name=None):
    if not OPENROUTER_LOG_USAGE:
        return

    usage = getattr(response, "usage", None)
    if usage is None:
        logger.info("OPENROUTER_USAGE operation=%s model=%s usage=missing", operation_name, model_name or "unknown")
        return

    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)

    logger.info(
        "OPENROUTER_USAGE operation=%s model=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s prompt_details=%s completion_details=%s",
        operation_name,
        model_name or "unknown",
        prompt_tokens,
        completion_tokens,
        total_tokens,
        prompt_details,
        completion_details,
    )


def call_openrouter_with_rate_limit_retry(operation, *args, operation_name="OpenRouter request", cooldown_seconds=None, max_attempts=None, **kwargs):
    cooldown = OPENROUTER_RATE_LIMIT_COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
    attempts = OPENROUTER_RATE_LIMIT_MAX_ATTEMPTS if max_attempts is None else max_attempts
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            response = operation(*args, **kwargs)
            log_openrouter_usage(response, operation_name, model_name=kwargs.get("model"))
            return response
        except APIStatusError as exc:
            last_error = exc
            if exc.status_code != 429 or attempt >= attempts:
                raise

            logger.warning(
                "%s hit rate limits (429). Cooling down for %s seconds before retry %s/%s.",
                operation_name,
                cooldown,
                attempt + 1,
                attempts,
            )
            time.sleep(cooldown)

    if last_error:
        raise last_error
