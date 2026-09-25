# Muninn on Kubernetes

Plain manifests plus a `kustomization.yaml`, with no Helm. They run one Muninn in-cluster
as a pull-through Hugging Face cache for model pods. Every pod needs a Muninn key, and the
key's rules decide which repositories it may pull.

These are a starting point, not someone's deployment. Every size below is a knob. Set it
from your own models, disks and RAM.

## Files

| file | what it is |
|---|---|
| `namespace.yaml` | Optional. The `muninn` namespace, with the `restricted` Pod Security Standard enforced. |
| `secret-template.yaml` | **Template only, never applied.** Shows the `muninn-upstream` Secret: the cache's own `HF_TOKEN` and `XHC_MANAGE_TOKEN`. |
| `rbac.yaml` | ServiceAccount, Role and RoleBinding for the provisioning init container. |
| `provision.py` | The init container's script. It sets up the principal, its rules and the model pods' key, then publishes the key as a Secret. |
| `provision-rules.txt` | What the model pods' key may pull. This file is the source of truth for those rules. |
| `statefulset.yaml` | Muninn itself: one replica, a state PVC, a blob PVC, probes, resources and the security context. |
| `service.yaml` | ClusterIP Service `muninn` on port 8080. |
| `networkpolicy.yaml` | Who may connect. **Decorative unless your CNI enforces NetworkPolicy**; see below. |
| `kustomization.yaml` | Ties the above together and pins the image tag. |
| `prewarm/` | Its own kustomization: a Job that prewarms one repo at a pinned commit and fails unless the result is `done`. |
| `model-pod-example.yaml` | A Deployment excerpt showing the environment a model pod needs. Not applied. |

## Apply, in this order

1. **Pin the image.** In `kustomization.yaml` and `prewarm/kustomization.yaml`, replace
   `SET-ME-X.Y.Z` with a release from `CHANGELOG.md` or the repository tags, or use
   `digest:` with the digest you actually pulled. The placeholder is meant to fail at pull.
2. **Edit what the pods may pull.** Change `provision-rules.txt`, and make
   `XHC_ALLOW_REPOS` in `statefulset.yaml` cover the same repositories.
3. **Create the namespace and the upstream Secret**, from real values:

   ```bash
   kubectl apply -f examples/k8s/namespace.yaml
   kubectl -n muninn create secret generic muninn-upstream \
     --from-literal=HF_TOKEN="$YOUR_HUB_TOKEN" \
     --from-literal=XHC_MANAGE_TOKEN="$(openssl rand -hex 32)"
   ```

4. **Apply the cache:**

   ```bash
   kubectl apply -k examples/k8s
   kubectl -n muninn rollout status statefulset/muninn
   kubectl -n muninn get secret muninn-pod-key     # written by the init container
   ```

5. **Prewarm**, and gate your model rollout on the Job:

   ```bash
   kubectl apply -k examples/k8s/prewarm
   kubectl -n muninn wait --for=condition=complete job/muninn-prewarm --timeout=6h
   ```

6. **Point model pods at it.** Copy the environment from `model-pod-example.yaml`.

## Verify

Run these from a pod labelled `muninn-client: "true"` in the namespace. The URL is
`http://muninn.muninn.svc:8080`, and `$KEY` is the `HF_TOKEN` value from the
`muninn-pod-key` Secret (`key_id:secret`). Do not echo it.

```bash
U=http://muninn.muninn.svc:8080
C=7ae557604adf67be50417f59c2c2f167def9a775

# no key: 401
curl -s -o /dev/null -w '%{http_code}\n' $U/Qwen/Qwen2.5-0.5B-Instruct/resolve/$C/config.json

# the pod key, a repo its rules allow: 200
curl -s -o /dev/null -w '%{http_code} %header{x-xhc-cache}\n' \
  -H "Authorization: Bearer $KEY" $U/Qwen/Qwen2.5-0.5B-Instruct/resolve/$C/config.json

# the pod key, a repo its rules do not allow: 403, X-Error-Code: GatedRepo, and a
# message naming the key and the repository
curl -s -D - -o /dev/null -H "Authorization: Bearer $KEY" \
  $U/openai-community/gpt2/resolve/main/config.json | grep -iE '^HTTP|x-error'

# health needs no key: 200 {"ok":true,"free_bytes":...}
curl -s $U/healthz
```

To check the NetworkPolicy, run the same `/healthz` curl from a pod **without** the label.
If your CNI enforces the policy, it times out. If it answers, the policy is not being
enforced.

## Decisions

### Provisioning: an init container, not a Job

The key store is a SQLite file on the `state` volume, which is ReadWriteOnce and attached
to the Muninn pod. A separate Job can reach it in two ways. It can mount that volume, which
only works on the same node and races the server. Or it can go over HTTP with
`XHC_MANAGE_TOKEN`, and that token can also create an administrator. The init container
has neither problem: it runs `python -m app.authzctl` against the database file before the
server starts. It uses the same image and needs no manage token.

