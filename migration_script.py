#!/usr/bin/env python3
import re
import sys
import urllib3
import requests
import questionary

# Disable SSL warnings for self-signed RAAS certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class SaltRAASClient:
    """Client for SaltStack Config / Salt RAAS eAPI (RPC).

    Per the official Aria Automation Config (RaaS) eAPI docs, RaaS does NOT
    have a `resource=auth, method=login` RPC call. Authentication is plain
    HTTP Basic Auth sent with every single request (both the priming GET and
    every /rpc POST) -- there is no separate "login" step or bearer token.
    See: https://developer.broadcom.com/xapis/vmware-salt-raas/latest/
    """
    def __init__(self, url, username, password, verify_ssl=False):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.auth = (username, password)

    def _prime_xsrf(self):
        """RaaS's Tornado backend rejects any POST to /rpc with a 403
        ("Missing xsrf argument") unless the client first performs a GET
        to receive the _xsrf cookie and echoes it back via X-XSRFToken.
        Per the docs this priming GET should hit /account/login (sent with
        Basic Auth), not just "/"."""
        res = self.session.get(f"{self.url}/account/login")
        res.raise_for_status()
        token = self.session.cookies.get("_xsrf")
        if not token:
            raise Exception(
                f"Could not obtain XSRF token from {self.url} "
                "(no _xsrf cookie returned)"
            )
        self.session.headers.update({"X-XSRFToken": token})

    def login(self):
        """Prime XSRF and verify credentials. There is no dedicated login
        RPC call in RaaS's eAPI -- auth is Basic Auth on every request, so
        we verify it here with a cheap real call instead."""
        self._prime_xsrf()
        # A lightweight authenticated call to confirm the credentials are
        # actually valid (Basic Auth failures surface as 401 here).
        self._rpc("job", "get_jobs")
        return True

    def _rpc(self, resource, method, kwarg=None):
        payload = {
            "resource": resource,
            "method": method,
            "kwarg": kwarg or {}
        }
        res = self.session.post(f"{self.url}/rpc", json=payload)
        res.raise_for_status()
        data = res.json()
        if data.get("error"):
            raise Exception(f"RPC Error [{resource}.{method}]: {data['error']}")
        return data.get("ret")

    def get_jobs(self):
        # job.get_jobs() returns a paginated dict: {'count', 'limit', 'results': [...]}.
        # See https://developer.broadcom.com/xapis/vmware-salt-raas/latest/rpc_job.html
        ret = self._rpc("job", "get_jobs", kwarg={"limit": 0})
        if isinstance(ret, dict):
            return ret.get("results") or []
        return ret or []

    def get_schedules(self):
        # The schedule interface's retrieval method is "get" (not "get_schedules"),
        # and it also returns a paginated {'count', 'limit', 'results': [...]} dict.
        # See https://developer.broadcom.com/xapis/vmware-salt-raas/latest/rpc_schedule.html
        ret = self._rpc("schedule", "get", kwarg={"limit": 0})
        if isinstance(ret, dict):
            return ret.get("results") or []
        return ret or []

    def save_job(self, job_data):
        # job.save_job() only accepts these named params -- NOT an arbitrary
        # job dict -- and its target reference field is "tgt_uuid" (there is
        # no "tgt" dict alternative for jobs, unlike schedules).
        return self._rpc("job", "save_job", kwarg=job_data)

    def save_schedule(self, schedule_data):
        # schedule.save()'s own identifier is "uuid" and its job reference
        # field is "job_uuid" (not "id"/"job_id").
        return self._rpc("schedule", "save", kwarg=schedule_data)

    # --- File Server (fs interface) ---
    # See https://developer.broadcom.com/xapis/vmware-salt-raas/latest/rpc_fs.html

    def get_fs_envs(self):
        """List all available Salt environments (e.g. ['base'])."""
        return self._rpc("fs", "get_envs") or []

    def get_fs_env(self, saltenv):
        """List all File Server files in a given Salt environment."""
        return self._rpc("fs", "get_env", kwarg={"saltenv": saltenv, "include_fs_metadata": False}) or []

    def get_fs_file(self, saltenv, path):
        """Fetch a File Server file's metadata + contents ("data" key)."""
        return self._rpc("fs", "get_file", kwarg={"saltenv": saltenv, "path": path})

    def save_fs_file(self, saltenv, path, contents, content_type=None):
        """Add or update a File Server file."""
        kwarg = {"saltenv": saltenv, "path": path, "contents": contents}
        if content_type:
            kwarg["content_type"] = content_type
        return self._rpc("fs", "save_file", kwarg=kwarg)

    def fs_file_exists(self, saltenv, path):
        """Return True if a File Server file already exists at this path."""
        return bool(self._rpc("fs", "file_exists", kwarg={"saltenv": saltenv, "path": path}))


