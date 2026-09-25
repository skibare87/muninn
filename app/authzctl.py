"""Provision principals, rules and keys from a shell: `python -m app.authzctl`.

Works directly on the SQLite store named by --db or XHC_AUTHZ_DB, so it runs
in an init container before the server starts, or beside a running server --
the server checks the database's own change counter on every authentication,
so a key minted here authenticates on the very next request and a key
disabled here is refused on the very next request.

    python -m app.authzctl create-principal svc:ci [--email L] [--admin] [--exist-ok]
    python -m app.authzctl set-rules svc:ci 'docker.io/library/* pull' ...
    python -m app.authzctl mint svc:ci [--label L] [--scope RULE ...]
                                       [--secret-file PATH [--secret-file-format secret|token]]
    python -m app.authzctl list
    python -m app.authzctl disable-key KEY_ID | enable-key KEY_ID | delete-key KEY_ID
    python -m app.authzctl delete-principal svc:ci
    python -m app.authzctl grant-admin SUBJECT | revoke-admin SUBJECT

Output is JSON on stdout; errors go to stderr with a non-zero exit. ONLY `mint`
ever prints a secret, and with --secret-file it prints none: the secret goes to
a new file created 0600, suitable for a Kubernetes Secret or a projected file.

Same operations, same validation and the same rule syntax as /_cache/authz --
both call app/authzadmin.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import authzadmin
from .authzstore import AuthzStore


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2))


def _open_secret_file(path: str, overwrite: bool) -> int:
    """Create the secret file BEFORE minting, so a path that cannot be written
    fails without leaving a live key behind that nobody holds the secret to.

    O_EXCL: an existing file is refused, never truncated and reused -- its mode
    may be wider than 0600, and chmod-after-open leaves a window. With
    --overwrite it is unlinked first and created fresh.
    """
    if overwrite:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m app.authzctl",
        description="Provision principals, rules and keys in a Muninn authz store.",
    )
    ap.add_argument("--db", default=os.environ.get("XHC_AUTHZ_DB") or None,
                    help="path to the authz SQLite store (default: $XHC_AUTHZ_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-principal", help="create a principal (never admin by default)")
    p.add_argument("subject")
    p.add_argument("--email", default="", help="a label only; never used as identity")
    p.add_argument("--admin", action="store_true",
                   help="make this principal an administrator (explicit, never implied)")
    p.add_argument("--exist-ok", action="store_true",
                   help="succeed if it already exists with the same admin flag")

    p = sub.add_parser("set-rules", help="replace a principal's allowlist")
    p.add_argument("subject")
    p.add_argument("rules", nargs="*", help="'<pattern> pull|push|pull+push', one per argument")
    p.add_argument("--empty", action="store_true",
                   help="required to set NO rules, which revokes everything the principal can do")

    p = sub.add_parser("mint", help="mint a key; the secret is shown once")
    p.add_argument("subject")
    p.add_argument("--label", default="")
    p.add_argument("--scope", action="append", default=[],
                   help="narrow this key to a rule; repeatable; '*' means no limit")
    p.add_argument("--secret-file",
                   help="write the secret to this NEW file, mode 0600, instead of stdout")
    p.add_argument("--secret-file-format", choices=("secret", "token"), default="secret",
                   help="'secret': the secret alone (a docker password). "
                        "'token': key_id:secret (an HF_TOKEN)")
    p.add_argument("--overwrite", action="store_true",
                   help="replace an existing --secret-file instead of refusing")

    sub.add_parser("list", help="principals and keys; never secrets")

    for name, text in (("disable-key", "disable a key"), ("enable-key", "re-enable a key"),
                       ("delete-key", "delete a key")):
        sub.add_parser(name, help=text).add_argument("key_id")

    sub.add_parser("delete-principal",
                   help="delete a principal and its keys").add_argument("subject")
    sub.add_parser("grant-admin",
                   help="make an existing principal an administrator -- the "
                        "recovery path for an instance with no admin").add_argument("subject")
    sub.add_parser("revoke-admin",
                   help="remove admin; refused for the last admin").add_argument("subject")
    return ap


def _run(args: argparse.Namespace, store: AuthzStore) -> None:
    if args.cmd == "create-principal":
        try:
            _emit(authzadmin.create_principal(store, args.subject, args.email, args.admin))
        except authzadmin.Conflict:
            existing = store.get_principal(args.subject)
            if not args.exist_ok or existing is None:
                raise
            if existing.is_admin != args.admin:
                raise authzadmin.Conflict(
                    f"principal {args.subject} exists with admin={existing.is_admin}, "
                    f"not admin={args.admin} as requested; left unchanged"
                ) from None
            _emit(authzadmin.principal_out(store, existing))
    elif args.cmd == "set-rules":
        if not args.rules and not args.empty:
            raise authzadmin.Invalid(
                "no rules given. An empty allowlist revokes everything this principal "
                "can do; pass --empty if that is what you mean"
            )
        _emit({"subject": args.subject,
               "rules": authzadmin.set_rules(store, args.subject, args.rules)})
    elif args.cmd == "mint":
        fd = None
        if args.secret_file:
            try:
                fd = _open_secret_file(args.secret_file, args.overwrite)
            except FileExistsError:
                raise authzadmin.Conflict(
                    f"{args.secret_file} exists; refusing to overwrite it "
                    "(pass --overwrite to replace it). No key was minted."
                ) from None
        try:
            minted = authzadmin.mint_key(store, args.subject, args.label, args.scope)
        except BaseException:
            if fd is not None:
                os.close(fd)
                os.unlink(args.secret_file)
            raise
        if fd is None:
            _emit(minted)
            return
        payload = minted["secret"]
        if args.secret_file_format == "token":
            payload = f"{minted['key_id']}:{minted['secret']}"
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        _emit({k: v for k, v in minted.items() if k != "secret"}
              | {"secret_file": args.secret_file, "secret_file_format": args.secret_file_format})
    elif args.cmd == "list":
        _emit({"principals": authzadmin.list_principals(store),
               "keys": authzadmin.list_keys(store)})
    elif args.cmd in ("disable-key", "enable-key"):
        authzadmin.set_key_disabled(store, args.key_id, args.cmd == "disable-key")
        _emit({"key_id": args.key_id, "disabled": args.cmd == "disable-key"})
    elif args.cmd == "delete-key":
        authzadmin.delete_key(store, args.key_id)
        _emit({"key_id": args.key_id, "deleted": True})
    elif args.cmd == "delete-principal":
        authzadmin.delete_principal(store, args.subject)
        _emit({"subject": args.subject, "deleted": True})
    elif args.cmd in ("grant-admin", "revoke-admin"):
        _emit(authzadmin.set_admin(store, args.subject, args.cmd == "grant-admin"))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.db:
        print("authzctl: no database: pass --db or set XHC_AUTHZ_DB", file=sys.stderr)
        return 2
    try:
        _run(args, AuthzStore(args.db))
    except authzadmin.ProvisionError as exc:
        print(f"authzctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
