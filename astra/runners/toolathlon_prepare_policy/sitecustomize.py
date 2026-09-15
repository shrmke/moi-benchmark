"""Prepare-only local WooCommerce transport policy; dataset files stay unchanged."""
import functools
import os
import sys
import time
from urllib.parse import urlsplit


def _install():
    from utils.app_specific.woocommerce import client

    original = client.WooCommerceClient._request_with_retry

    def local_attempt(self, method, url, **kwargs):
        started = time.monotonic()
        path = urlsplit(url).path
        try:
            response = self.session.request(
                method.upper(), url, timeout=(5, 60),
                proxies={"http": "", "https": ""}, **kwargs,
            )
            print(f"[runner-woo] method={method.upper()} path={path} "
                  f"status={response.status_code} seconds={time.monotonic() - started:.3f}",
                  file=sys.stderr, flush=True)
            if response.status_code >= 500 or response.status_code == 429:
                raise client._TransientWCError(
                    f"Local WooCommerce returned HTTP {response.status_code}"
                )
            response.raise_for_status()
            return response
        except Exception as exc:
            print(f"[runner-woo] method={method.upper()} path={path} "
                  f"error={type(exc).__name__} seconds={time.monotonic() - started:.3f} "
                  f"write_retry=disabled", file=sys.stderr, flush=True)
            raise

    retry_read = client.wc_retry(local_attempt)

    @functools.wraps(original)
    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        local = (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                 and parsed.port == 10003
                 and ("/wp-json/wc/" in parsed.path or "/wp-json/wp/" in parsed.path))
        if not local:
            return original(self, method, url, **kwargs)
        call = retry_read if method.upper() in {"GET", "HEAD", "OPTIONS"} else local_attempt
        return call(self, method, url, **kwargs)

    client.WooCommerceClient._request_with_retry = request
    print("[runner-woo] prepare-only policy enabled: local-direct connect=5s read=60s; "
          "read retries preserved; write requests attempted once", file=sys.stderr, flush=True)


if os.environ.get("TOOLATHLON_LOCAL_WOO_PREPARE_POLICY") == "1":
    try:
        _install()
    except Exception as exc:
        raise SystemExit(f"Cannot initialize runner WooCommerce policy: {type(exc).__name__}") from exc