`authzctl mint` is not idempotent, and an init container runs on every pod start. So
`provision.py` checks the Secret before it mints:

| on this start | action |
|---|---|
| Secret `muninn-pod-key` absent | delete earlier keys under this label (no one holds their secrets), mint one, create the Secret |
| Secret present, key live | nothing |
| Secret present, key **disabled** | nothing. The revocation stands; re-minting would undo it. |
| Secret present, key not in the database | the state volume was replaced: mint a key and replace the Secret |
| any other answer from the API | exit non-zero and mint nothing. An unknown answer is not treated as "absent". |

The principal is created with `--exist-ok`. Rules are replaced from `provision-rules.txt`
on every start. The secret never appears in argv, stdout or logs: `authzctl` writes it to a
0600 file on a memory-backed emptyDir, and the script reads it into the Secret body and
unlinks the file. Only the key id is logged, and the key id is the username, not a
credential.

**To rotate:** `kubectl -n muninn delete secret muninn-pod-key`, then restart the Muninn
pod, then restart the model pods. The old key is deleted when the new one is minted.

**The RBAC it needs** (`rbac.yaml`):

- `get` and `update` on the single Secret `muninn-pod-key`.
- `create` on Secrets in the namespace. Kubernetes cannot restrict `create` by name,
  because the name is not known until the object is submitted. So this grant covers the
  whole namespace. It can create a Secret, but it cannot read, list, change or delete any
  other. **Keep the namespace for Muninn only.**

Only the init container gets that credential. The pod sets
`automountServiceAccountToken: false`. The token is projected into a volume that only the
init container mounts, so the serving container, which answers every request from the
cluster, holds no Kubernetes credential.

This was chosen over the alternative of minting by hand and pasting the result into
`kubectl create secret`. The by-hand route needs no RBAC, but it puts the secret through
someone's terminal and shell history, and it cannot repair itself when the state volume is
replaced.

#### Model pods in another namespace

A pod can only reference a Secret in its own namespace. To serve pods in a namespace
`models`:

1. Move the Role and RoleBinding into `models`. The binding's subject stays the
   `muninn` ServiceAccount in `muninn`.
2. Set `PROVISION_NAMESPACE: models` on the `provision` init container. By default it
   writes the Secret into its own namespace.
3. Set `HF_ENDPOINT=http://muninn.muninn.svc:8080` in the pods.

### Security context

- **Non-root (uid/gid 10001).** The image declares no `USER`, so by default it runs as
  root. It does not need root: it binds 8080 and writes only to its volumes. The pod sets
  `runAsNonRoot`, and `fsGroup` makes the volumes writable.
- **`fsGroupChangePolicy: OnRootMismatch`.** Without it, the kubelet re-chowns every file
  on every volume at every start, before any container or probe runs. On a large cache that
  takes minutes to hours and looks like a hang.
- **`readOnlyRootFilesystem: true`.** This was measured with the image, non-root and
  read-only, using this environment. Serving, a streamed miss, a verified prewarm and
  `authzctl` wrote only to the mounted volumes. `hf_xet` writes its logs under
  `HF_XET_CACHE`. `HOME` and `HF_HOME` point at the scratch volume in case some other code
  path writes there. The OCI registry is **off** (`XHC_DOCKER_ENABLED=0`) because its store,
  `/docker`, would be on the read-only root. To enable it, mount a volume at `/docker`.
- **All capabilities dropped, no privilege escalation, `RuntimeDefault` seccomp.** With
  these settings the pod meets the `restricted` Pod Security Standard, and `namespace.yaml`
  enforces that standard.

### Volumes, and what is lost when each one goes

- **`state`** (small PVC) holds `XHC_STATE_DIR` (pins, orphan marks, runtime policy, job
  ledger) and `XHC_AUTHZ_DB` (principals, keys, rules). If it is lost, every model pod is
  locked out until the next start re-mints a key, and all pins are gone. Use a replicated or
  backed-up StorageClass.
- **`cache`** (large PVC) holds the blobs. If it is lost, each blob is **re-fetched** on its
  next request. **No pin, key or policy is lost.** The prewarm listings behind `complete` on
  `/_cache/repos` are also gone (they report `null` until re-prewarmed), and so is the
  regenerable dataset-metadata cache. **The exception is retained orphans.** Under
  `XHC_ORPHAN_POLICY=retain`, a repo deleted from the Hub is kept because this is the only
  copy. Its mark survives on `state`, but its bytes cannot be fetched again. If you retain
  orphans as an archive, this volume is not disposable.
- **`xet`** (emptyDir) holds `hf_xet` chunk scratch and logs. Losing it costs nothing.

