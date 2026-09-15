"""Run only three task preprocesses with private debug logs and normal cleanup."""
import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import sys
import traceback

from toolathlon_astra_969550b import Lifecycle, base
from toolathlon_astra_969550b_batch import load_key

class Prepared(Exception):
    pass

class DiagnosticLifecycle(Lifecycle):
    def _stash_private_artifacts(self):
        # execute() will run its normal finally/cleanup, but never start an agent.
        raise Prepared()

def main():
    os.umask(0o077)
    root = Path('/home/vagrant/moi-benchmark')
    work = root / 'work/toolathlon-astra-969550b'
    with (work / '.batch.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.environ['TOOLATHLON_DEEPSEEK_ASTRA_API_KEY'] = load_key(root / 'external/astra-optimize_0731_05/.models.yaml')
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        output = work / ('preprocess-diagnostic-' + stamp)
        output.mkdir(mode=0o700)
        print('DIAGNOSTIC_OUTPUT', output, flush=True)
        for task in ['experiments-recordings', 'fillout-online-forms', 'filter-low-selling-products']:
            run_id = 'astra969-preprocess-' + task + '-' + stamp
            sys.argv = [sys.argv[0], '--system', 'astra', '--task-id', task,
                        '--experiment-id', 'preprocess-diagnostic', '--run-id', run_id,
                        '--output-dir', str(output / task), '--docker-via-sudo']
            print('START', task, flush=True)
            with (output / (task + '.private.log')).open('w') as log:
                with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                    try:
                        lifecycle = DiagnosticLifecycle(base.parse_args())
                        original = lifecycle.docker_command
                        def command(*args):
                            cmd = original(*args)
                            if 'scripts.decoupled.container_preprocess' in cmd:
                                cmd = [*cmd, '--debug']
                            return cmd
                        lifecycle.docker_command = command
                        lifecycle.execute()
                        result = 'unexpected_return'
                    except Prepared:
                        result = 'preprocess_passed_and_cleanup_returned'
                    except Exception:
                        traceback.print_exc()
                        result = 'failed_see_private_log'
            print('END', task, result, flush=True)
        print('DIAGNOSTIC_FINISHED', flush=True)

if __name__ == '__main__':
    main()
