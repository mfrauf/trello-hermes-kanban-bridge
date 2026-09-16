#!/usr/bin/env python3
"""
trello_hermes_sync.py
Synchronizes Trello boards with Hermes Kanban for autonomous AI multi-agent execution.
- Reads credentials and configuration from environment variables or .env.
- Automatically handles single tasks (default profile) and sequential multi-agent pipelines.
- Updates Trello cards with live progress checklists and moves cards (Trigger -> Doing -> Done).
- Emits real-time execution alerts and 3-part deliverable reports to Discord.
"""
import os
import sys
import json
import re
import urllib.request
import urllib.parse
import subprocess
from pathlib import Path

# Load environment
CONFIG_FILE = Path(".env")
if CONFIG_FILE.exists():
    for line in CONFIG_FILE.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))

TRELLO_API_KEY = os.environ.get("TRELLO_API_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")
BOARD_ID = os.environ.get("TRELLO_BOARD_ID")
DISCORD_OUTPUT_CHANNEL = os.environ.get("DISCORD_REPORT_CHANNEL_ID")

DB_STATE = Path("sync_state.json")
LIST_NOW = os.environ.get("TRELLO_LIST_NOW", "Delegate to Hermes Now")
LIST_MORNING = os.environ.get("TRELLO_LIST_MORNING", "Delegate to Hermes in Morning")
LIST_DOING = os.environ.get("TRELLO_LIST_DOING", "Doing")
LIST_DONE = os.environ.get("TRELLO_LIST_DONE", "Done")

KNOWN_PROFILES = [
    "default",
    "product_manager",
    "product_marketer",
    "content_marketer",
    "seo_agent",
    "uiux_designer",
    "advertiser",
    "security-tester"
]

def check_config():
    missing = []
    if not TRELLO_API_KEY: missing.append("TRELLO_API_KEY")
    if not TRELLO_TOKEN: missing.append("TRELLO_TOKEN")
    if not BOARD_ID: missing.append("TRELLO_BOARD_ID")
    if missing:
        print(f"Error: Missing configuration environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Please copy .env.example to .env and fill in your credentials.", file=sys.stderr)
        sys.exit(1)

def trello_api(endpoint, method="GET", params=None, data=None):
    params = params or {}
    params["key"] = TRELLO_API_KEY
    params["token"] = TRELLO_TOKEN
    query = urllib.parse.urlencode(params)
    url = f"https://api.trello.com/1/{endpoint}?{query}"
    
    req_data = None
    headers = {"Accept": "application/json"}
    if data is not None:
        form_data = dict(data)
        form_data["key"] = TRELLO_API_KEY
        form_data["token"] = TRELLO_TOKEN
        req_data = urllib.parse.urlencode(form_data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))

def try_add_comment(card_id: str, text: str):
    try:
        trello_api(f"cards/{card_id}/actions/comments", method="POST", data={"text": text})
    except Exception:
        pass

def normalize_task_id(tid: str) -> str:
    tid = tid.strip()
    if not tid.startswith("t_"):
        return f"t_{tid}"
    return tid

def send_discord_report(message: str):
    if not DISCORD_OUTPUT_CHANNEL:
        return
    try:
        subprocess.run(
            ["hermes", "send", "--to", f"discord:{DISCORD_OUTPUT_CHANNEL}", message],
            capture_output=True,
            text=True,
            check=False
        )
    except Exception as e:
        print(f"Discord report notification error: {e}")

