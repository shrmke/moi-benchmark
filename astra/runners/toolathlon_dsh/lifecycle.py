from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from astra.runners.toolathlon_verified.contract import (
    JsonlEventWriter,
    read_json_object,
    sha256_file,
    utc_now,
    write_json_atomic,
    write_sha256_manifest,
)
from astra.runners.toolathlon_verified.lifecycle import (
    QUALIFICATION_TASK,
    SOURCE_COMMIT,
    TASK_IMAGE,
    LifecycleError,
    SingleTaskLifecycle,
    _free_port,
    _run,
    _safe_id,
    is_runtime_mutable_credential_record,
    load_task_reset_contract,
)
from astra.runners.toolathlon_verified.resources import ResourceSampler

from .dsh_adapter import DSH_KEY_ENV, DSH_NODE_VERSION, DSH_VERSION, DshRuntime
from .orchestrator import run as run_dsh_slot


DSH_RUNTIME_GENERATED_STATE_PATHS = frozenset(
    {
        "deployment/canvas/configs/canvas_admin_tokens.txt",
        "deployment/canvas/configs/canvas_admin_users.json",
        "deployment/canvas/configs/canvas_tokens.txt",
        "deployment/canvas/configs/canvas_users.json",
        "deployment/k8s/configs/cluster-inst-alpha1-config.yaml",
        "deployment/poste/configs/created_accounts.json",
        "deployment/woocommerce/configs/multisite-api-keys.json",
    }
)

FILTER_LOW_SELLING_PRODUCTS_TASK = "filter-low-selling-products"
FILTER_LOW_SELLING_PRODUCTS_PREPROCESS = (
    "/workspace/tasks/finalpool/filter-low-selling-products/"
    "preprocess/setup_test_products.py"
)
FILTER_LOW_SELLING_PRODUCTS_REPLACEMENTS = (
    (
        "self.wc_client.batch_delete_products(all_products)",
        "self.wc_client.batch_delete_products(all_products, batch_size=10)",
    ),
    (
        "self.wc_client.batch_create_products(test_products)",
        "self.wc_client.batch_create_products(test_products, batch_size=10)",
    ),
)


def _verify_frozen_credential(path: Path, item: dict[str, Any]) -> None:
    """Verify a frozen credential, including unreadable empty lock files."""
    expected = item.get("sha256")
    if (
        item.get("size_bytes") == 0
        and expected == hashlib.sha256(b"").hexdigest()
        and path.stat().st_size == 0
    ):
        return
    if sha256_file(path) != expected:
        raise LifecycleError(f"application credential fingerprint drift: {path}")


