"""Provision the model pods' principal and key, then publish the key as a Secret.

Runs as an init container of the Muninn StatefulSet, from the Muninn image,
BEFORE the server starts. It works on the authz database directly through
`python -m app.authzctl`, so it needs neither the server nor XHC_MANAGE_TOKEN.

Every run converges on the same state, so it is safe on every pod start:

  1. create-principal SUBJECT --exist-ok     (idempotent)
  2. set-rules SUBJECT <rules file>          (replaces; idempotent)
  3. the key, which is NOT idempotent in authzctl, so it is decided here:
       Secret absent                 -> delete this label's earlier keys (nobody
                                        holds their secrets), mint one, create
                                        the Secret
       Secret present, key live      -> nothing
       Secret present, key DISABLED  -> nothing. Someone revoked it on purpose;
                                        re-minting would silently undo that.
       Secret present, key MISSING   -> the database was replaced. Mint one and
                                        REPLACE the Secret, or every model pod
                                        holds a key that can never authenticate.
       anything else (403, 5xx, ...) -> exit non-zero. Unknown is not "absent":
                                        minting on an unreadable answer would
                                        leak a key per restart.

The secret never reaches stdout, stderr or argv: authzctl writes it to a 0600
file on a memory-backed emptyDir, this script reads it into the Secret body and
unlinks it. Only the key id (the Basic username, not a credential) is logged.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

SA_DIR = os.environ.get("PROVISION_SA_DIR", "/var/run/muninn-provisioner")
SUBJECT = os.environ["PROVISION_SUBJECT"]
KEY_LABEL = os.environ.get("PROVISION_KEY_LABEL", "k8s-model-pods")
RULES_FILE = os.environ.get("PROVISION_RULES_FILE", "/etc/muninn-provision/rules")
SECRET_NAME = os.environ["PROVISION_SECRET_NAME"]
SECRET_KEY = os.environ.get("PROVISION_SECRET_KEY", "HF_TOKEN")
MINT_DIR = os.environ.get("PROVISION_MINT_DIR", "/run/mint")


def log(msg: str) -> None:
    print(f"provision: {msg}", file=sys.stderr, flush=True)


def authzctl(*args: str) -> dict:
    """Run authzctl and return its JSON. authzctl prints a secret only for a
    `mint` WITHOUT --secret-file, which this script never does."""
    proc = subprocess.run(
        [sys.executable, "-m", "app.authzctl", *args],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"provision: authzctl {args[0]} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def read_rules() -> list[str]:
    with open(RULES_FILE) as fh:
        rules = [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]
    if not rules:
        # Fails on the argument: an empty allowlist revokes everything the model
        # pods can do, which is never what an empty ConfigMap meant.
        raise SystemExit(f"provision: {RULES_FILE} has no rules; refusing to revoke everything")
    return rules


class Api:
    """The three Kubernetes API calls needed, over the projected token.

    Standard library only: the Muninn image carries no Kubernetes client, and
    provisioning should not need a second image.
    """

    def __init__(self) -> None:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if ":" in host:
            host = f"[{host}]"
        # The Secret goes where the model pods are. Default: this pod's own
        # namespace; PROVISION_NAMESPACE puts it elsewhere (see README).
        self.ns = os.environ.get("PROVISION_NAMESPACE", "").strip()
        if not self.ns:
            with open(os.path.join(SA_DIR, "namespace")) as fh:
                self.ns = fh.read().strip()
        self.base = f"https://{host}:{port}/api/v1/namespaces/{self.ns}/secrets"
        self.ctx = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))

    def _call(self, method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        # Re-read on every call: the kubelet rotates a projected token.
        with open(os.path.join(SA_DIR, "token")) as fh:
            token = fh.read().strip()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=30) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            # An error body is a Status object, never the Secret.
            try:
                detail = json.loads(exc.read() or b"{}")
            except ValueError:
                detail = {}
            return exc.code, detail

    def get(self) -> tuple[int, dict]:
        return self._call("GET", f"{self.base}/{SECRET_NAME}")

    def write(self, token: str, key_id: str, replace_rv: str | None) -> tuple[int, dict]:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": SECRET_NAME,
                "labels": {"app.kubernetes.io/managed-by": "muninn-provision"},
                # The key id is the Basic username: an identifier, not a secret.
                # Recorded so `authzctl list` can be matched to this Secret.
                "annotations": {"muninn/key-id": key_id, "muninn/principal": SUBJECT},
            },
            "type": "Opaque",
            "data": {SECRET_KEY: base64.b64encode(token.encode()).decode()},
        }
        if replace_rv is None:
            return self._call("POST", self.base, body)
        body["metadata"]["resourceVersion"] = replace_rv
        return self._call("PUT", f"{self.base}/{SECRET_NAME}", body)


def mint_and_publish(api: Api, replace_rv: str | None) -> None:
    path = os.path.join(MINT_DIR, "token")
    # A leftover from a crashed run is a secret nobody will use. Remove it so
    # authzctl's exclusive create does not refuse.
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    minted = authzctl("mint", SUBJECT, "--label", KEY_LABEL,
                      "--secret-file", path, "--secret-file-format", "token")
    key_id = minted["key_id"]
    try:
        with open(path) as fh:
            token = fh.read()
    finally:
        os.unlink(path)
    status, detail = api.write(token, key_id, replace_rv)
    del token
    if status not in (200, 201):
        # Nobody holds this key's secret now, so it must not outlive the failure.
        authzctl("delete-key", key_id)
        raise SystemExit(
            f"provision: could not write Secret {SECRET_NAME} (HTTP {status}: "
            f"{detail.get('message', '')}); minted key {key_id} deleted again"
        )
    log(f"Secret {api.ns}/{SECRET_NAME} {'replaced' if replace_rv else 'created'} "
        f"with key {key_id}")


def main() -> int:
    rules = read_rules()
    authzctl("create-principal", SUBJECT, "--exist-ok")
    authzctl("set-rules", SUBJECT, *rules)
    log(f"principal {SUBJECT}: {len(rules)} rule(s) set")

    keys = {k["key_id"]: k for k in authzctl("list")["keys"] if k["principal"] == SUBJECT}

    api = Api()
    status, secret = api.get()
    if status == 404:
        # No Secret means no holder for any key under this label: each is left
        # over from a failed run, or from a Secret deleted to force rotation.
        for key_id, key in keys.items():
            if key.get("label") == KEY_LABEL:
                authzctl("delete-key", key_id)
                log(f"deleted unheld key {key_id} (label {KEY_LABEL})")
        mint_and_publish(api, None)
        return 0
    if status != 200:
        raise SystemExit(
            f"provision: GET Secret {SECRET_NAME} returned HTTP {status} "
            f"({secret.get('message', '')}). Refusing to mint on an unknown answer; "
            "check the Role and RoleBinding in rbac.yaml."
        )

    key_id = (secret.get("metadata", {}).get("annotations") or {}).get("muninn/key-id")
    if not key_id:
        raw = (secret.get("data") or {}).get(SECRET_KEY, "")
        key_id = base64.b64decode(raw).decode().split(":", 1)[0] if raw else ""
    key = keys.get(key_id)
    if key is None:
        log(f"Secret {SECRET_NAME} holds key {key_id or '?'}, which is not in the "
            "authz database (was the state volume replaced?); re-minting")
        mint_and_publish(api, secret["metadata"]["resourceVersion"])
    elif key["disabled"]:
        log(f"key {key_id} is DISABLED; leaving it. Delete Secret {SECRET_NAME} "
            "to issue a new one.")
    else:
        log(f"Secret {SECRET_NAME} holds live key {key_id}; nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
