from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from .proxy_relay import start_proxy_relay
from .products import PRODUCTS, Product
from .queue import Task, build_queue, load_queue, write_queue
from .results import completed_tasks, summarize
from .scheduler import run_tasks


EXPECTED_DATASET_COMMIT = "5c8eadf1f393183288fa08b8f73ca9a469cc5e00"
ASTRA_BRANCH = "optimize_0731_05"
LOOPBACK_PROXY_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        check=False,
        capture_output=capture,
    )


def _output(command: list[str], *, cwd: Path) -> str:
    process = _run(command, cwd=cwd, capture=True)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "command failed: " + " ".join(command))
    return process.stdout.strip()


def _harbor_binary() -> str:
    configured = os.environ.get("HARBOR_BIN")
    if configured:
        return configured
    resolved = shutil.which("harbor")
    if resolved:
        return resolved
    candidate = Path.home() / ".local/share/uv/tools/harbor/bin/harbor"
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError("Harbor was not found; set HARBOR_BIN to Harbor 0.20.0")


def _python_binary(harbor_bin: str) -> str:
    configured = os.environ.get("PYTHON_BIN")
    if configured:
        return configured
    adjacent = Path(harbor_bin).resolve().parent / "python"
    if adjacent.is_file():
        return str(adjacent)
    return sys.executable


def _container_proxy_url(proxy_url: str | None) -> str:
    if not proxy_url:
        return ""
    parsed = urlparse(proxy_url)
    if parsed.hostname not in LOOPBACK_PROXY_HOSTS:
        return proxy_url
    userinfo = (
        parsed.netloc.rsplit("@", 1)[0] + "@"
        if "@" in parsed.netloc
        else ""
    )
    port = f":{parsed.port}" if parsed.port is not None else ""
    return parsed._replace(
        netloc=f"{userinfo}host.docker.internal{port}"
    ).geturl()


def _validate_linux_host(root: Path) -> None:
    if platform.system() != "Linux" or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError("the 89-task cohort requires a native Linux x86_64 host")
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker is required")
    daemon = _output(
        [docker, "info", "--format", "{{.OSType}}/{{.Architecture}}"], cwd=root
    )
    if daemon not in {"linux/amd64", "linux/x86_64"}:
        raise RuntimeError(
            "tune-mjcf requires a native Linux amd64 Docker daemon; "
            f"found {daemon}"
        )


def _docker_bridge_gateway(root: Path) -> str:
    gateway = _output(
        [
            "docker",
            "network",
            "inspect",
            "bridge",
            "--format",
            "{{(index .IPAM.Config 0).Gateway}}",
        ],
        cwd=root,
    )
    if not gateway:
        raise RuntimeError("could not determine the Docker bridge gateway")
    return gateway


def _validate_dataset(dataset_root: Path, tasks_root: Path) -> str:
    if not tasks_root.is_dir():
        raise RuntimeError(f"missing Terminal-Bench tasks: {tasks_root}")
    commit = _output(["git", "-C", str(dataset_root), "rev-parse", "HEAD"], cwd=dataset_root)
    if commit != EXPECTED_DATASET_COMMIT:
        raise RuntimeError(f"unexpected Terminal-Bench commit: {commit}")
    changes = _output(
        [
            "git",
            "-C",
            str(dataset_root),
            "status",
            "--short",
            "--untracked-files=all",
            "--",
            "tasks",
        ],
        cwd=dataset_root,
    )
    if changes:
        raise RuntimeError("Terminal-Bench tasks have local changes:\n" + changes)
    return commit


