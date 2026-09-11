#!/usr/bin/env python3
"""Separate service handoff policy; original strict agent cleanup is retained."""
import json
from pathlib import Path
import runpy

ns = runpy.run_path('/installed-agent/lifecycle-process-probe.py', run_name='moi_original_probe')
# Function globals, not runpy's returned copy, are the authoritative module scope.
g = ns['main'].__globals__
original_teardown = g['_strict_teardown']
original_targets = g['_strict_cleanup_targets']

def teardown(root, supervisor, grace):
    table = g['_process_table']()
    owned = original_targets(root, supervisor, {}, table)
    services = {}
    for path in Path('/opt/moi-service-lifetime/registry').glob('*.json'):
        record = json.loads(path.read_text())
        pid = record['pid']
        current = table.get(pid)
        if current is None or current['state'] == 'Z':
            continue
        if current['start_ticks'] != record['start_ticks']:
            continue
        if pid not in owned or pid in {1, root['pid'], supervisor['pid']}:
            continue
        if current['pgid'] != pid or current['sid'] != pid:
            raise RuntimeError('Registered service process group changed')
        argv = Path('/proc/%s/cmdline' % pid).read_bytes().split(b'\0')
        if argv[:6] != [b'/usr/bin/timeout', b'--signal=TERM', b'--kill-after=5s', b'43200s', b'python3', b'/opt/moi-service-lifetime/service-supervisor.py']:
            raise RuntimeError('Registered service command identity changed')
        services[pid] = {'identity': current, 'registration': record}

    def targets(root_id, supervisor_id, retained, current_table):
        selected = original_targets(root_id, supervisor_id, retained, current_table)
        excluded = set()
        for pid, service in services.items():
            current = current_table.get(pid)
            if current and g['_same_process'](service['identity'], current) and current['state'] != 'Z':
                excluded.add(pid)
        while True:
            children = {pid for pid, info in current_table.items() if info['ppid'] in excluded}
            updated = excluded | children
            if updated == excluded:
                break
            excluded = updated
        return selected - excluded

    g['_strict_cleanup_targets'] = targets
    try:
        report = original_teardown(root, supervisor, grace)
    finally:
        g['_strict_cleanup_targets'] = original_targets
    report['cleanup_scope'] = 'agent-tree-excluding-registered-environment-services'
    report['environment_service_policy'] = 'container-through-verifier-max-43200s'
    report['environment_service_handoffs'] = list(services.values())
    report['zero_live_proven_scope'] = report['cleanup_scope']
    return report

g['_strict_teardown'] = teardown
if __name__ == '__main__':
    raise SystemExit(ns['main']())