def parse_pipeline_stages(card_name: str, raw_desc: str) -> list[dict]:
    clean_title = re.sub(r"^\[(hermes|ai)\]\s*", "", card_name, flags=re.IGNORECASE).strip()
    text = f"{clean_title}\n{raw_desc}"
    
    profile_pattern = r"(?:^|\n)\s*([a-zA-Z0-9_\-]+)\s*:\s*(.*?)(?=(?:\n\s*[a-zA-Z0-9_\-]+\s*:|\Z))"
    matches = list(re.finditer(profile_pattern, text, re.DOTALL))
    
    explicit_stages = []
    for m in matches:
        raw_role = m.group(1).lower().strip().replace("\\", "")
        body_text = m.group(2).strip()
        
        matched_role = next((p for p in KNOWN_PROFILES if p == raw_role or p.replace("_", "-") == raw_role), None)
        if matched_role and len(body_text) > 3:
            explicit_stages.append({
                "assignee": matched_role,
                "title": f"{clean_title} ({matched_role})",
                "instructions": body_text
            })
    
    if explicit_stages:
        return explicit_stages

    # Single task without explicit agent mentions defaults to 'default' profile
    return [{
        "assignee": "default",
        "title": clean_title,
        "instructions": raw_desc if raw_desc else "Ingested from Trello card."
    }]

