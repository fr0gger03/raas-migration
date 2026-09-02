# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Repository Overview

A single-file CLI tool (`migration_script.py`) that interactively migrates Jobs and
Schedules between two SaltStack Config / Salt RAAS servers via the `eAPI` JSON-RPC
interface (`POST {url}/rpc`). Built with `uv`; dependencies are `requests` and
`questionary` (see `pyproject.toml`).

Run it with:
```bash
uv run migration_script.py
```
It will interactively prompt for source/target URL, username, and password.

## Current RAAS Endpoints

- **Source:** `https://raas.tom.lab` — resolves via `/etc/hosts` (or local DNS) to
  `10.10.10.30`, the RHEL 9 VM on `ubuntutomlab` (see
  `~/Documents/repos/homelab/docs/salt-master-config.md`). This is the active Salt
  RAAS instance; the old Docker-based RaaS (formerly on a laptop at `192.168.6.111`,
  then `ubuntutomlab` itself) was decommissioned 2026-06-07.
- **Target:** `10.40.7.240` — only reachable over VPN. Confirm the VPN is connected
  before running the script (`ping 10.40.7.240`).
- Both servers present a **self-signed cert** (`CN=localhost`, issued by SaltStack) —
  this is expected. `verify_ssl=False` in `SaltRAASClient` is intentional and correct;
  do not "fix" this by turning on cert verification.

## Known Issue (fixed): RaaS eAPI auth is HTTP Basic Auth, not a login RPC call

**Symptom:** The script fails immediately on login for both source and target.
It went through two failure modes while diagnosing this:
1. `403 Client Error: ... Missing xsrf argument in request` — looked like a pure
   connection failure, even though the host was reachable and reachable over TLS.
2. After fixing XSRF (below), `401 Client Error: Authentication failed: no
   Authorization header` — happened even with real, correct credentials.

**Root cause (two layered bugs):**
1. **XSRF**: SaltStack Config's `eAPI` runs on Tornado, which enforces XSRF
   protection on all `POST` requests, including `/rpc`. Any POST lacking a valid
   `_xsrf` cookie + matching `X-XSRFToken` header is rejected with `403 Forbidden`
   before credentials are ever evaluated.
2. **Auth mechanism**: The original script invented a `{"resource": "auth",
   "method": "login", "kwarg": {"username":..., "password":...}}` RPC call to
   "log in" — **this endpoint/method does not exist** in RaaS's real eAPI. Per the
   official docs (https://developer.broadcom.com/xapis/vmware-salt-raas/latest/
   and the Aria Automation Config API PDF), RaaS has no login RPC at all.
   Authentication is plain **HTTP Basic Auth**, sent with *every single request*
   (both the priming GET and every `/rpc` POST) — confirmed by the documented curl
   example:
   ```bash
   curl --user 'root:PASSWORD' --url 'https://localhost/rpc' \
     --data '{"resource": "sec", "method": "download_content", "kwarg": {"auto_ingest": true}}'
   ```
   and the XSRF priming example, which primes via `/account/login` while already
   sending Basic Auth:
   ```bash
   curl -k -c $HOME/eAPICookie.txt -u root:PASSWORD 'https://localhost/account/login' >/dev/null
   curl -k -u root:PASSWORD -b $HOME/eAPICookie.txt \
     -H "X-Xsrftoken: $(grep -w '_xsrf' $HOME/eAPICookie.txt | cut -f7)" \
     -X POST https://localhost/rpc -d '{"resource": ..., "method": ..., "kwarg": {...}}'
   ```

**Fix applied** (in `SaltRAASClient`, `migration_script.py`):
- `session.auth = (username, password)` is set in `__init__` so every request
  (GET and POST) carries HTTP Basic Auth.
- `_prime_xsrf()` now performs the priming `GET` against `{url}/account/login`
  (not `/`), matching the documented flow, and captures the `_xsrf` cookie into
  an `X-XSRFToken` header for subsequent `/rpc` calls.
- `login()` no longer POSTs a fake `auth.login` RPC. It primes XSRF and then
  makes one real, cheap authenticated call (`job.get_jobs`) to confirm the Basic
  Auth credentials are actually valid.
