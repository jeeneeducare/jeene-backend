"""Run the API locally with authentication stubbed, so the app can be driven against it.

Firebase ID tokens cannot be minted from a test harness, and every Jeene Mode endpoint is
account-bound — so without this there is no way to point the app at a real server and
watch a real plan come back. That gap is why JM-6 shipped with DTOs that had never
deserialized a single response.

**This is a harness, not a mode.** It is not importable by anything in `app/`, it is not
deployed, and it refuses to start unless you say the uid out loud and the database is
local. Those two guards are cheap and they mean this file cannot be turned into a
production auth bypass by accident.

    JEENE_DEV_AUTH_UID=student-with-history .venv/bin/python tests/devserver.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

# Run directly rather than as a module, so put the repo root on the path. `pytest.ini`
# does this for the suite; a standalone script has to do it itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    uid = os.environ.get("JEENE_DEV_AUTH_UID")
    if not uid:
        print(
            "Refusing to start: set JEENE_DEV_AUTH_UID to the Firebase uid every request "
            "should be treated as. This server has no authentication.",
            file=sys.stderr,
        )
        return 2

    dsn = os.environ.get("DATABASE_URL", "")
    host = urlparse(dsn).hostname or ""
    if host not in ("localhost", "127.0.0.1", "::1", ""):
        print(
            f"Refusing to start: DATABASE_URL points at {host!r}. This server accepts "
            "every request as an authenticated user and must never be pointed at a "
            "database anybody else is using.",
            file=sys.stderr,
        )
        return 2

    import uvicorn

    from app.auth import current_tenant, optional_user, require_admin, require_user
    from app.main import app

    tenant = os.environ.get("JEENE_DEV_TENANT", "JEENE_MASTER")
    app.dependency_overrides[require_user] = lambda: {"uid": uid}
    app.dependency_overrides[optional_user] = lambda: {"uid": uid}
    app.dependency_overrides[current_tenant] = lambda: tenant
    app.dependency_overrides[require_admin] = lambda: {
        "uid": uid, "tenant_id": tenant, "admin_email": "dev@localhost"
    }

    port = int(os.environ.get("JEENE_DEV_PORT", "8000"))
    print(f"!! NO AUTHENTICATION — every request is {uid!r} on tenant {tenant!r}")
    print(f"!! local only, http://127.0.0.1:{port}")
    # Quiet by default; set JEENE_DEV_LOG_LEVEL=info to see every request, which is
    # the only way to tell "the app never asked" from "the app asked and we said no".
    level = os.environ.get("JEENE_DEV_LOG_LEVEL", "warning")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level=level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
