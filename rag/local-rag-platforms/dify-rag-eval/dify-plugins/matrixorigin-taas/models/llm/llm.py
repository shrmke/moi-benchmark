from __future__ import annotations

from collections.abc import Generator
import os
from typing import Optional

from dify_plugin import OAICompatLargeLanguageModel
from dify_plugin.entities.model.llm import LLMResult, LLMResultChunk
from dify_plugin.entities.model.message import PromptMessage, PromptMessageTool


DEFAULT_MAAS_BASE_URL = "https://api.modelarts-maas.com/v1"
MAAS_GLM_MODEL = "glm-5.2"


def _normalise_url(value: object) -> str:
    return str(value or "").strip().rstrip("/")


def is_huawei_maas_glm(model: str, credentials: dict) -> bool:
    """Identify only the exact MaaS GLM contract, leaving legacy providers alone."""

    configured_base_url = credentials.get("base_url") or os.getenv("MAAS_BASE_URL", DEFAULT_MAAS_BASE_URL)
    expected_base_url = os.getenv("MAAS_BASE_URL", DEFAULT_MAAS_BASE_URL)
    return (
        str(model).strip() == MAAS_GLM_MODEL
        and _normalise_url(configured_base_url) == _normalise_url(expected_base_url)
    )


def prepare_huawei_maas_model_parameters(
    model: str,
    credentials: dict,
    model_parameters: dict,
) -> dict:
    """Add MaaS GLM's required disabled-thinking mode without mutating input."""

    prepared = dict(model_parameters)
    if is_huawei_maas_glm(model, credentials):
        prepared["thinking"] = {"type": "disabled"}
    return prepared


class MatrixOriginTaaSLargeLanguageModel(OAICompatLargeLanguageModel):
    """Use the SDK's OpenAI-compatible chat implementation with TaaS credentials."""

    @staticmethod
    def _oai_credentials(model: str, credentials: dict) -> dict:
        mapped = dict(credentials)
        mapped.update(
            {
                "endpoint_url": str(credentials.get("base_url") or "").rstrip("/"),
                "endpoint_model_name": model,
                "mode": "chat",
                "stream_mode_auth": "not_use",
                "function_calling_type": "no_call",
                "stream_function_calling": "not_supported",
                "vision_support": "not_support",
            }
        )
        return mapped

    def _invoke(
        self,
        model: str,
        credentials: dict,
        prompt_messages: list[PromptMessage],
        model_parameters: dict,
        tools: Optional[list[PromptMessageTool]] = None,
        stop: Optional[list[str]] = None,
        stream: bool = True,
        user: Optional[str] = None,
    ) -> LLMResult | Generator[LLMResultChunk, None, None]:
        return super()._invoke(
            model=model,
            credentials=self._oai_credentials(model, credentials),
            prompt_messages=prompt_messages,
            model_parameters=prepare_huawei_maas_model_parameters(model, credentials, model_parameters),
            tools=tools,
            stop=stop,
            stream=stream,
            user=user,
        )

    def validate_credentials(self, model: str, credentials: dict) -> None:
        super().validate_credentials(model, self._oai_credentials(model, credentials))

    def get_num_tokens(
        self,
        model: str,
        credentials: dict,
        prompt_messages: list[PromptMessage],
        tools: Optional[list[PromptMessageTool]] = None,
    ) -> int:
        return super().get_num_tokens(
            model,
            self._oai_credentials(model, credentials),
            prompt_messages,
            tools,
        )