- `get_jobs`/`get_schedules`/`save_job`/`save_schedule` now use the **singular**
  resource names `job` and `schedule` (matching the documented RPC endpoint list
  and permission strings like `job-read`, `schedule-write`), not the plural
  `jobs`/`schedules` the script used before.

**Verifying manually with curl:**
```bash
# 1. Prime the XSRF cookie AND authenticate, in one request
curl -sk -c /tmp/raas_cookies.txt -u 'USER:PASS' https://raas.tom.lab/account/login -o /dev/null

# 2. Use Basic Auth + the cookie + XSRF header on the real RPC call
XSRF=$(grep _xsrf /tmp/raas_cookies.txt | awk '{print $7}')
curl -sk -u 'USER:PASS' -b /tmp/raas_cookies.txt -H "X-Xsrftoken: $XSRF" \
  -H "Content-Type: application/json" \
  -X POST https://raas.tom.lab/rpc \
  -d '{"resource":"job","method":"get_jobs"}'
```
- `403`/"Missing xsrf" → priming GET or header still missing.
- `401 ... no Authorization header` → Basic Auth (`-u`/`session.auth`) isn't being sent.
- `401 ... invalid basic-auth credentials` → XSRF/auth mechanics are correct; it's a
  real wrong-username/password issue.

## Official API reference: use the per-resource doc pages, not general web search

The authoritative source for RaaS RPC syntax is the per-resource pages at
`https://developer.broadcom.com/xapis/vmware-salt-raas/latest/rpc_<resource>.html`
(e.g. `rpc_auth.html`, `rpc_job.html`, `rpc_schedule.html`, `rpc_cmd.html`, ...).
Fetch these pages directly for the resource in question — general web search and
the downloadable "Aria Automation Config API Documentation" PDF are unreliable
here (the PDF fetch consistently truncates before reaching the per-interface
method listings). See the full endpoint reference map in the project rules.

**Confirmed schema from `rpc_job.html` / `rpc_schedule.html`:**
- `job.get_jobs(...)` returns a **paginated** dict: `{'count': N, 'limit': N,
  'results': [...]}` — the job list is in `ret['results']`, not `ret.values()`.
  Its own identity field is `job_uuid` (not `id`/`_id`), and its target field is
  `tgt_uuid` (there is no `tgt` dict alternative for jobs).
- `job.save_job(name, desc, cmd, fun, arg, masters, job_uuid, tgt_uuid)` only
  accepts these named params — NOT an arbitrary job dict. It returns the new
  job's UUID directly as a string (not a dict with `.id`).
- `schedule`'s retrieval method is **`get`**, not `get_schedules` (RPC error
  code `3001` = "RPC func ... was not found" is what you get if you guess
  wrong). It also returns `{'count', 'limit', 'results': [...]}`.
- `schedule.save(name, schedule, masters, cmd, arg, tgt, tgt_uuid, job_uuid,
  uuid, fun, function, enabled)` — its own identity field is `uuid` and its
  job-reference field is `job_uuid` (the script previously and incorrectly
  assumed `id`/`job_id`).
- `auth` has no `login` method at all (confirmed via `rpc_auth.html`'s full
  method list: `change_password`, `delete_group`, `get_all_groups`,
  `get_all_users`, `get_jwt`, `get_role`, etc.) — reinforcing that HTTP Basic
  Auth on every request is the only auth mechanism.

`migration_script.py`'s `JOB_SAVE_FIELDS`/`SCHEDULE_SAVE_FIELDS` allow-lists and
`clean_job_payload`/`clean_schedule_payload` now build save payloads from this
confirmed schema instead of a generic dict copy.

## File Server (fs interface) migration for job assets

Jobs often reference states, scripts, or config files served from RaaS's
built-in File Server (the `sseapi` fileserver backend -- see
`fileserver_backend` in `~/Documents/repos/homelab/docs/salt-master-config.md`).
Copying a job alone does **not** copy these files, so `migrate_fs_files_for_jobs()`
in `migration_script.py` does this as an optional step 6 after jobs/schedules
are copied.

