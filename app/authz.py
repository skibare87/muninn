"""Principals, keys, and per-key push/pull authorisation with wildcards.

WHAT THIS CHANGES. Until now Muninn's client auth was a GATE: any valid credential
could pull, and could push to any upstream the global allow-list permitted. There
was no notion of a principal and no per-caller scope. This module is the first
thing that makes the cache genuinely multi-principal, which is why it is written
and tested in isolation before any of it is wired to an identity provider or a UI.

THE DECISION IS DELIBERATELY BORING AND ALLOW-ONLY. A rule GRANTS; nothing denies.
Any matching rule that carries the operation is sufficient. There is no precedence
order to get wrong, no deny-overrides-allow subtlety, and no rule whose effect
depends on its position in the list -- because an authorisation bug that depends on
ordering is invisible in review and only shows up as the wrong person pushing.

WHAT A RULE MATCHES AGAINST is the full reference `<upstream>/<repo>`, e.g.
`docker.io/library/redis`. NOT the tag, and NOT the digest: authorisation is about
which repository you may read or write, and a scheme that varied by tag would let
`:latest` be more privileged than `:v2`.

WILDCARDS. `*` matches any run of characters INCLUDING `/`, so `docker.io/*` covers
`docker.io/library/redis` and a bare `*` is everything. Matching `/` was a choice:
per-segment wildcards mean `docker.io/*` silently fails to cover the nested repos
that are the normal case, and the failure direction is an operator widening the
rule until it works, which lands them at `*`. A `?` matches exactly one character.

CASE. Registry hosts are case-insensitive and repository paths are not, but no
registry in practice uses case to distinguish repos, and treating a rule as
case-sensitive means `Docker.io/*` silently grants nothing. Matching is
case-insensitive, deliberately, and it is stated here because silently granting
LESS than intended is the safe direction and silently granting more is not -- this
choice is the safe one only because it cannot broaden a rule beyond its pattern.
"""

from __future__ import annotations

import fnmatch
import re
import secrets
from dataclasses import dataclass, field
from typing import Literal

# `delete` exists only on the Hugging Face surface, and only with XHC_HF_WRITES=on:
# it is what a destructive write (a deleted file, branch, tag or repository, a
# history squash, an LFS purge) needs IN ADDITION to push. See check_rule.
Operation = Literal["pull", "push", "delete"]
# Which surface a reference belongs to. A rule is written in one flat pattern
# space, but it grants on exactly one surface -- see "THE HUGGING FACE
# NAMESPACE" below -- except a bare `*`, which grants on both.
Surface = Literal["registry", "hf"]

# A key id is shown in UIs and logs; the secret never is. Split so that a log line
# can name WHICH key acted without the log becoming a credential store.
_KEY_ID_BYTES = 8
_KEY_SECRET_BYTES = 32


@dataclass(frozen=True)
class Rule:
    """One grant. `pattern` is matched against `<upstream>/<repo>`.

    A rule with neither pull nor push is inert rather than an error: it is what a
    UI produces when someone unchecks both boxes, and refusing it would turn a
    harmless no-op into a failed save.

    `delete` is a separate grant, never implied by push: see check_rule.
    """

    pattern: str
    pull: bool = True
    push: bool = False
    delete: bool = False

    def grants(
        self, operation: Operation, reference: str, surface: Surface = "registry"
    ) -> bool:
        if operation == "pull" and not self.pull:
            return False
        if operation == "push" and not self.push:
            return False
        if operation == "delete" and not self.delete:
            return False
        # A BARE `*` NEVER GRANTS A WRITE ON THE HUGGING FACE SURFACE. It still
        # grants pull there. `* pull+push` was written, by everyone who has one,
        # before writes toward the Hub existed, and meant registry push; turning
        # on XHC_HF_WRITES must not silently give each of them the power to
        # commit to the Hub as this cache. A Hub write has to be named:
        # `models/<org>/* pull+push`.
        if surface == "hf" and operation != "pull" and self.pattern.strip() == "*":
            return False
        # The surface decides which patterns may even be tried. Without this, a
        # registry whose default upstream were named `models` would produce
        # references `models/...` that a Hugging Face rule matched as text.
        if surface == "hf":
            if not (is_hf_pattern(self.pattern) or self.pattern.strip() == "*"):
                return False
        elif is_hf_pattern(self.pattern):
            return False
        return _matches(self.pattern, reference)


