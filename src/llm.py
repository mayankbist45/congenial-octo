"""LLM client wrapper.

Talks to any OpenAI-compatible endpoint so the provider can be swapped in
``.env``. Two things matter for this fund: JSON responses must be parseable,
and an LLM outage must never stop the daily cycle - callers fall back to the
rules-only path when :meth:`LLMClient.available` is False.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


# LLMClient instances are short-lived throughout a multi-persona run. Keep the
# rate budget at module scope so digest and decision clients share one limit.
_RATE_LOCK = threading.Lock()
_RATE_WINDOWS: Dict[str, Deque[Tuple[float, int]]] = defaultdict(deque)


class LLMUnavailable(RuntimeError):
    """Raised when a completion cannot be obtained."""


def extract_json(text: str) -> Optional[Any]:
    """Pull a JSON object or array out of a model response.

    Models wrap JSON in prose or fenced code blocks often enough that this is
    worth handling rather than failing the run.
    """
    if not text:
        return None

    candidate = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost brace or bracket pair.
    for opening, closing in (("{", "}"), ("[", "]")):
        start = candidate.find(opening)
        end = candidate.rfind(closing)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    logger.warning("Could not extract JSON from model response (%d chars)", len(text))
    return None


def _truncate_prompt(text: str, limit_chars: int) -> str:
    """Keep prompt instructions and output schema when trimming large briefs."""
    if len(text) <= limit_chars:
        return text
    if limit_chars <= 80:
        return text[:limit_chars]

    head = int(limit_chars * 0.65)
    tail = limit_chars - head - 32
    return f"{text[:head]}\n\n[...brief truncated...]\n\n{text[-tail:]}"


class LLMClient:
    """Thin, provider-agnostic chat completion client."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._secrets = self._config.secrets
        self._client: Optional[Any] = None

    @property
    def available(self) -> bool:
        return bool(self._api_key())

    @property
    def provider(self) -> str:
        configured = self._secrets.llm_provider or self._config.get("llm.provider", "openai")
        return str(configured).strip().lower() or "openai"

    @property
    def _profile(self) -> Dict[str, Any]:
        profiles = self._config.get("llm.providers", {}) or {}
        profile = profiles.get(self.provider, {}) if isinstance(profiles, dict) else {}
        return profile if isinstance(profile, dict) else {}

    def _setting(self, key: str, default: Any = None) -> Any:
        return self._profile.get(key, self._config.get(f"llm.{key}", default))

    def _api_key(self) -> str:
        resolver = getattr(self._secrets, "llm_api_key", None)
        if callable(resolver):
            return resolver(self.provider)
        return str(getattr(self._secrets, "openai_api_key", "") or "")

    def _base_url(self) -> Optional[str]:
        configured = self._profile.get("base_url")
        if configured:
            return str(configured)
        return getattr(self._secrets, "openai_base_url", None)

    def _request_overrides(self) -> Dict[str, Any]:
        overrides = self._profile.get("request_overrides", {})
        return dict(overrides) if isinstance(overrides, dict) else {}

    @property
    def model(self) -> str:
        return str(self._setting("model", "gpt-4o"))

    @property
    def digest_model(self) -> str:
        return str(self._setting("digest_model", self.model))

    def _bounded_messages(self, system_prompt: str, user_prompt: str) -> List[Dict[str, str]]:
        # English financial briefs average roughly four characters per token.
        # Use a conservative three-character estimate to stay below Groq's TPM
        # ceiling even when prices, tables, and JSON add tokenizer overhead.
        budget_tokens = int(self._setting("max_input_tokens", 0) or 0)
        if budget_tokens <= 0:
            return [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

        budget_chars = budget_tokens * 3
        system_limit = min(len(system_prompt), max(budget_chars // 3, 1))
        user_limit = max(budget_chars - system_limit, 1)
        return [
            {"role": "system", "content": _truncate_prompt(system_prompt, system_limit)},
            {"role": "user", "content": _truncate_prompt(user_prompt, user_limit)},
        ]

    def _reserve_rate_budget(self, model: str, estimated_tokens: int) -> None:
        limit = int(self._setting("rate_limit_tokens_per_minute", 0) or 0)
        if limit <= 0:
            return

        key = f"{self._base_url() or 'default'}::{model}"
        while True:
            with _RATE_LOCK:
                now = time.monotonic()
                window = _RATE_WINDOWS[key]
                while window and now - window[0][0] >= 60:
                    window.popleft()
                used = sum(tokens for _, tokens in window)
                if not window or used + estimated_tokens <= limit:
                    window.append((now, estimated_tokens))
                    return
                wait_seconds = max(0.1, 60 - (now - window[0][0]))

            logger.info(
                "LLM rate budget reached for %s (%d/%d estimated tokens); waiting %.1fs",
                model, used, limit, wait_seconds,
            )
            time.sleep(wait_seconds)

    @staticmethod
    def _native_json_mode(model: str) -> bool:
        # Groq's GPT-OSS endpoint can reject otherwise valid complex briefs in
        # native JSON mode with json_validate_failed. The parser below already
        # accepts JSON fenced in ordinary text, so avoid that unstable mode.
        return not model.startswith("openai/gpt-oss-")

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        api_key = self._api_key()
        if not api_key:
            raise LLMUnavailable(f"No API key configured for LLM provider {self.provider!r}")

        from openai import OpenAI

        kwargs: Dict[str, Any] = {"api_key": api_key}
        if self._base_url():
            kwargs["base_url"] = self._base_url()
        kwargs["timeout"] = float(self._setting("timeout_seconds", 180) or 180)

        self._client = OpenAI(**kwargs)
        return self._client

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> str:
        """Return the assistant's text, raising :class:`LLMUnavailable` on failure."""
        client = self._ensure_client()

        selected_model = model or self.model
        output_tokens = max_tokens or int(self._setting("max_output_tokens", 4000) or 4000)
        messages = self._bounded_messages(system_prompt, user_prompt)
        request: Dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "temperature": (
                temperature
                if temperature is not None
                else float(self._setting("temperature", 0.2) or 0.2)
            ),
            "max_tokens": output_tokens,
        }
        if json_mode and self._native_json_mode(selected_model):
            request["response_format"] = {"type": "json_object"}
        if self._request_overrides():
            # The OpenAI SDK forwards these provider-specific fields unchanged.
            request["extra_body"] = self._request_overrides()

        input_chars = sum(len(message["content"]) for message in messages)
        self._reserve_rate_budget(selected_model, input_chars // 3 + output_tokens)
        retries = int(self._setting("rate_limit_max_retries", 0) or 0)
        for attempt in range(retries + 1):
            try:
                response = client.chat.completions.create(**request)
                break
            except Exception as exc:
                if getattr(exc, "status_code", None) != 429 or attempt >= retries:
                    raise LLMUnavailable(f"LLM request failed: {exc}") from exc
                headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
                retry_after = headers.get("retry-after")
                try:
                    wait_seconds = max(float(retry_after), 1.0)
                except (TypeError, ValueError):
                    wait_seconds = min(60.0, 2.0 ** (attempt + 1))
                logger.warning(
                    "LLM rate limited for %s; retrying in %.1fs (%d/%d)",
                    selected_model, wait_seconds, attempt + 1, retries,
                )
                time.sleep(wait_seconds)

        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMUnavailable("LLM returned no choices")

        content = choices[0].message.content or ""
        if not content.strip():
            raise LLMUnavailable("LLM returned an empty response")

        usage = getattr(response, "usage", None)
        if usage is not None:
            logger.info(
                "LLM call: model=%s prompt=%s completion=%s tokens",
                request["model"],
                getattr(usage, "prompt_tokens", "?"),
                getattr(usage, "completion_tokens", "?"),
            )
        return content

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """Request JSON and parse it. Raises on unparseable output."""
        text = self.complete(
            system_prompt,
            user_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        payload = extract_json(text)
        if payload is None:
            raise LLMUnavailable("LLM response was not valid JSON")
        return payload