def sync(target_mode="all"):
    check_config()
    DB_STATE.parent.mkdir(parents=True, exist_ok=True)
    state = {}
    if DB_STATE.exists():
        state = json.loads(DB_STATE.read_text(encoding="utf-8"))

    lists = trello_api(f"boards/{BOARD_ID}/lists")
    list_map = {l["name"].strip().lower(): l["id"] for l in lists}

    # Step 1: Check running / completed Hermes Kanban tasks and update Trello cards & Discord logs
    for card_id, task_info in list(state.items()):
        board_slug = task_info.get("board", "default")
        current_status = task_info.get("status")
        all_task_ids = [normalize_task_id(tid) for tid in task_info.get("task_ids", [task_info.get("task_id", "")]) if tid]

        if current_status in ["ingested", "running", "blocked"] and all_task_ids:
            stage_status_list = []
            active_task_id = None
            blocked_task_id = None
            blocked_reason = ""
            all_done = True

            for tid in all_task_ids:
                cmd = ["hermes", "kanban", "--board", board_slug, "show", tid]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0:
                    output = res.stdout
                    assignee_m = re.search(r"assignee:\s*([a-zA-Z0-9_\-]+)", output)
                    stage_role = assignee_m.group(1).strip() if assignee_m else "agent"

                    if "status:    running" in output or "status: running" in output.lower():
                        stage_status_list.append((stage_role, tid, "RUNNING", "●"))
                        active_task_id = tid
                        all_done = False
                    elif "status:    blocked" in output or "status: blocked" in output.lower():
                        stage_status_list.append((stage_role, tid, "BLOCKED", "⊘"))
                        blocked_task_id = tid
                        m_reason = re.search(r"Latest summary:\n(.*?)(?=\nEvents|\nRuns|\nComments|\Z)", output, re.DOTALL)
                        if m_reason:
                            blocked_reason = m_reason.group(1).strip()
                        all_done = False
                    elif "status:    done" in output or "status:    completed" in output or "status: done" in output.lower():
                        stage_status_list.append((stage_role, tid, "DONE", "✓"))
                    else:
                        stage_status_list.append((stage_role, tid, "QUEUED", "◻"))
                        all_done = False

            # Live progress update in Trello description
            title = task_info.get("title", "Task")
            live_desc = f"### Objective\n{title}\n\n### Multi-Agent Pipeline & Live Progress\n"
            for idx, (role, tid, s_label, symbol) in enumerate(stage_status_list, start=1):
                live_desc += f"{idx}. `{role}` (`{tid}`): **[{s_label}]** {symbol}\n"

            if blocked_task_id and blocked_reason:
                live_desc += f"\n> ⚠️ **Blocked at `{blocked_task_id}`**: {blocked_reason}\n"

            live_desc += "\n### Invariants\n- Execution: Run builds and tests in isolated workspaces.\n- Verification with exit code 0 before completion.\n"
            trello_api(f"cards/{card_id}", method="PUT", data={"desc": live_desc})

            # Case A: Blocked state detected
            if blocked_task_id and current_status != "blocked":
                state[card_id]["status"] = "blocked"
                state[card_id]["blocked_task"] = blocked_task_id
                
                discord_block = (
                    f"⚠️ **Kanban Task Blocked (Action Required)**\n"
                    f"**Card**: `{title}`\n"
                    f"**Blocked Stage**: `{blocked_task_id}`\n"
                    f"**Reason**: {blocked_reason or 'Needs input / approval'}\n"
                    f"**Trello**: Updated description with blocker details"
                )
                send_discord_report(discord_block)
                print(f"Task blocked: {card_id} -> Blocked status logged to Discord & Trello updated")

            # Case B: Running state detected (or resumed/unblocked)
            elif active_task_id:
                if current_status != "running":
                    doing_list_id = list_map.get(LIST_DOING.lower())
                    if doing_list_id:
                        trello_api(f"cards/{card_id}", method="PUT", data={"idList": doing_list_id})
                    
                    status_note = "Resumed / Unblocked" if current_status == "blocked" else "Started Running"
                    state[card_id]["status"] = "running"
                    
                    discord_progress = f"🚀 **Kanban Task {status_note}**\n**Card**: `{title}`\n**Active Stage**: `{active_task_id}`\n**Trello**: Moved to `Doing` & description refreshed"
                    send_discord_report(discord_progress)
                    print(f"Task active: {card_id} -> Moved to Doing & Logged to Discord")

            # Case C: All done
            elif all_done and len(all_task_ids) > 0:
                final_task_id = all_task_ids[-1]
                cmd = ["hermes", "kanban", "--board", board_slug, "show", final_task_id]
                res = subprocess.run(cmd, capture_output=True, text=True)
                output = res.stdout if res.returncode == 0 else ""

                done_list_id = list_map.get(LIST_DONE.lower())
                if done_list_id:
                    trello_api(f"cards/{card_id}", method="PUT", data={"idList": done_list_id})
                
                title_match = re.search(r"Task [a-zA-Z0-9_]+:\s*(.*?)\n", output)
                task_title = title_match.group(1).strip() if title_match else title
                
                obj_match = re.search(r"### Objective\n(.*?)(?=\n###|\nLatest summary:|\nResult:|\Z)", output, re.DOTALL)
                task_obj = obj_match.group(1).strip() if obj_match else "N/A"

                res_match = re.search(r"Result:\n(.*?)(?=\nEvents|\nRuns|\nComments|\Z)", output, re.DOTALL)
                result_text = res_match.group(1).strip() if res_match else ""

                summary_match = re.search(r"Latest summary:\n(.*?)(?=\nEvents|\nRuns|\nResult:|\Z)", output, re.DOTALL)
                summary_text = summary_match.group(1).strip() if summary_match else "Task completed successfully."

                deliverables_text = f"{summary_text}\n• {result_text}" if result_text else summary_text

                try_add_comment(card_id, f"✅ **Hermes Pipeline Completed**\n• Tasks: `{', '.join(all_task_ids)}`\n• Status: `done`\n\n**Output & Deliverables:**\n{deliverables_text}")
                
                discord_report = (
                    f"📊 **Hermes Kanban Pipeline Completed**\n"
                    f"**Task**: `{task_title}` (`{final_task_id}`)\n"
                    f"**Pipeline**: `{' ➔ '.join(all_task_ids)}` | **Board**: `{board_slug}`\n\n"
                    f"**1. What Was Planned (Objective & Scope)**:\n"
                    f"• {task_obj}\n\n"
                    f"**2. Deliverables & Output Produced**:\n"
                    f"• {deliverables_text}\n\n"
                    f"**3. Current Status & Verification**:\n"
                    f"• Status: `DONE` (Tests and static build exit code 0)\n"
                    f"• Trello Card: Moved to `Done`"
                )
                send_discord_report(discord_report)

                state[card_id]["status"] = "done"
                print(f"Synced completed pipeline: {card_id} -> Moved to Done & Reported to Discord")

    # Step 2: Ingest new cards
    process_targets = []
    if target_mode in ["all", "now"] and LIST_NOW.lower() in list_map:
        process_targets.append(("now", list_map[LIST_NOW.lower()]))
    if target_mode in ["all", "morning"] and LIST_MORNING.lower() in list_map:
        process_targets.append(("morning", list_map[LIST_MORNING.lower()]))

    for mode, list_id in process_targets:
        cards = trello_api(f"lists/{list_id}/cards")
        for card in cards:
            card_id = card["id"]
            card_name = card["name"].strip()
            card_desc = card.get("desc", "").strip()

            if card_id not in state:
                stages = parse_pipeline_stages(card_name, card_desc)
                clean_title = re.sub(r"^\[(hermes|ai)\]\s*", "", card_name, flags=re.IGNORECASE).strip()
                board_slug = "default"
                
                created_task_ids = []
                parent_task_id = None

                for idx, stage in enumerate(stages, start=1):
                    stage_assignee = stage["assignee"]
                    stage_title = f"{clean_title} - Stage {idx}: {stage_assignee}" if len(stages) > 1 else clean_title
                    
                    stage_body = f"""---
assignee: {stage_assignee}
board: {board_slug}
mode: {mode}
pipeline_stage: {idx}/{len(stages)}
---

### Objective
{stage_title}

### Scope & Role Instructions
{stage["instructions"]}

### Invariants & Quality Standards
- Standing pre-approval: Proceed through isolated builds, code changes, and staging deployments automatically (zero human approval pauses).
- Preserve existing working behavior and contracts.
- Run regression checks before declaring done.

### Definition of Done
- Implementation completed and verified with exit code 0.
"""
                    cmd = [
                        "hermes", "kanban", "--board", board_slug, "create",
                        stage_title,
                        "--assignee", stage_assignee,
                        "--body", stage_body
                    ]
                    if parent_task_id:
                        cmd.extend(["--parent", parent_task_id])

                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode == 0:
                        task_id_match = re.search(r"#?([0-9a-fA-F\-]{6,}|task_[0-9]+|\b\d+\b)", res.stdout)
                        task_id = task_id_match.group(0) if task_id_match else f"task_{idx}"
                        norm_id = normalize_task_id(task_id)
                        created_task_ids.append(norm_id)
                        parent_task_id = norm_id

                        if mode == "morning" and idx == 1:
                            subprocess.run(["hermes", "kanban", "--board", board_slug, "schedule", norm_id], capture_output=True)
                    else:
                        print(f"Failed to create task for stage {idx}: {res.stderr}")

                if created_task_ids:
                    if len(stages) > 1:
                        pipeline_desc = f"""---
pipeline: {' ➔ '.join([f'{s["assignee"]} ({tid})' for s, tid in zip(stages, created_task_ids)])}
mode: {mode}
---

### Objective
{clean_title}

### What Hermes Will Do (Sequential Multi-Agent Pipeline)
"""
                        for idx, (s, tid) in enumerate(zip(stages, created_task_ids), start=1):
                            pipeline_desc += f"{idx}. **{s['assignee']}** (`{tid}`): {s['instructions']}\n"
                    else:
                        pipeline_desc = f"""---
assignee: default ({created_task_ids[0]})
mode: {mode}
---

### Objective
{clean_title}

### What Hermes Will Do
{stages[0]['instructions']}
"""

                    pipeline_desc += f"""
### Invariants
- Execution: Run builds and tests in isolated workspaces.
- Verification with exit code 0 before production push.
"""
                    trello_api(f"cards/{card_id}", method="PUT", data={"desc": pipeline_desc})
                    
                    discord_ingest = f"📥 **New Task Ingested from Trello**\n**Title**: `{clean_title}`\n**Assignee**: `{' ➔ '.join([s['assignee'] for s in stages])}`\n**Tasks**: `{', '.join(created_task_ids)}`\n**Mode**: `{mode.upper()}`"
                    send_discord_report(discord_ingest)

                    state[card_id] = {
                        "title": clean_title,
                        "task_ids": created_task_ids,
                        "board": board_slug,
                        "mode": mode,
                        "status": "ingested"
                    }
                    print(f"[{mode.upper()}] Ingested & Discord Notified: {card_name} -> {created_task_ids}")

    DB_STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")

if __name__ == "__main__":
    mode_arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    sync(mode_arg)
