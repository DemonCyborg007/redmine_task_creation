#!/usr/bin/env python3
"""
Redmine Bulk Task Uploader (CSV Version)
Reads tasks from CSV and creates issues in Redmine via REST API.

FIXES:
- Sends estimated hours DIRECTLY (not converted to days)
- Assignee matching using project members (no admin required)

Required packages: pip install requests python-dotenv

Setup:
1. Create .env file with:
   REDMINE_URL=https://20.207.146.131/redmine
   REDMINE_API_KEY=your_api_key
   REDMINE_PROJECT_ID=380   (optional: only if a row has no RedmineID and no usable Parent task)

2. Prepare CSV with columns including **RedmineID** (numeric project id or project identifier slug).
   Other columns: Tracker, Status, Priority, Subject, Assignee, Target version, Start date, Due date, Estimated time, Parent task.
   Project resolution per row: **RedmineID** in CSV, else parent issue's project, else REDMINE_PROJECT_ID.

3. Call from code: ``from Test import run_bulk_upload; run_bulk_upload("tasks.csv")``
   Optional: ``result_out={}``, ``log_lines=[]`` for the web UI (see ``redmine_bulk_web.py``).
"""

import csv
import io
import os
from typing import Any, Dict, List, Optional, Union

import requests
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIGURATION — override via .env (REDMINE_URL, REDMINE_API_KEY; REDMINE_PROJECT_ID optional fallback)
# ============================================================================

REDMINE_URL = os.getenv("REDMINE_URL", "").rstrip("/") 
API_KEY = os.getenv("REDMINE_API_KEY", "")


def _env_int(name):
    v = os.getenv(name, "").strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None

# Redmine host uses a cert not trusted by default CA bundle
VERIFY_SSL = False

if not VERIFY_SSL:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

_log_lines_sink: Optional[List[str]] = None


