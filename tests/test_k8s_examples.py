"""The Kubernetes examples must stay true to the code they deploy.

An example is documentation that people copy verbatim, and nothing executes it
in this repository. A setting renamed in app/config.py leaves a manifest that
still applies cleanly, starts a pod, and silently ignores the stale variable --
a Muninn with its key gate or its state volume quietly not configured, and no
error anywhere. So the manifests are checked against the code here, where a
mismatch goes red, rather than discovered in someone's cluster.

What this asserts, and what it does not:
  - every XHC_* name in examples/k8s (manifests, scripts AND prose) is a
    variable app/config.py reads and the README names;
  - every path the cache container is told to write to is on a mounted volume,
    because the root filesystem is read-only;
  - the security posture and the Xet placement are what the README says;
  - the prewarm script's final states are the job states app/jobs.py defines.
It does NOT prove the manifests are valid Kubernetes: that is kubectl's job
(`kubectl apply --dry-run=client -k examples/k8s`), run outside this suite.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import get_args

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "examples" / "k8s"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_docs_name_their_knobs import _knobs  # the same mapping the docs tests use

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "Job"}


def _docs() -> list[tuple[Path, dict]]:
    out = []
    for path in sorted(K8S.rglob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                out.append((path, doc))
    return out


def _workloads() -> list[tuple[Path, dict]]:
    return [(p, d) for p, d in _docs() if d.get("kind") in WORKLOAD_KINDS]


def _pod_spec(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]


def _containers(doc: dict) -> list[dict]:
    spec = _pod_spec(doc)
    return list(spec.get("initContainers") or []) + list(spec.get("containers") or [])


def _env(container: dict) -> dict[str, str | None]:
    return {e["name"]: e.get("value") for e in container.get("env") or []}


def _muninn_container() -> dict:
    (sts,) = [d for _, d in _workloads() if d["kind"] == "StatefulSet"]
    (c,) = [c for c in _pod_spec(sts)["containers"] if c["name"] == "muninn"]
    return c


def test_the_sweep_sees_the_files_it_polices():
    """Negative control: a moved directory must not turn every check vacuous."""
    kinds = {d["kind"] for _, d in _docs()}
    assert {"StatefulSet", "Service", "NetworkPolicy", "Job", "Deployment"} <= kinds, kinds
    assert sum(n.startswith("XHC_") for n in _env(_muninn_container())) >= 8
    assert len(_referenced_xhc_names()) >= 8
    assert len(_knobs()) > 40


def test_every_yaml_file_parses():
    for path in sorted(K8S.rglob("*.yaml")):
        list(yaml.safe_load_all(path.read_text()))


def _referenced_xhc_names() -> dict[str, set[str]]:
    """Every XHC_ name anywhere under examples/k8s, prose included.

    Prose counts: a comment telling the reader to set a variable that does not
    exist is the same defect as a manifest setting it.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(K8S.rglob("*")):
        if path.is_file() and path.suffix in (".yaml", ".py", ".md", ".txt"):
            for name in re.findall(r"\bXHC_[A-Z0-9_]*[A-Z0-9]\b", path.read_text()):
                found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_xhc_variable_the_examples_name_is_a_real_setting():
    knobs = _knobs()
    referenced = _referenced_xhc_names()
    assert referenced, "found no XHC_ names at all; the sweep is broken"
    bogus = {n: sorted(where) for n, where in referenced.items() if n not in knobs}
    assert not bogus, (
        "examples/k8s names settings app/config.py does not read, so a copy of "
        f"them is silently ignored: {bogus}"
    )


def test_every_xhc_variable_the_manifests_set_is_documented():
    readme = (ROOT / "README.md").read_text()
    set_names = {n for _, d in _workloads() for c in _containers(d) for n in _env(c)
                 if n.startswith("XHC_")}
    missing = sorted(n for n in set_names if n not in readme)
    assert not missing, f"the manifests set variables the README never names: {missing}"


def _mounts(container: dict) -> list[str]:
    return [m["mountPath"] for m in container.get("volumeMounts") or []]


def _under(path: str, mounts: list[str]) -> bool:
    return any(path == m or path.startswith(m.rstrip("/") + "/") for m in mounts)


def test_every_path_the_cache_writes_is_on_a_mounted_volume():
    """The root filesystem is read-only, so a path off every volume is a
    PermissionError at the first write -- or, for the key store and state, a
    refusal to start."""
    c = _muninn_container()
    env = _env(c)
    mounts = _mounts(c)
    for var in ("HF_HUB_CACHE", "HF_XET_CACHE", "XHC_STATE_DIR", "XHC_AUTHZ_DB",
                "HOME", "HF_HOME"):
        assert env.get(var), f"{var} is not set on the cache container"
        assert _under(env[var], mounts), f"{var}={env[var]} is not under any mount {mounts}"
    # The OCI store's default directory is not mounted, so it must be off.
    assert env.get("XHC_DOCKER_ENABLED") == "0" or _under(
        env.get("XHC_DOCKER_DIR", "/docker"), mounts)