def _validate_astra(root: Path, env: dict[str, str]) -> str:
    source = Path(
        env.get("ASTRA_SOURCE_ROOT", str(root / "external/astra-optimize_0731_05"))
    ).resolve()
    if not (source / "Cargo.toml").is_file():
        raise RuntimeError(f"missing Astra {ASTRA_BRANCH} checkout: {source}")
    branch = _output(["git", "-C", str(source), "branch", "--show-current"], cwd=root)
    if branch != ASTRA_BRANCH:
        raise RuntimeError(
            f"Astra checkout must be on {ASTRA_BRANCH}; "
            f"found {branch or 'detached HEAD'}"
        )
    remote = _output(["git", "-C", str(source), "remote", "get-url", "origin"], cwd=root)
    if not remote.removesuffix(".git").endswith("matrixorigin/astra"):
        raise RuntimeError(f"Astra origin must be matrixorigin/astra; found {remote}")
    changes = _output(
        [
            "git",
            "-C",
            str(source),
            "status",
            "--short",
            "--untracked-files=no",
        ],
        cwd=root,
    )
    if changes:
        raise RuntimeError("Astra tracked source has local changes:\n" + changes)
    commit = _output(["git", "-C", str(source), "rev-parse", "HEAD"], cwd=root)
    binary = Path(env["ASTRA_TBENCH_LINUX_BINARY"])
    if not binary.is_file():
        raise RuntimeError(f"missing Linux Astra binary: {binary}")
    description = _output(["file", str(binary)], cwd=root)
    if "ELF 64-bit" not in description or "x86-64" not in description:
        raise RuntimeError(f"Astra binary must be Linux x86-64 ELF: {description}")
    parsed = urlparse(env["ASTRA_API_URL"])
    if parsed.hostname != "host.docker.internal":
        raise RuntimeError("ASTRA_API_URL must use host.docker.internal")
    default_port = 443 if parsed.scheme == "https" else 80
    host_url = parsed._replace(
        netloc=f"127.0.0.1:{parsed.port or default_port}", path="/health"
    ).geturl()
    try:
        with urlopen(host_url, timeout=5) as response:
            if response.status >= 400:
                raise RuntimeError(f"Astra API health returned {response.status}")
    except OSError as exc:
        raise RuntimeError(f"Astra API health check failed: {host_url}") from exc
    return commit


