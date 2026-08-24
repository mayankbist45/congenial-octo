from types import SimpleNamespace

from src.llm import LLMClient


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
    def __init__(self):
        self.request = None

    def create(self, **request):
        self.request = request
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"status": "ok"}'))],
            usage=None,
        )


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
