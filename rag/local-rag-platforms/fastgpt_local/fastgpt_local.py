#!/usr/bin/env python3
"""Read-only deployment checks and opt-in FastGPT API smoke for the local stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
ROOT = PLATFORM_ROOT.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))
from env import inject_central_env  # noqa: E402

RUNTIME = ROOT / ".local-services/fastgpt_local"
COMPOSE = RUNTIME / "compose/docker-compose.pg.yml"
SOURCE_LOCK = RUNTIME / "compose/source-lock.json"
LEGACY_CONTRACTS = Path(__file__).with_name("legacy_provider_contracts.json")
MAAS_CONTRACTS = Path(__file__).with_name("maas_provider_contract.json")
DEEPSEEK_CONTRACTS = Path(__file__).with_name("deepseek_provider_contract.json")
MAAS_MODELS = Path(__file__).with_name("maas-models.example.json")
DEEPSEEK_MODELS = Path(__file__).with_name("deepseek-models.example.json")
EXPECTED_TAG = "v4.15.6"
EXPECTED_COMMIT = "3db33e93b78e75b37c93f7a6e3d0fafeafbfd256"
# The standalone helper is used by the current benchmark handoff. Legacy
# providers remain available only through an explicit --provider selection.
DEFAULT_PROVIDER = "maas"
DEFAULT_MAAS_BASE_URL = "https://api.modelarts-maas.com/v1"
MAAS_HOST = "api.modelarts-maas.com"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_HOST = "api.deepseek.com"
# FastGPT's pinned vector-store schemas are VECTOR(1536)/HALFVEC(1536).  The
# upstream embedding dimension is recorded separately so a successful API
# call cannot be mistaken for a full-width 4096-dimensional index.
FASTGPT_VECTOR_DIMENSION_LIMIT = 1536


def native_search_limit() -> int:
    """Return a bounded native-app search limit for external LLM context."""
    try:
        return max(1, int(os.getenv("FASTGPT_NATIVE_SEARCH_LIMIT", "10")))
    except ValueError:
        return 10

PROVIDER_PROFILES = {
    "maas": {
        "env_prefix": "MAAS",
        "channel_name": "Huawei Cloud MaaS",
        "base_url": DEFAULT_MAAS_BASE_URL,
        "llm_model": "glm-5.2",
        "embedding_model": "bge-m3",
        "embedding_dimension": 1024,
        "channel_type": 1,
    },
    "taas": {
        "env_prefix": "TAAS",
        "channel_name": "MatrixOrigin TaaS",
        "base_url": "https://token.moi.matrixorigin.cn/v1",
        "llm_model": "qwen3.6-flash",
        "embedding_model": "bge-m3",
        "channel_type": 1,
    },
    "qianfan": {
        "env_prefix": "QIANFAN",
        "channel_name": "Baidu Qianfan V2",
        "base_url": "https://qianfan.baidubce.com/v2",
        "llm_model": "deepseek-v4-flash",
        "embedding_model": "qwen3-embedding-8b",
        "embedding_dimension": 4096,
        "reranker_model": "qwen3-reranker-8b",
        # AIProxy v0.6.5 model.ChannelType: ChannelTypeQianfan = 49.
        # ChannelTypeBaiduV2 = 13 is the legacy ak|sk adapter, not Qianfan V2 API keys.
        "channel_type": 49,
    },
    "deepseek-official": {
        "env_prefix": "DEEPSEEK",
        "api_key_env": "DEEPSEEK_API_KEY_NEW",
        "channel_name": "DeepSeek Official",
        "base_url": DEFAULT_DEEPSEEK_BASE_URL,
        "llm_model": "deepseek-v4-flash",
        # The embedding model is supplied by the separate MaaS channel.
        "embedding_model": "bge-m3",
        "embedding_dimension": 1024,
        "channel_type": 1,
    },
}


class ContractError(RuntimeError):
    pass


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def provider_contract(provider_name: str) -> dict[str, Any]:
    """Load the active provider contract, keeping legacy templates non-active."""

    if provider_name not in PROVIDER_PROFILES:
        raise ContractError(f"unknown provider: {provider_name}")
    path = {
        "maas": MAAS_CONTRACTS,
        "deepseek-official": DEEPSEEK_CONTRACTS,
    }.get(provider_name, LEGACY_CONTRACTS)
    return load_json(path)


def maas_model_configuration() -> list[dict[str, Any]]:
    """Load the exact text-only MaaS model metadata required by FastGPT.

    FastGPT keeps model type metadata in its own ``system_models`` collection;
    an AIProxy channel by itself is not enough for the saved-channel test or
    for dataset/app model selectors. Keep this small configuration beside the
    provider contract so the MaaS path cannot silently inherit a legacy model.
    """

    models = load_json(MAAS_MODELS)
    if not isinstance(models, list) or not models:
        raise ContractError("MaaS model configuration must be a non-empty list")
    required = {"glm-5.2": "llm", "bge-m3": "embedding"}
    observed: dict[str, str] = {}
    for item in models:
        if not isinstance(item, dict):
            raise ContractError("MaaS model configuration contains a non-object")
        model = str(item.get("model", "")).strip()
        metadata = item.get("metadata")
        if not model or not isinstance(metadata, dict):
            raise ContractError("MaaS model configuration requires model and metadata")
        model_type = str(metadata.get("type", "")).strip()
        if model in observed:
            raise ContractError(f"duplicate MaaS model configuration: {model}")
        observed[model] = model_type
        if model not in required:
            raise ContractError(f"unexpected MaaS model configuration: {model}")
        if model_type != required[model]:
            raise ContractError(f"MaaS model {model} must be type {required[model]}")
        if str(metadata.get("model", "")).strip() != model:
            raise ContractError(f"MaaS model metadata.model mismatch: {model}")
        if metadata.get("isActive") is not True:
            raise ContractError(f"MaaS model must be active: {model}")
    if observed != required:
        raise ContractError("MaaS model configuration must contain exactly glm-5.2 and bge-m3")
    return models


def deepseek_model_configuration() -> list[dict[str, Any]]:
    """Load the single official DeepSeek text model required by FastGPT."""

    models = load_json(DEEPSEEK_MODELS)
    if not isinstance(models, list) or len(models) != 1:
        raise ContractError("DeepSeek model configuration must contain exactly one model")
    item = models[0]
    metadata = item.get("metadata") if isinstance(item, dict) else None
    if (
        not isinstance(item, dict)
        or str(item.get("model", "")).strip() != "deepseek-v4-flash"
        or not isinstance(metadata, dict)
        or metadata.get("type") != "llm"
        or metadata.get("isActive") is not True
    ):
        raise ContractError("DeepSeek model configuration must be active deepseek-v4-flash llm metadata")
    return models


def build_model_configuration_mongosh_script(models: list[dict[str, Any]]) -> str:
    """Build an idempotent Mongo update without deleting unrelated model rows."""

    encoded = json.dumps(models, ensure_ascii=False, separators=(",", ":"))
    return f"""
