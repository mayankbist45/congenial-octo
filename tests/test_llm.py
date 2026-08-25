from types import SimpleNamespace

import pytest

from src.llm import LLMClient, LLMUnavailable


class FakeConfig:
    def __init__(self):
        self.secrets = SimpleNamespace(
            openai_api_key="test-key",
            openai_base_url="https://api.groq.com/openai/v1",
            llm_provider="",
        )
        self.values = {
            "llm.provider": "groq",
            "llm.model": "openai/gpt-oss-120b",
            "llm.digest_model": "openai/gpt-oss-20b",
            "llm.temperature": 0.2,
            "llm.max_input_tokens": 100,
            "llm.max_output_tokens": 20,
            "llm.rate_limit_tokens_per_minute": 0,
            "llm.rate_limit_max_retries": 0,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeCompletions:
    def __init__(self, *responses):
        self.request = None
        self.requests = []
        self.responses = list(responses) or [
            SimpleNamespace(message=SimpleNamespace(content='{"status": "ok"}'), finish_reason="stop")
        ]

    def create(self, **request):
        self.request = request
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(choices=[response], usage=None)


def test_gpt_oss_json_request_avoids_native_json_mode_and_bounds_prompt():
    client = LLMClient(FakeConfig())
    completions = FakeCompletions()
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = client.complete_json("system " * 80, "user " * 160)

    assert result == {"status": "ok"}
    assert "response_format" not in completions.request
    assert sum(len(message["content"]) for message in completions.request["messages"]) <= 300


def test_named_provider_profile_selects_its_model_and_endpoint():
    config = FakeConfig()
    config.secrets.llm_provider = "deepseek"
    config.secrets.llm_api_key = lambda provider: "deepseek-key" if provider == "deepseek" else ""
    config.values["llm.providers"] = {
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "digest_model": "deepseek-v4-flash",
            "request_overrides": {"thinking": {"type": "disabled"}},
        }
    }
    client = LLMClient(config)

    assert client.provider == "deepseek"
    assert client.model == "deepseek-v4-flash"
    assert client.digest_model == "deepseek-v4-flash"
    assert client._base_url() == "https://api.deepseek.com"
    assert client._request_overrides() == {"thinking": {"type": "disabled"}}
    assert client.available


def test_deepseek_json_failure_retries_once_with_groq(monkeypatch):
    config = FakeConfig()
    config.secrets = SimpleNamespace(
        openai_api_key="groq-key",
        openai_base_url=None,
        llm_provider="deepseek",
        llm_api_key=lambda provider: f"{provider}-key",
    )
    config.values["llm.fallback_provider"] = "groq"
    config.values["llm.providers"] = {
        "deepseek": {"model": "deepseek-v4-flash", "digest_model": "deepseek-v4-flash"},
        "groq": {"model": "openai/gpt-oss-120b", "digest_model": "openai/gpt-oss-20b"},
    }
    primary = FakeCompletions(
        SimpleNamespace(message=SimpleNamespace(content='{"status":'), finish_reason="length")
    )
    fallback = FakeCompletions(
        SimpleNamespace(message=SimpleNamespace(content='{"status": "ok"}'), finish_reason="stop")
    )
    clients = {
        "deepseek": SimpleNamespace(chat=SimpleNamespace(completions=primary)),
        "groq": SimpleNamespace(chat=SimpleNamespace(completions=fallback)),
    }
    monkeypatch.setattr(LLMClient, "_ensure_client", lambda self: clients[self.provider])

    result = LLMClient(config).complete_json("system", "user", model="deepseek-v4-flash")

    assert result == {"status": "ok"}
    assert len(primary.requests) == len(fallback.requests) == 1
    assert fallback.request["model"] == "openai/gpt-oss-20b"
    assert fallback.request["messages"] == primary.request["messages"]


@pytest.mark.parametrize("content", ["not json", "[]", "", '{"status":'])
def test_invalid_deepseek_json_retries_once_with_groq(monkeypatch, content):
    config = FakeConfig()
    config.secrets = SimpleNamespace(
        openai_api_key="groq-key",
        openai_base_url=None,
        llm_provider="deepseek",
        llm_api_key=lambda provider: f"{provider}-key",
    )
    config.values["llm.fallback_provider"] = "groq"
    config.values["llm.providers"] = {
        "deepseek": {"model": "deepseek-v4-flash", "digest_model": "deepseek-v4-flash"},
        "groq": {"model": "openai/gpt-oss-120b", "digest_model": "openai/gpt-oss-20b"},
    }
    primary = FakeCompletions(
        SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")
    )
    fallback = FakeCompletions(
        SimpleNamespace(message=SimpleNamespace(content='{"status": "ok"}'), finish_reason="stop")
    )
    clients = {
        "deepseek": SimpleNamespace(chat=SimpleNamespace(completions=primary)),
        "groq": SimpleNamespace(chat=SimpleNamespace(completions=fallback)),
    }
    monkeypatch.setattr(LLMClient, "_ensure_client", lambda self: clients[self.provider])

    assert LLMClient(config).complete_json("system", "user") == {"status": "ok"}
    assert len(primary.requests) == len(fallback.requests) == 1


def test_groq_failure_after_deepseek_failure_does_not_retry_again(monkeypatch):
    config = FakeConfig()
    config.secrets = SimpleNamespace(
        openai_api_key="groq-key",
        openai_base_url=None,
        llm_provider="deepseek",
        llm_api_key=lambda provider: f"{provider}-key",
    )
    config.values["llm.fallback_provider"] = "groq"
    config.values["llm.providers"] = {
        "deepseek": {"model": "deepseek-v4-flash"},
        "groq": {"model": "openai/gpt-oss-120b"},
    }
    primary = FakeCompletions(RuntimeError("DeepSeek unavailable"))
    fallback = FakeCompletions(RuntimeError("Groq unavailable"))
    clients = {
        "deepseek": SimpleNamespace(chat=SimpleNamespace(completions=primary)),
        "groq": SimpleNamespace(chat=SimpleNamespace(completions=fallback)),
    }
    monkeypatch.setattr(LLMClient, "_ensure_client", lambda self: clients[self.provider])

    with pytest.raises(LLMUnavailable, match="fallback groq failed"):
        LLMClient(config).complete_json("system", "user")

    assert len(primary.requests) == len(fallback.requests) == 1