# job.save_job()'s only accepted parameters (per rpc_job.html). "job_uuid"
# (own identity) and "tgt_uuid" (Minion Group target) are deliberately
# excluded here so the target server generates a new, untargeted job.
JOB_SAVE_FIELDS = ("name", "desc", "cmd", "fun", "arg", "masters")

# schedule.save()'s only accepted parameters (per rpc_schedule.html), minus
# "uuid" (own identity), "tgt"/"tgt_uuid" (Minion Group target -- stripped),
# and "job_uuid" (handled separately below via remapping).
SCHEDULE_SAVE_FIELDS = ("name", "schedule", "masters", "cmd", "arg", "fun", "function", "enabled")


def clean_job_payload(job):
    """Build a job.save_job() payload: only documented fields, with the
    source job_uuid and Minion Group target (tgt_uuid) stripped so the
    target server assigns a new UUID and the job is created untargeted."""
    return {field: job[field] for field in JOB_SAVE_FIELDS if field in job}


def clean_schedule_payload(schedule, job_uuid_map, target_job_name_map):
    """Build a schedule.save() payload: only documented fields, with the
    source uuid and Minion Group target (tgt/tgt_uuid) stripped, and the
    job_uuid remapped to the equivalent job on the target server."""
    payload = {field: schedule[field] for field in SCHEDULE_SAVE_FIELDS if field in schedule}

    # Re-link schedule to the target server's Job UUID
    old_job_uuid = schedule.get("job_uuid")
    if old_job_uuid:
        if old_job_uuid in job_uuid_map:
            payload["job_uuid"] = job_uuid_map[old_job_uuid]
        else:
            job_name = schedule.get("job_name")
            if job_name and job_name in target_job_name_map:
                payload["job_uuid"] = target_job_name_map[job_name]
            else:
                print(f"  [!] Warning: Could not remap Job UUID for schedule '{schedule.get('name')}'")

    return payload


# File extensions that are very likely to be salt:// File Server assets
# regardless of which function references them (scripts, states, configs).
FS_PATH_EXTENSIONS = (
    ".sls", ".sh", ".ps1", ".py", ".j2", ".jinja", ".yaml", ".yml",
    ".cfg", ".conf", ".tpl", ".txt",
)

# Functions whose job inputs commonly reference a dotted Salt state module
# name (e.g. "myapp.config") rather than a literal salt:// path.
STATE_FUNS = {"state.apply", "state.sls", "state.sls_id", "state.highstate"}


def _walk_arg_strings(value):
    """Recursively yield string leaves out of a job's `arg` structure,
    unwrapping job-input-definition dicts (identified by an "input_type"
    key, per rpc_job.html) to their "default" value."""
    if isinstance(value, dict):
        if "input_type" in value:
            yield from _walk_arg_strings(value.get("default"))
        else:
            for v in value.values():
                yield from _walk_arg_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk_arg_strings(v)
    elif isinstance(value, str):
        yield value


def _state_names_from_arg(arg):
    """Extract dotted Salt state module names from a state.apply/state.sls
    job's arg structure. Only looks at the "mods" kwarg and the first
    positional arg (the two places Salt actually accepts sls names) --
    NOT every string in the structure -- so unrelated kwargs like
    "saltenv" (which also holds a plain word like "base") aren't
    mistaken for a state name."""
    if not isinstance(arg, dict):
        return
    kwarg = arg.get("kwarg")
    if isinstance(kwarg, dict) and "mods" in kwarg:
        yield from _walk_arg_strings(kwarg["mods"])
    positional = arg.get("arg")
    if isinstance(positional, list) and positional:
        yield from _walk_arg_strings(positional[0])


