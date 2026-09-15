#!/usr/bin/env bash
# Restore existing deployment without recreating containers, data, or source files.
set -euo pipefail

restore_canvas_proxy() {
    local root source state
    root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
    source=${TOOLATHLON_SOURCE:-/home/vagrant/dataset/Toolathlon}
    state="$root/work/toolathlon-astra-969550b/canvas-https-proxy"
    mkdir -p "$state"
    (
        flock -x 9
        if ss -lntH 'sport = :20001' | grep -q .; then
            printf 'Canvas HTTPS port 20001 already listening; leaving it unchanged.\n'
            exit 0
        fi
        umask 077
        nohup node "$source/deployment/utils/build_proxy.mjs" \
            20001 10001 127.0.0.1 http "$state" \
            </dev/null >>"$state/proxy.log" 2>&1 9>&- &
        printf '%s\n' "$!" >"$state/proxy.pid"
        printf 'Canvas HTTPS proxy launched: PID %s; log %s\n' "$!" "$state/proxy.log"
    ) 9>"$state/start.lock"
}
if [[ ${1:-} == --canvas-proxy-only ]]; then
    restore_canvas_proxy
    exit 0
fi
services=(all-in-one-matrixone-1 all-in-one-memoria-1
    woo-db-inst-alpha woo-wp-inst-alpha canvas-docker-inst-alpha poste-inst-alpha
    cluster-inst-alpha1-control-plane cluster-mysql-inst-alpha-control-plane
    cluster-redis-helm-inst-alpha-control-plane cluster-pr-preview-inst-alpha-control-plane)
sudo -n docker start "${services[@]}"
# Wait for application responses, not merely container state.
for endpoint in '10001/api/v1/accounts:401' '10003/:302' '10005/:302' '18100/health:200'; do
    address=${endpoint%:*}; expected=${endpoint##*:}; code=000
    for ((i=0; i<90; i++)); do
        code=$(curl --noproxy '*' -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$address" || true)
        [[ $code == "$expected" ]] && break
        sleep 2
    done
    [[ $code == "$expected" ]] || { printf 'Not ready: %s HTTP %s\n' "$address" "$code" >&2; exit 1; }
    printf 'Ready: %s HTTP %s\n' "$address" "$code"
done
restore_canvas_proxy
for name in "${services[@]:6}"; do
    sudo -n docker exec "$name" kubectl --kubeconfig=/etc/kubernetes/admin.conf \
        wait --for=condition=Ready node --all --timeout=180s
done
for port in 2525 1587; do
    reply=$(printf 'EHLO readiness\r\nQUIT\r\n' | timeout 5 nc -w 2 127.0.0.1 "$port" || true)
    [[ $reply == *ESMTP* ]] || { printf 'SMTP not ready: %s\n' "$port" >&2; exit 1; }
done
reply=$(printf 'a1 CAPABILITY\r\na2 LOGOUT\r\n' | timeout 5 nc -w 2 127.0.0.1 1143 || true)
[[ $reply == *IMAP4rev1* ]] || { printf 'IMAP not ready\n' >&2; exit 1; }
printf 'All existing local Toolathlon services are ready. External SaaS credentials were not refreshed.\n'
