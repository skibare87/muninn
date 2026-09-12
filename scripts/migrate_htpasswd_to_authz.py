#!/usr/bin/env python3
"""Migrate existing htpasswd consumers into the per-key authorisation store.

RUN THIS BEFORE SETTING XHC_AUTHZ_DB, NOT AFTER. Setting it REPLACES the
htpasswd gate rather than layering on it, so every existing consumer is refused
at the next restart unless their credential already exists in the store. This
script is what makes the cutover invisible to them.

WHY IT TAKES PLAINTEXT AND NOT THE HTPASSWD FILE. The htpasswd line is a bcrypt
hash; the store needs a sha256 of the same secret. A hash cannot be converted
into a different hash. So the plaintext has to come from wherever it is kept --
the vault, in this deployment -- and if it is not recoverable, that consumer
must re-`docker login` and there is no way around that. Find out BEFORE the
cutover, not during.

WHAT IT DELIBERATELY DOES NOT DO:

  * It never routes a consumer through the first-principal-becomes-admin path.
    Machine consumers are created with is_admin explicitly 0. A service account
    holding admin over a shared cache is not a thing to arrive at by accident of
    ordering.

  * It grants no more authority than the consumer has today. Where the only
    existing control was a registry allow-list, the faithful translation is `*`
    with PULL ONLY -- the registry allow-list still applies independently, and
    push being disabled today is not a reason to grant it.

Reads secrets from stdin as `key_id<TAB>secret` lines, so nothing lands in argv
where `ps` can see it, or in shell history.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.authz import Rule
from app.authzstore import AuthzStore


def parse_rules(spec: str) -> list[Rule]:
    """`pattern:verbs` items, e.g. `*:pull` or `docker.io/*:pull+push`.

    An unparseable item raises rather than defaulting. A typo that quietly
    narrows a grant is a support ticket; one that quietly widens it is an
    incident, and defaulting is how you get the second.
    """
    rules = []
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        pattern, _, verbs = item.rpartition(":")
        if not pattern:
            raise SystemExit(f"rule {item!r} has no pattern; use pattern:verbs")
        verbs = verbs.lower()
        if verbs not in ("pull", "push", "pull+push", "push+pull"):
            raise SystemExit(f"rule {item!r}: verbs must be pull, push or pull+push")
        rules.append(Rule(pattern, pull="pull" in verbs, push="push" in verbs))
    if not rules:
        raise SystemExit("no rules given -- a key with no rules can do nothing")
    return rules


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to the authz SQLite store")
    ap.add_argument("--principal", required=True,
                    help="subject for this consumer, e.g. svc:ci-runner")
    ap.add_argument("--email", default="", help="label only; never used as identity")
    ap.add_argument("--rules", required=True,
                    help="comma-separated pattern:verbs, e.g. '*:pull'")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rules = parse_rules(args.rules)

    creds = []
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if "\t" not in line:
            raise SystemExit("stdin lines must be key_id<TAB>secret")
        key_id, secret = line.split("\t", 1)
        if not key_id or not secret:
            raise SystemExit("empty key id or secret")
        creds.append((key_id, secret))
    if not creds:
        raise SystemExit("no credentials on stdin")

    print(f"principal : {args.principal}  (is_admin=0, explicitly)")
    print(f"rules     : {', '.join(f'{r.pattern} pull={r.pull} push={r.push}' for r in rules)}")
    print(f"keys      : {len(creds)} -> {', '.join(k for k, _ in creds)}")
    if args.dry_run:
        print("\nDRY RUN -- nothing written.")
        return 0

    store = AuthzStore(args.db)

    # Never via claim_or_get_principal: that is the human login path and carries
    # the first-principal-becomes-admin grant. A machine consumer must not take
    # it, and must not consume it either -- doing so would silently deny the
    # first real person their admin claim.
    try:
        store.create_principal(args.principal, args.email, is_admin=False)
        print(f"created principal {args.principal} with is_admin=0")
    except KeyError:
        print(f"principal {args.principal} already exists -- left as is")

    store.set_principal_rules(args.principal, rules)
    print("allowlist set")

    for key_id, secret in creds:
        store.add_key(key_id, secret, args.principal, [], label="migrated from htpasswd")
        print(f"added key {key_id}")

    # VERIFY AGAINST THE STORE, not against this script exiting 0. Resolving the
    # credential and authorising a real operation is the claim; "the insert did
    # not raise" is adjacent to it.
    from app import authz
    ok = True
    for key_id, secret in creds:
        key = store.resolve(key_id, secret)
        if key is None:
            print(f"  FAIL {key_id}: does not resolve against the store")
            ok = False
            continue
        allowed, reason = authz.decide(key, "pull", "docker.io/library/alpine")
        print(f"  {'ok  ' if allowed else 'FAIL'} {key_id}: pull docker.io/library/alpine -- {reason}")
        ok = ok and allowed
        # a negative control: the same key must NOT be able to push unless asked
        pushed, _ = authz.decide(key, "push", "docker.io/library/alpine")
        want_push = any(r.push for r in rules)
        if pushed != want_push:
            print(f"  FAIL {key_id}: push={pushed}, expected {want_push}")
            ok = False

    print("\nOK -- migrated and verified" if ok else "\nFAILED -- do not cut over")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