def _base_environment(
    root: Path,
    data_root: Path,
    harbor_bin: str,
    python_bin: str,
) -> dict[str, str]:
    env = dict(os.environ)
    env["MOI_BENCH_ROOT"] = str(root)
    env["MOI_BENCH_DATA_ROOT"] = str(data_root)
    env["HARBOR_BIN"] = harbor_bin
    env["PYTHON_BIN"] = python_bin
    env["PYTHONPATH"] = str(root) + (f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")
    env["PI_TBENCH_VERIFIER_CACHE"] = str(
        data_root / "work/linux-terminal-bench/verifier-cache"
    )
    host_proxy = (
        env.get("TBENCH_VERIFIER_PROXY")
        or env.get("HTTPS_PROXY")
        or env.get("https_proxy")
        or env.get("HTTP_PROXY")
        or env.get("http_proxy")
    )
    env["TBENCH_VERIFIER_PROXY"] = _container_proxy_url(host_proxy)
    if host_proxy:
        env.setdefault("PI_TBENCH_CACHE_PROXY_URL", host_proxy)
        env.setdefault("HERMES_TBENCH_BUILD_PROXY", host_proxy)
    env.setdefault("ASTRA_SOURCE_ROOT", str(root / "external/astra-optimize_0731_05"))
    env.setdefault(
        "ASTRA_TBENCH_LINUX_BINARY",
        str(root / "work/astra-optimize-0731-05-linux-amd64/target/release/astra"),
    )
    env.setdefault("ASTRA_API_URL", "http://host.docker.internal:17101")
    env.setdefault("ASTRA_TBENCH_MODEL", "glm-5.2(thinking:high)")
    return env


def _validate_product(
    product: Product,
    root: Path,
    env: dict[str, str],
    *,
    require_key: bool,
) -> str | None:
    if not product.config_path.is_file():
        raise RuntimeError(f"missing product config: {product.config_path}")
    if require_key and product.credential_env and not env.get(product.credential_env):
        raise RuntimeError(f"{product.credential_env} is required for {product.id}")
    if product.id == "astra":
        return _validate_astra(root, env)
    return None


def _prepare_verifier_cache(root: Path, env: dict[str, str]) -> None:
    builder = root / "astra/runners/pi_terminal_bench/prebuilt/prepare-verifier-cache.sh"
    process = _run(
        ["/bin/bash", str(builder), "--cache-root", env["PI_TBENCH_VERIFIER_CACHE"]],
        cwd=root,
        env=env,
    )
    if process.returncode != 0:
        raise RuntimeError("failed to prepare the shared verifier cache")


def _prepare_dsh_runtime(product_root: Path, env: dict[str, str]) -> None:
    from astra.runners.dsh_terminal_bench.install_runtime import (
        ensure_runtime_wheel,
        select_runtime,
    )

    artifact = select_runtime(platform.machine())
    wheel = product_root / "cache" / artifact.filename
    ensure_runtime_wheel(wheel)
    env["DSH_RUNTIME_WHEEL"] = str(wheel.resolve())
    print(f"DSH runtime cache: {wheel}")


def _prepare_images(
    product: Product,
    tasks: list[Task],
    *,
    root: Path,
    tasks_root: Path,
    generated_root: Path,
    jobs_dir: Path,
    env: dict[str, str],
) -> None:
    names = [task.name for task in tasks]
    if not names or product.prebuilt is None:
        return
    if product.prebuilt == "pi":
        builder = root / "astra/runners/pi_terminal_bench/prebuilt/build-images.sh"
        env.update(
            {
                "PI_TBENCH_TASKS_ROOT": str(tasks_root),
                "PI_TBENCH_GENERATED_TASKS_ROOT": str(generated_root),
                "PI_TBENCH_JOBS_DIR": str(jobs_dir),
                "PI_TBENCH_CONFIG": str(product.config_path),
            }
        )
    else:
        builder = root / "astra/runners/hermes_terminal_bench/prebuilt/build-images.sh"
        env.update(
            {
                "HERMES_TBENCH_TASKS_ROOT": str(tasks_root),
                "HERMES_TBENCH_GENERATED_TASKS_ROOT": str(generated_root),
                "HERMES_TBENCH_CONFIG": str(product.config_path),
                "HERMES_PREBUILT_LOCK_DIR": str(
                    generated_root.parent / ".image-build.lock"
                ),
            }
        )
    process = _run(
        ["/bin/bash", str(builder), "--build-only", "--keep-images", *names],
        cwd=root,
        env=env,
    )
    if process.returncode != 0:
        raise RuntimeError(f"failed to prepare {product.id} task images")


def _cleanup_images(product: Product, tasks: list[Task], *, root: Path) -> None:
    if product.prebuilt is None:
        return
    docker = shutil.which("docker")
    if not docker:
        return
    if product.prebuilt == "pi":
        prefix, tag, label_prefix = "moi/pi-tbench", "0.73.1", "io.moi.pi-tbench"
    else:
        prefix, tag, label_prefix = "moi/hermes-tbench", "v2026.7.20", "io.moi.hermes-tbench"
    for task in tasks:
        image = f"{prefix}-{task.name}:{tag}"
        inspect = _run(
            [
                docker,
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{label_prefix}.kind" }}}}|'
                f'{{{{ index .Config.Labels "{label_prefix}.task" }}}}',
                image,
            ],
            cwd=root,
            capture=True,
        )
        if inspect.returncode != 0:
            continue
        if inspect.stdout.strip() != f"ephemeral-task|{task.name}":
            print(
                f"warning: refusing to remove image with unexpected labels: {image}",
                file=sys.stderr,
            )
            continue
        removed = _run([docker, "image", "rm", "--force", image], cwd=root)
        if removed.returncode != 0:
            print(f"warning: failed to remove task image: {image}", file=sys.stderr)


async def _execute_tasks(
    tasks: list[Task],
    *,
    product: Product,
    root: Path,
    tasks_root: Path,
    generated_root: Path,
    jobs_dir: Path,
    harbor_bin: str,
    env: dict[str, str],
    max_workers: int,
) -> int:
    selected_root = generated_root if product.prebuilt else tasks_root
    compose_overlay = root / "astra/runners/astra_terminal_bench/host-docker-internal.compose.yaml"
    prepare_lock = asyncio.Lock()

    async def execute(task: Task) -> int:
        job_name = (
            datetime.now(timezone.utc).strftime("%Y-%m-%d__%H-%M-%S-%f")
            + f"__{task.name}"
        )
        command = [
            harbor_bin,
            "run",
            "--config",
            str(product.config_path),
            "--jobs-dir",
            str(jobs_dir),
            "--job-name",
            job_name,
            "--path",
            str(selected_root / task.name),
            "--no-force-build",
            "--yes",
        ]
        if product.id == "astra":
            command.extend(
                [
                    "--extra-docker-compose",
                    str(compose_overlay),
                    "--allow-environment-host",
                    "host.docker.internal",
                ]
            )
        if product.prebuilt == "hermes":
            async with prepare_lock:
                print(f"preparing {task.name} image on demand", flush=True)
                try:
                    _prepare_images(
                        product,
                        [task],
                        root=root,
                        tasks_root=tasks_root,
                        generated_root=generated_root,
                        jobs_dir=jobs_dir,
                        env=env,
                    )
                except RuntimeError as exc:
                    print(str(exc), file=sys.stderr, flush=True)
                    return 1
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=root, env=env
                )
        else:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=root, env=env
            )
        return await process.wait()

    return await run_tasks(tasks, execute, max_workers=max_workers)


