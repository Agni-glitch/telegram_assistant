"""
Pluggable AI model layer.

Models are configured with a comma-separated list of "provider:model" entries,
tried in order until one succeeds, e.g.

    AI_MODELS=anthropic:claude-opus-5-5,groq:llama-3.3-70b-versatile

Built-in providers are listed in PROVIDERS below. Any other provider name works
too, as long as it speaks the OpenAI-compatible /chat/completions API: set
<NAME>_BASE_URL and <NAME>_API_KEY, e.g. "ollama:llama3" with OLLAMA_BASE_URL.
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "25"))

# provider name -> (OpenAI-compatible base URL or "anthropic", API key env var)
PROVIDERS = {
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
    # Azure OpenAI: model = your DEPLOYMENT name, e.g. azure:IPMgpt-4.
    # Also needs AZURE_OPENAI_ENDPOINT (and optionally AZURE_OPENAI_API_VERSION).
    "azure": ("azure", "AZURE_OPENAI_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
}

# Claude models that support server-side refusal fallbacks.
ANTHROPIC_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class AIError(Exception):
    pass


def parse_models(spec: str | None) -> list[tuple[str, str]]:
    models = []
    for item in (spec or "").split(","):
        item = item.strip()
        if ":" not in item:
            continue
        provider, model = item.split(":", 1)  # model ids may contain ':' (OpenRouter)
        if provider.strip() and model.strip():
            models.append((provider.strip().lower(), model.strip()))
    return models


def _resolve(provider: str) -> tuple[str, str | None]:
    if provider in PROVIDERS:
        base, key_env = PROVIDERS[provider]
        return base, os.getenv(key_env)
    prefix = provider.upper().replace("-", "_")
    base = os.getenv(f"{prefix}_BASE_URL")
    if not base:
        raise AIError(f"unknown provider '{provider}' (set {prefix}_BASE_URL)")
    return base, os.getenv(f"{prefix}_API_KEY")


def check_provider(provider: str) -> str | None:
    """Return a problem description if this provider can't be used, else None."""
    try:
        _, key = _resolve(provider)
    except AIError as e:
        return str(e)
    if provider in PROVIDERS and not key:
        return f"{provider}: {PROVIDERS[provider][1]} is not set in Vercel"
    if provider == "azure" and not os.getenv("AZURE_OPENAI_ENDPOINT"):
        return "azure: AZURE_OPENAI_ENDPOINT is not set in Vercel"
    return None


def _call_anthropic(api_key: str, model: str, system: str, user_text: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=AI_TIMEOUT, max_retries=1)
    params = {
        "model": model,
        "max_tokens": int(os.getenv("ANTHROPIC_MAX_TOKENS", "4000")),
        "system": system,
        "messages": [{"role": "user", "content": user_text}],
    }
    effort = os.getenv("ANTHROPIC_EFFORT", "low").strip()
    if effort:
        params["output_config"] = {"effort": effort}

    if model in ANTHROPIC_FALLBACK_MODELS:
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **params,
        )
    else:
        response = client.messages.create(**params)

    if response.stop_reason == "refusal":
        raise AIError("model declined the request")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise AIError(f"empty response (stop_reason={response.stop_reason})")
    return text


def _call_azure(api_key: str, deployment: str, system: str, user_text: str) -> str:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise AIError("AZURE_OPENAI_ENDPOINT not set")
    version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    url = (
        f"{endpoint}/openai/deployments/{urllib.parse.quote(deployment, safe='')}"
        f"/chat/completions?api-version={urllib.parse.quote(version, safe='')}"
    )
    return _chat_completions(url, {"api-key": api_key}, deployment, system, user_text)


def _call_openai_compatible(base_url: str, api_key: str | None, model: str, system: str, user_text: str) -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return _chat_completions(base_url.rstrip("/") + "/chat/completions", headers, model, system, user_text)


def _chat_completions(url: str, auth_headers: dict, model: str, system: str, user_text: str) -> str:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", **auth_headers}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AIError(f"HTTP {e.code}: {detail}") from e
    try:
        text = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as e:
        raise AIError(f"unexpected response: {str(data)[:300]}") from e
    if not text:
        raise AIError("empty response")
    return text


def generate(models: list[tuple[str, str]], system: str, user_text: str) -> tuple[str, str]:
    """Try each model in order. Returns (reply_text, "provider:model"). Raises AIError if all fail."""
    if not models:
        raise AIError("no AI models configured")
    errors = []
    for provider, model in models:
        label = f"{provider}:{model}"
        try:
            base, key = _resolve(provider)
            if base == "anthropic":
                if not key:
                    raise AIError("ANTHROPIC_API_KEY not set")
                return _call_anthropic(key, model, system, user_text), label
            if base == "azure":
                if not key:
                    raise AIError("AZURE_OPENAI_API_KEY not set")
                return _call_azure(key, model, system, user_text), label
            return _call_openai_compatible(base, key, model, system, user_text), label
        except Exception as e:  # try the next model on any failure
            logger.warning(f"AI model {label} failed: {e}")
            errors.append(f"{label}: {e}")
    raise AIError(" | ".join(errors))
