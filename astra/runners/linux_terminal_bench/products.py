from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Product:
    id: str
    agent: str
    model: str | None
    match_model: bool
    required_kwargs: dict[str, Any]
    credential_env: str | None
    prebuilt: str | None = None

    @property
    def config_path(self) -> Path:
        return Path(__file__).with_name("configs") / f"{self.id}.yaml"


PRODUCTS = {
    "astra": Product(
        id="astra",
        agent=(
            "astra.runners.astra_terminal_bench.agent:"
            "AstraTerminalBenchC0Agent"
        ),
        model="glm-5.2(thinking:high)",
        match_model=True,
        required_kwargs={
            "max_turns": 50,
            "turn_timeout_sec": 24000,
            "trigger_timeout_sec": 24000,
            "stream_transport_retries": 2,
            "product_timeout_multiplier": 1.0,
        },
        credential_env=None,
    ),
    "hermes": Product(
        id="hermes",
        agent=(
            "astra.runners.hermes_terminal_bench.agent:"
            "HermesTerminalBenchC0Agent"
        ),
        model="zai/glm-5.2",
        match_model=True,
        required_kwargs={
            "version": "v2026.7.20",
            "preinstalled": True,
            "max_turns": 90,
            "turn_timeout_sec": 24000,
            "trigger_timeout_sec": 24000,
            "poll_interval_sec": 0.5,
            "product_timeout_multiplier": 1.0,
        },
        credential_env="ZAI_API_KEY",
        prebuilt="hermes",
    ),
    "pi": Product(
        id="pi",
        agent=(
            "astra.runners.pi_terminal_bench.agent:"
            "PiTerminalBenchC0Agent"
        ),
        model="zai/glm-5.2",
        match_model=True,
        required_kwargs={
            "version": "0.73.1",
            "preinstalled": True,
            "thinking": "high",
            "turn_timeout_sec": 24000,
            "trigger_timeout_sec": 24000,
            "poll_interval_sec": 0.5,
            "product_timeout_multiplier": 1.0,
        },
        credential_env="ZAI_API_KEY",
        prebuilt="pi",
    ),
    "dsh": Product(
        id="dsh",
        agent=(
            "astra.runners.dsh_terminal_bench.agent:"
            "DshTerminalBenchC0Agent"
        ),
        model="zai/glm-5.2",
        match_model=True,
        required_kwargs={
            "version": "0.1.0rc6",
            "profile": "terminalbench-glm52",
            "turn_timeout_sec": 24000,
            "trigger_timeout_sec": 24000,
            "poll_interval_sec": 0.5,
            "product_timeout_multiplier": 1.0,
        },
        credential_env="ZAI_API_KEY",
    ),
}