const models = {encoded};
const results = [];
for (const item of models) {{
  const result = db.system_models.updateOne(
    {{ model: item.model }},
    {{ $set: {{ model: item.model, metadata: item.metadata }} }},
    {{ upsert: true }}
  );
  results.push({{ model: item.model, matched: result.matchedCount, modified: result.modifiedCount, upserted: result.upsertedCount }});
}}
print(JSON.stringify({{ models: results }}));
""".strip()


def ensure_fastgpt_model_configuration(provider_name: str) -> dict[str, Any]:
    """Materialize provider model metadata in the local FastGPT Mongo store."""

    if provider_name == "maas":
        models = maas_model_configuration()
    elif provider_name == "deepseek-official":
        models = deepseek_model_configuration()
    else:
        return {"status": "not_required", "provider": provider_name, "models": []}
    username = os.getenv("FASTGPT_MONGO_USERNAME", "myusername").strip()
    password = os.getenv("FASTGPT_MONGO_PASSWORD", "mypassword").strip()
    database = os.getenv("FASTGPT_MONGO_DATABASE", "fastgpt").strip()
    if not username or not password or not database:
        raise ContractError("FastGPT Mongo model configuration credentials are incomplete")
    script = build_model_configuration_mongosh_script(models)
    result = run([
        "docker",
        "exec",
        "fastgpt-mongo",
        "mongosh",
        "--quiet",
        "-u",
        username,
        "-p",
        password,
        "--authenticationDatabase",
        "admin",
        database,
        "--eval",
        script,
    ])
    if result.returncode:
        raise ContractError(
            f"FastGPT {provider_name} model metadata update failed: "
            f"{redact_error_message(result.stderr.strip())[:1200]}"
        )
    try:
        update_result = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ContractError("FastGPT MaaS model metadata update returned invalid JSON") from exc
    reload_wait = max(0.0, float(os.getenv("FASTGPT_MODEL_RELOAD_WAIT_SECONDS", "1")))
    if reload_wait:
        time.sleep(reload_wait)
    return {
        "status": "ready",
        "provider": provider_name,
        "database": database,
        "models": [item["model"] for item in models],
        "update": update_result,
        "reload": "mongo_change_stream",
        "reload_wait_seconds": reload_wait,
    }


def provider_api_key(provider_name: str, env: dict[str, str]) -> str:
    """Load only the selected provider's API key; never fall back across providers."""

    if provider_name not in PROVIDER_PROFILES:
        raise ContractError(f"unknown provider: {provider_name}")
    profile = PROVIDER_PROFILES[provider_name]
    key_name = str(profile.get("api_key_env") or f"{profile['env_prefix']}_API_KEY")
    value = str(env.get(key_name, "")).strip()
    if not value or value.startswith("<"):
        raise ContractError(f"{key_name} must be loaded before provider --execute")
    return value


