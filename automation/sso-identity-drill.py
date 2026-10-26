#!/usr/bin/env python3
"""Controlled SSO-01..SSO-05 identity drills against the staging Okta org.

The drill creates exactly one ephemeral, Okta-mastered user whose login starts
with ``sso-drill-``, exercises password, staged, lockout, app-assignment and
MFA-enrollment behaviour through the Okta APIs, collects System Log evidence,
and always deletes that user at the end. Every mutating call is guarded so it
can only target the drill user created by this run; existing users, groups,
apps and policies are read only. Passwords, tokens and activation links are
never printed or written to the report.

Scenario definitions: docs/BAO_CAO_KICH_BAN_LOI.md, section 4

Requires the ``cryptography`` package and an Okta API Services app using
private_key_jwt (DPoP is supported) with the scopes in DEFAULT_SCOPES.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCOPES = (
    "okta.users.manage okta.users.read okta.apps.read okta.groups.read "
    "okta.policies.read okta.logs.read"
)
DRILL_PREFIX = "sso-drill-"
MAX_LOCKOUT_ATTEMPTS = 10
REDACTED_KEYS = {"password", "sessionToken", "stateToken", "activationToken", "activationUrl", "access_token"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class OktaClient:
    """Minimal Okta API client: private_key_jwt client auth with optional DPoP."""

    def __init__(self, org_url: str, client_id: str, key_path: Path, key_id: str, scopes: str) -> None:
        self.org = org_url.rstrip("/")
        self.client_id = client_id
        self.key_id = key_id
        self.scopes = scopes
        self.signing_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        self.dpop_key = ec.generate_private_key(ec.SECP256R1())
        self.use_dpop = False
        self.nonce: Optional[str] = None
        self.access_token: Optional[str] = None

    # --- token ---------------------------------------------------------------
    def _client_assertion(self) -> str:
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT", "kid": self.key_id}
        payload = {
            "iss": self.client_id, "sub": self.client_id, "aud": f"{self.org}/oauth2/v1/token",
            "iat": now, "exp": now + 300, "jti": str(uuid.uuid4()),
        }
        signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
        signature = self.signing_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{b64url(signature)}"

    def _dpop_proof(self, method: str, url: str, access_token: Optional[str] = None) -> str:
        numbers = self.dpop_key.public_key().public_numbers()
        jwk = {
            "kty": "EC", "crv": "P-256",
            "x": b64url(numbers.x.to_bytes(32, "big")), "y": b64url(numbers.y.to_bytes(32, "big")),
        }
        header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": jwk}
        payload: Dict[str, Any] = {
            "htm": method, "htu": url.split("?", 1)[0], "iat": int(time.time()), "jti": str(uuid.uuid4()),
        }
        if self.nonce:
            payload["nonce"] = self.nonce
        if access_token:
            payload["ath"] = b64url(hashlib.sha256(access_token.encode()).digest())
        signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(payload).encode())}"
        r, s = decode_dss_signature(self.dpop_key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        return f"{signing_input}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    def authenticate(self) -> None:
        url = f"{self.org}/oauth2/v1/token"
        for _ in range(4):
            form = urllib.parse.urlencode({
                "grant_type": "client_credentials", "scope": self.scopes,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": self._client_assertion(),
            }).encode()
            headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
            if self.use_dpop:
                headers["DPoP"] = self._dpop_proof("POST", url)
            status, body, resp_headers = raw_request("POST", url, headers, form)
            if status == 200 and body.get("access_token"):
                self.access_token = body["access_token"]
                self.use_dpop = body.get("token_type", "").lower() == "dpop"
                # Okta silently drops scopes the app was not granted; fail closed.
                missing = set(self.scopes.split()) - set(body.get("scope", "").split())
                if missing:
                    raise RuntimeError("Okta API app is missing granted scopes: " + " ".join(sorted(missing)))
                return
            error = body.get("error", "")
            if error == "invalid_dpop_proof" and not self.use_dpop:
                self.use_dpop = True
                continue
            if error == "use_dpop_nonce":
                self.nonce = resp_headers.get("dpop-nonce")
                continue
            raise RuntimeError(f"Okta token request failed: {error}: {body.get('error_description')}")
        raise RuntimeError("Okta token request failed after DPoP negotiation")

    # --- API -----------------------------------------------------------------
    def api(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        url = path if path.startswith("http") else f"{self.org}{path}"
        data = json.dumps(body).encode() if body is not None else None
        for _ in range(2):
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            if self.use_dpop:
                headers["Authorization"] = f"DPoP {self.access_token}"
                headers["DPoP"] = self._dpop_proof(method, url, self.access_token)
            else:
                headers["Authorization"] = f"Bearer {self.access_token}"
            status, payload, resp_headers = raw_request(method, url, headers, data)
            nonce = resp_headers.get("dpop-nonce")
            if status == 401 and nonce and nonce != self.nonce:
                self.nonce = nonce
                continue
            return status, payload
        return status, payload


def raw_request(method: str, url: str, headers: Dict[str, str], data: Optional[bytes] = None,
                follow_redirects: bool = True) -> Tuple[int, Any, Dict[str, str]]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None

    opener = urllib.request.build_opener() if follow_redirects else urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=30) as response:
            status, raw, resp_headers = response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        status, raw, resp_headers = exc.code, exc.read(), exc.headers
    lowered = {k.lower(): v for k, v in resp_headers.items()}
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {"_non_json": raw[:20000].decode("utf-8", "replace")}
    return status, payload, lowered


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("<redacted>" if k in REDACTED_KEYS and v else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class Drill:
    def __init__(self, okta: OktaClient, moodle_client_id: str, redirect_uri: str, max_lockout: int) -> None:
        self.okta = okta
        self.moodle_client_id = moodle_client_id
        self.redirect_uri = redirect_uri
        self.max_lockout = max_lockout
        self.started_at = utc_now()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        self.login = f"{DRILL_PREFIX}{stamp}@example.com"
        self.password = secrets.token_urlsafe(18) + "Aa1!"
        self.user_id: Optional[str] = None
        self.results: List[Dict[str, Any]] = []
        self.findings: List[str] = []

    # --- guard -----------------------------------------------------------------
    def guarded(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
        """Mutations may only target the drill user created by this run."""
        if not self.user_id or f"/users/{self.user_id}" not in path:
            raise PermissionError(f"Refusing {method} {path}: not the drill user of this run")
        return self.okta.api(method, path, body)

    # --- primitives ----------------------------------------------------------
    def authn(self, password: str) -> Dict[str, Any]:
        status, body, _ = raw_request(
            "POST", f"{self.okta.org}/api/v1/authn",
            {"Content-Type": "application/json", "Accept": "application/json"},
            json.dumps({"username": self.login, "password": password}).encode(),
        )
        return {"http": status, "status": body.get("status"), "errorCode": body.get("errorCode"),
                "sessionToken": body.get("sessionToken")}

    def user_status(self, user_id: Optional[str] = None) -> Optional[str]:
        status, body = self.okta.api("GET", f"/api/v1/users/{user_id or self.user_id}")
        return body.get("status") if status == 200 else None

    def step(self, scenario: Dict[str, Any], name: str, expected: str, observed: Any, passed: bool) -> None:
        scenario["steps"].append({"step": name, "expected": expected, "observed": redact(observed), "pass": passed})

    def new_scenario(self, sid: str, title: str) -> Dict[str, Any]:
        scenario = {"scenario_id": sid, "title": title, "steps": [], "verdict": None, "notes": []}
        self.results.append(scenario)
        return scenario

    @staticmethod
    def close(scenario: Dict[str, Any]) -> None:
        if scenario["verdict"] is None:
            scenario["verdict"] = "PASS" if scenario["steps"] and all(s["pass"] for s in scenario["steps"]) else "FAIL"

    # --- preflight -----------------------------------------------------------
    def preflight(self) -> None:
        status, body, _ = raw_request("GET", f"{self.okta.org}/.well-known/openid-configuration", {"Accept": "application/json"})
        if status != 200 or body.get("issuer") != self.okta.org:
            raise RuntimeError(f"Okta org preflight failed (HTTP {status}, issuer={body.get('issuer')}); check the org domain")
        self.okta.authenticate()

    # --- scenarios -----------------------------------------------------------
    def sso02_staged(self) -> None:
        sc = self.new_scenario("SSO-02", "Imported-style user left in STAGED cannot sign in")
        status, body = self.okta.api("POST", "/api/v1/users?activate=false", {
            "profile": {"firstName": "SSO", "lastName": "Drill", "email": self.login, "login": self.login},
            "credentials": {"password": {"value": self.password}},
        })
        if status != 200:
            sc["verdict"] = "ERROR"
            sc["notes"].append(f"create drill user failed: HTTP {status} {body.get('errorCode')} {body.get('errorSummary')}")
            raise RuntimeError("cannot create drill user")
        self.user_id = body["id"]
        self.step(sc, "create user with activate=false", "status STAGED", body.get("status"), body.get("status") == "STAGED")
        result = self.authn(self.password)
        self.step(sc, "authn with correct password while STAGED", "rejected (HTTP 401 E0000004)",
                  result, result["http"] == 401)
        status, _ = self.guarded("POST", f"/api/v1/users/{self.user_id}/lifecycle/activate?sendEmail=false")
        current = self.user_status()
        self.step(sc, "activate without email", "status ACTIVE", {"http": status, "status": current}, current == "ACTIVE")
        result = self.authn(self.password)
        self.step(sc, "authn with correct password after activation", "accepted (HTTP 200)",
                  result, result["http"] == 200)
        self.close(sc)

    def sso01_password(self) -> None:
        sc = self.new_scenario("SSO-01", "Wrong password is rejected; correct Okta password is accepted")
        wrong = self.authn(self.password + "x")
        self.step(sc, "authn with wrong password", "rejected (HTTP 401 E0000004)", wrong,
                  wrong["http"] == 401 and wrong["errorCode"] == "E0000004")
        right = self.authn(self.password)
        self.step(sc, "authn with correct Okta password", "accepted (HTTP 200)", right, right["http"] == 200)
        sc["notes"].append(f"post-password state returned by Okta: {right['status']}")
        self.close(sc)

    def _lockout_threshold(self) -> Optional[int]:
        status, policies = self.okta.api("GET", "/api/v1/policies?type=PASSWORD")
        if status != 200 or not isinstance(policies, list):
            return None
        status, groups = self.okta.api("GET", "/api/v1/groups?q=Everyone&limit=5")
        everyone = next((g["id"] for g in groups if g.get("profile", {}).get("name") == "Everyone"), None) if status == 200 else None
        active = sorted((p for p in policies if p.get("status") == "ACTIVE"), key=lambda p: p.get("priority", 99))
        for policy in active:
            include = policy.get("conditions", {}).get("people", {}).get("groups", {}).get("include", [])
            if everyone in include or policy.get("system"):
                return policy.get("settings", {}).get("password", {}).get("lockout", {}).get("maxAttempts")
        return None

    def sso03_lockout(self) -> None:
        sc = self.new_scenario("SSO-03", "Repeated wrong passwords lock the account; unlock restores access")
        threshold = self._lockout_threshold()
        sc["notes"].append(f"password-policy lockout threshold: {threshold}")
        if not threshold:
            sc["verdict"] = "FINDING"
            self.findings.append("SSO-03: the password policy applying to Everyone has no lockout threshold (maxAttempts 0/unset)")
            return
        if threshold > self.max_lockout:
            sc["verdict"] = "SKIPPED"
            sc["notes"].append(f"threshold {threshold} exceeds the drill cap {self.max_lockout}")
            return
        for _ in range(threshold):
            self.authn(self.password + "x")
        current = self.user_status()
        self.step(sc, f"{threshold} wrong passwords", "status LOCKED_OUT", current, current == "LOCKED_OUT")
        locked = self.authn(self.password)
        self.step(sc, "authn with correct password while locked", "not accepted (no SUCCESS/MFA session)", locked,
                  locked["http"] != 200 or locked["status"] == "LOCKED_OUT")
        status, _ = self.guarded("POST", f"/api/v1/users/{self.user_id}/lifecycle/unlock")
        current = self.user_status()
        self.step(sc, "admin unlock", "status ACTIVE", {"http": status, "status": current}, current == "ACTIVE")
        after = self.authn(self.password)
        self.step(sc, "authn after unlock", "accepted (HTTP 200)", after, after["http"] == 200)
        self.close(sc)

    def _moodle_app(self) -> Optional[Dict[str, Any]]:
        status, apps = self.okta.api("GET", "/api/v1/apps?filter=" + urllib.parse.quote('status eq "ACTIVE"') + "&limit=200")
        if status != 200:
            return None
        return next((a for a in apps if a.get("credentials", {}).get("oauthClient", {}).get("client_id") == self.moodle_client_id), None)

    def sso04_assignment(self) -> None:
        sc = self.new_scenario("SSO-04", "User without the Moodle app assignment is denied by Okta")
        app = self._moodle_app()
        if not app:
            sc["verdict"] = "ERROR"
            sc["notes"].append("Moodle OIDC app not found by client_id")
            return
        app_id = app["id"]
        status, _ = self.okta.api("GET", f"/api/v1/apps/{app_id}/users/{self.user_id}")
        self.step(sc, "drill user assignment lookup", "not assigned (HTTP 404)", status, status == 404)

        result = self.authn(self.password)
        if result.get("sessionToken"):
            query = urllib.parse.urlencode({
                "client_id": self.moodle_client_id, "response_type": "code", "scope": "openid",
                "redirect_uri": self.redirect_uri, "state": "sso-drill", "nonce": uuid.uuid4().hex,
                "sessionToken": result["sessionToken"],
            })
            http_status, page, headers = raw_request(
                "GET", f"{self.okta.org}/oauth2/v1/authorize?{query}",
                {"Accept": "text/html", "User-Agent": "sso-identity-drill"}, None, follow_redirects=False)
            location = headers.get("location", "")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
            text = page.get("_non_json", "") if isinstance(page, dict) else ""
            outcome = {"http": http_status, "redirect_host": urllib.parse.urlparse(location).netloc,
                       "redirect_path": urllib.parse.urlparse(location).path,
                       "error": params.get("error", [None])[0],
                       "error_description": params.get("error_description", [None])[0],
                       "code_issued": "code" in params,
                       "page_says_not_assigned": "not assigned" in text.lower(),
                       "page_title": (text.split("<title>", 1)[-1].split("</title>", 1)[0].strip()[:80]
                                      if "<title>" in text else None),
                       "page_has_signin_widget": "okta-sign-in" in text,
                       "okta_request_id": headers.get("x-okta-request-id")}
            denied = outcome["error"] == "access_denied" or outcome["page_says_not_assigned"]
            self.step(sc, "authorize Moodle client with drill session (redirect not followed)",
                      "denied (access_denied redirect or 'not assigned' page), no code", outcome,
                      denied and not outcome["code_issued"])
        else:
            sc["notes"].append(f"behavioural authorize check skipped: Okta returned {result['status']} without a session token")

        # Read-only controls on real users: allowed groups assigned, denied group not.
        for group_name, expect_assigned in (("moodle-students", True), ("moodle-teachers", True), ("moodle-denied", False)):
            status, groups = self.okta.api("GET", "/api/v1/groups?q=" + urllib.parse.quote(group_name) + "&limit=10")
            group = next((g for g in groups if g.get("profile", {}).get("name") == group_name), None) if status == 200 else None
            if not group:
                self.step(sc, f"control: group {group_name} exists", "found", None, False)
                continue
            status, _ = self.okta.api("GET", f"/api/v1/apps/{app_id}/groups/{group['id']}")
            assigned = status == 200
            self.step(sc, f"control: {group_name} assigned to Moodle app",
                      "assigned" if expect_assigned else "not assigned", assigned, assigned == expect_assigned)
        self.close(sc)

    def sso05_mfa(self) -> None:
        sc = self.new_scenario("SSO-05", "Moodle access requires a possession factor; new users start without one")
        status, factors = self.okta.api("GET", f"/api/v1/users/{self.user_id}/factors")
        enrolled = [f.get("factorType") for f in factors] if status == 200 and isinstance(factors, list) else None
        self.step(sc, "drill user enrolled factors", "none enrolled", enrolled, enrolled == [])

        app = self._moodle_app()
        policy_href = (app or {}).get("_links", {}).get("accessPolicy", {}).get("href")
        if not policy_href:
            self.step(sc, "Moodle app has an authentication policy", "policy linked", None, False)
            self.close(sc)
            return
        status, rules = self.okta.api("GET", policy_href.rstrip("/") + "/rules")
        rules = rules if status == 200 and isinstance(rules, list) else []
        catch_all = next((r for r in rules if r.get("system")), None)
        catch_all_access = (catch_all or {}).get("actions", {}).get("appSignOn", {}).get("access")
        self.step(sc, "catch-all rule", "DENY", catch_all_access, catch_all_access == "DENY")
        allow_rules = [r for r in rules if not r.get("system") and r.get("status") == "ACTIVE"
                       and r.get("actions", {}).get("appSignOn", {}).get("access") == "ALLOW"]
        mfa_ok = bool(allow_rules) and all(
            r.get("actions", {}).get("appSignOn", {}).get("verificationMethod", {}).get("factorMode") == "2FA"
            for r in allow_rules)
        self.step(sc, "every ALLOW rule requires 2 factors", "factorMode 2FA",
                  [{"name": r.get("name"), "factorMode": r.get("actions", {}).get("appSignOn", {}).get("verificationMethod", {}).get("factorMode")}
                   for r in allow_rules], mfa_ok)

        # Read-only readiness of the real test users.
        for login_prefix in ("student.test@", "teacher.test@"):
            status, users = self.okta.api("GET", "/api/v1/users?q=" + urllib.parse.quote(login_prefix.rstrip("@")) + "&limit=5")
            user = next((u for u in users if u.get("profile", {}).get("login", "").startswith(login_prefix)), None) if status == 200 else None
            if not user:
                sc["notes"].append(f"{login_prefix} not found")
                continue
            status, factors = self.okta.api("GET", f"/api/v1/users/{user['id']}/factors")
            types = sorted({f.get("factorType") for f in factors}) if status == 200 and isinstance(factors, list) else None
            sc["notes"].append(f"readiness {login_prefix.rstrip('@')}: status={user.get('status')}, factors={types}")
        self.close(sc)

    # --- evidence & cleanup ---------------------------------------------------
    def evidence(self) -> List[Dict[str, Any]]:
        if not self.user_id:
            return []
        # System Log is eventually consistent: poll by actor/target id for up to ~90 s.
        query = urllib.parse.urlencode({
            "since": self.started_at, "limit": 200, "sortOrder": "ASCENDING",
            "filter": f'actor.id eq "{self.user_id}" or target.id eq "{self.user_id}"',
        })
        status, events, previous = 0, [], -1
        for _ in range(9):
            time.sleep(10)
            status, events = self.okta.api("GET", f"/api/v1/logs?{query}")
            if status != 200 or not isinstance(events, list):
                break
            if len(events) == previous:  # no new events for one poll interval
                break
            previous = len(events)
        if status != 200 or not isinstance(events, list):
            return [{"error": f"System Log query failed: HTTP {status}"}]
        return [{"published": e.get("published"), "eventType": e.get("eventType"),
                 "result": e.get("outcome", {}).get("result"), "reason": e.get("outcome", {}).get("reason")}
                for e in events]

    def cleanup(self) -> Dict[str, Any]:
        if not self.user_id:
            return {"user_created": False}
        self.guarded("POST", f"/api/v1/users/{self.user_id}/lifecycle/deactivate?sendEmail=false")
        delete_status, _ = self.guarded("DELETE", f"/api/v1/users/{self.user_id}?sendEmail=false")
        check_status, _ = self.okta.api("GET", f"/api/v1/users/{self.user_id}")
        return {"user_created": True, "delete_http": delete_status, "lookup_after_delete_http": check_status,
                "deleted": check_status == 404}


def write_report(output_dir: Path, report: Dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [f"# SSO drill {report['run_id']}", "", f"Org: {report['org']}  ", f"Window: {report['started_at']} → {report['finished_at']}", "",
             "| Scenario | Verdict | Steps passed |", "|---|---|---|"]
    for sc in report["scenarios"]:
        passed = sum(1 for s in sc["steps"] if s["pass"])
        lines.append(f"| {sc['scenario_id']} {sc['title']} | {sc['verdict']} | {passed}/{len(sc['steps'])} |")
    lines += ["", f"Cleanup: {json.dumps(report['cleanup'])}", ""]
    for finding in report["findings"]:
        lines.append(f"- FINDING: {finding}")
    path = output_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--org-url", default=os.getenv("OKTA_ORG_URL", "https://trial-3489054.okta.com"))
    parser.add_argument("--client-id", default=os.getenv("OKTA_API_CLIENT_ID"))
    parser.add_argument("--private-key", type=Path, default=Path(os.getenv("OKTA_API_PRIVATE_KEY", "~/.okta/terraform-moodle.pem")).expanduser())
    parser.add_argument("--key-id", default=os.getenv("OKTA_API_PRIVATE_KEY_ID"))
    parser.add_argument("--scopes", default=os.getenv("OKTA_API_SCOPES", DEFAULT_SCOPES))
    parser.add_argument("--moodle-client-id", default=os.getenv("MOODLE_OKTA_CLIENT_ID"))
    parser.add_argument("--redirect-uri", default=os.getenv("MOODLE_OKTA_REDIRECT_URI"))
    parser.add_argument("--max-lockout-attempts", type=int, default=MAX_LOCKOUT_ATTEMPTS)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--preflight-only", action="store_true", help="check org and API token, change nothing")
    args = parser.parse_args()
    missing = [n for n in ("client_id", "key_id", "moodle_client_id", "redirect_uri") if not getattr(args, n)]
    if missing:
        parser.error("missing: " + ", ".join(missing))

    okta = OktaClient(args.org_url, args.client_id, args.private_key, args.key_id, args.scopes)
    drill = Drill(okta, args.moodle_client_id, args.redirect_uri, args.max_lockout_attempts)
    drill.preflight()
    print(f"Preflight OK: issuer {okta.org}, token type {'DPoP' if okta.use_dpop else 'Bearer'}")
    if args.preflight_only:
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir or REPO_ROOT / "terraform" / ".artifacts" / "sso-drill" / run_id
    cleanup: Dict[str, Any] = {}
    error: Optional[str] = None
    try:
        drill.sso02_staged()       # creates the drill user; must run first
        drill.sso01_password()
        drill.sso03_lockout()
        drill.sso04_assignment()
        drill.sso05_mfa()
    except Exception as exc:  # noqa: BLE001 - always clean up and report
        error = f"{type(exc).__name__}: {exc}"
    finally:
        events = drill.evidence() if drill.user_id else []
        try:
            cleanup = drill.cleanup()
        except Exception as exc:  # noqa: BLE001
            cleanup = {"error": f"{type(exc).__name__}: {exc}", "user_id": drill.user_id}
    report = {"run_id": run_id, "org": okta.org, "drill_login": drill.login, "started_at": drill.started_at,
              "finished_at": utc_now(), "error": error, "scenarios": drill.results, "findings": drill.findings,
              "system_log_events": events, "cleanup": cleanup}
    path = write_report(output_dir, report)
    for sc in drill.results:
        print(f"{sc['scenario_id']}: {sc['verdict']}")
    print(f"Cleanup: {cleanup}")
    print(f"Report: {path}")
    return 0 if not error and all(sc["verdict"] in ("PASS", "FINDING", "SKIPPED") for sc in drill.results) else 1


if __name__ == "__main__":
    sys.exit(main())