@dataclass
class Key:
    """A credential a machine presents. The secret is stored HASHED, never raw."""

    key_id: str
    secret_hash: str
    principal: str
    # WHAT THE HOLDER MAY DO. The principal's allowlist, set by an administrator.
    # This is the grant, and an empty one grants nothing.
    rules: list[Rule] = field(default_factory=list)
    # WHAT THIS PARTICULAR KEY MAY DO OF THAT. A narrowing, and only a narrowing.
    #
    # EMPTY MEANS NO NARROWING, which is the opposite of what empty means for
    # `rules` above, and the difference is deliberate. A grant list that is empty
    # grants nothing; a CONSTRAINT list that is empty constrains nothing. Reading
    # an absent constraint as deny-all would stop every existing key the moment
    # this field was introduced.
    scope: list[Rule] = field(default_factory=list)
    label: str = ""
    disabled: bool = False

    def allows(
        self, operation: Operation, reference: str, surface: Surface = "registry"
    ) -> bool:
        """Both lists must permit it. NARROWING, not widening.

        The earlier model unioned the two, so a key could only ever be granted
        MORE than its holder. That made a narrower credential impossible to
        express, which in turn forced a separate principal per scope -- machine
        consumers appearing in the user list as if they were people, because
        that was the only place a scope could be hung.

        Conjunction of DECISIONS rather than intersection of patterns: working
        out the overlap of two globs is a hard problem and an unnecessary one,
        since the question is only ever asked about a concrete reference.
        """
        if self.disabled:
            return False
        if not any(r.grants(operation, reference, surface) for r in self.rules):
            return False
        if not self.scope:
            return True
        return any(r.grants(operation, reference, surface) for r in self.scope)


@dataclass
class Principal:
    """A human, identified by whatever the identity provider calls stable.

    `is_admin` is STORED rather than derived from being first to log in. Deriving
    it would mean the admin changes when the first account is deleted, or that
    deleting your own account silently promotes someone else -- and an operator who
    removes their own account should not be able to lock the instance out of ever
    having an admin again.
    """

    subject: str
    email: str = ""
    is_admin: bool = False
    disabled: bool = False


def _matches(pattern: str, reference: str) -> bool:
    """Wildcard match of a rule pattern against `<upstream>/<repo>`.

    Uses fnmatch semantics where `*` spans `/`, via translate() so that the
    behaviour is a regex we control rather than fnmatch's platform-dependent
    case handling -- fnmatch.fnmatch() lowercases according to the OS on some
    platforms, which would make authorisation differ between a developer's
    machine and the container.
    """
    if not pattern or not reference:
        return False
    regex = fnmatch.translate(pattern.strip().lower())
    return re.match(regex, reference.strip().lower()) is not None


def new_secret() -> tuple[str, str]:
    """Return (key_id, secret). The secret is returned ONCE and never stored raw.

    Shown to the user at creation and unrecoverable afterwards, which is the same
    contract as a cloud provider's API key -- and the reason the UI has to say so
    at the moment of creation rather than in documentation nobody opens.
    """
    return secrets.token_hex(_KEY_ID_BYTES), secrets.token_urlsafe(_KEY_SECRET_BYTES)


