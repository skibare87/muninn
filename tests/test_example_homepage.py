"""The shipped example homepage, as an artefact rather than as a route.

test_web_root.py proves the SERVING is right -- precedence, containment,
traversal. This proves the page we actually ship is safe to put on a public
hostname, which is a different claim and one nothing else checks.

It matters because this page is the one surface of Muninn that an anonymous
stranger loads in a browser, and because every regression available here is
silent: a web font renders identically whether it comes from disk or from a
third party that now has every visitor's IP.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "examples" / "web-root"
PAGE = WEB / "index.html"
# The key-management surface is its OWN PAGE. It used to be a section at the
# bottom of the homepage, which is somewhere nobody looks -- the report that
# moved it was "I have to scroll down a mile to find it".
CONSOLE = WEB / "console" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return PAGE.read_text()


@pytest.fixture(scope="module")
def console_html() -> str:
    return CONSOLE.read_text()


@pytest.fixture(scope="module")
def console_script(console_html: str) -> str:
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", console_html, re.S)
    assert len(scripts) == 1
    return _strip_comments(scripts[0])


@pytest.fixture(scope="module")
def script_source(html: str) -> str:
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
    assert len(scripts) == 1, "one inline script expected"
    return scripts[0]


def _strip_comments(js: str) -> str:
    """Remove // and /* */ comments, leaving string literals intact.

    Written because the first version of this file asserted "innerHTML" was
    absent from the script and failed -- on the COMMENT explaining why innerHTML
    is never used. A ban that trips on its own documentation is a ban nobody can
    document, and the fix is not to stop writing the comment.

    Tracks quotes so a // inside a string is not mistaken for a comment. Regex
    literals are not tracked, which is safe here and asserted below rather than
    assumed: no regex in this page contains // or /*.
    """
    out = []
    i, n = 0, len(js)
    quote = None
    while i < n:
        ch = js[i]
        if quote:
            if ch == "\\":
                out.append(js[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
            out.append(ch)
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if js.startswith("//", i):
            i = js.find("\n", i)
            if i == -1:
                break
            continue
        if js.startswith("/*", i):
            end = js.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@pytest.fixture(scope="module")
def script(script_source: str) -> str:
    """The script with comments removed -- what actually EXECUTES."""
    stripped = _strip_comments(script_source)
    # The stripper's own assumption, checked rather than trusted: no regex
    # literal in this page contains a comment opener, so not tracking regex
    # literals cannot swallow code.
    for literal in re.findall(r"(?<==\s)/[^/\n]+/[gimsuy]*", script_source):
        assert "//" not in literal and "/*" not in literal, literal
    return stripped


def test_the_page_loads_nothing_from_a_third_party(html: str):
    """A cache whose purpose is to stop machines reaching the internet must not
    need the internet to draw its own homepage -- and on a public host, every
    external asset is a third party silently collecting visitor IPs.

    Links out are fine; SUBRESOURCES are not. The distinction is that a reader
    chooses to follow a link.
    """
    for attr, value in re.findall(r'\b(src|action|formaction)\s*=\s*"([^"]*)"', html):
        assert not value.startswith(("http://", "https://", "//")), f"{attr}={value}"

    for value in re.findall(r'<link[^>]*\bhref\s*=\s*"([^"]*)"', html):
        assert not value.startswith(("http://", "https://", "//")), value

    # @import and url() in CSS reach the network just as effectively as <link>.
    assert "@import" not in html
    for url in re.findall(r"url\(\s*['\"]?([^)'\"]+)", html):
        assert not url.startswith(("http://", "https://", "//")), url


def test_no_script_reaches_a_remote_origin(script: str):
    """fetch/XHR to somewhere else would exfiltrate just as well as a <script
    src>, and is easier to miss in review because it is not in the markup."""
    for url in re.findall(r"""fetch\(\s*['"]([^'"]+)""", script):
        assert url.startswith("/"), f"fetch to a non-relative URL: {url}"
    assert "XMLHttpRequest" not in script
    assert "WebSocket" not in script


def test_console_user_strings_are_never_written_as_html(console_script: str):
    """Same rule, and the console is where it matters most: an admin reading the
    user list is the high-privilege reader an injected script wants."""
    for banned in ("innerHTML", "outerHTML", "document.write", "insertAdjacentHTML"):
        assert banned not in console_script, banned
    assert not re.search(r"\beval\s*\(", console_script)


def test_console_reaches_no_remote_origin(console_script: str, console_html: str):
    for url in re.findall(r"""fetch\(\s*['"]([^'"]+)""", console_script):
        assert url.startswith("/"), url
    for attr, value in re.findall(r'\b(src|href)\s*=\s*"([^"]*)"', console_html):
        assert not value.startswith(("http://", "https://", "//")), f"{attr}={value}"


def test_user_controlled_strings_are_never_written_as_html(script: str):
    """THE XSS BOUNDARY, and it is enforced structurally rather than by review.

    Labels, emails, subjects and rule patterns are all user-written and all
    rendered into this page. An admin viewing the user list is precisely the
    high-privilege reader an injected script wants, so the rule is that nothing
    is ever assigned to innerHTML or passed to document.write -- text goes in
    through textContent, which cannot create an element.
    """
    assert "innerHTML" not in script
    assert "outerHTML" not in script
    assert "document.write" not in script
    assert "insertAdjacentHTML" not in script
    # eval and Function are not XSS themselves but make the above unenforceable
    assert not re.search(r"\beval\s*\(", script)
    assert not re.search(r"\bnew\s+Function\s*\(", script)


def test_the_secret_is_never_put_where_a_shell_would_record_it(script: str):
    """The reveal panel suggests a docker login. `echo <secret> | docker login`
    is the obvious form and writes the credential into the user's shell
    history, which is exactly the habit a credential service should not teach.
    """
    assert "--password-stdin" not in script
    assert not re.search(r"echo\s*'\s*\+\s*k\.secret", script)


def test_the_login_button_points_at_the_apex_and_takes_no_destination(script: str):
    """Matt's instruction: the homepage has a login button that runs through
    OAuth. No subdomain, and specifically no `next`/`redirect_uri` parameter --
    a destination taken from the URL is how an OAuth callback becomes an open
    redirect, which hands the authorisation code to whoever supplied it.
    """
    assert "/_auth/login" in script
    assert "admin." not in script, "no management subdomain"
    hrefs = re.findall(r"\.href\s*=\s*'(/_auth/[^']*)'", script)
    assert hrefs == ["/_auth/login"], hrefs
    assert "?" not in hrefs[0]


def test_the_console_is_a_separate_page_not_a_homepage_section(html: str, console_html: str):
    """A management surface at the bottom of a marketing page is somewhere
    nobody looks. The homepage links to it; it does not contain it."""
    assert CONSOLE.is_file()
    assert 'href="/console"' in html
    for owned_by_console in ('id="keys"', 'id="users"', 'id="new-key"', "_console/"):
        assert owned_by_console not in html, f"homepage still carries {owned_by_console}"
        assert owned_by_console in console_html, f"console page lacks {owned_by_console}"


def test_hidden_beats_display_on_both_pages(html: str, console_html: str):
    """`[hidden]` is only a UA-stylesheet rule, so ANY `display` declaration
    overrides it. Both pages set display on classes that are also hidden
    (.who, .cta, .row), so without an explicit rule an element the script means
    to keep hidden is silently visible."""
    for name, doc in (("homepage", html), ("console", console_html)):
        assert re.search(r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", doc), name


def test_the_console_distinguishes_its_three_states(console_script: str):
    """Signed in, signed out, and no-login-configured are three different
    things. Collapsing them renders a blank page, which is what made the first
    version look broken rather than logged out."""
    for element_id in ("no-login", "signed-out", "console"):
        assert f"$('{element_id}')" in console_script, element_id


def test_the_console_surfaces_failures_instead_of_swallowing_them(console_script: str):
    """The first version ended loadMe() with an empty catch, so a console that
    failed to load was indistinguishable from one that was merely logged out --
    and being logged out is the state the user is trying to leave."""
    assert "showError" in console_script
    tail = console_script[console_script.index("function loadMe"):]
    assert "showError" in tail, "loadMe must report its own failure"


def test_logout_is_a_post(console_script: str):
    """A GET logout fires from any page that embeds an image pointing at it."""
    assert "/_auth/logout" in console_script
    assert re.search(r"f\.method\s*=\s*'POST'", console_script)


def test_the_console_is_hidden_until_the_server_says_there_is_a_login(html: str):
    """A deployment with no identity provider shows no sign-in button, rather
    than one that leads to a 404. The server decides, via /_auth/me; the page
    starts hidden so the default is the quiet one."""
    for element_id in ("who", "signin", "goconsole"):
        assert re.search(rf'id="{element_id}"[^>]*\shidden', html), f"homepage: {element_id}"


def test_the_page_is_still_a_homepage(html: str):
    """Guards against the console quietly becoming the whole page. This is the
    public face of the project first; the management UI is a section of it."""
    assert "<h1>" in html
    for phrase in ("pull-through cache", "Hugging Face", "OCI"):
        assert phrase in html, phrase


def test_the_comment_stripper_does_not_eat_code(script_source: str, script: str):
    """A stripper that returned "" would make every absence assertion above pass
    vacuously. This is the positive control for the fixture itself.
    """
    assert len(script) > 0.4 * len(script_source), "stripper removed too much"
    for must_survive in (
        "/_auth/me",
        "/_auth/login",
        "/console",
        "credentials: 'same-origin'",
    ):
        assert must_survive in script, must_survive
    assert "// This page is a homepage." not in script, "stripper removed nothing"


# ---------------------------------------------------------------------------
# Brand assets.
#
# The web root CLAIMS the paths it holds and falls through to the upstream proxy
# for the ones it does not. That makes a missing asset invisible in a specific
# and embarrassing way: a browser asking for /favicon.ico gets HUGGING FACE'S
# favicon, 200, 200 kB, and every tab wears someone else's identity.
#
# The same mechanism makes a typo'd <link href> silent -- the browser gets the
# upstream's 404 page with a 404 status and simply shows no icon.
# ---------------------------------------------------------------------------

WEB_ROOT = PAGE.parent


def _resolves(ref: str) -> bool:
    """Does this reference resolve the way the SERVER resolves it?

    Mirrors _web_root_file: a file serves directly, and a DIRECTORY serves its
    index.html. Testing `is_file()` alone would have called /console broken
    while the server serves it correctly -- a check that disagrees with the
    thing it checks.
    """
    target = WEB_ROOT / ref.lstrip("/")
    return target.is_file() or (target / "index.html").is_file()


def test_a_favicon_is_shipped_at_the_root():
    """Browsers request /favicon.ico without being told to. If it is absent it
    is proxied upstream, so this file existing is what stops the tab showing
    another project's icon."""
    assert (WEB_ROOT / "favicon.ico").is_file()
    assert (WEB_ROOT / "apple-touch-icon.png").is_file()


def test_every_local_asset_the_page_references_actually_exists(html: str):
    """A broken reference here does not 404 visibly -- it falls through to the
    upstream proxy. So the failure looks like 'the icon just does not show up',
    with nothing in any log pointing at a missing file.
    """
    refs = set(re.findall(r'\b(?:src|href|content)\s*=\s*"(/[^"]*)"', html))
    # Application routes are served by the app, not from the web root.
    refs = {r for r in refs if not r.startswith(("/_auth", "/_console", "/v2", "/_cache"))}
    assert refs, "expected the page to reference local assets"
    assert not (missing := sorted(r for r in refs if r != "/" and not _resolves(r))), \
        f"referenced but absent from the web root: {missing}"


def test_every_local_asset_the_console_references_exists(console_html: str):
    refs = set(re.findall(r'\b(?:src|href)\s*=\s*"(/[^"]*)"', console_html))
    refs = {r for r in refs if not r.startswith(("/_auth", "/_console", "/v2", "/_cache"))}
    missing = sorted(r for r in refs if r != "/" and not (WEB / r.lstrip("/")).is_file())
    assert not missing, f"referenced but absent: {missing}"


def test_the_mark_sits_on_a_paper_surface(html: str):
    """The mark is ink on transparency. On the dark theme it would vanish into
    the background, so every place it appears is backed by the palette's paper
    colour -- which is also what sumi-e actually is."""
    for cls in ("chip", "art"):
        # findall, not search: a class legitimately has several rule blocks (a
        # base one and a media-query override), and search returns whichever
        # comes first in the file rather than the one that sets the background.
        blocks = re.findall(rf"\.{cls}\{{[^}}]*\}}", html)
        assert blocks, cls
        assert any("var(--paper)" in b for b in blocks), f".{cls} must sit on paper"


def test_the_brand_palette_is_the_published_one(html: str):
    """ink #1C222B, gold #C7A764, paper #E1DED1 -- as published with the mark.
    A landing page drifting off the kit is how two 'official' palettes start."""
    root = re.search(r":root\{(.*?)\}", html, re.S)
    assert root
    for name, value in (("--ink", "#1C222B"), ("--gold", "#C7A764"), ("--paper", "#E1DED1")):
        assert f"{name}:{value}" in root.group(1).replace(" ", ""), f"{name} must be {value}"


def test_the_page_does_not_ship_a_multi_megabyte_hero():
    """The originals in images/ are 1-2 MB each. The pre-sized brand icons exist
    so a homepage does not ship one."""
    for f in WEB_ROOT.rglob("*"):
        if f.is_file():
            assert f.stat().st_size < 200_000, f"{f.name} is {f.stat().st_size} bytes"
