# Terminal-Bench service lifetime v2

AstraTerminalBenchC0Agent defaults to service_lifetime="v2". Normal runner
invocations no longer need the external harbor-services wrapper. Set the Harbor
agent kwarg service_lifetime="legacy" to retain the original strict cleanup.
Legacy fallback requires running without an external v2 wrapper.

Only explicitly registered native environment-background tasks are retained.
A subreaper owns descendants, including double-fork and setsid daemons. The
original PID identity checks and strict cleanup remain for Agent/non-service
processes. Services survive the Agent/verifier handoff and are removed with the
container, with a 43200-second safety lifetime. Agent/verifier timeouts remain
unchanged. This is the C0 no-fault evaluation policy, not a workload-kill fault
injection policy. S0 and Astra core are unchanged.

Existing external v2 wrappers take precedence so in-flight batches are not
installed twice. Verifier/model/dataset resource caches remain separate.

The three process scripts are copied unchanged from the independently exercised
service-lifetime-adapter-v2-20260911. This source integration has not been tested.
