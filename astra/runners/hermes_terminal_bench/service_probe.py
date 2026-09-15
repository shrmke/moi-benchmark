"""Use shared v2 cleanup, retaining only serving Hermes background trees."""
from pathlib import Path
import runpy

helper = runpy.run_path("/opt/moi-service-lifetime/hermes-service-lifetime.py")
ns = runpy.run_path("/installed-agent/moi-service-probe.py", run_name="hermes_shared_probe")
g = ns["ns"]["main"].__globals__
original = g["_strict_teardown"]


def teardown(root, supervisor, grace):
    for path in Path("/opt/moi-service-lifetime/registry").glob("*.json"):
        record = helper["live_record"](path)
        if record and record.get("source") in {"hermes-background", "hermes-local"} and not helper["prune_work"](record):
            path.rename(path.with_suffix(".inactive"))
    return original(root, supervisor, grace)


g["_strict_teardown"] = teardown
if __name__ == "__main__":
    raise SystemExit(ns["ns"]["main"]())
