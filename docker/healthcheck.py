"""Secret-free Docker liveness probe; intentionally uses only the stdlib."""

import http.client
import ipaddress
import json
import os


def check() -> int:
    connection = None
    try:
        host = os.environ.get("PULLARR_HOST") or "0.0.0.0"
        try:
            address = ipaddress.ip_address(host)
            if address.is_unspecified:
                host = "::1" if address.version == 6 else "127.0.0.1"
        except ValueError:
            pass  # DNS bind names are supported by uvicorn too.
        port = int(os.environ.get("PULLARR_PORT") or "6997")
        if not 1 <= port <= 65535:
            return 1
        # HTTPConnection never uses environment proxies or follows redirects.
        connection = http.client.HTTPConnection(host, port, timeout=4)
        # Listener DNS names need not be public browser names. Use an already
        # trusted Host instead of weakening the application's Host validation.
        connection.request("GET", "/ping", headers={"Host": "localhost"})
        response = connection.getresponse()
        if response.status != 200:
            return 1
        body = response.read(65)
        if len(body) > 64:
            return 1
        return 0 if json.loads(body) == {"status": "OK"} else 1
    except Exception:
        # Docker reserves exit code 2. Never print response bodies or errors.
        return 1
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(check())