**This is inherently heuristic**, per `rpc_job.html`: a job record has no
explicit field linking it to specific File Server file(s) -- only `fun`
(e.g. `state.apply`) and `arg` (positional args + keyword job-input
definitions, each optionally wrapping a `default` value). `extract_fs_references()`
walks that structure looking for:
- `salt://` URIs (most reliable signal)
- strings ending in common asset extensions (`.sls`, `.sh`, `.ps1`, `.py`, ...)
- for `state.apply`/`state.sls`, dotted state module names (e.g. `myapp.config`
  is tried as both `myapp/config.sls` and `myapp/config/init.sls`)

Candidates are only actually copied if they exactly match a real path from
`fs.get_env(saltenv)` on the source (`resolve_fs_paths()`); everything else is
printed as "unresolved" for manual review, with `find_close_matches()`
suggesting real paths that share a meaningful path segment (helpful for
plural/singular or directory-nesting mismatches) rather than guessing at it.

The state-module heuristic only reads the job's actual `mods` kwarg (and the
first positional `arg`, for `state.sls`) via `_state_names_from_arg()` --
NOT every string in the arg tree. It originally walked everything, which
misread an unrelated `saltenv: "base"` kwarg as a state name and produced a
bogus `/base.sls` candidate.

**Sibling-file expansion:** a resolved `.../<state>/init.sls` often has
associated static assets (templates, `index.html`, etc.) in the same
directory that a job's `arg` can't reveal (they're referenced only inside the
sls file's own `salt://` sources). After resolving candidates,
`migrate_fs_files_for_jobs()` lists every other file in that same directory
+ environment (via the per-env listing in `env_to_paths`) and adds them to
the copy set automatically.

**Idempotency:** before copying each file, `fs_file_exists()` is checked on
the *target*; already-present files are skipped rather than overwritten.
This matters because the same File Server file is often referenced by
multiple states/jobs, so re-running the migration (or migrating overlapping
job sets) won't stomp on files another job already placed there.

File content round-trips as opaque data: per rpc_fs.html, `fs.get_file()`
is documented to return the contents under a `data` key, but the **live
server actually uses `contents`** -- the code accepts either key, preferring
the confirmed-real `contents`. It's passed straight through to
`fs.save_file()`'s `contents` param unchanged (same for `content_type`).

**Known gap:** `rpc_fs.html` documents `fs.get_env()`'s return as `list[dict]`
but does **not** document the dict's schema (unlike `get_file()`, whose `data`
key is documented). The first live run found that every candidate ended up
"unresolved" -- i.e. `extract_fs_env_paths()`'s guessed key names didn't match
any entry. It now tries several candidate keys (`FS_ENV_PATH_KEYS` in
`migration_script.py`: `path`, `file`, `name`, `filename`, `rel_path`,
`relpath`) and, if none match, prints a raw sample entry so the correct key
can be added. If you see that `[!] fs.get_env(...) returned N entries, but
none of the expected path keys were found` warning, add the correct key name
(from the printed sample) to `FS_ENV_PATH_KEYS` and re-run.

## Diagnostics Checklist

```bash
# DNS resolves raas.tom.lab to the RHEL 9 VM
dscacheutil -q host -a name raas.tom.lab   # expect 10.10.10.30

# Network reachability
ping -c 2 10.10.10.30      # source (raas.tom.lab)
ping -c 2 10.40.7.240      # target — requires VPN connected

# TLS + HTTP layer sanity (expect HTTP 405 on a bare GET; /rpc only accepts POST)
curl -sk -o /dev/null -w "HTTP %{http_code}\n" https://raas.tom.lab/rpc
curl -sk -o /dev/null -w "HTTP %{http_code}\n" https://10.40.7.240/rpc
```

Note: `ubuntutomlab` itself (`10.10.10.15`) may not respond to ICMP even when the
RAAS VM (`10.10.10.30`) it hosts is reachable — this is normal, they are different
hosts on the same subnet; don't treat it as a RAAS connectivity problem.

## Related Context

- Full ubuntutomlab / RAAS VM infrastructure details:
  `~/Documents/repos/homelab/docs/ubuntutomlab-reference.md`
- Salt master ↔ RAAS integration (`sseapi_server: https://10.10.10.30`):
  `~/Documents/repos/homelab/docs/salt-master-config.md`
- `~/Documents/repos/raas` is the **decommissioned** Docker Compose RaaS repo — not
  relevant to this migration tool anymore.
