from __future__ import annotations

import time
from typing import Optional

from dify_plugin import TextEmbeddingModel
from dify_plugin.entities.model import EmbeddingInputType, PriceType
from dify_plugin.entities.model.text_embedding import EmbeddingUsage, TextEmbeddingResult
from dify_plugin.errors.model import (
    CredentialsValidateFailedError,
    InvokeAuthorizationError,
    InvokeBadRequestError,
    InvokeConnectionError,
    InvokeError,
    InvokeRateLimitError,
    InvokeServerUnavailableError,
)

from models.shared import post_json, validate_base_url


MAAS_EMBEDDING_MODEL = "bge-m3"


class HuaweiMaaSTextEmbeddingModel(TextEmbeddingModel):
    """Text-only BGE-M3 embedding adapter for Huawei MaaS."""

    def _invoke(
        self,
        model: str,
        credentials: dict,
        texts: list[str],
        user: Optional[str] = None,
        input_type: EmbeddingInputType = EmbeddingInputType.DOCUMENT,
    ) -> TextEmbeddingResult:
        if str(model).strip() != MAAS_EMBEDDING_MODEL:
            raise InvokeBadRequestError(
                f"Huawei MaaS text-only plugin only serves {MAAS_EMBEDDING_MODEL}"
            )
        validate_base_url(credentials.get("base_url"))
        response = post_json(
            credentials,
            "/embeddings",
            {"model": model, "input": texts, "encoding_format": "float"},
        )
        data = response.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("Huawei MaaS embedding response count does not match request")
        indexed = sorted(
            ((int(item.get("index", index)), item.get("embedding")) for index, item in enumerate(data)),
            key=lambda item: item[0],
        )
        embeddings = [vector for _, vector in indexed]
        if any(not isinstance(vector, list) or not vector for vector in embeddings):
            raise ValueError("Huawei MaaS embedding response is missing a vector")
        usage = response.get("usage") or {}
        tokens = int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
        return TextEmbeddingResult(model=model, embeddings=embeddings, usage=self._usage(model, credentials, tokens))

    def get_num_tokens(self, model: str, credentials: dict, texts: list[str]) -> list[int]:
        return [self._get_num_tokens_by_gpt2(text) for text in texts]

    def validate_credentials(self, model: str, credentials: dict) -> None:
        try:
            self._invoke(model=model, credentials=credentials, texts=["ping"])
        except Exception as exc:
            raise CredentialsValidateFailedError(str(exc)) from exc

    @property
    def _invoke_error_mapping(self) -> dict[type[InvokeError], list[type[Exception]]]:
        return {
            InvokeConnectionError: [InvokeConnectionError],
            InvokeServerUnavailableError: [InvokeServerUnavailableError],
            InvokeRateLimitError: [InvokeRateLimitError],
            InvokeAuthorizationError: [InvokeAuthorizationError],
            InvokeBadRequestError: [InvokeBadRequestError, KeyError, TypeError, ValueError],
        }

    def _usage(self, model: str, credentials: dict, tokens: int) -> EmbeddingUsage:
        price = self.get_price(model=model, credentials=credentials, price_type=PriceType.INPUT, tokens=tokens)
        return EmbeddingUsage(
            tokens=tokens,
            total_tokens=tokens,
            unit_price=price.unit_price,
            price_unit=price.unit,
            total_price=price.total_amount,
            currency=price.currency,
            latency=time.perf_counter() - self.started_at,
        )
