"""Default v2 service handoff for the Terminal-Bench C0 adapter."""
import json
from pathlib import Path

REMOTE_PROBE = "/installed-agent/moi-service-probe.py"

async def install(agent, environment):
    source = Path(__file__).parent
    result = await environment.exec(
        command='mkdir -p /opt/moi-service-lifetime/bin /opt/moi-service-lifetime/registry && chmod 700 /opt/moi-service-lifetime/registry && printf "%s" "$PATH"',
        user="root", timeout_sec=10,
    )
    if result.return_code != 0:
        raise RuntimeError("Cannot prepare environment service registry")
    path = result.stdout.strip()
    for local, remote in (
        ("timeout", "/opt/moi-service-lifetime/bin/timeout"),
        ("service-probe.py", REMOTE_PROBE),
        ("service-supervisor.py", "/opt/moi-service-lifetime/service-supervisor.py"),
    ):
        await environment.upload_file(source / local, remote)
    result = await environment.exec(
        command="chmod 555 /opt/moi-service-lifetime/bin/timeout /installed-agent/moi-service-probe.py /opt/moi-service-lifetime/service-supervisor.py",
        user="root", timeout_sec=10,
    )
    if result.return_code != 0:
        raise RuntimeError("Cannot install environment service adapter")
    agent._service_lifetime_path = "/opt/moi-service-lifetime/bin:" + path
    (agent.logs_dir / "service-lifetime-policy.json").write_text(json.dumps({
        "mode": "v2", "policy": "container-through-verifier",
        "max_service_seconds": 43200,
        "native_background_ttl": "requested value recorded; overridden by container service policy",
        "agent_and_verifier_timeouts": "unchanged",
        "cleanup_scope": "agent-tree-excluding-registered-environment-services",
        "integration": "runner-default",
    }, indent=2) + "\n")
