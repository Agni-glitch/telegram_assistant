"""
Pluggable AI model layer.

Models are configured with a comma-separated list of "provider:model" entries,
tried in order until one succeeds, e.g.

    AI_MODELS=anthropic:claude-opus-5-5,azure:IPMgpt-4,groq:llama-3.3-70b-versatile

Set it in Vercel (AI_MODELS / OWNER_AI_MODELS) or from Telegram with /model.

- Built-in providers (PROVIDERS below) only need their API key env var.
- Any other OpenAI-compatible provider: pick a name and set <NAME>_BASE_URL
  and <NAME>_API_KEY. If it expects the key in a header other than
  "Authorization: Bearer", set <NAME>_API_KEY_HEADER (e.g. "api-key").
- <NAME>_BASE_URL also overrides a built-in provider's URL (e.g. a proxy).
"""

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

AI_TIMEOUT = float(os.getenv("AI_TIMEOUT", "25"))

# provider name -> (base URL, or "anthropic"/"azure" for native APIs, API key env var)
PROVIDERS = {
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
    # Azure OpenAI: model = your DEPLOYMENT name, e.g. azure:IPMgpt-4.
    # Also needs AZURE_OPENAI_ENDPOINT (and optionally AZURE_OPENAI_API_VERSION).
    "azure": ("azure", "AZURE_OPENAI_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "perplexity": ("https://api.perplexity.ai", "PERPLEXITY_API_KEY"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "fireworks": ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY"),
    "huggingface": ("https://router.huggingface.co/v1", "HUGGINGFACE_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "cohere": ("https://api.cohere.ai/compatibility/v1", "COHERE_API_KEY"),
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


def _env_prefix(provider: str) -> str:
    return provider.upper().replace("-", "_")


def _resolve(provider: str) -> dict:
    """Returns {"kind", "base", "key", "header"}; raises AIError if unusable."""
    prefix = _env_prefix(provider)
    base_override = os.getenv(f"{prefix}_BASE_URL")
    header = os.getenv(f"{prefix}_API_KEY_HEADER")

    if provider in PROVIDERS:
        base, key_env = PROVIDERS[provider]
        key = os.getenv(key_env)
        if not key:
            raise AIError(f"{provider}: {key_env} is not set in Vercel")
        if base == "anthropic":
            return {"kind": "anthropic", "key": key}
        if base == "azure":
            if not os.getenv("AZURE_OPENAI_ENDPOINT"):
                raise AIError("azure: AZURE_OPENAI_ENDPOINT is not set in Vercel")
            return {"kind": "azure", "key": key}
        return {"kind": "openai", "base": base_override or base, "key": key, "header": header}

    if not base_override:
        raise AIError(f"unknown provider '{provider}' (set {prefix}_BASE_URL and {prefix}_API_KEY in Vercel)")
    return {"kind": "openai", "base": base_override, "key": os.getenv(f"{prefix}_API_KEY"), "header": header}


def check_provider(provider: str) -> str | None:
    """Return a problem description if this provider can't be used, else None."""
    try:
        _resolve(provider)
    except AIError as e:
        return str(e)
    return None


def ready_providers() -> list[str]:
    """Providers that are configured in the environment and usable right now."""
    names = list(PROVIDERS)
    reserved = {_env_prefix(p) for p in PROVIDERS} | {"AZURE_OPENAI", "ANTHROPIC"}
    for var in os.environ:
        if var.endswith("_BASE_URL") and os.environ[var]:
            prefix = var[: -len("_BASE_URL")]
            if prefix and prefix not in reserved:
                names.append(prefix.lower())
    return [n for n in names if check_provider(n) is None]


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
    version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    url = (
        f"{endpoint}/openai/deployments/{urllib.parse.quote(deployment, safe='')}"
        f"/chat/completions?api-version={urllib.parse.quote(version, safe='')}"
    )
    return _chat_completions(url, {"api-key": api_key}, deployment, system, user_text)


def _call_openai_compatible(base_url: str, api_key: str | None, header: str | None,
                            model: str, system: str, user_text: str) -> str:
    if not api_key:
        auth = {}
    elif header and header.lower() != "authorization":
        auth = {header: api_key}
    else:
        auth = {"Authorization": f"Bearer {api_key}"}
    return _chat_completions(base_url.rstrip("/") + "/chat/completions", auth, model, system, user_text)


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


def call_model(provider: str, model: str, system: str, user_text: str) -> str:
    cfg = _resolve(provider)
    if cfg["kind"] == "anthropic":
        return _call_anthropic(cfg["key"], model, system, user_text)
    if cfg["kind"] == "azure":
        return _call_azure(cfg["key"], model, system, user_text)
    return _call_openai_compatible(cfg["base"], cfg["key"], cfg["header"], model, system, user_text)


def generate(models: list[tuple[str, str]], system: str, user_text: str) -> tuple[str, str]:
    """Try each model in order. Returns (reply_text, "provider:model"). Raises AIError if all fail."""
    if not models:
        raise AIError("no AI models configured")
    errors = []
    for provider, model in models:
        label = f"{provider}:{model}"
        try:
            return call_model(provider, model, system, user_text), label
        except Exception as e:  # try the next model on any failure
            logger.warning(f"AI model {label} failed: {e}")
            errors.append(f"{label}: {e}")
    raise AIError(" | ".join(errors))
