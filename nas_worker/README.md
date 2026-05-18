# PikPak NAS Worker

This is an optional download backend for `PikPak_Enhancement_Master.user.js`.
When enabled in the userscript settings, the browser still asks PikPak for
temporary download URLs, but the actual file transfer is performed by this
worker on the NAS.

## API

- `GET /health`
- `GET /config`
- `POST /config`
- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `POST /jobs/{job_id}/cancel`

All job endpoints require a token:

```text
X-PikPak-NAS-Token: <PIKPAK_NAS_TOKEN>
```

## Portainer Stack

Use one of these stack files:

- `portainer-stack.yml`: best when adding the stack from a Git repository in
  Portainer. It builds this directory with `Dockerfile`.
- `portainer-stack.bind-script.yml`: best when using Portainer Web editor. Copy
  `pikpak_nas_worker.py` to the NAS first, then bind mount it into the Python
  container.

Set these Portainer stack environment variables before deployment:

```text
PIKPAK_NAS_TOKEN=change-this-long-random-token
PIKPAK_NAS_PORT=18082
PIKPAK_NAS_DOWNLOAD_PATH=/share/Data/PikPak
PIKPAK_NAS_STATE_PATH=/share/Container/pikpak-nas-worker/state
PIKPAK_NAS_TEMP_DIR=/state/tmp
PIKPAK_NAS_CONCURRENCY=2
PIKPAK_NAS_MAX_CONCURRENCY=8
PIKPAK_NAS_JOB_CONCURRENCY=16
PIKPAK_NAS_MAX_ITEMS=200
PIKPAK_NAS_OVERWRITE=0
PIKPAK_NAS_ALLOWED_HOSTS=
PIKPAK_NAS_SEGMENTED=auto
PIKPAK_NAS_SEGMENTS=4
PIKPAK_NAS_SEGMENT_MIN_BYTES=67108864
PIKPAK_NAS_SEGMENT_MIN_SPLIT_BYTES=33554432
```

For Portainer Web editor, either replace `change-this-long-random-token`
directly in the YAML or add `PIKPAK_NAS_TOKEN` in the stack environment
variables. This token is required. The userscript `NAS Worker Token` setting
must use the same value.

For `portainer-stack.bind-script.yml`, also set:

```text
PIKPAK_NAS_WORKER_FILE=/share/Container/pikpak-nas-worker/pikpak_nas_worker.py
```

Verified path mapping on `QNAP-453D`:

```text
\\QNAP-453D\Container -> /share/Container -> /share/CACHEDEV2_DATA/Container
\\QNAP-453D\Data      -> /share/Data      -> /share/CACHEDEV4_DATA/Data
```

If Portainer rejects a `/share/...` path, confirm the real path over SSH:

```sh
readlink -f /share/Container
readlink -f /share/Data
getcfg Container path -f /etc/config/smb.conf
getcfg Data path -f /etc/config/smb.conf
```

After deployment, open:

```text
http://<NAS-IP>:18082/health
```

Then set the userscript NAS worker settings:

```text
NAS Worker URL: http://<NAS-IP>:18082
NAS Worker Token: <PIKPAK_NAS_TOKEN>
```

## Docker CLI

Build:

```sh
docker build -t pikpak-nas-worker .
```

Run:

```sh
docker run -d \
  --name pikpak-nas-worker \
  -p 18082:18082 \
  -e PIKPAK_NAS_TOKEN='change-this-long-token' \
  -e PIKPAK_NAS_CONCURRENCY=2 \
  -e PIKPAK_NAS_MAX_CONCURRENCY=8 \
  -e PIKPAK_NAS_JOB_CONCURRENCY=16 \
  -v /share/CACHEDEV4_DATA/Data/PikPak:/downloads \
  -v /share/CACHEDEV4_DATA/Container/pikpak-nas-worker:/state \
  --restart unless-stopped \
  pikpak-nas-worker
```

Then set the userscript NAS worker URL to:

```text
http://<NAS-IP>:18082
```

## Notes

- The worker stores files only under `PIKPAK_NAS_DOWNLOAD_ROOT`.
- Relative paths are sanitized. Absolute paths, drive letters, and `..` are
  rejected.
- Existing files are not overwritten by default. If an existing file has the
  expected size, the item is treated as already complete.
- `PIKPAK_NAS_CONCURRENCY` is the startup default for simultaneous file
  downloads. The userscript can change it at runtime before submitting jobs.
  `PIKPAK_NAS_MAX_CONCURRENCY` is the upper bound accepted by the worker.
  `PIKPAK_NAS_JOB_CONCURRENCY` controls how many submitted jobs may be active
  at once. The userscript batch size only controls how many files are grouped
  into a single job request.
- `PIKPAK_NAS_TEMP_DIR` stores `.part` and range segment files while a download is in
  progress. The default `/state/tmp` is suitable when the state bind mount is
  on SSD. Completed files are moved to `/downloads` only after all ranges pass
  size validation.
- Signed PikPak URLs are kept only in memory. The state file persists progress
  and results, but not the temporary URLs.
- `PIKPAK_NAS_ALLOWED_HOSTS` is optional. If set, it is a comma-separated host
  allowlist such as `*.mypikpak.com,*.pikpakdrive.com`.
- `PIKPAK_NAS_SEGMENTED=auto` enables segmented downloads only when the source
  supports HTTP Range and the file is at least `PIKPAK_NAS_SEGMENT_MIN_BYTES`.
  `PIKPAK_NAS_SEGMENTS` is the worker-side default concurrent Range connection
  count. The userscript can override it per download job with the `NAS Segment
  Connections` setting, capped to 1-8 by the worker. Segmented downloads start
  with long continuous ranges. When a connection finishes early, it splits the
  largest remaining active range and takes its tail, similar to IDM/aria2 style
  dynamic range splitting. `PIKPAK_NAS_SEGMENT_MIN_SPLIT_BYTES` prevents tiny
  tail splits. Use `off` to force the original single-stream behavior.