def extract_fs_references(job):
    """Best-effort extraction of File Server paths referenced by a job's
    fun/arg. RaaS job records don't store an explicit link to File Server
    files (see rpc_job.html), so this heuristically looks for salt://
    URIs, common script/config file extensions, and -- for state.apply /
    state.sls -- dotted state module names from the "mods" input
    (e.g. "myapp.config").

    All returned paths are normalized with a single leading "/", matching
    the confirmed real File Server path format (e.g. "/LAMP/init.sls")."""
    fun = (job.get("fun") or "").strip()
    arg = job.get("arg")
    candidates = set()

    for raw in _walk_arg_strings(arg):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("salt://"):
            candidates.add("/" + s[len("salt://"):].lstrip("/"))
        elif s.lower().endswith(FS_PATH_EXTENSIONS):
            candidates.add("/" + s.lstrip("/"))

    if fun in STATE_FUNS:
        for raw in _state_names_from_arg(arg):
            # "mods" can be a single name or a comma/space-separated list.
            for name in re.split(r"[,\s]+", raw.strip()):
                name = name.strip()
                if not name or not all(c.isalnum() or c in "._-" for c in name):
                    continue
                base = "/" + name.replace(".", "/")
                candidates.add(base + ".sls")
                candidates.add(base + "/init.sls")

    return candidates


def resolve_fs_paths(candidates, available_paths):
    """Match heuristic candidate paths against the File Server's actual file
    listing. Returns (resolved, unresolved) sets."""
    available = set(available_paths)
    resolved = {c for c in candidates if c in available}
    unresolved = candidates - resolved
    return resolved, unresolved


def find_close_matches(candidate, available_paths, limit=5):
    """Suggest real File Server paths that share a meaningful path segment
    with an unresolved candidate, so the actual naming convention (e.g.
    plural vs singular, different directory nesting) is visible without
    having to dump the entire file listing."""
    stem = candidate.strip("/")
    stem = re.sub(r"\.(sls|sh|ps1|py|j2|jinja|yaml|yml|cfg|conf|tpl|txt)$", "", stem, flags=re.I)
    stem = re.sub(r"(^|/)init$", "", stem)
    parts = [p for p in re.split(r"[/._-]+", stem) if len(p) > 2]
    if not parts:
        return []
    matches = [p for p in available_paths if any(part.lower() in p.lower() for part in parts)]
    return sorted(matches)[:limit]


# rpc_fs.html documents fs.get_env()'s return as "list[dict]" without
# specifying the dict schema (unlike get_file(), whose "data" key IS
# documented). Try these candidate key names, in order, for the file's path.
FS_ENV_PATH_KEYS = ("path", "file", "name", "filename", "rel_path", "relpath")


def extract_fs_env_paths(env_files):
    """Pull file paths out of fs.get_env()'s (undocumented-shape) entries.
    Returns (paths, sample) where `sample` is a raw entry to print for
    debugging if no known path key was found."""
    paths = set()
    sample = None
    for entry in env_files or []:
        if isinstance(entry, str):
            paths.add(entry)
            continue
        if not isinstance(entry, dict):
            continue
        if sample is None:
            sample = entry
        for key in FS_ENV_PATH_KEYS:
            val = entry.get(key)
            if isinstance(val, str) and val:
                paths.add(val)
                break
    return paths, sample


