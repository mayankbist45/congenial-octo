"""LLM client wrapper.

Talks to any OpenAI-compatible endpoint so the provider can be swapped in
``.env``. Two things matter for this fund: JSON responses must be parseable,
and an LLM outage must never stop the daily cycle - callers fall back to the
rules-only path when :meth:`LLMClient.available` is False.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from src.config import Config, get_config
from src.logging_setup import get_logger

logger = get_logger(__name__)


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


class LLMClient:
    """Thin, provider-agnostic chat completion client."""

    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._secrets = self._config.secrets
        self._client: Optional[Any] = None

    @property
    def available(self) -> bool:
        return self._secrets.has_llm_credentials

    @property
    def model(self) -> str:
        return str(self._config.get("llm.model", "gpt-4o"))

    @property
    def digest_model(self) -> str:
        return str(self._config.get("llm.digest_model", self.model))

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.available:
            raise LLMUnavailable("No LLM API key configured")

        from openai import OpenAI

        kwargs: Dict[str, Any] = {"api_key": self._secrets.openai_api_key}
        if self._secrets.openai_base_url:
            kwargs["base_url"] = self._secrets.openai_base_url
        kwargs["timeout"] = float(self._config.get("llm.timeout_seconds", 180) or 180)

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

        request: Dict[str, Any] = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": (
                temperature
                if temperature is not None
                else float(self._config.get("llm.temperature", 0.2) or 0.2)
            ),
            "max_tokens": max_tokens
            or int(self._config.get("llm.max_output_tokens", 4000) or 4000),
        }
        if json_mode:
            request["response_format"] = {"type": "json_object"}

        try:
            response = client.chat.completions.create(**request)
        except Exception as exc:
            raise LLMUnavailable(f"LLM request failed: {exc}") from exc

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