def decide(
    key: Key | None, operation: Operation, reference: str, surface: Surface = "registry"
) -> tuple[bool, str]:
    """Authorise one operation. Returns (allowed, reason).

    The reason is for the LOG, not for the client: telling an unauthorised caller
    which rule refused them describes the policy to someone who has just failed to
    satisfy it. The wire gets a status; the operator gets the sentence.
    """
    if key is None:
        return False, "no key presented"
    if key.disabled:
        return False, f"key {key.key_id} is disabled"
    if not key.rules:
        return False, f"key {key.key_id} has no rules"
    if key.allows(operation, reference, surface):
        return True, f"key {key.key_id} allows {operation} on {reference}"
    # Two ways to be refused, and an operator chasing a 403 needs to know which:
    # the holder was never granted it, or this key was scoped away from it.
    if key.scope and any(r.grants(operation, reference, surface) for r in key.rules):
        return False, (f"key {key.key_id} is scoped away from {operation} on "
                       f"{reference}, which its holder is otherwise allowed")
    return False, f"key {key.key_id} has no rule granting {operation} on {reference}"


# ---------------------------------------------------------------------------
# THE HUGGING FACE NAMESPACE. Rules match HF repositories in the same shape as
# XHC_ALLOW_REPOS:
#
#     models/<org>/<name>     datasets/<org>/<name>     spaces/<org>/<name>
#
# (or `models/<name>` for a canonical id with no org, such as `gpt2`), verb
# `pull` -- and, with XHC_HF_WRITES=on, `push` and `delete` (app/hfwrites.py).
# One shape across the ingest allowlist and the per-key rules.
#
# WHY IT CANNOT BE CONFUSED WITH A REGISTRY REFERENCE. A registry reference
# always begins with a HOST: a segment with a dot or a port, `localhost`, or the
# default upstream. `models`, `datasets` and `spaces` are none of those. That
# argument alone would leave one hole -- a dotless default upstream named, say,
# `models` -- so it is not relied on: a rule's first segment decides its
# SURFACE, and Rule.grants refuses to try a pattern on the other surface.
#
#   first segment models|datasets|spaces   Hugging Face only, never an image
#   anything else, including `*/x/*`       registry only, never a model
#   exactly `*`                            both surfaces
#
# Matching within a surface is the registry's: `*` spans `/`, case-insensitive,
# allow-only. XHC_ALLOW_REPOS uses fnmatch.fnmatch, where `*` also spans `/`,
# but which is CASE-SENSITIVE on Linux. So `models/Org/*` in the allowlist does
# not match `models/org/x`, while the same rule here does.
#
# References that name no single repository, used by hfauthz:
#
#   models/  datasets/  spaces/   a listing or search over one whole type.
#                                 Matched by `models/*` or `*`: `*` matches the
#                                 empty string, so no special case is needed.
#   HF_ANY_ENDPOINT               a path naming no repository (whoami,
#                                 collections, papers, anything unknown). Its
#                                 first segment is not a type, so no HF-surface
#                                 pattern can match it, and the surface check
#                                 above admits no other pattern except `*`. It is
#                                 not expressible as anything narrower, and
#                                 check_rule refuses it as a pattern anyway.
# ---------------------------------------------------------------------------

HF_TYPES = {"models": "model", "datasets": "dataset", "spaces": "space"}
HF_REPO_TYPES = tuple(HF_TYPES.values())
HF_ANY_ENDPOINT = "(any other Hugging Face endpoint)"
_WILDCARD = re.compile(r"[*?\[]")


def _first_segment(pattern: str) -> str:
    return pattern.strip().split("/", 1)[0].lower()


def is_hf_pattern(pattern: str) -> bool:
    """True when a pattern's first segment is a Hugging Face repo type."""
    return _first_segment(pattern) in HF_TYPES


def hf_reference(repo_type: str, repo_id: str = "") -> str:
    """The rule reference for an HF repo, or for its whole type when repo_id is ''."""
    if repo_type not in HF_REPO_TYPES:
        raise ValueError(f"unknown Hugging Face repo type {repo_type!r}")
    return f"{repo_type}s/{repo_id}"


