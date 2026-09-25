"""Parsing XHC_JWT_ISSUERS: which OIDC issuers' tokens this cache accepts.

Pure: no environment, no I/O, no import of settings. config.py calls parse() so a
malformed declaration refuses to start the server with a named error, on the
ARGUMENTS alone, before anything is fetched. Checks that need I/O (a CA file
that exists, a file JWKS that parses) run at startup in jwtauth.load().

WHY JSON IN ONE ENVIRONMENT VARIABLE, AND NOT A FILE. The declaration holds no
secret -- HMAC issuers are not supported, see ALGORITHMS -- so there is nothing
that needs a file's permissions, and one variable is what a container, a compose
file and a Kubernetes Deployment (`value:` or `valueFrom: configMapKeyRef`) all
set the same way. JSON rather than YAML because JSON needs no dependency this
project does not already declare, and because a second accepted source (an env
var AND a file) is a second place to look when the two disagree.

THE SHAPE, one object per issuer:

    [{"issuer": "https://kubernetes.default.svc.cluster.local",
      "audience": "muninn",
      "subject_template": "k8s:{sub}"}]

Unknown keys are refused. A misspelt optional key would otherwise silently take
its default -- `auto_create` typed as `autocreate` is "never create", which is
safe; `algorithms` typed as `algorithm` is "the default set", which may not be.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Asymmetric only. The default and the ceiling are the same set minus EdDSA,
# which is allowed but not defaulted because no workload issuer this was
# written for signs with it.
#
# NO HS256/384/512, ANYWHERE. An HMAC issuer shares its signing secret with
# every verifier, so a cache holding it could MINT tokens for that issuer --
# and the classic algorithm-confusion attack (sign HS256 with the RSA public
# key as the secret) exists only because a verifier accepted both families.
# Every issuer this feature is for (Kubernetes, Keycloak, GitHub Actions, any
# OIDC provider's client_credentials) signs asymmetrically, so refusing the
# whole family costs nothing and removes the class.
DEFAULT_ALGORITHMS = (
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512",
)
ALGORITHMS = (*DEFAULT_ALGORITHMS, "EdDSA")

DEFAULT_LEEWAY_S = 30.0
MAX_LEEWAY_S = 300.0
DEFAULT_FETCH_TIMEOUT_S = 5.0

_KEYS = {
    "issuer", "audience", "algorithms", "jwks_uri", "subject_claim",
    "subject_template", "auto_create", "leeway_s", "fetch_timeout_s",
    "ca_file", "fetch_token_file",
}
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class JWTConfigError(ValueError):
    """XHC_JWT_ISSUERS does not describe a usable configuration."""


@dataclass(frozen=True)
class IssuerConfig:
    issuer: str
    audiences: tuple[str, ...]
    subject_template: str
    algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS
    jwks_uri: str | None = None
    subject_claim: str = "sub"
    auto_create: bool = False
    leeway_s: float = DEFAULT_LEEWAY_S
    fetch_timeout_s: float = DEFAULT_FETCH_TIMEOUT_S
    ca_file: str | None = None
    fetch_token_file: str | None = None

    def subject_prefix(self) -> str:
        """The literal text a mapped subject starts with, `{iss}` substituted."""
        before = self.subject_template.split("{sub}", 1)[0]
        return before.replace("{iss}", encode_component(self.issuer))

    def map_subject(self, value: str) -> str:
        """The principal subject for one identity-claim value.

        Encoded, not copied. XHC_AUTHZ_DB subjects may not contain `/` (they are
        addressed as a path segment on /_cache/authz), and a GitHub Actions
        `sub` is `repo:org/name:ref:refs/heads/main`. Percent-encoding `%`, `/`
        and control characters is INJECTIVE -- `%` is itself encoded -- so two
        different claim values can never map to one subject.
        """
        return (self.subject_template
                .replace("{iss}", encode_component(self.issuer))
                .replace("{sub}", encode_component(value)))


def encode_component(value: str) -> str:
    out = []
    for ch in value:
        if ch in "%/" or ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("".join(f"%{b:02X}" for b in ch.encode()))
        else:
            out.append(ch)
    return "".join(out)


def _err(index: int, entry: dict | None, message: str) -> JWTConfigError:
    who = entry.get("issuer") if isinstance(entry, dict) else None
    label = f"issuer #{index}" + (f" ({who!r})" if isinstance(who, str) else "")
    return JWTConfigError(f"XHC_JWT_ISSUERS {label}: {message}")


def _string(entry: dict, index: int, field: str, *, required: bool = False) -> str | None:
    value = entry.get(field)
    if value is None:
        if required:
            raise _err(index, entry, f"'{field}' is required")
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise _err(index, entry, f"'{field}' must be a non-empty string without "
                                 "surrounding whitespace")
    return value


def _number(entry: dict, index: int, field: str, default: float, lo: float,
            hi: float) -> float:
    value = entry.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _err(index, entry, f"'{field}' must be a number of seconds")
    if not lo <= float(value) <= hi:
        raise _err(index, entry, f"'{field}' must be between {lo:g} and {hi:g}, "
                                 f"got {value}")
    return float(value)


def _check_template(template: str, index: int, entry: dict) -> None:
    names = _PLACEHOLDER.findall(template)
    unknown = sorted({n for n in names if n not in ("iss", "sub")})
    if unknown:
        raise _err(index, entry, f"'subject_template' uses unknown placeholder(s) "
                                 f"{unknown}; only {{iss}} and {{sub}} exist")
    stray = _PLACEHOLDER.sub("", template)
    if "{" in stray or "}" in stray:
        raise _err(index, entry, "'subject_template' has an unbalanced brace")
    if names.count("sub") != 1:
        raise _err(index, entry, "'subject_template' must contain {sub} exactly once")
    literal = _PLACEHOLDER.sub("", template)
    if "/" in literal or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in literal):
        raise _err(index, entry, "'subject_template' must not contain '/' or control "
                                 "characters: principal subjects cannot hold them")
    if template.startswith("{sub}"):
        # The prefix is what keeps issuers from colliding with each other and
        # with every other principal in the store. With none, a token whose
        # claim is `svc:ci` IS the principal `svc:ci`.
        raise _err(index, entry, "'subject_template' must start with a literal prefix "
                                 "or {iss}, e.g. 'k8s:{sub}', so the subjects it "
                                 "produces cannot be another principal's")


def _check_uri(uri: str, index: int, entry: dict) -> None:
    parts = urlsplit(uri)
    if parts.scheme == "https" and parts.netloc:
        return
    if parts.scheme == "file" and not parts.netloc and parts.path.startswith("/"):
        return
    raise _err(index, entry, f"'jwks_uri' must be https://... or file:///absolute/path, "
                             f"got {uri!r}")


def _one(entry: object, index: int) -> IssuerConfig:
    if not isinstance(entry, dict):
        raise _err(index, None, "must be a JSON object")
    unknown = sorted(set(entry) - _KEYS)
    if unknown:
        raise _err(index, entry, f"unknown key(s) {unknown}; accepted: {sorted(_KEYS)}")

    issuer = _string(entry, index, "issuer", required=True)
    assert issuer is not None

    aud = entry.get("audience")
    if isinstance(aud, str):
        aud = [aud]
    if (not isinstance(aud, list) or not aud
            or not all(isinstance(a, str) and a.strip() and a == a.strip() for a in aud)):
        # REQUIRED, with no default. The audience is what says a token was
        # issued FOR THIS CACHE; without it, any token the issuer mints for any
        # service -- a Kubernetes API token, a token for some other app -- would
        # be accepted here.
        raise _err(index, entry, "'audience' is required: a string or a non-empty "
                                 "list of strings naming this cache")

    algs = entry.get("algorithms", list(DEFAULT_ALGORITHMS))
    if not isinstance(algs, list) or not algs or not all(isinstance(a, str) for a in algs):
        raise _err(index, entry, "'algorithms' must be a non-empty list of strings")
    for alg in algs:
        if alg.lower() == "none":
            raise _err(index, entry, "'algorithms' must not contain 'none'")
        if alg.upper().startswith("HS"):
            raise _err(index, entry, f"'algorithms' contains {alg!r}: HMAC algorithms "
                                     "are not supported; this cache verifies "
                                     "asymmetric signatures only")
        if alg not in ALGORITHMS:
            raise _err(index, entry, f"'algorithms' contains unknown {alg!r}; accepted: "
                                     f"{list(ALGORITHMS)}")

    jwks_uri = _string(entry, index, "jwks_uri")
    if jwks_uri is not None:
        _check_uri(jwks_uri, index, entry)
    elif not issuer.startswith("https://"):
        # Discovery is fetched from the issuer URL, and the keys it names are
        # the whole trust decision. An issuer that is only an identifier (not
        # an https URL) can still be used -- with an explicit jwks_uri.
        raise _err(index, entry, "'issuer' is not an https URL, so its keys cannot be "
                                 "discovered from it; set 'jwks_uri'")

    template = _string(entry, index, "subject_template", required=True)
    assert template is not None
    _check_template(template, index, entry)

    claim = entry.get("subject_claim", "sub")
    if not isinstance(claim, str) or not claim:
        raise _err(index, entry, "'subject_claim' must be a claim name")

    auto = entry.get("auto_create", False)
    if not isinstance(auto, bool):
        raise _err(index, entry, "'auto_create' must be true or false")

    ca_file = _string(entry, index, "ca_file")
    token_file = _string(entry, index, "fetch_token_file")
    for name, path in (("ca_file", ca_file), ("fetch_token_file", token_file)):
        if path is not None and not path.startswith("/"):
            raise _err(index, entry, f"'{name}' must be an absolute path")
    if (ca_file or token_file) and jwks_uri is not None and jwks_uri.startswith("file:"):
        raise _err(index, entry, "'ca_file' and 'fetch_token_file' apply to fetching "
                                 "over https, and 'jwks_uri' is a file")

    return IssuerConfig(
        issuer=issuer,
        audiences=tuple(aud),
        subject_template=template,
        algorithms=tuple(dict.fromkeys(algs)),
        jwks_uri=jwks_uri,
        subject_claim=claim,
        auto_create=auto,
        leeway_s=_number(entry, index, "leeway_s", DEFAULT_LEEWAY_S, 0, MAX_LEEWAY_S),
        fetch_timeout_s=_number(entry, index, "fetch_timeout_s",
                                DEFAULT_FETCH_TIMEOUT_S, 0.1, 60),
        ca_file=ca_file,
        fetch_token_file=token_file,
    )


def parse(raw: str | None) -> tuple[IssuerConfig, ...]:
    """Parse XHC_JWT_ISSUERS. Unset or blank means the feature is OFF."""
    if raw is None or not raw.strip():
        return ()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JWTConfigError(f"XHC_JWT_ISSUERS is not valid JSON: {exc}") from exc
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        raise JWTConfigError("XHC_JWT_ISSUERS must be a JSON object or a non-empty "
                             "list of objects")
    issuers = tuple(_one(entry, i) for i, entry in enumerate(data))

    seen: set[str] = set()
    for cfg in issuers:
        if cfg.issuer in seen:
            raise JWTConfigError(f"XHC_JWT_ISSUERS names issuer {cfg.issuer!r} twice")
        seen.add(cfg.issuer)
    # NO TWO ISSUERS MAY PRODUCE THE SAME SUBJECT. Sufficient condition, checked
    # on the templates alone: neither issuer's literal prefix is a prefix of the
    # other's. Two subjects starting with prefixes that diverge cannot be equal,
    # whatever the claim values are.
    for i, a in enumerate(issuers):
        for b in issuers[i + 1:]:
            pa, pb = a.subject_prefix(), b.subject_prefix()
            if pa.startswith(pb) or pb.startswith(pa):
                raise JWTConfigError(
                    f"XHC_JWT_ISSUERS: issuers {a.issuer!r} and {b.issuer!r} have "
                    f"subject prefixes {pa!r} and {pb!r}, one a prefix of the other, "
                    "so a token from one could map to a principal of the other. Give "
                    "each a distinct prefix, e.g. 'k8s:{sub}' and 'gha:{sub}'."
                )
    return issuers