def migrate_fs_files_for_jobs(source_client, target_client, jobs):
    """Best-effort migration of File Server (fs interface) files referenced
    by the given (source) job records. This is heuristic -- see
    extract_fs_references() -- since RaaS jobs don't store an explicit link
    to File Server files.

    Searches ALL source Salt environments (not just "base") since RaaS-managed
    content commonly lives in a separate environment (e.g. "sse") from
    gitfs-backed "base" content, and there's no reliable way to know ahead of
    time which one a given job's files live in."""
    try:
        src_envs = source_client.get_fs_envs()
    except Exception as e:
        print(f" [!] Could not list source File Server environments: {e}")
        src_envs = []

    if not src_envs:
        envs_input = questionary.text(
            "Could not auto-discover source File Server environments. Enter a "
            "comma-separated list to search (e.g. base,sse):",
            default="base",
        ).ask()
        src_envs = [e.strip() for e in (envs_input or "").split(",") if e.strip()]
    if not src_envs:
        print(" No environment specified; skipping File Server migration.")
        return

    print(f" Searching File Server environment(s): {', '.join(src_envs)}")

    candidates = set()
    for job in jobs:
        candidates |= extract_fs_references(job)

    if not candidates:
        print(" No File Server references detected in the copied jobs' fun/arg definitions.")
        return

    # Map each discovered path -> the (first) env it was found in, and keep
    # a per-env listing too (needed for sibling-file directory expansion).
    path_to_env = {}
    env_to_paths = {}
    for env in src_envs:
        try:
            env_files = source_client.get_fs_env(env)
            env_paths, sample_entry = extract_fs_env_paths(env_files)
            if env_files and not env_paths:
                print(
                    f" [!] fs.get_env('{env}') returned {len(env_files)} entries, but none of "
                    f"the expected path keys {FS_ENV_PATH_KEYS} were found. Raw sample entry:\n"
                    f"     {sample_entry!r}\n"
                    " Update FS_ENV_PATH_KEYS in migration_script.py with the correct key name."
                )
            elif not env_files:
                print(f" [!] fs.get_env('{env}') returned no files.")
            env_to_paths[env] = env_paths
            for p in env_paths:
                path_to_env.setdefault(p, env)
        except Exception as e:
            print(f" [!] Could not list source File Server files for env '{env}': {e}")

    resolved, unresolved = resolve_fs_paths(candidates, path_to_env.keys())

    # Expand directory-per-state layouts (".../<state>/init.sls") to include
    # sibling files in the same directory + environment -- e.g. templates or
    # static assets like index.html that a state references with a relative
    # salt:// path the job-arg heuristic has no way to see.
    for path in list(resolved):
        if not path.endswith("/init.sls"):
            continue
        env = path_to_env[path]
        state_dir = path[: -len("init.sls")]  # keeps the trailing "/"
        siblings = {
            p for p in env_to_paths.get(env, ()) if p.startswith(state_dir) and p != path
        }
        new_siblings = siblings - resolved
        if new_siblings:
            print(f" Found sibling file(s) alongside '{path}' (env: {env}):")
            for s in sorted(new_siblings):
                print(f"     + {s}")
        resolved |= siblings
        for s in siblings:
            path_to_env.setdefault(s, env)

    for path in sorted(resolved):
        env = path_to_env[path]
        try:
            try:
                already_exists = target_client.fs_file_exists(env, path)
            except Exception as e:
                print(f" [!] Could not check if '{path}' exists on target (env: {env}): {e} -- attempting copy anyway")
                already_exists = False
            if already_exists:
                print(f" Skipping File Server file '{path}' (env: {env}): already exists on target")
                continue

            file_obj = source_client.get_fs_file(env, path)
            # rpc_fs.html documents the contents key as "data", but the live
            # server actually returns it as "contents" -- accept either.
            if not isinstance(file_obj, dict) or (
                "contents" not in file_obj and "data" not in file_obj
            ):
                raise Exception(f"unexpected get_file response: {file_obj!r}")
            contents = file_obj["contents"] if "contents" in file_obj else file_obj["data"]
            target_client.save_fs_file(
                env,
                path,
                contents,
                content_type=file_obj.get("content_type"),
            )
            print(f" Successfully copied File Server file: '{path}' (env: {env})")
        except Exception as e:
            print(f" Failed to copy File Server file '{path}' (env: {env}): {e}")

    if unresolved:
        print(
            "\n [!] Could not confidently match these referenced paths to actual "
            "File Server files -- verify and copy manually if needed:"
        )
        all_paths = list(path_to_env.keys())
        for path in sorted(unresolved):
            print(f"     - {path}")
            for match in find_close_matches(path, all_paths):
                print(f"         possible match: {match}  (env: {path_to_env[match]})")