To put the blobs on local NVMe, use a local-volume StorageClass for the `cache` claim, or
replace the claim with the commented emptyDir in `statefulset.yaml`. If you use an emptyDir,
set `XHC_CACHE_MAX_SIZE` below its `sizeLimit`, because `statfs` reports the whole node
disk.

### Memory

These are measurements from one deployment, not guarantees. They were taken with
`hf_xet` 1.6.0 through `huggingface_hub` 0.34.4:

- **With Xet** (the default, and the fast ingest path), peak memory was about 2 to 2.5 GiB
  per concurrent ingest. Above a few GB, it did not grow with file size.
- **With `HF_HUB_DISABLE_XET=1` on the cache**, peak memory was about 50 MiB, at roughly
  60% of the throughput in one measurement. The README calls disabling Xet on the cache the
  single-stream failure mode, so treat this as a trade-off to measure, not a default.

**The figure is per file in flight, and it climbs across a snapshot.** A four-file 15 GB
snapshot peaked at about 3.0 GiB with one file in flight and 4.4 GiB with eight.
`XHC_INGEST_CONCURRENCY` bounds jobs; `XHC_SNAPSHOT_MAX_WORKERS` (default 1) bounds files
within one snapshot job, and the two multiply.

Hence the memory limit is **>= 3 GiB × `XHC_INGEST_CONCURRENCY` at one file in flight,
plus headroom**. The example uses 2 × 3 + 1 = 7 GiB, with the request equal to the limit.
Muninn logs a warning at startup when the cgroup limit looks too small for its settings.
See "Sizing memory for ingest" in the top-level README for every measurement.

### Probes

All three probes hit `/healthz`. It needs no key, even with `XHC_HF_AUTH=key`. It returns
`200 {"ok": true, "free_bytes": N}`, or `503` if it cannot stat the cache volume. The check
is a `statvfs`, not a scan, so it is cheap at any size.

- **startup**: 10 s × 60, so the process has 10 minutes to come up.
- **readiness**: 10 s × 3.
- **liveness**: 30 s × 6, about 3 minutes. It is generous because a restart kills every
  ingest in flight.

`/metrics` is deliberately not probed. Alert on the scrape instead.

### NetworkPolicy: decorative unless enforced

The API server accepts a NetworkPolicy on every cluster. Whether any packet is ever dropped
depends on the CNI. **`XHC_HF_AUTH=key` with per-key rules is the control that holds
regardless**: no key gets 401, and a key gets only the repositories its rules name, on hits
as well as misses. The policy only narrows who can connect.

Egress allows DNS and TCP 443 and 6443 to anywhere. The Hub and its CDNs have no stable
address list, and the init container needs to reach the Kubernetes API.

### The manage token is a key-minting credential

With `XHC_AUTHZ_DB` set, `XHC_MANAGE_TOKEN` also opens `/_cache/authz`, which can create
administrators and mint keys. Muninn has one token for the whole `/_cache` surface. The
prewarm Job therefore holds a credential far broader than prewarming needs. Keep that Job
in the Muninn namespace, and never put the token in a manifest.

## Client-side limits (model pods)

- **`HF_HUB_DISABLE_XET=1` on the pods and never on the cache.** With Xet off,
  `huggingface_hub` refuses any single file larger than `MAX_HTTP_DOWNLOAD_SIZE` before
  it sends a request. In 0.34.4, `constants.py` line 39 is
  `MAX_HTTP_DOWNLOAD_SIZE = 50 * 1000 * 1000 * 1000  # 50 GB`, and `file_download.py`
  line 414 raises on `expected_size > constants.MAX_HTTP_DOWNLOAD_SIZE`. The limit is
  decimal, so the ceiling is 46.57 GiB, and no environment variable changes it. Sharded
  weights are well below it. A single huge file, such as some GGUFs, is not. See
  "Two client-side limits" in the repository README.
- **`HF_HUB_DOWNLOAD_TIMEOUT=600`.** The default is 10 s. On a cold file, the first pod
  waits for the ingest to start and times out, while every later pod is served from disk.

## Validation in this repository

`tests/test_k8s_examples.py` loads these manifests and fails in these cases:

- any `XHC_*` name they mention, in YAML, scripts or prose, is not a setting
  `app/config.py` reads, or is not named in the README;
- a path the cache writes is not on a mounted volume;
- the security context loosens;
- Xet moves onto the cache;
- the prewarm script's final states drift from `app/jobs.py`.

`tests/test_k8s_provision.py` runs `provision.py` against a real `authzctl` and database,
with a fake Kubernetes API, through each case in the table above.

These tests do not prove the manifests are valid Kubernetes. To check that:

```bash
kubectl apply --dry-run=client -k examples/k8s
kubectl apply --dry-run=client -k examples/k8s/prewarm
```