def preflight() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    lock = load_json(SOURCE_LOCK)
    if lock.get("resolved_tag") != EXPECTED_TAG:
        failures.append(f"source tag is {lock.get('resolved_tag')!r}, expected {EXPECTED_TAG}")
    if lock.get("commit") != EXPECTED_COMMIT:
        failures.append("source commit does not match the pinned v4.15.6 commit")

    source = ROOT / lock["source"]
    head = run(["git", "-C", str(source), "rev-parse", "HEAD"])
    if head.returncode or head.stdout.strip() != EXPECTED_COMMIT:
        failures.append("prepared source checkout HEAD does not match source-lock.json")

    config = run(["docker", "compose", "-p", "moi_fastgpt_local", "-f", str(COMPOSE), "config", "--quiet"])
    if config.returncode:
        failures.append(f"docker compose config failed: {config.stderr.strip()}")
    images_result = run(["docker", "compose", "-p", "moi_fastgpt_local", "-f", str(COMPOSE), "config", "--images"])
    images = sorted(set(images_result.stdout.splitlines())) if images_result.returncode == 0 else []
    if "ghcr.io/labring/fastgpt:v4.15.4" not in images:
        failures.append("runtime compose no longer resolves the checked-in v4.15.4 FastGPT image")

    manifests: list[dict[str, Any]] = []
    for path in sorted((RUNTIME / "logs").glob("image-manifest-*.json")):
        data = load_json(path)
        platforms = data.get("platforms", [])
        native_arm64 = any(x.get("os") == "linux" and x.get("architecture") == "arm64" for x in platforms)
        manifests.append({"image": data.get("image"), "linux_arm64": native_arm64})
        if not native_arm64:
            failures.append(f"no linux/arm64 manifest recorded for {data.get('image')}")

    running = run(["docker", "ps", "--format", "{{.Names}}"])
    running_names = running.stdout.splitlines() if running.returncode == 0 else []
    fastgpt_running = sorted(name for name in running_names if "fastgpt" in name.lower())
    if fastgpt_running:
        failures.append("FastGPT containers are running during the no-start preparation phase")
    expected_existing = {
        "moi-openxml-parser": any(name == "moi-openxml-parser" for name in running_names),
        "matrixone": any(name == "matrixone" for name in running_names),
        "dify": any(name.startswith("moi_dify_local-") for name in running_names),
    }
    for service, present in expected_existing.items():
        if service == "dify":
            continue
        if not present:
            warnings.append(f"expected existing {service} service is not running")

    env = {
        "MAAS_API_KEY": bool(os.getenv("MAAS_API_KEY")),
        "TAAS_API_KEY": bool(os.getenv("TAAS_API_KEY")),
        "FASTGPT_API_KEY": bool(os.getenv("FASTGPT_API_KEY")),
        "FASTGPT_APP_ID": bool(os.getenv("FASTGPT_APP_ID")),
    }
    if not env["TAAS_API_KEY"]:
        warnings.append("TAAS_API_KEY is not loaded; configure the AI Proxy channel later in the local UI")
    if not env["MAAS_API_KEY"]:
        warnings.append("MAAS_API_KEY is not loaded; Huawei MaaS channel setup remains unavailable")
    if not env["FASTGPT_API_KEY"] or not env["FASTGPT_APP_ID"]:
        warnings.append("FastGPT local API key/app id are not loaded; API smoke remains blocked")

    report = {
        "status": "ready" if not failures else "blocked",
        "mutated_runtime": False,
        "source": {"tag": lock.get("resolved_tag"), "commit": lock.get("commit")},
        "compose": {"file": str(COMPOSE.relative_to(ROOT)), "valid": config.returncode == 0, "images": images},
        "image_platforms": manifests,
        "running": {"fastgpt": fastgpt_running, "existing_services": expected_existing},
        "taas": {
            "base_url": os.getenv("TAAS_BASE_URL", "https://token.moi.matrixorigin.cn/v1"),
            "llm_model": os.getenv("TAAS_LLM_MODEL", "qwen3.6-flash"),
            "embedding_model": os.getenv("TAAS_EMBEDDING_MODEL", "bge-m3"),
            "api_key_loaded": env["TAAS_API_KEY"],
            "model_egress": "external",
        },
        "maas": {
            "base_url": os.getenv("MAAS_BASE_URL", DEFAULT_MAAS_BASE_URL),
            "llm_model": os.getenv("MAAS_LLM_MODEL", "glm-5.2"),
            "embedding_model": os.getenv("MAAS_EMBEDDING_MODEL", "bge-m3"),
            "reranker_model": os.getenv("MAAS_RERANKER_MODEL", ""),
            "api_key_loaded": env["MAAS_API_KEY"],
            "model_egress": "external",
        },
        "local_credentials": {"api_key_loaded": env["FASTGPT_API_KEY"], "app_id_loaded": env["FASTGPT_APP_ID"]},
        "warnings": warnings,
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def api_request(
    base_url: str,
    path: str,
    api_key: str,
    *,
    body: Any = None,
    multipart: tuple[Path, dict[str, Any]] | None = None,
    timeout: float | None = None,
    method: str = "POST",
) -> Any:
    timeout = timeout if timeout is not None else float(os.getenv("FASTGPT_HTTP_TIMEOUT_SECONDS", "60"))
    headers = {"Authorization": f"Bearer {api_key}"}
    payload: bytes | None = None
    if multipart:
        file_path, data = multipart
        boundary = f"----moi-fastgpt-{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"data\"\r\nContent-Type: application/json\r\n\r\n{json.dumps(data)}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{file_path.name}\"\r\nContent-Type: {content_type}\r\n\r\n".encode(),
            file_path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        payload = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = Request(f"{base_url.rstrip('/')}{path}", data=payload, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise ContractError(f"{path} returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ContractError(f"{path} is unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ContractError(f"{path} timed out after {timeout:g}s") from exc
    if isinstance(result, dict) and result.get("code") not in (None, 200):
        raise ContractError(f"{path} returned API code {result.get('code')}: {result.get('message')}")
    return result.get("data", result) if isinstance(result, dict) else result


def validate_local_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ContractError("FASTGPT_BASE_URL must target localhost; cloud endpoints are refused")
    return value.rstrip("/")


def validate_maas_base_url(value: str) -> str:
    """Accept only the frozen Huawei MaaS HTTPS endpoint."""

    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError as exc:
        raise ContractError("MAAS_ONLY_BASE_URL_REQUIRED") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != MAAS_HOST
        or port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ContractError("MAAS_ONLY_BASE_URL_REQUIRED")
    return raw.rstrip("/")


def validate_deepseek_base_url(value: str) -> str:
    """Accept only the frozen official DeepSeek HTTPS endpoint."""

    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != DEEPSEEK_HOST
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ContractError("DEEPSEEK_OFFICIAL_BASE_URL_REQUIRED")
    return raw.rstrip("/")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.lower() in {"key", "api_key", "authorization"} else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def redact_error_message(message: str) -> str:
    """Keep actionable failures while removing loaded credentials and bearer tokens."""
    redacted = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", message)
    for name, secret in os.environ.items():
        if secret and any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            redacted = redacted.replace(secret, "<redacted>")
    return redacted[:2000]


def write_smoke_result(output_dir: Path, result: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=False)
    output = output_dir / "smoke-result.json"
    encoded = json.dumps(redact(result), ensure_ascii=False, indent=2).encode()
    output.write_bytes(encoded)
    output.with_suffix(".json.sha256").write_text(
        hashlib.sha256(encoded).hexdigest() + "  smoke-result.json\n",
        encoding="utf-8",
    )
    return output


def validate_provider_test_results(output: dict[str, Any], *, allow_empty: bool = False) -> None:
    """Reject AIProxy's HTTP-success envelope when model probes failed."""
    test_results = output.get("test", [])
    if not test_results:
        if allow_empty:
            return
        raise ContractError("provider model test returned no model results")
    failed_models = [
        item.get("data", {}).get("model", "unknown")
        for item in test_results
        if item.get("data", {}).get("success") is not True
    ]
    if failed_models:
        raise ContractError(f"provider model test failed for: {', '.join(failed_models)}")


def embedding_spec(provider_name: str, env: dict[str, str]) -> dict[str, Any]:
    """Return the selected upstream embedding and FastGPT's effective width.

    FastGPT v4.15.x does not expose an embedding-dimension field in its model
    preset schema.  Its vector stores are fixed at 1536 dimensions and the
    embedding client truncates wider responses.  Keeping this conversion in a
    small, testable contract prevents the Qianfan 4096-dimensional model from
    being silently represented as an unqualified native 4096 index.
    """
    if provider_name not in PROVIDER_PROFILES:
        raise ContractError(f"unknown provider: {provider_name}")
    profile = PROVIDER_PROFILES[provider_name]
    prefix = profile["env_prefix"]
    model = env.get(f"{prefix}_EMBEDDING_MODEL", profile["embedding_model"]).strip()
    if not model:
        raise ContractError(f"{prefix}_EMBEDDING_MODEL must not be empty")
    if provider_name == "maas" and model != "bge-m3":
        raise ContractError("MAAS_EMBEDDING_MODEL must be exactly bge-m3")

    configured = env.get(f"{prefix}_EMBEDDING_DIMENSION", "").strip()
    default_dimension = profile.get("embedding_dimension")
    raw_dimension = configured or (str(default_dimension) if default_dimension else "")
    source_dimension: int | None = None
    if raw_dimension:
        try:
            source_dimension = int(raw_dimension)
        except ValueError as exc:
            raise ContractError(f"{prefix}_EMBEDDING_DIMENSION must be an integer") from exc
        if source_dimension <= 0:
            raise ContractError(f"{prefix}_EMBEDDING_DIMENSION must be positive")

    effective_dimension = (
        min(source_dimension, FASTGPT_VECTOR_DIMENSION_LIMIT)
        if source_dimension is not None
        else None
    )
    return {
        "provider": provider_name,
        "model": model,
        "source_dimension": source_dimension,
        "fastgpt_dimension_limit": FASTGPT_VECTOR_DIMENSION_LIMIT,
        "effective_dimension": effective_dimension,
        "dimension_action": (
            "truncate_to_fastgpt_1536"
            if source_dimension is not None and source_dimension > FASTGPT_VECTOR_DIMENSION_LIMIT
            else "native_or_provider_defined"
        ),
    }


def build_channel_payload(provider_name: str, env: dict[str, str], *, api_key: str) -> dict[str, Any]:
    """Build a provider-specific AIProxy payload without inheriting TaaS adapter semantics."""
    contract = provider_contract(provider_name)["provider_channel_api"]
    profile = PROVIDER_PROFILES[provider_name]
    prefix = profile["env_prefix"]
    reranker_model = ""
    if provider_name != "maas":
        reranker_model = env.get(f"{prefix}_RERANKER_MODEL", profile.get("reranker_model", "")).strip()
    elif str(env.get(f"{prefix}_ENABLE_RERANKER", "")).strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        reranker_model = env.get(f"{prefix}_RERANKER_MODEL", profile.get("reranker_model", "")).strip()
    llm_model = env.get(f"{prefix}_LLM_MODEL", profile["llm_model"]).strip()
    if provider_name == "maas":
        if llm_model != "glm-5.2":
            raise ContractError("MAAS_LLM_MODEL must be exactly glm-5.2")
        embedding_model = embedding_spec(provider_name, env)["model"]
        embedding_only = str(env.get("MAAS_EMBEDDING_ONLY", "")).strip().casefold() in {
            "1", "true", "yes", "on"
        }
        models = [embedding_model] if embedding_only else [llm_model, embedding_model]
    elif provider_name == "deepseek-official":
        if llm_model != "deepseek-v4-flash":
            raise ContractError("DEEPSEEK_LLM_MODEL must be exactly deepseek-v4-flash")
        models = [llm_model]
    else:
        models = [llm_model, embedding_spec(provider_name, env)["model"]]
    if reranker_model:
        models.append(reranker_model)

    channel_name = env.get(f"{prefix}_CHANNEL_NAME", profile["channel_name"]).strip()
    base_url = env.get(f"{prefix}_BASE_URL", profile["base_url"]).strip().rstrip("/")
    if provider_name == "maas":
        base_url = validate_maas_base_url(base_url)
        if any(marker in channel_name.casefold() for marker in ("taas", "qianfan")):
            raise ContractError("MAAS_CHANNEL_NAME must not identify a legacy provider")
        if any(marker in base_url.casefold() for marker in ("qianfan", "matrixorigin.cn")):
            raise ContractError("MAAS_BASE_URL must not identify a legacy provider")
    elif provider_name == "deepseek-official":
        base_url = validate_deepseek_base_url(base_url)

    payload = dict(contract["create"]["payload"])
    payload.update({
        "type": profile["channel_type"],
        "name": channel_name,
        "base_url": base_url,
        "models": models,
        "key": api_key,
    })
    if provider_name == "qianfan":
        appid = env.get("QIANFAN_APPID", "").strip()
        if appid:
            # Qianfan's adaptor loads Config{AppID json:"appid"} from ChannelConfigs.
            payload["configs"] = {"appid": appid}
    return payload


def build_dataset_payload(
    *, provider_name: str, dataset_name: str, env: dict[str, str]
) -> dict[str, Any]:
    """Build the dataset request used by the local smoke and API contract."""
    profile = PROVIDER_PROFILES[provider_name]
    prefix = profile["env_prefix"]
    embedding = embedding_spec(provider_name, env)
    llm_model = env.get(f"{prefix}_LLM_MODEL", profile["llm_model"]).strip()
    if provider_name == "maas" and llm_model != "glm-5.2":
        raise ContractError("MAAS_LLM_MODEL must be exactly glm-5.2")
    return {
        "parentId": None,
        "type": "dataset",
        "name": dataset_name,
        "intro": "MOI local RAG FastGPT smoke",
        "avatar": "",
        "vectorModel": embedding["model"],
        "agentModel": llm_model,
    }


def build_native_chat_payload(
    *,
    provider_name: str,
    app_id: str,
    question: str,
    env: dict[str, str],
    chat_id: str | None = None,
) -> dict[str, Any]:
    """Build a native app request and disable thinking for exact MaaS GLM calls."""

    if provider_name not in PROVIDER_PROFILES:
        raise ContractError(f"unknown provider: {provider_name}")
    profile = PROVIDER_PROFILES[provider_name]
    prefix = profile["env_prefix"]
    model = str(env.get(f"{prefix}_LLM_MODEL", profile["llm_model"])).strip()
    base_url = str(env.get(f"{prefix}_BASE_URL", profile["base_url"])).strip().rstrip("/")
    expected_base_url = str(env.get("MAAS_BASE_URL", DEFAULT_MAAS_BASE_URL)).strip().rstrip("/")
    if provider_name == "maas" and model != "glm-5.2":
        raise ContractError("MAAS_LLM_MODEL must be exactly glm-5.2")
    if provider_name == "maas":
        base_url = validate_maas_base_url(base_url)
        expected_base_url = validate_maas_base_url(expected_base_url)
    payload: dict[str, Any] = {
        "appId": app_id,
        "chatId": chat_id or str(uuid.uuid4()),
        "stream": False,
        "detail": True,
        "messages": [{"role": "user", "content": question}],
    }
    if provider_name == "maas" and model == "glm-5.2" and base_url == expected_base_url:
        payload["thinking"] = {"type": "disabled"}
    return payload


def _app_input(key: str, value_type: str, value: Any, render_type: str = "hidden") -> dict[str, Any]:
    return {
        "key": key,
        "label": "",
        "valueType": value_type,
        "renderTypeList": [render_type],
        "value": value,
    }


def build_isolated_app_payload(
    *,
    provider_name: str,
    dataset_id: str,
    dataset_name: str,
    llm_model: str | None = None,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    """Create a minimal v4.15.x simple RAG app bound to exactly one new dataset."""
    if provider_name == "deepseek-official":
        # The benchmark uses a split upstream contract: DeepSeek serves only
        # the chat model while the dataset keeps MaaS bge-m3. Both explicit
        # model IDs are supplied by the runner below, so no cross-provider
        # credential or embedding fallback is inferred here.
        profile = {
            "env_prefix": "DEEPSEEK",
            "llm_model": "deepseek-v4-flash",
        }
    else:
        profile = PROVIDER_PROFILES[provider_name]
    prefix = profile["env_prefix"]
    llm_model = (llm_model or os.getenv(f"{prefix}_LLM_MODEL", profile["llm_model"])).strip()
    embedding_model = (embedding_model or embedding_spec(provider_name, dict(os.environ))["model"]).strip()
    start_id = "workflowStartNodeId"
    dataset_node_id = "isolatedDatasetSearch"
    chat_node_id = "isolatedAiChat"
    selected_dataset = [{
        "datasetId": dataset_id,
        "avatar": "",
        "name": dataset_name,
        "vectorModel": {"model": embedding_model},
    }]
    modules = [
        {
            "nodeId": "userGuide",
            "name": "System configuration",
            "flowNodeType": "userGuide",
            "inputs": [],
            "outputs": [],
        },
        {
            "nodeId": start_id,
            "name": "Workflow start",
            "flowNodeType": "workflowStart",
            "inputs": [_app_input("userChatInput", "string", None, "textarea")],
            "outputs": [
                {"id": "userChatInput", "key": "userChatInput", "type": "static", "valueType": "string"},
                {"id": "userFiles", "key": "userFiles", "type": "static", "valueType": "arrayString"},
            ],
        },
        {
            "nodeId": dataset_node_id,
            "name": "Dataset search",
            "flowNodeType": "datasetSearchNode",
            "version": "4.9.2",
            "showStatus": True,
            "inputs": [
                _app_input("datasets", "selectDataset", selected_dataset, "selectDataset"),
                _app_input("similarity", "number", 0),
                _app_input("limit", "number", native_search_limit()),
                _app_input("searchMode", "string", "embedding"),
                _app_input("embeddingWeight", "number", 0.5),
                _app_input("usingReRank", "boolean", False),
                _app_input("rerankModel", "string", ""),
                _app_input("rerankWeight", "number", 0.5),
                _app_input("datasetSearchUsingExtensionQuery", "boolean", False),
                _app_input("datasetSearchExtensionModel", "string", ""),
                _app_input("datasetSearchExtensionBg", "string", ""),
                _app_input("authTmbId", "boolean", False),
                _app_input("datasetSearchInput", "arrayString", [[start_id, "userChatInput"]], "reference"),
            ],
            "outputs": [
                {"id": "quoteQA", "key": "quoteQA", "type": "static", "valueType": "datasetQuote"}
            ],
        },
        {
            "nodeId": chat_node_id,
            "name": "AI chat",
            "flowNodeType": "chatNode",
            "version": "4.9.7",
            "showStatus": True,
            "inputs": [
                _app_input("model", "string", llm_model, "settingLLMModel"),
                _app_input("isResponseAnswerText", "boolean", True),
                _app_input("aiChatQuoteRole", "string", "system"),
                _app_input("quoteTemplate", "string", ""),
                _app_input("quotePrompt", "string", ""),
                _app_input("systemPrompt", "string", "Answer using the supplied knowledge. If it is insufficient, say so.", "textarea"),
                _app_input("maxContext", "chatHistory", 6, "numberInput"),
                _app_input("userChatInput", "string", [start_id, "userChatInput"], "reference"),
                _app_input("quoteQA", "datasetQuote", [dataset_node_id, "quoteQA"], "settingDatasetQuotePrompt"),
            ],
            "outputs": [
                {"id": "history", "key": "history", "type": "static", "valueType": "chatHistory"},
                {"id": "answerText", "key": "answerText", "type": "static", "valueType": "string"},
            ],
        },
    ]
    return {
        "name": f"moi-fastgpt-{provider_name}-isolated-{time.strftime('%Y%m%d-%H%M%S')}",
        "intro": f"Isolated {provider_name} full-chain smoke app",
        "type": "simple",
        "modules": modules,
        "edges": [
            {"source": start_id, "target": dataset_node_id, "sourceHandle": f"{start_id}-source-right", "targetHandle": f"{dataset_node_id}-target-left"},
            {"source": dataset_node_id, "target": chat_node_id, "sourceHandle": f"{dataset_node_id}-source-right", "targetHandle": f"{chat_node_id}-target-left"},
        ],
        "chatConfig": {},
    }


def provider(args: argparse.Namespace) -> int:
    contract = provider_contract(args.provider)
    profile = PROVIDER_PROFILES[args.provider]
    if not args.execute:
        prefix = profile["env_prefix"]
        key_env = str(profile.get("api_key_env") or f"{prefix}_API_KEY")
        configured_key = os.getenv(key_env, "").strip()
        dry_key = configured_key if configured_key and not configured_key.startswith("<") else f"<{key_env}>"
        effective_channel = build_channel_payload(
            args.provider,
            dict(os.environ),
            api_key=dry_key,
        )
        print(json.dumps({
            "selected_provider": args.provider,
            "contract_scope": (
                "active_maas"
                if args.provider == "maas"
                else "official_deepseek"
                if args.provider == "deepseek-official"
                else "legacy_provider"
            ),
            "profile": profile,
            "effective_channel": redact(effective_channel),
            "provider_contract": contract,
        }, ensure_ascii=False, indent=2))
        print("Dry run only. Pass --execute after MaxKB is stopped and FastGPT is started.", file=sys.stderr)
        return 0

    running = run(["docker", "ps", "--format", "{{.Names}}"])
    if running.returncode:
        raise ContractError(f"cannot inspect Docker: {running.stderr.strip()}")
    names = running.stdout.splitlines()
    if any("maxkb" in name.lower() for name in names):
        raise ContractError("MaxKB is still running; refusing to consume the serial competitor window")
    for required in ("fastgpt-app", "fastgpt-aiproxy"):
        if required not in names:
            raise ContractError(f"required container is not running: {required}")

    provider_key = provider_api_key(args.provider, dict(os.environ))

    model_configuration = ensure_fastgpt_model_configuration(args.provider)

    inspect = run([
        "docker", "inspect", "fastgpt-aiproxy", "--format", "{{json .Config.Env}}"
    ])
    if inspect.returncode:
        raise ContractError(f"cannot inspect fastgpt-aiproxy: {inspect.stderr.strip()}")
    container_env = json.loads(inspect.stdout)
    admin_key = next((item.split("=", 1)[1] for item in container_env if item.startswith("ADMIN_KEY=")), "")
    if not admin_key:
        raise ContractError("fastgpt-aiproxy has no ADMIN_KEY")

    create = build_channel_payload(args.provider, dict(os.environ), api_key=provider_key)
    request_input = json.dumps({
        "adminKey": admin_key,
        "channel": create,
        # Provider-specific channels may be repaired
        # only after the Node verifier finds exactly one channel with the
        # requested name; duplicate or mismatched saved identities fail closed.
        "repairExisting": args.provider in {"qianfan", "maas", "deepseek-official"},
    })
    node_program = r"""
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const base = 'http://fastgpt-aiproxy:3000';
async function request(path, options = {}) {
  const response = await fetch(base + path, {
    ...options,
    headers: {
      Authorization: `Bearer ${input.adminKey}`,
      'Content-Type': 'application/json',
      ...(options.headers || {})
    }
  });
  const text = await response.text();
  let payload;
  try { payload = JSON.parse(text); } catch { payload = { message: text }; }
  if (!response.ok || payload.success === false) {
    throw new Error(`${options.method || 'GET'} ${path}: HTTP ${response.status} ${payload.message || text}`);
  }
  return payload.data;
}
(async () => {
  let channels = await request('/api/channels/all');
  let matches = channels.filter((item) => item.name === input.channel.name);
  if (matches.length > 1) throw new Error(`duplicate channels named ${input.channel.name}`);
  if (matches.length === 1 &&
      (String(matches[0].name) !== String(input.channel.name) || !matches[0].id)) {
    throw new Error('existing channel failed exact name/id identity check');
  }
  let created = false;
  if (matches.length === 0) {
    await request('/api/channel/', { method: 'POST', body: JSON.stringify(input.channel) });
    created = true;
    channels = await request('/api/channels/all');
    matches = channels.filter((item) => item.name === input.channel.name);
  } else if (input.repairExisting) {
    await request(`/api/channel/${matches[0].id}`, {
      method: 'PUT',
      body: JSON.stringify(input.channel)
    });
    channels = await request('/api/channels/all');
    matches = channels.filter((item) => item.name === input.channel.name);
  }
  if (matches.length !== 1) throw new Error('created channel was not returned by /api/channels/all');
  if (String(matches[0].name) !== String(input.channel.name) || !matches[0].id) {
    throw new Error('saved channel failed exact name/id identity check');
  }
  const channel = matches[0];
  const expectedModels = [...input.channel.models].sort().join(',');
  const actualModels = [...(channel.models || [])].sort().join(',');
  if (Number(channel.type) !== Number(input.channel.type) ||
      String(channel.base_url || '').replace(/\/$/, '') !== input.channel.base_url.replace(/\/$/, '') ||
      actualModels !== expectedModels) {
    throw new Error('existing channel differs from requested type/base_url/models; refusing to overwrite it');
  }
  const test = await request(`/api/channel/${channel.id}/test?return_success=true&success_body=false&stream=false`);
  process.stdout.write(JSON.stringify({
    created,
    updated: !created && input.repairExisting,
    channel: { id: channel.id, name: channel.name, type: channel.type, base_url: channel.base_url, models: channel.models },
    test
  }));
})().catch((error) => { process.stderr.write(error.message); process.exit(1); });
"""
    result = subprocess.run(
        ["docker", "exec", "-i", "fastgpt-app", "node", "-e", node_program],
        cwd=ROOT,
        text=True,
        input=request_input,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        error_message = result.stderr.strip()[:1500]
        if admin_key:
            error_message = error_message.replace(admin_key, "<redacted>")
        raise ContractError(
            "provider create/list/test failed: "
            f"{redact_error_message(error_message)}"
        )
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("provider verifier returned invalid JSON") from exc
    # AIProxy v0.6.5 returns an empty aggregate for the generic OpenAI/MaaS
    # channel even when the channel is saved and the model metadata is active.
    # The actual FastGPT dataset/native-QA smoke below is authoritative for
    # MaaS; keep explicit per-model failures fatal.
    # AIProxy v0.6.5 does not yet classify the official DeepSeek v4 model
    # family in its optional channel tester, so it returns an empty aggregate
    # even though the OpenAI-compatible channel is saved and the upstream
    # endpoint accepts the model.  The FastGPT native smoke is authoritative
    # for this route, just as it is for the MaaS channel.
    allow_empty_saved_test = args.provider in {"qianfan", "maas", "deepseek-official"}
    validate_provider_test_results(output, allow_empty=allow_empty_saved_test)
    if allow_empty_saved_test and not output.get("test"):
        output["saved_test_status"] = (
            "empty_non_authoritative_use_fastgpt_native_smoke"
            if args.provider in {"maas", "deepseek-official"}
            else "empty_non_authoritative_use_fastgpt_type_aware_tests"
        )
    else:
        output["saved_test_status"] = "passed"
    output["model_configuration"] = model_configuration
    print(json.dumps(redact(output), ensure_ascii=False, indent=2))
    return 0


def smoke(args: argparse.Namespace) -> int:
    if not args.execute:
        provider_name = os.getenv("FASTGPT_MODEL_PROVIDER", DEFAULT_PROVIDER).strip().lower()
        if provider_name not in PROVIDER_PROFILES:
            raise ContractError(f"FASTGPT_MODEL_PROVIDER must be one of {sorted(PROVIDER_PROFILES)}")
        print(json.dumps(provider_contract(provider_name), ensure_ascii=False, indent=2))
        print("Dry run only. Pass --execute after FastGPT is started and local credentials are loaded.", file=sys.stderr)
        return 0

    base_url = validate_local_url(os.getenv("FASTGPT_BASE_URL", "http://127.0.0.1:3000"))
    provider_name = os.getenv("FASTGPT_MODEL_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if provider_name not in PROVIDER_PROFILES:
        raise ContractError(f"FASTGPT_MODEL_PROVIDER must be one of {sorted(PROVIDER_PROFILES)}")
    embedding = embedding_spec(provider_name, dict(os.environ))
    api_key = os.getenv("FASTGPT_API_KEY", "").strip()
    app_id = os.getenv("FASTGPT_APP_ID", "").strip()
    if not api_key:
        raise ContractError("FASTGPT_API_KEY is required for --execute")
    if provider_name != "qianfan" and not app_id:
        raise ContractError("FASTGPT_APP_ID is required for non-Qianfan smoke execution")
    source_dir = (ROOT / os.getenv("FASTGPT_SMOKE_SOURCE_DIR", "local-rag-platforms/fixtures/smoke")).resolve()
    if not source_dir.is_dir():
        raise ContractError(f"smoke source directory does not exist: {source_dir}")

    output_dir = RUNTIME / "logs" / f"smoke-contract-{time.strftime('%Y%m%d-%H%M%S')}"
    result: dict[str, Any] = {
        "system_id": "fastgpt_local",
        "deployment_mode": "self_hosted",
        "platform": "linux/arm64",
        "version": "v4.15.6",
        "image_digest": None,
        "model_egress": "external",
        "service_status": "error",
        "ingest_status": "error",
        "native_status": "error",
        "retrieval_status": "error",
        "blocked_reason": None,
        "artifacts": [str((output_dir / "smoke-result.json").relative_to(ROOT))],
        "details": {
            "runtime_image": "ghcr.io/labring/fastgpt:v4.15.4",
            "embedding": embedding,
        },
    }
    stage = "service"
    dataset_id: str | None = None
    try:
        dataset_name = f"moi-fastgpt-{provider_name}-smoke-{time.strftime('%Y%m%d-%H%M%S')}"
        dataset = api_request(
            base_url,
            "/api/core/dataset/create",
            api_key,
            body=build_dataset_payload(
                provider_name=provider_name,
                dataset_name=dataset_name,
                env=dict(os.environ),
            ),
        )
        dataset_id = dataset if isinstance(dataset, str) else dataset.get("datasetId") or dataset.get("id")
        if not dataset_id:
            raise ContractError("create dataset response did not contain a dataset id")
        result["service_status"] = "ready"
        result["details"]["dataset_id"] = dataset_id

        if provider_name == "qianfan":
            dataset_detail = api_request(
                base_url,
                f"/api/core/dataset/detail?id={dataset_id}",
                api_key,
                method="GET",
            )
            actual_vector_model = dataset_detail.get("vectorModel", {})
            actual_vector_model = (
                actual_vector_model.get("model")
                if isinstance(actual_vector_model, dict)
                else actual_vector_model
            )
            expected_vector_model = embedding["model"]
            if actual_vector_model != expected_vector_model:
                raise ContractError(
                    "new Qianfan dataset vector model mismatch before upload: "
                    f"expected {expected_vector_model}, got {actual_vector_model}"
                )
            result["details"]["dataset_vector_model"] = actual_vector_model
            created_app = api_request(
                base_url,
                "/api/core/app/create",
                api_key,
                body=build_isolated_app_payload(
                    provider_name=provider_name,
                    dataset_id=dataset_id,
                    dataset_name=dataset_name,
                ),
            )
            app_id = created_app if isinstance(created_app, str) else created_app.get("appId") or created_app.get("id")
            if not app_id:
                raise ContractError("create isolated Qianfan app response did not contain an app id")
        result["details"]["app_id"] = app_id

        stage = "ingest"
        uploads = []
        for path in sorted(source_dir.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.suffix.lower() in {".json", ".jsonl"}:
                continue
            response = api_request(base_url, "/api/core/dataset/collection/create/localFile", api_key, multipart=(path, {
                "datasetId": dataset_id,
                "parentId": None,
                "trainingType": "chunk",
                "chunkSize": 512,
                "chunkSplitter": "",
                "qaPrompt": "",
                "metadata": {"benchmark": "moi-local-rag"},
            }))
            collection_id = response.get("collectionId") if isinstance(response, dict) else None
            if not collection_id:
                raise ContractError(f"upload response for {path.name} did not contain a collection id")
            uploads.append({"file": path.name, "collectionId": str(collection_id)})
        if not uploads:
            raise ContractError("no supported smoke documents were found")

        expected_collection_ids = {item["collectionId"] for item in uploads}
        wait_seconds = int(os.getenv("FASTGPT_SMOKE_WAIT_SECONDS", "300"))
        deadline = time.monotonic() + wait_seconds
        collections: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            listing = api_request(base_url, "/api/core/dataset/collection/listV2", api_key, body={
                "offset": 0, "pageSize": 30, "datasetId": dataset_id, "parentId": None, "searchText": ""
            })
            collections = listing.get("list", []) if isinstance(listing, dict) else []
            collections_by_id = {
                str(collection_id): item
                for item in collections
                if (collection_id := item.get("_id") or item.get("id") or item.get("collectionId"))
            }
            uploaded_collections = [
                collections_by_id[collection_id]
                for collection_id in expected_collection_ids
                if collection_id in collections_by_id
            ]
            if any(
                item.get("hasError", False) or item.get("finalErrorAmount", 0)
                for item in uploaded_collections
            ):
                raise ContractError("one or more uploaded FastGPT collections failed indexing")
            if expected_collection_ids.issubset(collections_by_id) and all(
                item.get("trainingAmount", 0) == 0
                and item.get("activeTrainingAmount", 0) == 0
                and item.get("finalErrorAmount", 0) == 0
                and not item.get("hasError", False)
                for item in uploaded_collections
            ):
                collections = uploaded_collections
                break
            time.sleep(2)
        else:
            raise ContractError(f"collection indexing did not finish within {wait_seconds}s")
        result["ingest_status"] = "ready"
        result["details"].update({"uploads": uploads, "collection_count": len(collections)})

        stage = "retrieval"
        query = os.getenv("FASTGPT_SMOKE_RETRIEVAL_QUERY", "local RAG smoke")
        retrieval = api_request(base_url, "/api/core/dataset/searchTest", api_key, body={
            "datasetId": dataset_id,
            "text": query,
            "limit": 5000,
            "similarity": 0,
            "searchMode": "embedding",
            "usingReRank": False,
            "datasetSearchUsingExtensionQuery": False,
        })
        hits = retrieval.get("list", []) if isinstance(retrieval, dict) else []
        if not hits:
            raise ContractError("direct retrieval returned no hits")
        sentinel = os.getenv("FASTGPT_SMOKE_SENTINEL", "").strip()
        if sentinel and sentinel not in json.dumps(hits, ensure_ascii=False):
            raise ContractError("direct retrieval did not contain the required sentinel")
        result["retrieval_status"] = "success"
        result["details"].update({"retrieval_hit_count": len(hits), "retrieval": retrieval})

        stage = "native"
        question = os.getenv("FASTGPT_SMOKE_QUESTION", "What facts are stated in the local smoke documents?")
        native_request = build_native_chat_payload(
            provider_name=provider_name,
            app_id=app_id,
            question=question,
            env=dict(os.environ),
        )
        native = api_request(
            base_url,
            "/api/v1/chat/completions",
            api_key,
            body=native_request,
            timeout=float(os.getenv("FASTGPT_NATIVE_TIMEOUT_SECONDS", "60")),
        )
        choices = native.get("choices", []) if isinstance(native, dict) else []
        answer = choices[0].get("message", {}).get("content", "") if choices else ""
        if not answer.strip():
            raise ContractError("native QA returned no answer")
        if sentinel and sentinel not in answer:
            raise ContractError("native QA answer did not contain the required sentinel")
        if sentinel:
            response_steps = native.get("responseData", []) if isinstance(native, dict) else []
            quote_context = [
                quote
                for step in response_steps
                for quote in step.get("quoteList", [])
                if isinstance(step, dict)
            ]
            if sentinel not in json.dumps(quote_context, ensure_ascii=False):
                raise ContractError("native QA responseData did not quote the sentinel knowledge")
        result["native_status"] = "success"
        result["details"].update({"native_request": native_request, "native_response": native})
    except ContractError as exc:
        failure_kind = "timeout" if "timed out" in str(exc).lower() else "request_error"
        result["blocked_reason"] = f"{stage.upper()}_SMOKE_{failure_kind.upper()}"
        result["details"]["failed_stage"] = stage
        result["details"]["failure_kind"] = failure_kind
        result["details"]["failure_message"] = redact_error_message(str(exc))

    output = write_smoke_result(output_dir, result)
    success = all(result[key] in {"ready", "success"} for key in (
        "service_status", "ingest_status", "retrieval_status", "native_status"
    ))
    print(json.dumps({
        "status": "success" if success else "error",
        "artifact": str(output.relative_to(ROOT)),
        "dataset_id": dataset_id,
        "blocked_reason": result["blocked_reason"],
    }, indent=2))
    return 0 if success else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="read-only source/compose/ARM64/runtime check")
    smoke_parser = subparsers.add_parser("smoke", help="print the contract, or execute it explicitly")
    smoke_parser.add_argument("--execute", action="store_true", help="create a local dataset and run smoke APIs")
    provider_parser = subparsers.add_parser("provider", help="print or execute AIProxy channel create/list/test")
    provider_parser.add_argument("--provider", choices=sorted(PROVIDER_PROFILES), default=DEFAULT_PROVIDER)
    provider_parser.add_argument("--execute", action="store_true", help="create or validate the selected provider channel")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "preflight":
            return preflight()
        if args.command == "provider":
            return provider(args)
        return smoke(args)
    except (ContractError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    inject_central_env()
    raise SystemExit(main())