def test_state_and_key_store_share_the_durable_volume_not_the_blob_volume():
    c = _muninn_container()
    env = _env(c)
    assert env["XHC_AUTHZ_DB"].startswith(env["XHC_STATE_DIR"].rstrip("/") + "/")
    assert not _under(env["XHC_STATE_DIR"], [env["HF_HUB_CACHE"]])


def test_every_volume_mount_names_a_declared_volume():
    for path, doc in _workloads():
        spec = _pod_spec(doc)
        declared = {v["name"] for v in spec.get("volumes") or []}
        declared |= {t["metadata"]["name"] for t in doc["spec"].get("volumeClaimTemplates") or []}
        for c in _containers(doc):
            for m in c.get("volumeMounts") or []:
                assert m["name"] in declared, f"{path.name}: {c['name']} mounts undeclared {m['name']}"


def test_xet_is_off_on_the_model_pod_and_never_on_the_cache():
    assert "HF_HUB_DISABLE_XET" not in _env(_muninn_container()), (
        "HF_HUB_DISABLE_XET on the cache is the single-stream failure mode")
    (dep,) = [d for _, d in _workloads() if d["kind"] == "Deployment"]
    env = _env(_pod_spec(dep)["containers"][0])
    assert env.get("HF_HUB_DISABLE_XET") == "1"
    assert env.get("HF_ENDPOINT", "").startswith("http://muninn.")
    assert int(env["HF_HUB_DOWNLOAD_TIMEOUT"]) > 10


def test_every_muninn_image_container_is_locked_down():
    for path, doc in _workloads():
        pod = _pod_spec(doc)
        for c in _containers(doc):
            if not c["image"].startswith("ghcr.io/skibare87/muninn"):
                continue
            sc = c.get("securityContext") or {}
            assert sc.get("readOnlyRootFilesystem") is True, f"{path.name}:{c['name']}"
            assert sc.get("allowPrivilegeEscalation") is False, f"{path.name}:{c['name']}"
            assert sc.get("capabilities", {}).get("drop") == ["ALL"], f"{path.name}:{c['name']}"
            assert pod["securityContext"]["runAsNonRoot"] is True, path.name
            assert pod.get("automountServiceAccountToken") is False, path.name


def test_probes_use_the_only_health_endpoint():
    c = _muninn_container()
    ports = {p["name"] for p in c["ports"]}
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        get = c[probe]["httpGet"]
        assert get["path"] == "/healthz", probe
        assert get["port"] in ports, probe


def test_prewarm_final_states_are_the_job_states_the_code_defines():
    from app.jobs import ACTIVE_STATES, JobState

    ns: dict = {}
    src = (K8S / "prewarm" / "prewarm.py").read_text()
    exec(compile(re.search(r"^FINAL = .*$", src, re.M).group(0), "prewarm.py", "exec"), ns)
    assert ns["FINAL"] == set(get_args(JobState)) - set(ACTIVE_STATES)


def test_kustomization_lists_real_files_and_never_the_placeholder_secret():
    for kfile in K8S.rglob("kustomization.yaml"):
        k = yaml.safe_load(kfile.read_text())
        for res in k.get("resources", []):
            assert (kfile.parent / res).is_file(), f"{kfile}: {res} does not exist"
            assert "secret-template" not in res, "the placeholder Secret must not be applied"
        for gen in k.get("configMapGenerator", []):
            for f in gen.get("files", []):
                assert (kfile.parent / f.split("=", 1)[-1]).is_file(), f"{kfile}: {f}"


@pytest.mark.parametrize("path", sorted(K8S.rglob("*.py")), ids=lambda p: p.name)
def test_the_scripts_compile(path):
    compile(path.read_text(), str(path), "exec")


def test_the_examples_carry_no_deployment_specifics():
    """Public repository: no one machine's paths or sizes, no pinned release,
    no host-path volumes that only make sense on one node."""
    for path in sorted(K8S.rglob("*")):
        if not path.is_file() or path.suffix not in (".yaml", ".py", ".md", ".txt"):
            continue
        text = path.read_text()
        for leak in ("80T", "70T", "/mnt/nvme", "NAS", "hostPath"):
            assert leak not in text, f"{path.name} carries {leak!r}"
        assert not re.findall(r"muninn:\d+\.\d+", text), f"{path.name} pins a release"