def log(level, message):
    """Print timestamped log messages; optionally mirror to a list (see run_bulk_upload)."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {level}: {message}"
    print(line)
    sink = _log_lines_sink
    if sink is not None:
        sink.append(line)

def get_user_id(username):
    """Fetch user ID from username via Redmine API."""
    if not username or not username.strip():
        return None

    name = username.strip()
    try:
        response = requests.get(
            f"{REDMINE_URL}/users.json",
            params={"name": name},
            headers={"X-Redmine-API-Key": API_KEY},
            verify=VERIFY_SSL,
            timeout=10,
        )
        if response.status_code == 200:
            users = response.json().get("users", [])
            if users:
                return users[0]["id"]
            return None
        if response.status_code in (401, 403):
            log(
                "WARN",
                f"User lookup denied (HTTP {response.status_code}). Use API key from Redmine admin.",
            )
            return None
        return None
    except Exception as e:
        log("ERROR", f"Failed to fetch user '{username}': {str(e)}")
        return None


def _normalize_assignee_label(s):
    if not s:
        return ""
    return " ".join(str(s).strip().split()).lower()


def fetch_project_member_users(project_id):
    """List users who are members of the project (memberships API)."""
    out = []
    seen_ids = set()
    offset = 0
    limit = 100
    try:
        while True:
            response = requests.get(
                f"{REDMINE_URL}/projects/{project_id}/memberships.json",
                params={"limit": limit, "offset": offset},
                headers={"X-Redmine-API-Key": API_KEY},
                verify=VERIFY_SSL,
                timeout=30,
            )
            if response.status_code != 200:
                log(
                    "WARN",
                    f"Project {project_id} memberships: HTTP {response.status_code}",
                )
                break
            memberships = response.json().get("memberships", [])
            if not memberships:
                break
            for m in memberships:
                if m.get("group"):
                    continue
                u = m.get("user")
                if not u or not isinstance(u, dict) or "id" not in u:
                    continue
                uid = u["id"]
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)
                out.append(u)
            if len(memberships) < limit:
                break
            offset += limit
    except Exception as e:
        log("ERROR", f"Failed to fetch memberships for project {project_id}: {e}")
    return out


def match_assignee_to_project_member(member_users, assignee_label):
    """Match CSV Assignee to a project member by login, name, or firstname + lastname."""
    if not assignee_label or not str(assignee_label).strip():
        return None
    needle = _normalize_assignee_label(assignee_label)
    if not needle:
        return None
    for u in member_users:
        candidates = []
        for key in ("login", "name"):
            v = u.get(key)
            if v:
                candidates.append(_normalize_assignee_label(v))
        fn = (u.get("firstname") or "").strip()
        ln = (u.get("lastname") or "").strip()
        if fn or ln:
            candidates.append(_normalize_assignee_label(f"{fn} {ln}"))
        if any(c == needle for c in candidates):
            return u.get("id")
    return None


def resolve_assignee_id(project_id, assignee_label, project_members_cache, assignee_cache):
    """Resolve assignee to user id: project members first, then users.json."""
    if not assignee_label or not str(assignee_label).strip():
        return None
    label = assignee_label.strip()
    key = (project_id, label)
    if key in assignee_cache:
        return assignee_cache[key]
    if project_id not in project_members_cache:
        project_members_cache[project_id] = fetch_project_member_users(project_id)
        n = len(project_members_cache[project_id])
        log("OK", f"  → Loaded {n} project member user(s) for assignee matching")
    member_users = project_members_cache[project_id]
    uid = match_assignee_to_project_member(member_users, label)
    if uid is None:
        uid = get_user_id(label)
    assignee_cache[key] = uid
    return uid


def get_tracker_id(tracker_name):
    """Fetch tracker ID from tracker name via Redmine API."""
    if not tracker_name:
        return None
    
    t_name = tracker_name
    t_stripped = t_name.strip()
    if not t_stripped:
        return None
        
    try:
        response = requests.get(
            f"{REDMINE_URL}/trackers.json",
            headers={"X-Redmine-API-Key": API_KEY},
            verify=VERIFY_SSL,
            timeout=10
        )
        if response.status_code == 200:
            trackers = response.json().get("trackers", [])
            for tracker in trackers:
                curr_name = tracker.get("name", "")
                if curr_name.lower() == t_name.lower() or curr_name.lower().strip() == t_stripped.lower():
                    return tracker["id"]
        log("WARN", f"Tracker '{tracker_name}' not found")
        return None
    except Exception as e:
        log("ERROR", f"Failed to fetch tracker '{tracker_name}': {str(e)}")
        return None

def get_priority_id(priority_name):
    """Fetch priority ID from priority name via Redmine API."""
    if not priority_name:
        return None
        
    p_name = priority_name
    p_stripped = p_name.strip()
    if not p_stripped:
        return None
    
    try:
        response = requests.get(
            f"{REDMINE_URL}/enumerations/issue_priorities.json",
            headers={"X-Redmine-API-Key": API_KEY},
            verify=VERIFY_SSL,
            timeout=10
        )
        if response.status_code == 200:
            priorities = response.json().get("issue_priorities", [])
            for priority in priorities:
                curr_name = priority.get("name", "")
                if curr_name.lower() == p_name.lower() or curr_name.lower().strip() == p_stripped.lower():
                    return priority["id"]
        log("WARN", f"Priority '{priority_name}' not found")
        return None
    except Exception as e:
        log("ERROR", f"Failed to fetch priority '{priority_name}': {str(e)}")
        return None

def get_version_id(project_id, version_name):
    """Fetch version ID from project and version name via Redmine API."""
    if not version_name:
        return None

    vn = version_name
    vn_stripped = vn.strip()
    if vn_stripped.startswith("#"):
        vn_sub = vn_stripped[1:].strip()
        if vn_sub.isdigit():
            return int(vn_sub)

    # If it's already a numeric ID, skip the API call entirely
    if vn_stripped.isdigit():
        log("OK", f"Version resolved directly from numeric ID: {vn_stripped}")
        return int(vn_stripped)

    try:
        response = requests.get(
            f"{REDMINE_URL}/projects/{project_id}/versions.json",
            headers={"X-Redmine-API-Key": API_KEY},
            verify=VERIFY_SSL,
            timeout=10
        )
        if response.status_code == 200:
            versions = response.json().get("versions", [])
            for version in versions:
                v_name = version.get("name", "")
                if v_name.lower() == vn.lower() or v_name.lower().strip() == vn_stripped.lower():
                    return version["id"]
        log("WARN", f"Version '{vn}' not found in project {project_id}")
        return None
    except Exception as e:
        log("ERROR", f"Failed to fetch version '{version_name}': {str(e)}")
        return None

def get_parent_issue_project_id(parent_id):
    """Fetch parent issue and return its project ID."""
    if not parent_id:
        return None
    
    try:
        response = requests.get(
            f"{REDMINE_URL}/issues/{parent_id}.json",
            headers={"X-Redmine-API-Key": API_KEY},
            verify=VERIFY_SSL,
            timeout=10
        )
        if response.status_code == 200:
            issue = response.json().get("issue", {})
            project_id = issue.get("project", {}).get("id")
            if project_id:
                project_name = issue.get("project", {}).get("name", "Unknown")
                log("OK", f"Parent issue #{parent_id} → project '{project_name}' (ID: {project_id})")
                return project_id
            return None
        if response.status_code == 404:
            log("WARN", f"Parent issue #{parent_id} not found (404)")
        elif response.status_code in (401, 403):
            log("WARN", f"Parent issue #{parent_id} not visible (HTTP {response.status_code})")
        else:
            log("WARN", f"Parent issue #{parent_id} lookup failed: HTTP {response.status_code}")
        return None
    except Exception as e:
        log("ERROR", f"Failed to fetch parent issue #{parent_id}: {str(e)}")
        return None

def parse_parent_id(parent_str):
    """Extract issue ID from parent task string (e.g., '#104886' -> 104886)."""
    if not parent_str or not str(parent_str).strip():
        return None
    
    parent_str = str(parent_str).strip()
    if parent_str.startswith('#'):
        parent_str = parent_str[1:]
    
    try:
        return int(parent_str)
    except ValueError:
        log("WARN", f"Invalid parent task ID: '{parent_str}'")
        return None


def _csv_first_nonempty(row, *header_names):
    """Match CSV headers case-insensitively; strip BOM from keys."""
    lower_map = {}
    for k in row:
        if k is None:
            continue
        ks = str(k).lstrip("\ufeff").strip()
        if not ks:
            continue
        lower_map[ks.lower()] = ks
    for name in header_names:
        orig = lower_map.get(name.strip().lower())
        if orig is None:
            continue
        v = row.get(orig)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def parse_project_ref(raw):
    """
    Redmine project from CSV: numeric id (optional leading #) or string identifier (e.g. my-project).
    """
    if raw is None or not str(raw).strip():
        return None
    s = str(raw).strip()
    if s.startswith("#"):
        s = s[1:].strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    return s


def clean_subject(subject):
    """Clean subject by removing pipe characters but preserving extra whitespace."""
    if not subject:
        return subject
    
    s = str(subject)
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]
    return s

def parse_date(date_str):
    """Parse date string in multiple formats."""
    if not date_str or not str(date_str).strip():
        return None
    
    date_str = str(date_str).strip()
    
    formats = [
        "%m/%d/%Y",  # 3/31/2026
        "%d/%m/%Y",  # 31/3/2026
        "%Y-%m-%d",  # 2026-03-31
        "%m-%d-%Y",  # 07-03-2026 (Month-Day-Year)
        "%d-%m-%Y",  # 31-03-2026 (Day-Month-Year)
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.date().isoformat()
        except ValueError:
            continue
    
    log("WARN", f"Could not parse date: '{date_str}'")
    return None

def parse_estimated_hours(hours_str):
    """Parse estimated hours and return as HOURS (not days)."""
    if not hours_str or not str(hours_str).strip():
        return None
    
    try:
        hours = float(str(hours_str).strip())
        # ✅ FIXED: Return hours directly, don't convert to days
        return hours if hours > 0 else None
    except ValueError:
        return None

def create_issue(project_id, subject, assignee_id, due_date, priority_id, 
                 tracker_id, version_id, parent_id, estimated_hours, start_date):
    """Create a single issue in Redmine via REST API."""
    
    payload = {
        "issue": {
            "project_id": project_id,
            "subject": subject,
            "tracker_id": tracker_id,
            "priority_id": priority_id,
        }
    }
    
    if assignee_id:
        payload["issue"]["assigned_to_id"] = assignee_id
    if due_date:
        payload["issue"]["due_date"] = due_date
    if start_date:
        payload["issue"]["start_date"] = start_date
    if version_id:
        payload["issue"]["fixed_version_id"] = version_id
    if parent_id:
        payload["issue"]["parent_issue_id"] = parent_id
    if estimated_hours:
        # ✅ FIXED: Send hours directly
        payload["issue"]["estimated_hours"] = estimated_hours
    
    try:
        response = requests.post(
            f"{REDMINE_URL}/issues.json",
            json=payload,
            headers={"X-Redmine-API-Key": API_KEY},
            verify=VERIFY_SSL,
            timeout=10
        )
        
        if response.status_code in [200, 201]:
            issue_data = response.json().get("issue", {})
            issue_id = issue_data.get("id")
            log("OK", f"Created issue #{issue_id}: {subject[:50]}")
            return issue_id
        else:
            error_msg = response.text
            try:
                error_msg = response.json().get("errors", error_msg)
            except:
                pass
            log("ERROR", f"Failed to create '{subject}': {error_msg}")
            return None
    except Exception as e:
        log("ERROR", f"Exception creating issue '{subject}': {str(e)}")
        return None

def parse_csv(source: Union[str, os.PathLike, io.TextIOBase]):
    """Parse CSV from a path or an open text stream."""
    close_after = False
    f = None
    try:
        if isinstance(source, io.TextIOBase):
            f = source
        else:
            f = open(os.fspath(source), "r", encoding="utf-8")
            close_after = True

        tasks = []
        reader = csv.DictReader(f)
        for row_num, row in enumerate(reader, 2):
            tasks.append({
                "row": row_num,
                "tracker": row.get("Tracker", ""),
                "status": row.get("Status", ""),
                "priority": row.get("Priority", ""),
                "subject": row.get("Subject", ""),
                "assignee": row.get("Assignee", ""),
                "target_version": row.get("Target version", ""),
                "start_date": row.get("Start date", "").strip(),
                "due_date": row.get("Due date", "").strip(),
                "estimated_time": row.get("Estimated time", "").strip(),
                "parent_task": row.get("Parent task", "").strip(),
                "redmine_id_raw": _csv_first_nonempty(
                    row,
                    "RedmineID",
                    "Redmine ID",
                    "Project ID",
                    "project_id",
                    "Project id",
                ),
            })

        log("OK", f"Parsed {len(tasks)} rows from CSV")
        return tasks
    except Exception as e:
        log("ERROR", f"Failed to parse CSV: {str(e)}")
        return None
    finally:
        if close_after and f is not None:
            f.close()

def validate_config():
    """Validate that API key and URL are configured."""
    if REDMINE_URL == "https://your-redmine-instance.com" or API_KEY == "your_api_key_here":
        log("ERROR", "Please update REDMINE_URL and API_KEY in the script or .env file.")
        return False
    return True

def run_bulk_upload(
    csv: Union[str, os.PathLike, io.TextIOBase],
    *,
    result_out: Optional[Dict[str, Any]] = None,
    log_lines: Optional[List[str]] = None,
) -> bool:
    """Create Redmine issues from CSV.

    result_out: if provided, filled with keys success, failed, skipped, total_rows,
        completed (bool), and optional error (str) on early failure.
    log_lines: if provided, each log line is appended (for UIs); still prints to stdout.
    """
    global _log_lines_sink
    prev_sink = _log_lines_sink
    if log_lines is not None:
        log_lines.clear()
    _log_lines_sink = log_lines

    def _result(**kwargs: Any) -> None:
        if result_out is not None:
            result_out.clear()
            result_out.update(kwargs)

    try:
        if not validate_config():
            _result(
                completed=False,
                success=0,
                failed=0,
                skipped=0,
                total_rows=0,
                error="Invalid REDMINE_URL or REDMINE_API_KEY (check .env)",
            )
            return False

        label = getattr(csv, "name", None) or "(stream)"
        log("INFO", f"Starting bulk upload from {label}")

        tasks = parse_csv(csv)
        if tasks is None:
            _result(
                completed=False,
                success=0,
                failed=0,
                skipped=0,
                total_rows=0,
                error="Failed to parse CSV",
            )
            return False
        if not tasks:
            log("ERROR", "No tasks found in CSV file")
            _result(
                completed=False,
                success=0,
                failed=0,
                skipped=0,
                total_rows=0,
                error="No data rows in CSV",
            )
            return False

        # Cache for IDs to reduce API calls
        id_cache = {
            "trackers": {},
            "priorities": {},
            "versions": {},
        }
        project_members_cache = {}
        assignee_cache = {}

        fallback_project_id = _env_int("REDMINE_PROJECT_ID")
        if fallback_project_id is not None:
            log("OK", f"REDMINE_PROJECT_ID={fallback_project_id} set as fallback")
        log("OK", "Ready to process tasks.")

        # Process each task
        success_count = 0
        skipped_count = 0
        failed_count = 0

        for idx, task in enumerate(tasks, 1):
            subject = task.get('subject')
            parent_task_str = task.get('parent_task')

            if subject:
                subject = clean_subject(subject)

            if not any([subject, parent_task_str]):
                skipped_count += 1
                continue

            if not subject:
                log("WARN", f"Row {task['row']}: Skipping empty task (no subject)")
                skipped_count += 1
                continue

            log("INFO", f"Processing task {idx}: {subject[:60]}")

            parent_id = parse_parent_id(parent_task_str)
            raw_redmine = (task.get("redmine_id_raw") or "").strip()
            project_ref = parse_project_ref(raw_redmine) if raw_redmine else None
            project_id = project_ref
            if raw_redmine and project_ref is None:
                log(
                    "WARN",
                    f"  → Row {task['row']}: invalid RedmineID {raw_redmine!r} (empty after #); using parent or env",
                )
            if project_id is not None:
                log("INFO", f"  → Project from CSV RedmineID={project_id!r}")
            elif parent_id:
                log("INFO", f"  → Looking up parent issue #{parent_id}...")
                project_id = get_parent_issue_project_id(parent_id)
            if project_id is None and fallback_project_id is not None:
                project_id = fallback_project_id
                log("INFO", f"  → Using fallback REDMINE_PROJECT_ID={project_id}")
            if project_id is None:
                log(
                    "ERROR",
                    f"Skipping: no project (set RedmineID on row, or Parent task, or REDMINE_PROJECT_ID)",
                )
                failed_count += 1
                continue

            # Get or cache tracker ID
            tracker_name = task.get('tracker') or 'Task'
            if tracker_name not in id_cache['trackers']:
                id_cache['trackers'][tracker_name] = get_tracker_id(tracker_name)
            tracker_id = id_cache['trackers'][tracker_name]

            if not tracker_id:
                log("ERROR", f"Skipping: Tracker '{tracker_name}' not found")
                failed_count += 1
                continue

            # Get or cache priority ID
            priority_name = task.get('priority') or 'Normal'
            if priority_name not in id_cache['priorities']:
                id_cache['priorities'][priority_name] = get_priority_id(priority_name)
            priority_id = id_cache['priorities'][priority_name]

            if not priority_id:
                log("ERROR", f"Skipping: Priority '{priority_name}' not found")
                failed_count += 1
                continue

            # Assignee: match CSV label to project members first, then users.json
            assignee_id = None
            if task.get('assignee'):
                assignee_name = task.get('assignee')
                assignee_id = resolve_assignee_id(
                    project_id,
                    assignee_name,
                    project_members_cache,
                    assignee_cache,
                )
                if not assignee_id:
                    log("INFO", f"  → Assignee '{assignee_name}' not found; task unassigned")

            # Get or cache version ID (optional)
            version_id = None
            if task.get('target_version'):
                version_name = task.get('target_version')
                cache_key = f"{project_id}:{version_name}"
                if cache_key not in id_cache['versions']:
                    id_cache['versions'][cache_key] = get_version_id(project_id, version_name)
                version_id = id_cache['versions'][cache_key]
                if not version_id:
                    log("INFO", f"  → Version '{version_name}' not found; task created without version")

            # Parse dates
            due_date = parse_date(task.get('due_date'))
            start_date = parse_date(task.get('start_date'))

            # Parse estimated hours (✅ FIXED: returns hours directly, not days)
            estimated_hours = parse_estimated_hours(task.get('estimated_time'))

            # Create issue
            issue_id = create_issue(
                project_id,
                subject,
                assignee_id,
                due_date,
                priority_id,
                tracker_id,
                version_id,
                parent_id,
                estimated_hours,
                start_date
            )

            if issue_id:
                success_count += 1
            else:
                failed_count += 1

        # Summary
        log("INFO", "=" * 70)
        log("INFO", f"COMPLETED: {success_count} created, {failed_count} failed, {skipped_count} skipped")
        log("INFO", f"Total rows processed: {len(tasks)}")
        log("INFO", "=" * 70)
        _result(
            completed=True,
            success=success_count,
            failed=failed_count,
            skipped=skipped_count,
            total_rows=len(tasks),
        )
        return True
    finally:
        _log_lines_sink = prev_sink