class DshSingleTaskLifecycle(SingleTaskLifecycle):
    """Reuse the verified task environment with an exploratory DSH slot."""

    def preflight(self) -> None:
        if not self.task_source.is_dir():
            raise LifecycleError(f"task source is unavailable: {self.task_source}")
        self.task_reset_contract = load_task_reset_contract(
            task_id=self.args.task_id,
            task_source=self.task_source,
            requirements_path=self.freeze / "task-requirements.json",
        )
        if not self.host_python.is_file() or not self.runtime_overlay.is_file():
            raise LifecycleError("frozen Toolathlon runtime is unavailable")
        source_commit = _run(
            ["git", "-C", str(self.source), "rev-parse", "HEAD^{commit}"],
            timeout=30,
        ).stdout.decode().strip()
        if source_commit != SOURCE_COMMIT:
            raise LifecycleError("Toolathlon source commit does not match the freeze")
        image = self._docker("image", "inspect", TASK_IMAGE, timeout=60)
        if not image.stdout:
            raise LifecycleError("frozen task image is not available")
        if not os.environ.get(DSH_KEY_ENV):
            raise LifecycleError(f"required runtime credential is absent: {DSH_KEY_ENV}")
        self.dsh_runtime = DshRuntime.load_from_environment()

        credential_manifest = os.environ.get("TOOLATHLON_DSH_CREDENTIAL_MANIFEST")
        self.credential_manifest_path = (
            Path(credential_manifest).resolve()
            if credential_manifest
            else self.freeze / "credential-manifest.json"
        )
        if (
            not self.credential_manifest_path.is_file()
            or self.credential_manifest_path.is_symlink()
        ):
            raise LifecycleError(
                f"DSH credential manifest is unavailable: {self.credential_manifest_path}"
            )
        manifest = read_json_object(self.credential_manifest_path)
        if (
            manifest.get("source_commit") != SOURCE_COMMIT
            or manifest.get("secret_values_recorded") is not False
            or manifest.get("toolathlon_application_credentials", {}).get("state")
            != "GO"
        ):
            raise LifecycleError("DSH credential manifest is not a qualified fingerprint set")
        records = manifest.get("toolathlon_application_credentials", {}).get("files")
        if not isinstance(records, list) or not records:
            raise LifecycleError("frozen application credential manifest is empty")
        mutable: list[str] = []
        runtime_generated: list[str] = []
        for item in records:
            if not isinstance(item, dict):
                raise LifecycleError("invalid application credential record")
            relative = Path(str(item.get("path", "")))
            if relative.is_absolute() or ".." in relative.parts:
                raise LifecycleError("unsafe application credential path")
            path = self.source / relative
            if not path.is_file() or path.is_symlink():
                raise LifecycleError(f"application credential is unavailable: {path}")
            relative_key = relative.as_posix()
            if relative_key in DSH_RUNTIME_GENERATED_STATE_PATHS:
                runtime_generated.append(relative_key)
            elif is_runtime_mutable_credential_record(item):
                mutable.append(relative.as_posix())
            elif item.get("runtime_mutable") is True:
                raise LifecycleError(f"invalid mutable credential policy: {relative}")
            else:
                _verify_frozen_credential(path, item)
        self.mutable_credential_paths = sorted(mutable)
        self.dsh_runtime_generated_paths = sorted(runtime_generated)

    def _copy_tree(self) -> None:
        super()._copy_tree()
        if self.args.task_id != FILTER_LOW_SELLING_PRODUCTS_TASK:
            return
        assert self.lifecycle is not None
        script = (
            "import json,pathlib,sys; "
            "p=pathlib.Path(sys.argv[1]); s=p.read_text(encoding='utf-8'); "
            "changes=json.loads(sys.argv[2]); "
            "assert all(s.count(old)==1 for old,new in changes); "
            "exec(\"for old,new in changes:\\n s=s.replace(old,new)\"); "
            "p.write_text(s,encoding='utf-8')"
        )
        self._docker(
            "exec",
            self.container_name,
            "python3",
            "-c",
            script,
            FILTER_LOW_SELLING_PRODUCTS_PREPROCESS,
            json.dumps(
                FILTER_LOW_SELLING_PRODUCTS_REPLACEMENTS,
                separators=(",", ":"),
            ),
            timeout=60,
        )
        self.lifecycle.append(
            "preprocess.overlay_applied",
            policy="toolathlon.dsh-filter-low-selling-products-batch-size.v1",
            scope="task_container_copy_only",
            batch_size=10,
            frozen_toolathlon_source_modified=False,
        )

    def _write_slot_config(self, gateway_port: int) -> Path:
        path = super()._write_slot_config(gateway_port)
        config = read_json_object(path)
        config["benchmark_status"] = "exploratory_dsh_only"
        config["run"]["system_id"] = "dsh"
        config["dsh_runtime"] = {
            "version": DSH_VERSION,
            "node_version": DSH_NODE_VERSION,
            "node_env": "TOOLATHLON_DSH_NODE",
            "root_env": "TOOLATHLON_DSH_ROOT",
            "bridge": "astra/runners/toolathlon_dsh/classic_sse_stdio_bridge.mjs",
            "runtime_generated_state_paths": getattr(
                self, "dsh_runtime_generated_paths", []
            ),
        }
        permission_path = Path(str(config["run"]["permission_policy"]))
        permission = read_json_object(permission_path)
        permission.setdefault("products", {})["dsh"] = {
            "mode": "external_lifecycle_boundary",
            "unresolved_action": "deny",
        }
        write_json_atomic(permission_path, permission, mode=0o600)
        config["evaluator"]["command"].append(
            "--evaluate_regardless_of_agent_status"
        )
        config["credential_manifest"] = str(self.credential_manifest_path)
        write_json_atomic(path, config, mode=0o600)
        return path

    def _finalize_dsh(self) -> dict[str, Any]:
        assert self.lifecycle is not None
        required = (
            "resolved-config.json",
            "tool-schema-observed.json",
            "lifecycle-events.jsonl",
            "adapter-events.jsonl",
            "trajectory.jsonl",
            "tool-calls.jsonl",
            "model-usage.jsonl",
            "resource-usage.jsonl",
            "evaluator/eval_res.json",
            "evaluator/eval.log",
            "failure-evidence.json",
            "run.json",
        )
        missing = [name for name in required if not (self.output / name).is_file()]
        if missing:
            raise LifecycleError(f"DSH run artifacts are missing: {missing}")
        run = read_json_object(self.output / "run.json")
        resolved = read_json_object(self.output / "resolved-config.json")
        if (
            run.get("system_id") != "dsh"
            or resolved.get("system_id") != "dsh"
            or run.get("benchmark_status") != "exploratory_dsh_only"
        ):
            raise LifecycleError("DSH artifact identity mismatch")
        if run.get("run_id") != self.args.run_id or run.get("task_id") != self.args.task_id:
            raise LifecycleError("DSH artifact run identity mismatch")
        if run.get("terminal_status") == "completed" and not (
            self.output / "dsh-session.jsonl"
        ).is_file():
            raise LifecycleError("completed DSH run has no raw session artifact")
        for relative in (
            "lifecycle-events.jsonl",
            "adapter-events.jsonl",
            "trajectory.jsonl",
            "tool-calls.jsonl",
            "model-usage.jsonl",
            "resource-usage.jsonl",
        ):
            for number, line in enumerate(
                (self.output / relative).read_text(encoding="utf-8").splitlines(), 1
            ):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LifecycleError(f"invalid JSONL {relative}:{number}") from exc
                if not isinstance(value, dict):
                    raise LifecycleError(f"non-object JSONL {relative}:{number}")
        if (self.output / "dsh-session.jsonl").is_file():
            for number, line in enumerate(
                (self.output / "dsh-session.jsonl")
                .read_text(encoding="utf-8")
                .splitlines(),
                1,
            ):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LifecycleError(f"invalid JSONL dsh-session.jsonl:{number}") from exc
                if not isinstance(value, dict):
                    raise LifecycleError(f"non-object JSONL dsh-session.jsonl:{number}")
        self.lifecycle.append("artifact_validation.start")
        run["artifact_gate"] = {
            "status": "passed",
            "validator": "validate_dsh_exploratory_artifacts",
            "validated_at": utc_now(),
        }
        write_json_atomic(self.output / "run.json", run, mode=0o644)
        self.lifecycle.append("artifact_validation.end", status="passed")
        manifest = self.output / "artifacts.sha256"
        candidates = [
            item
            for item in self.output.rglob("*")
            if item.is_file() and not item.is_symlink() and item != manifest
        ]
        write_sha256_manifest(manifest, candidates, root=self.output)
        return {
            "status": "passed",
            "benchmark_status": "exploratory_dsh_only",
            "run_id": self.args.run_id,
            "task_id": self.args.task_id,
            "system_id": "dsh",
            "artifact_count": len(candidates) + 1,
            "verify_status": run.get("verify_status"),
            "run_validity": run.get("run_validity"),
        }

    def execute(self) -> int:
        if self.output.exists() and any(self.output.iterdir()):
            raise LifecycleError("output directory must be absent or empty")
        self.output.mkdir(parents=True, exist_ok=True)
        self.lifecycle = JsonlEventWriter(
            self.output / "lifecycle-events.jsonl",
            run_id=self.args.run_id,
            system_id="dsh",
        )
        self.private_dir = Path(tempfile.mkdtemp(prefix=f"toolathlon-{self.args.run_id}-"))
        os.chmod(self.private_dir, 0o700)
        sampler = ResourceSampler(
            self.output / "resource-usage.jsonl",
            run_id=self.args.run_id,
            system_id="dsh",
            product_pid=lambda: self.product_pid[0],
            container_id=lambda: self.container_id[0],
            container_pid=lambda: self.container_pid[0],
        )
        sampler.start()
        slot_code = 2
        try:
            self.preflight()
            self._reset()
            self._start_container()
            self._copy_tree()
            self._preprocess()
            self._stash_private_artifacts()
            gateway_port = _free_port()
            self._start_gateway(gateway_port)
            config_path = self._write_slot_config(gateway_port)
            slot_code = run_dsh_slot(
                config_path,
                before_evaluator=self._restore_for_evaluator,
                lifecycle_writer=self.lifecycle,
                resource_sampler=sampler,
                on_product_pid=lambda pid: self.product_pid.__setitem__(0, pid),
            )
        finally:
            try:
                self._cleanup()
            finally:
                sampler.close()
                if self.private_dir is not None:
                    shutil.rmtree(self.private_dir, ignore_errors=True)
                    self.private_dir = None
        validation = self._finalize_dsh()
        print(json.dumps(validation, ensure_ascii=False, sort_keys=True))
        return slot_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Toolathlon task with DSH headless")
    parser.add_argument("--task-id", default=QUALIFICATION_TASK)
    parser.add_argument("--experiment-id", default="toolathlon-dsh-0.1.0-rc.7-v1")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--replacement-for-run-id")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--toolathlon-source",
        type=Path,
        default=Path("/home/vagrant/dataset/Toolathlon"),
    )
    parser.add_argument("--docker-via-sudo", action="store_true")
    args = parser.parse_args(argv)
    args.system = "dsh"
    args.run_id = _safe_id(args.run_id, "run_id")
    args.experiment_id = _safe_id(args.experiment_id, "experiment_id")
    args.task_id = _safe_id(args.task_id, "task_id")
    if args.replacement_for_run_id is not None:
        args.replacement_for_run_id = _safe_id(
            args.replacement_for_run_id, "replacement_for_run_id"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    return DshSingleTaskLifecycle(parse_args(argv)).execute()


if __name__ == "__main__":
    raise SystemExit(main())
