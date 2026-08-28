from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread


ROOT = Path(__file__).resolve().parents[3]
DEPLOYMENT_DIR = Path(__file__).resolve().parent
if str(DEPLOYMENT_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOYMENT_DIR))

import prepare_local_services as deployment  # noqa: E402


def _manifest() -> dict:
    return json.loads((ROOT / "local-rag-platforms" / "versions.json").read_text(encoding="utf-8"))


def test_versions_freeze_matrixone_and_competitor_deployment_images() -> None:
    versions = _manifest()

    matrixone = versions["services"]["moi_matrixone"]
    assert matrixone["version"] == "4.1.4"
    assert matrixone["image"].endswith(":4.1.4")
    assert matrixone["health"]["type"] == "tcp"

    dify = versions["services"]["dify_local"]
    assert dify["compose"]["path"] == "docker/docker-compose.yaml"
    assert dify["compose"]["images"] == {
        "api": "langgenius/dify-api:1.16.1",
        "web": "langgenius/dify-web:1.16.1",
    }

    fastgpt = versions["services"]["fastgpt_local"]
    assert fastgpt["compose"]["path"].endswith("docker-compose.pg.yml")
    assert fastgpt["compose"]["images"]["app"] == "ghcr.io/labring/fastgpt:v4.15.4"

    maxkb = versions["services"]["maxkb_local"]
    assert maxkb["image"] == "1panel/maxkb:v2.10.4-lts"
    assert maxkb["image_digest"].startswith("sha256:")
    assert maxkb["compose"]["type"] == "docker-run"
    for service in (dify, fastgpt, maxkb, matrixone):
        active = service["active_benchmark"]
        assert active["provider_policy"] == "deepseek_text_maas_embedding"
        assert active["text_llm_provider"] == "deepseek-official"
        assert active["embedding_provider"] == "maas"
        assert active["embedding_model"] == "bge-m3"
        assert active["embedding_dimension"] == 1024
        assert active["text_llm"] == "deepseek-v4-flash"
        assert active["thinking"] == {"type": "disabled"}
        assert active["mllm"] == "NOT_APPLICABLE"


def test_active_text_only_templates_use_split_provider_contract() -> None:
    dify = (ROOT / "local-rag-platforms/dify_local/runtime.env.example").read_text(encoding="utf-8")
    fastgpt = (ROOT / "local-rag-platforms/fastgpt_local/.env.example").read_text(encoding="utf-8")
    maxkb = (ROOT / "local-rag-platforms/maxkb_local/runtime.env.example").read_text(encoding="utf-8")
    provider = (ROOT / "local-rag-platforms/providers/huawei-maas.env.example").read_text(encoding="utf-8")

    for text in (dify, fastgpt, maxkb, provider):
        assert "qwen" not in text.casefold()
        assert "=taas" not in text.casefold()
        assert "=qianfan" not in text.casefold()
    assert "DEEPSEEK_LLM_MODEL=deepseek-v4-flash" in dify
    assert "DIFY_DEEPSEEK_LLM_PROVIDER=<active-deepseek-provider-id>" in dify
    assert "FASTGPT_LLM_PROVIDER=deepseek-official" in fastgpt
    assert "MAXKB_EMBEDDING_PROVIDER=maas" in maxkb
    assert "MAXKB_LLM_MODEL_NAME=deepseek-v4-flash" in maxkb
    assert "MAAS_VL_MODEL=NOT_APPLICABLE" in provider


def test_matrixone_compose_exposes_the_frozen_image_and_healthcheck() -> None:
    compose = (ROOT / "moi-prototypes" / "local-matrixflow-rag" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "image: matrixorigin/matrixone:4.1.4" in compose
    assert "healthcheck:" in compose
    assert "curl -fsS http://127.0.0.1:9876/debug/pprof/" in compose


class _ReadyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *_args: object) -> None:
        return


def test_readiness_cli_writes_machine_readable_ready_artifact(tmp_path: Path, capsys) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReadyHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        exit_code = deployment.main(
            [
                "readiness",
                "fastgpt_local",
                "--url",
                f"http://127.0.0.1:{server.server_port}/healthz",
                "--runtime-root",
                str(tmp_path),
            ]
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == "moi-deployment-readiness-v1"
    assert output["system_id"] == "fastgpt_local"
    assert output["ready"] is True
    assert output["status"] == "READY"
    assert output["checks"][0]["status"] == "READY"
    artifact = tmp_path / "fastgpt_local" / "logs" / "readiness.json"
    assert json.loads(artifact.read_text(encoding="utf-8")) == output


def test_readiness_cli_reports_unreachable_without_echoing_credentials(tmp_path: Path, capsys) -> None:
    exit_code = deployment.main(
        [
            "readiness",
            "maxkb_local",
            "--url",
            "http://127.0.0.1:1/admin/",
            "--runtime-root",
            str(tmp_path),
        ]
    )

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == "moi-deployment-readiness-v1"
    assert output["ready"] is False
    assert output["status"] == "BLOCKED"
    assert output["checks"][0]["status"] == "BLOCKED"
    assert "password" not in json.dumps(output).lower()
    assert "secret" not in json.dumps(output).lower()