def main():
    print("=== Salt RAAS Migration Tool ===\n")
    
    # 1. Connection Inputs
    src_url = questionary.text("Source RAAS Server URL (e.g., https://raas-src.local):").ask()
    src_user = questionary.text("Source Username:").ask()
    src_pass = questionary.password("Source Password:").ask()

    tgt_url = questionary.text("Target RAAS Server URL (e.g., https://raas-tgt.local):").ask()
    tgt_user = questionary.text("Target Username:").ask()
    tgt_pass = questionary.password("Target Password:").ask()

    # 2. Authenticate
    try:
        print("\nConnecting to Source server...")
        source_client = SaltRAASClient(src_url, src_user, src_pass)
        source_client.login()
        
        print("Connecting to Target server...")
        target_client = SaltRAASClient(tgt_url, tgt_user, tgt_pass)
        target_client.login()
        print("Authentication successful.\n")
    except Exception as e:
        print(f"\n[Error] {e}")
        sys.exit(1)

    # 3. Query existing data
    print("Fetching jobs and schedules from both servers...")
    src_jobs = source_client.get_jobs()
    src_schedules = source_client.get_schedules()
    tgt_jobs = target_client.get_jobs()
    tgt_schedules = target_client.get_schedules()

    # Map existing target resources for duplication check (Option 2-A)
    tgt_job_names = {j["name"]: j.get("job_uuid") for j in tgt_jobs if "name" in j}
    tgt_sched_names = {s["name"] for s in tgt_schedules if "name" in s}

    # Filter out items that already exist on target (Option 2-A: Skip duplicates)
    available_jobs = [j for j in src_jobs if j.get("name") not in tgt_job_names]
    available_schedules = [s for s in src_schedules if s.get("name") not in tgt_sched_names]

    if not available_jobs and not available_schedules:
        print("No new jobs or schedules available to migrate. All items already exist on the target.")
        sys.exit(0)

    # 4. Interactive Selection (Option 4-A)
    selected_jobs = []
    if available_jobs:
        job_choices = [questionary.Choice(title=j["name"], value=j) for j in available_jobs]
        selected_jobs = questionary.checkbox("Select JOBS to copy:", choices=job_choices).ask() or []
    else:
        print("All source jobs already exist on target.")

    selected_schedules = []
    if available_schedules:
        sched_choices = [questionary.Choice(title=s["name"], value=s) for s in available_schedules]
        selected_schedules = questionary.checkbox("Select SCHEDULES to copy:", choices=sched_choices).ask() or []
    else:
        print("All source schedules already exist on target.")

    if not selected_jobs and not selected_schedules:
        print("No items selected. Exiting.")
        sys.exit(0)

    # 5. Migration Execution
    job_uuid_map = {}  # { old_job_uuid: new_job_uuid }

    # Copy Jobs
    if selected_jobs:
        print("\n--- Copying Jobs ---")
        for job in selected_jobs:
            old_uuid = job.get("job_uuid")
            name = job.get("name")
            payload = clean_job_payload(job)

            try:
                # save_job() returns the new job's UUID directly (a string).
                new_uuid = target_client.save_job(payload)
                job_uuid_map[old_uuid] = new_uuid
                # Update map for schedules that might reference it by name
                tgt_job_names[name] = new_uuid
                print(f" Successfully copied Job: '{name}'")
            except Exception as e:
                print(f" Failed to copy Job '{name}': {e}")

    # Copy Schedules
    if selected_schedules:
        print("\n--- Copying Schedules ---")
        for sched in selected_schedules:
            name = sched.get("name")
            payload = clean_schedule_payload(sched, job_uuid_map, tgt_job_names)

            try:
                target_client.save_schedule(payload)
                print(f" Successfully copied Schedule: '{name}'")
            except Exception as e:
                print(f" Failed to copy Schedule '{name}': {e}")

    # 6. Migrate File Server files referenced by the copied jobs
    if selected_jobs:
        migrate_fs = questionary.confirm(
            "\nAlso migrate File Server files (states/scripts/configs) referenced "
            "by the copied jobs?",
            default=True,
        ).ask()
        if migrate_fs:
            migrate_fs_files_for_jobs(source_client, target_client, selected_jobs)

    print("\nMigration completed.")

if __name__ == "__main__":
    main()