# ---------------------------------------------------------------------------
# RULE TEXT. One rule per line: `<pattern> [verbs]`, where verbs are `pull`,
# `push` and `delete` joined by `+` in any order, defaulting to pull:
#
#     docker.io/library/*    pull
#     ghcr.io/myorg/*        pull+push
#     models/myorg/*         pull+push           (needs XHC_HF_WRITES=on)
#     models/myorg/scratch-* pull+push+delete    (needs XHC_HF_WRITES=on)
#
# WHY `+delete` AND NOT A NEW SYNTAX. The grammar was already a set of verbs
# joined by `+`; `pull+push` and `push+pull` were two spellings of one set. A
# third member of the set is the smallest extension that keeps every existing
# rule meaning what it meant, and it puts the destructive grant in the same
# visible place as the others -- on the rule line, where a reviewer reads it --
# rather than in a separate switch that changes what `push` means. Order is
# free and a repeated verb is refused, so a set is exactly what it looks like.
#
# This is the syntax the console's allowlist and scope
# fields accept, and until headless provisioning existed it was parsed ONLY in
# the browser -- the server took structured JSON. The CLI and /_cache/authz
# both need text, so the grammar lives here, once, and both call it.
# ---------------------------------------------------------------------------

_VERB_NAMES = ("pull", "push", "delete")
MAX_PATTERN_LEN = 512
_USAGE = ("use '<pattern> pull|push|pull+push', adding '+delete' for destructive "
          "writes to Hugging Face repositories")


def _parse_verbs(text: str) -> tuple[bool, bool, bool] | None:
    """(pull, push, delete) from `pull+push+delete` in any order, or None.

    Empty is pull. An unknown or repeated verb is None, never a default.
    """
    if text == "":
        return True, False, False
    parts = text.split("+")
    if any(p not in _VERB_NAMES for p in parts) or len(set(parts)) != len(parts):
        return None
    return "pull" in parts, "push" in parts, "delete" in parts


class RuleSyntaxError(ValueError):
    """A rule line that does not parse. The message names the line."""


def parse_rule(line: str) -> Rule:
    """Parse one rule line. Raises RuleSyntaxError rather than defaulting.

    A typo that quietly narrows a grant is a support ticket; one that quietly
    WIDENS it is an incident, and defaulting an unknown verb is how you get the
    second. Same refusal the console makes, for the same reason.
    """
    parts = line.split()
    if not parts:
        raise RuleSyntaxError(f"empty rule {line!r}")
    verbs = _parse_verbs(parts[1].lower() if len(parts) > 1 else "")
    if len(parts) > 2 or verbs is None:
        raise RuleSyntaxError(f"could not parse rule {line!r}: {_USAGE}")
    if len(parts[0]) > MAX_PATTERN_LEN:
        raise RuleSyntaxError(f"rule pattern longer than {MAX_PATTERN_LEN} characters")
    pull, push, delete = verbs
    return check_rule(Rule(parts[0], pull=pull, push=push, delete=delete))


def hf_writes_enabled() -> bool:
    """XHC_HF_WRITES, read lazily for the same reason _is_registry_host is."""
    from .config import settings

    return settings.hf_writes == "on"


def rule_verbs(rule: Rule) -> str:
    """The canonical verb text for a rule, e.g. `pull+push+delete`."""
    return "+".join(v for v, on in (("pull", rule.pull), ("push", rule.push),
                                    ("delete", rule.delete)) if on)


