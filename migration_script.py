#!/usr/bin/env python3
import sys
import urllib3
import requests
import questionary

# Disable SSL warnings for self-signed RAAS certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class SaltRAASClient:
    """Client for SaltStack Config / Salt RAAS eAPI (RPC)."""
    def __init__(self, url, username, password, verify_ssl=False):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        self.session.verify = verify_ssl

    def login(self):
        payload = {
            "resource": "auth",
            "method": "login",
            "kwarg": {"username": self.username, "password": self.password}
        }
        res = self.session.post(f"{self.url}/rpc", json=payload)
        res.raise_for_status()
        data = res.json()
        if data.get("error"):
            raise Exception(f"Auth failed for {self.url}: {data['error']}")
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
        ret = self._rpc("jobs", "get_jobs")
        return list(ret.values()) if isinstance(ret, dict) else (ret or [])

    def get_schedules(self):
        ret = self._rpc("schedules", "get_schedules")
        return list(ret.values()) if isinstance(ret, dict) else (ret or [])

    def save_job(self, job_data):
        return self._rpc("jobs", "save", kwarg=job_data)

    def save_schedule(self, schedule_data):
        return self._rpc("schedules", "save", kwarg=schedule_data)


def clean_job_payload(job):
    """Strip system IDs and Minion Group targets (Option 3-B)."""
    payload = job.copy()
    for field in ["id", "_id", "created", "modified", "user"]:
        payload.pop(field, None)
    
    # Strip Minion Group / Target references
    for target_field in ["tgt", "tgt_type", "target_group_id", "target_id", "minion_group_id", "tgts"]:
        if target_field in payload:
            payload[target_field] = "" if isinstance(payload[target_field], str) else None
            
    return payload


def clean_schedule_payload(schedule, job_id_map, target_job_name_map):
    """Strip targets (Option 3-B) and remap Job UUID to the target server (Option 1-A)."""
    payload = schedule.copy()
    for field in ["id", "_id", "created", "modified", "user"]:
        payload.pop(field, None)

    # Strip Minion Group / Target references
    for target_field in ["tgt", "tgt_type", "target_group_id", "target_id", "minion_group_id", "tgts"]:
        if target_field in payload:
            payload[target_field] = "" if isinstance(payload[target_field], str) else None

    # Re-link schedule to target Job UUID
    old_job_id = payload.get("job_id")
    if old_job_id:
        if old_job_id in job_id_map:
            payload["job_id"] = job_id_map[old_job_id]
        else:
            job_name = schedule.get("job_name")
            if job_name and job_name in target_job_name_map:
                payload["job_id"] = target_job_name_map[job_name]
            else:
                print(f"  [!] Warning: Could not remap Job UUID for schedule '{schedule.get('name')}'")

    return payload


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
    tgt_job_names = {j["name"]: j.get("id") or j.get("_id") for j in tgt_jobs if "name" in j}
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
    job_id_map = {}  # { old_job_id: new_job_id }

    # Copy Jobs
    if selected_jobs:
        print("\n--- Copying Jobs ---")
        for job in selected_jobs:
            old_id = job.get("id") or job.get("_id")
            name = job.get("name")
            payload = clean_job_payload(job)
            
            try:
                res = target_client.save_job(payload)
                new_id = res.get("id") or res.get("_id") if isinstance(res, dict) else res
                job_id_map[old_id] = new_id
                # Update map for schedules that might reference it by name
                tgt_job_names[name] = new_id
                print(f" Successfully copied Job: '{name}'")
            except Exception as e:
                print(f" Failed to copy Job '{name}': {e}")

    # Copy Schedules
    if selected_schedules:
        print("\n--- Copying Schedules ---")
        for sched in selected_schedules:
            name = sched.get("name")
            payload = clean_schedule_payload(sched, job_id_map, tgt_job_names)
            
            try:
                target_client.save_schedule(payload)
                print(f" Successfully copied Schedule: '{name}'")
            except Exception as e:
                print(f" Failed to copy Schedule '{name}': {e}")

    print("\nMigration completed.")

if __name__ == "__main__":
    main()