def _manifest(
    *,
    root: Path,
    state_dir: Path,
    product: Product,
    dataset_commit: str,
    astra_commit: str | None,
    tasks: list[Task],
    max_workers: int,
) -> None:
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform": "linux/amd64",
        "workspace_commit": _output(["git", "rev-parse", "HEAD"], cwd=root),
        "dataset_commit": dataset_commit,
        "product": product.id,
        "model": product.model,
        "reasoning_effort": "high",
        "temperature": 0,
        "astra_branch": ASTRA_BRANCH if product.id == "astra" else None,
        "astra_commit": astra_commit,
        "condition": "C0",
        "task_count": len(tasks),
        "contains_tune_mjcf": any(task.name == "tune-mjcf" for task in tasks),
        "product_timeout_multiplier": 1.0,
        "resource_policy": "pi_dsh_3_memory_tokens_6_cpus",
        "max_workers": max_workers,
        "config": str(product.config_path.relative_to(root)),
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "run-manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one product on the unified 89-task Linux Terminal-Bench cohort"
    )
    parser.add_argument("--product", choices=sorted(PRODUCTS), required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--retry-queue", type=Path)
    parser.add_argument("--rerun-completed", action="store_true")
    args = parser.parse_args(argv)
    if args.max_tasks is not None and args.max_tasks <= 0:
        parser.error("--max-tasks must be positive")
    if args.max_workers <= 0:
        parser.error("--max-workers must be positive")
    if args.retry_queue and args.case:
        parser.error("--retry-queue and --case cannot be combined")

    root = Path(__file__).resolve().parents[3]
    data_root = Path(os.environ.get("MOI_BENCH_DATA_ROOT", str(root))).resolve()
    dataset_root = data_root / "work/terminal-bench-2-1"
    tasks_root = dataset_root / "tasks"
    product = PRODUCTS[args.product]
    product_root = data_root / "work/linux-terminal-bench" / product.id
    jobs_dir = product_root / "jobs"
    state_dir = product_root / "state"
    generated_root = product_root / "generated/tasks"

    _validate_linux_host(root)
    dataset_commit = _validate_dataset(dataset_root, tasks_root)
    harbor_bin = _harbor_binary()
    harbor_version = _output([harbor_bin, "--version"], cwd=root).splitlines()[-1]
    if not harbor_version.strip().endswith("0.20.0"):
        raise RuntimeError(f"this runner requires Harbor 0.20.0; found {harbor_version}")
    python_bin = _python_binary(harbor_bin)
    env = _base_environment(root, data_root, harbor_bin, python_bin)
    astra_commit = _validate_product(product, root, env, require_key=not args.check)

    canonical_tasks = build_queue(tasks_root)
    canonical_queue = state_dir / "resource.queue.tsv"
    write_queue(canonical_queue, canonical_tasks)
    if args.retry_queue:
        selected = load_queue(args.retry_queue.resolve())
        canonical_by_name = {task.name: task for task in canonical_tasks}
        for task in selected:
            if canonical_by_name.get(task.name) != task:
                raise RuntimeError(f"retry queue row is not canonical: {task.as_tsv().strip()}")
        rerun_completed = True
    else:
        selected = build_queue(
            tasks_root,
            included_tasks=frozenset(args.case) if args.case else None,
        )
        rerun_completed = args.rerun_completed

    completed = completed_tasks(jobs_dir, {task.name for task in selected}, product)
    pending = (
        selected
        if rerun_completed
        else [task for task in selected if task.name not in completed]
    )
    if args.max_tasks is not None:
        pending = pending[: args.max_tasks]
    write_queue(state_dir / "pending.queue.tsv", pending)

    print(f"Product: {product.id}")
    print(f"Harbor: {harbor_version}")
    print(f"Dataset: {dataset_commit}")
    includes_tune = any(task.name == "tune-mjcf" for task in selected)
    print(f"Cohort: {len(selected)} tasks; tune-mjcf included={includes_tune}")
    print(f"Pending this invocation: {len(pending)}")
    print("Product timeout: dataset [agent].timeout_sec x 1.0")
    print(
        f"Parallel policy: one product, up to {args.max_workers} tasks, "
        "3 memory tokens / 6 CPUs"
    )
    if args.check:
        print("Check complete; no image build, model call, or trial was started.")
        return 0

    _manifest(
        root=root,
        state_dir=state_dir,
        product=product,
        dataset_commit=dataset_commit,
        astra_commit=astra_commit,
        tasks=selected,
        max_workers=args.max_workers,
    )
    if not pending:
        summarize(
            product=product,
            jobs_dir=jobs_dir,
            tasks=[task.name for task in selected],
            output_dir=state_dir / "analysis",
            dataset_commit=dataset_commit,
        )
        print("All selected tasks already have valid verifier results.")
        return 0

    if product.id == "dsh":
        _prepare_dsh_runtime(product_root, env)
    # ShellCrash rejects Docker bridge clients on its public proxy port.
    # The existing bridge-bound relay reaches it through host loopback.
    if product.id == "hermes" and not env.get("PI_TBENCH_CACHE_PROXY_URL"):
        if Path("/usr/share/ShellCrash").is_dir():
            env["PI_TBENCH_CACHE_PROXY_URL"] = "http://127.0.0.1:7890"
            env.setdefault("HERMES_TBENCH_BUILD_PROXY", "http://127.0.0.1:7890")
    _prepare_verifier_cache(root, env)
    relay = None
    host_proxy = env.get("PI_TBENCH_CACHE_PROXY_URL")
    if host_proxy:
        relay = start_proxy_relay(host_proxy, _docker_bridge_gateway(root))
        if relay is not None:
            env["TBENCH_VERIFIER_PROXY"] = relay.proxy_url
            relay_url = urlparse(relay.proxy_url)
            print(
                "Verifier proxy relay ready: "
                f"{relay_url.hostname}:{relay_url.port}"
            )
    status = 1
    try:
        if product.prebuilt != "hermes":
            _prepare_images(
                product,
                pending,
                root=root,
                tasks_root=tasks_root,
                generated_root=generated_root,
                jobs_dir=jobs_dir,
                env=env,
            )
        status = asyncio.run(
            _execute_tasks(
                pending,
                product=product,
                root=root,
                tasks_root=tasks_root,
                generated_root=generated_root,
                jobs_dir=jobs_dir,
                harbor_bin=harbor_bin,
                env=env,
                max_workers=args.max_workers,
            )
        )
    finally:
        if relay is not None:
            relay.close()
        _cleanup_images(product, pending, root=root)
        summary = summarize(
            product=product,
            jobs_dir=jobs_dir,
            tasks=[task.name for task in selected],
            output_dir=state_dir / "analysis",
            dataset_commit=dataset_commit,
        )
        print(
            f"Result coverage: {summary['valid_verifier_tasks']}/"
            f"{summary['expected_tasks']}; pending={summary['pending_tasks']}"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