def check_rule(rule: Rule) -> Rule:
    """Refuse a rule that could never do what it says. Returns it unchanged.

    Separate from parse_rule because the console submits STRUCTURED rules and
    never reaches the text parser; the console, /_cache/authz and authzctl all
    have to refuse the same things. NO SILENT NEVER-MATCH: a rule that is
    stored but can never grant is believed by whoever typed it.

      `*`                          valid, both surfaces; never grants a WRITE
                                   on Hugging Face (see Rule.grants)
      models|datasets|spaces/...   valid; `push` only with XHC_HF_WRITES=on,
                                   refused otherwise because it could never
                                   take effect; a bare type with no repo part
                                   refused
      ... delete                   only on a Hugging Face pattern, only with
                                   push, only with XHC_HF_WRITES=on
      hf/...                       refused, pointing at models/...
      first segment is a host      valid registry rule (dot, port, localhost,
                                   a Docker Hub alias, or the default upstream)
      first segment has a wildcard valid registry rule (`*/library/*`); it
                                   never grants on Hugging Face
      anything else                refused: `org/name`, `model/...`, `library/*`
                                   -- no registry reference starts with it and
                                   it is not a Hugging Face type, so it could
                                   never match either surface
    """
    pattern = rule.pattern.strip()
    first = _first_segment(pattern)
    if rule.delete:
        # Checked before anything else, `*` included: a delete grant that can
        # never be exercised is believed by whoever typed it.
        if first not in HF_TYPES:
            raise RuleSyntaxError(
                f"rule {rule.pattern!r} grants delete, which applies only to Hugging "
                "Face repositories: write it on 'models/...', 'datasets/...' or "
                "'spaces/...'"
            )
        if not rule.push:
            raise RuleSyntaxError(
                f"rule {rule.pattern!r} grants delete without push. A destructive "
                "write needs both: write it as '<pattern> pull+push+delete'"
            )
    if pattern == "*":
        return rule
    if first in HF_TYPES:
        if rule.push and not hf_writes_enabled():
            raise RuleSyntaxError(
                f"rule {rule.pattern!r} grants {rule_verbs(rule)} on the Hugging Face "
                "surface, which is pull-only while XHC_HF_WRITES=off: enable "
                "XHC_HF_WRITES=on to forward writes to the Hub, or write it as "
                "'<pattern> pull'"
            )
        rest = pattern.split("/", 1)[1] if "/" in pattern else ""
        if not rest:
            raise RuleSyntaxError(
                f"rule {rule.pattern!r} names no repository: write "
                f"'{first}/<org>/<name>', or '{first}/*' for every {HF_TYPES[first]}"
            )
        return rule
    if first == "hf":
        raise RuleSyntaxError(
            f"rule {rule.pattern!r}: 'hf/' is not a rule prefix. Hugging Face rules "
            "are written 'models/<org>/<name>', 'datasets/<org>/<name>' or "
            "'spaces/<org>/<name>', the same shape as XHC_ALLOW_REPOS"
        )
    if _WILDCARD.search(first) or _is_registry_host(first):
        return rule
    if first.rstrip("s") in ("model", "dataset", "space"):
        hint = f"unknown type prefix '{first}/': use models/, datasets/ or spaces/"
    else:
        hint = (f"'{first}' is neither a registry host nor a Hugging Face type, so "
                "the rule could never match. Registry rules start with a host "
                "('docker.io/library/*'); Hugging Face rules with a type "
                "('models/<org>/<name>')")
    raise RuleSyntaxError(f"rule {rule.pattern!r}: {hint}")


def _is_registry_host(segment: str) -> bool:
    """Whether a first segment could begin a registry reference.

    Mirrors registry.resolve: a dot or a port marks a host, as does
    `localhost`; otherwise the reference begins with the default upstream,
    canonicalised to docker.io for the Hub's aliases.
    """
    if "." in segment or ":" in segment or segment == "localhost":
        return True
    from .config import settings

    return segment == (settings.docker_default_upstream or "").strip().lower()


def parse_rules(lines: list[str]) -> list[Rule]:
    """Parse every line, skipping blank ones, or raise on the first bad one.

    All-or-nothing: the caller gets a complete list or an exception, never a
    prefix -- a partially applied allowlist is neither the old grant nor the
    new one.
    """
    return [parse_rule(line) for line in lines if line.strip()]


def normalise_scope(rules: list[Rule]) -> list[Rule]:
    """A key scope containing `*` is NO LIMIT, and is stored as no limit.

    That is what everyone reads it as. It is not an escalation: a key is
    bounded by its holder's allowlist regardless, so the widest a scope can
    reach is what that holder already has. An earlier version refused it,
    which stopped a legitimate edit and protected nothing.
    """
    return [] if any(r.pattern == "*" for r in rules) else rules
