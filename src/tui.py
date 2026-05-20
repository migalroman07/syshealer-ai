# tui.py: SessionLocao
# provides the ui and configures app.
import glob
import os
import re
import uuid

import questionary as q
from dotenv import get_key, set_key
from questionary import Choice
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.ai_core import generate_solution
from src.collector import collect_logs
from src.config import BASE_DIR, load_config, save_config
from src.database import Incident, SessionLocal


def cleanup_menu():
    """Deletes old logs and associated scripts to free space."""
    db = SessionLocal()
    try:
        while True:
            clear_screen()
            target = q.select(
                "========== Database Maintenance ==========",
                choices=[
                    Choice("1. Delete resolved logs only", value="resolved"),
                    Choice("2. Wipe entire Database", value="all"),
                    Choice("<- Back", value="back"),
                ],
            ).ask()

            if not target or target == "back":
                break

            if not q.confirm("Are you absolutely sure?").ask():
                continue

            if target == "resolved":
                logs = db.query(Incident).filter(Incident.status == "resolved").all()
            else:
                logs = db.query(Incident).filter(Incident.status != "ignored").all()

            count = 0
            for log in logs:
                pattern = os.path.join(
                    BASE_DIR, "data", "scripts", f"fix_incident_{log.id}_*.sh"
                )
                for file_path in glob.glob(pattern):
                    if os.path.exists(file_path):
                        os.remove(file_path)

                legacy_path = os.path.join(
                    BASE_DIR, "data", "scripts", f"fix_incident_{log.id}.sh"
                )
                if os.path.exists(legacy_path):
                    os.remove(legacy_path)

                db.delete(log)
                count += 1

            db.commit()
            print(f"\n[+] Cleanup complete. Deleted {count} records and scripts.")
            input("Press Enter to return...")
    finally:
        db.close()


def resolve_placeholders(script: str) -> str | None:
    """Finds placeholders and prompts user. Returns None if aborted."""
    config = load_config()
    features = config.get("features", {})

    placeholders = set(re.findall(r"<[A-Za-z0-9_-]+>|\[[A-Z0-9_]+\]", script))

    if not placeholders:
        return script

    # Smart Placeholders.
    if not features.get("smart_placeholders", True):
        return script

    print("\n========== [Script Requires Manual Variables] ==========")
    print(script.strip())
    print("========================================================\n")
    print("[!] Please provide the missing values.")

    print(
        "Hint: Press Ctrl+Z to suspend app, find info in terminal, then type 'fg' to return.\n"
    )

    for ph in placeholders:
        val = q.text(f"Value for {ph} (empty to abort):").ask()
        if not val:
            return None
        script = script.replace(ph, val)

    return script


def view_resolved_log(log: Incident):
    clear_screen()
    print(f"========== Solved incident ID {log.id} ==========\n")
    print(f"Original issue: {log.raw_log.strip()}")
    print("========== Script that helped ==========")
    print(log.ai_summary.strip() if log.ai_summary else "Script is not available.")
    print("\n===================================================")
    input("Press Enter to return...")


def clear_screen():
    os.system("clear" if os.name == "posix" else "cls")


def ask_for_feedback():
    action = q.select(
        "Did the script execute successfully?",
        choices=[
            Choice("Yes, issue is fixed", value="success"),
            Choice("No, I got an error", value="error"),
            Choice("Abort and exit", value="abort"),
        ],
    ).ask()

    error_output = None
    if action == "error":
        error_output = q.text("Paste the error output from the terminal:").ask()

    return action, error_output


def fix_log(log: Incident, db: Session):
    original_status = log.status
    log.status = "processing"
    db.commit()

    previous_error = None

    while True:
        clear_screen()
        config = load_config()
        features = config.get("features", {})

        if features.get("circuit_breaker", True) and log.attempt > 3:
            print(
                f"\n[-] LIMIT REACHED: AI failed to solve log ID {log.id} after 3 attempts."
            )
            print("[!] This usually means the problem requires human intervention.")

            action = q.select(
                "What to do with this incident?",
                choices=[
                    Choice("1. Force 1 more attempt", value="retry"),
                    Choice("2. Delete log and exit", value="delete"),
                    Choice("3. Keep in waiting and exit", value="exit"),
                ],
            ).ask()

            if action == "retry":
                log.attempt = 3
            elif action == "delete":
                log.status = "ignored"
                db.commit()
                print("\n[+] Log ignored. It won't be collected again.")
                input("Press Enter to return...")
                break
            else:
                log.status = "waiting"
                db.commit()
                break

        print(f"\n[*] Analyzing log ID {log.id} (Attempt {log.attempt})...\n")

        try:
            explanation = "No description"

            if log.executed:
                clean_commands = log.ai_summary
                print("========== [Awaiting Feedback from Previous Run] ==========")
                print(clean_commands)
                print("===========================================================\n")
                print(
                    "[*] Script had already been executed earlier. Type in the result, please."
                )

                action, err_text = ask_for_feedback()

                if action == "success":
                    log.status = "resolved"
                    log.executed = False
                    db.commit()
                    print("\nIncident resolved successfully.")
                    break
                elif action == "error":
                    if err_text:
                        if previous_error:
                            previous_error += f"\n\n[FURTHER ERROR]\n{err_text}"
                        else:
                            previous_error = err_text

                    log.attempt += 1
                    log.executed = False
                    db.commit()
                    continue
                else:
                    log.status = "waiting"
                    db.commit()
                    print("\nAborted. Task returned to waiting status.")
                    break

            elif (
                original_status == "waiting" and log.attempt == 1 and not previous_error
            ):
                clean_commands = log.ai_summary
                explanation = getattr(log, "ai_log_review", "Auto-generated by Daemon")
                print("========== [Auto-generated by Daemon in background] ==========")

            else:
                explanation, clean_commands = generate_solution(
                    log.raw_log, config, previous_error
                )
                print("========== [Generated Bash Script] ==========")

            if (
                not clean_commands
                or clean_commands == "MANUAL_INTERVENTION_REQUIRED"
                or "API Error" in str(explanation)
            ):
                print(f"\n[-] AI could not generate a script. Reason: {explanation}\n")

                action = q.select(
                    "What to do with this broken log?",
                    choices=[
                        Choice("1. Retry AI generation right now", value="retry"),
                        Choice("2. Delete this log and exit", value="delete"),
                        Choice("3. Keep in waiting and exit", value="exit"),
                    ],
                ).ask()

                if action == "retry":
                    log.attempt += 1
                    log.ai_summary = None
                    log.ai_log_review = None
                    db.commit()
                    continue
                elif action == "delete":
                    log.status = "ignored"
                    db.commit()
                    print("\n[+] Log ignored. It won't be collected again.")
                    break
                else:
                    log.status = (
                        "waiting" if original_status == "waiting" else "pending"
                    )
                    db.commit()
                    break
                # ==================================
            print(f"AI Analysis: {explanation}\n")

            resolved_commands = resolve_placeholders(clean_commands)

            if not resolved_commands:
                print("[-] Aborted. Saved to waiting status.")
                log.status = "waiting"
                log.ai_summary = clean_commands
                db.commit()
                break

            clean_commands = resolved_commands

            print(clean_commands)
            print("=============================================\n")
            script_dir = os.path.join(BASE_DIR, "data", "scripts")
            os.makedirs(script_dir, exist_ok=True)

            script_id = uuid.uuid4().hex[:8]
            script_path = os.path.join(
                script_dir, f"fix_incident_{log.id}_{script_id}.sh"
            )

            with open(script_path, "w", encoding="utf-8") as f:
                if not clean_commands.startswith("#!"):
                    f.write("#!/bin/bash\n\n")
                f.write(clean_commands)
                f.write("\n")

            os.chmod(script_path, 0o755)
            print(f"[+] Script saved successfully: {script_path}")

            # Warn about reboots
            if re.search(r"\b(reboot|shutdown|init [06]|poweroff)\b", clean_commands):
                print("\nWARNING: Script contains reboot/shutdown commands.")

            if q.confirm("Run it?").ask():
                log.executed = True
                log.ai_summary = clean_commands
                log.status = "waiting"
                db.commit()

                # Save output.
                auto_capture = features.get("auto_capture", True)
                log_file = "/tmp/syshealer_script.log"

                if auto_capture:
                    cmd = f"sudo bash -c 'set -o pipefail; {script_path} 2>&1 | tee {log_file}'"
                else:
                    cmd = f"sudo bash {script_path}"

                print("\n[*] Executing script...")
                exit_status = os.system(cmd)
                exit_code = (exit_status >> 8) if os.name == "posix" else exit_status

                if exit_code == 0:
                    # Script did well, so just ask if helped.
                    action, err_text = ask_for_feedback()
                else:
                    print(f"\n[-] Script failed with bash exit code {exit_code}.")

                    # Else try to read captured error from terminal
                    err_text = ""
                    if auto_capture and os.path.exists(log_file):
                        with open(log_file, "r", encoding="utf-8") as f:
                            # Send only 30 lines not to waste tokens.
                            err_text = "".join(f.readlines()[-30:]).strip()

                        if q.confirm(
                            "Auto-capture this error and send to AI for a fix right now?"
                        ).ask():
                            action = "error"
                            err_text = err_text or f"Unknown exit code {exit_code}"
                        else:
                            action = "abort"
                            err_text = None
                    else:
                        # Ask user for manual issue input.
                        action, err_text = ask_for_feedback()

                # Process the results.
                if action == "success":
                    log.status = "resolved"
                    log.executed = False
                    db.commit()
                    print("\n[+] Incident resolved successfully.")
                    break
                elif action == "error":
                    if err_text:
                        if previous_error:
                            previous_error += f"\n\n[FURTHER ERROR]\n{err_text}"
                        else:
                            previous_error = err_text

                    log.attempt += 1
                    log.executed = False
                    db.commit()
                    continue
                else:
                    print("\n[*] Saved in waiting status. Provide feedback later.")
                    break
            else:
                print(f"Execute manually via: {script_path}\n")
                log.status = "waiting"
                log.ai_summary = clean_commands
                db.commit()
                input("Press enter to return...")
                break
        except Exception as e:
            print(f"[-] App error: {e}")
            log.status = original_status
            db.commit()
            break

    input("Press Enter to return to menu...")


def fix_menu():
    db = SessionLocal()

    try:
        while True:
            clear_screen()
            log_status = q.select(
                "========== Choose the log status ==========",
                [
                    Choice("1. Pending", value="pending"),
                    Choice("2. Waiting", value="waiting"),
                    Choice("3. Solved", value="resolved"),
                    Choice("4. Force scan system.", value="scan"),
                    Choice("5. Cleanup database.", value="cleanup"),
                    Choice("<- Back", value="back"),
                ],
            ).ask()

            if not log_status or log_status == "back":
                return

            if log_status == "scan":
                clear_screen()
                time_choice = q.select(
                    "How far back should we scan the system logs?",
                    choices=[
                        Choice("1. Last 1 hour", value="1 hour ago"),
                        Choice("2. Last 24 hours", value="24 hours ago"),
                        Choice("3. Last 7 days", value="7 days ago"),
                        Choice("4. Since current system boot", value="boot"),
                        Choice("<- Cancel", value="back"),
                    ],
                ).ask()

                if not time_choice or time_choice == "back":
                    continue

                clear_screen()
                print(f"[*] Force scanning journalctl ({time_choice})...\n")

                collect_logs(time_choice)

                print(
                    "[+] Scan complete! Check the 'Fix issues' -> 'Pending' or 'Waiting' menu."
                )
                input("\nPress Enter to return...")

            if log_status == "cleanup":
                cleanup_menu()

            requested_logs = db.scalars(
                select(Incident).where(Incident.status == log_status)
            ).all()

            if not requested_logs:
                print(f"\nNo {log_status} incidents found.")
                input("Press Enter to return...")
                continue

            ui_choices = []
            for log in requested_logs:
                desc = getattr(log, "ai_log_review")

                explanation = (
                    str("\n" + desc)
                    if desc and desc != "No desciption"
                    else ("\n" + "=" * 30)
                )

                # Shorten long logs for better UI display
                log_preview = log.raw_log.replace("\n", " ")
                if len(log_preview) > 80:
                    log_preview = log_preview[:77] + "..."

                ui_choices.append(
                    Choice(
                        f"ID {log.id} | {log_preview}" + explanation,
                        value=log,
                    )
                )

            ui_choices += [
                Choice("\nDelete logs.", value="delete"),
                Choice("<- Back", value="back"),
            ]

            selected_log = q.select(
                f"========== Select log to work with ({log_status}) ==========",
                ui_choices,
            ).ask()

            if not selected_log or selected_log == "back":
                return
            if selected_log == "delete":
                delete_logs(db, log_status)
                return

            if log_status in ["pending", "waiting"]:
                fix_log(selected_log, db)
            elif log_status == "resolved":
                view_resolved_log(selected_log)

    finally:
        db.close()


def delete_logs(db: Session, log_status: str):
    logs = (
        db.query(Incident)
        .where(Incident.status == log_status)
        .order_by(Incident.id.desc())
        .all()
    )

    choices = [
        q.Choice(f"ID {l.id} | Occurrences: {l.occurrences}", value=l.id) for l in logs
    ]

    clear_screen()
    selected_ids = q.checkbox(
        "Select logs to delete permanently (Space to check, Enter to confirm):",
        choices=choices,
    ).ask()

    if selected_ids:
        confirm = q.confirm(
            f"Are you sure you want to delete {len(selected_ids)} logs?"
        ).ask()
        if confirm:
            db.query(Incident).filter(Incident.id.in_(selected_ids)).update(
                {"status": "ignored"}, synchronize_session=False
            )
            db.commit()
            print(f"[+] Successfully ignored {len(selected_ids)} logs.")
            input("Press Enter to continue...")


def configure_menu(config):
    while True:
        clear_screen()

        aspect = q.select(
            "=========== What you'd like to configure? ==========",
            choices=[
                Choice("1. AI Provider/Model", value="provider"),
                Choice("2. Adjust some features.", value="features"),
                Choice("3. System setup.", value="system"),
                Choice("<- Back", value="back"),
            ],
        ).ask()

        if not aspect or aspect == "back":
            return

        match aspect:

            case "provider":
                while True:
                    clear_screen()
                    providers = list(map(Choice, config["providers"].keys())) + [
                        Choice("New provider (Manual enter)", value="new"),
                        Choice("<- Back", value="back"),
                    ]

                    new_provider = q.select(
                        "========== Select provider ==========", choices=providers
                    ).ask()

                    if not new_provider or new_provider == "back":
                        break

                    # Get .env file.
                    env_path = os.path.join(BASE_DIR, ".env")

                    if new_provider == "new":
                        provider = q.text("Enter provider name: ").ask()
                        if not provider:
                            continue

                        base_url = q.text("Enter base URL: ").ask()
                        if not base_url:
                            continue

                        # GEt the api key.
                        actual_key = q.password(
                            f"Enter the SECRET API key for {provider} (input hidden): "
                        ).ask()
                        if not actual_key:
                            continue

                        # Auto-generate new var name
                        api_key_var = f"{provider.upper().replace(' ', '_')}_API_KEY"

                        # Create if not exists.
                        if not os.path.exists(env_path):
                            open(env_path, "a").close()

                        set_key(env_path, api_key_var, actual_key)

                        # Refresh python's memory
                        os.environ[api_key_var] = actual_key

                        # Save to config
                        config["providers"][provider] = {
                            "base_url": base_url,
                            "api_key": api_key_var,
                            "model": "",
                            "available_models": [],
                        }
                    else:
                        provider = new_provider
                        api_key_var = config["providers"][provider].get("api_key", "")

                        if not api_key_var or not get_key(env_path, api_key_var):

                            actual_key = q.password(
                                f"API key missing in .env. Enter the secret api key for {provider}:"
                            ).ask()

                            if actual_key:
                                if not api_key_var:
                                    api_key_var = (
                                        f"{provider.upper().replace(' ', '_')}_API_KEY"
                                    )

                                if not os.path.exists(env_path):
                                    open(env_path, "a").close()

                                set_key(env_path, api_key_var, actual_key)
                                os.environ[api_key_var] = actual_key

                                config["providers"][provider]["api_key"] = api_key_var
                                clear_screen()
                                print(
                                    f"[+] Config updated. Key securely saved to .env as {api_key_var}"
                                )
                                input("Press Enter to continue...")
                            else:
                                clear_screen()
                                print("[-] Input is empty.")
                                continue
                    config["ai_provider"] = provider

                    clear_screen()
                    models = list(
                        map(
                            Choice,
                            config["providers"][provider].get("available_models", []),
                        )
                    ) + [
                        Choice("New model (Manual enter)", value="new"),
                        Choice("<- Back", value="back"),
                    ]

                    new_model = q.select(
                        f"========== Select model for {provider} ==========",
                        choices=models,
                    ).ask()

                    if not new_model or new_model == "back":
                        continue

                    if new_model == "new":
                        model = q.text("Enter exact model name: ").ask()
                        if not model:
                            continue

                        if (
                            model
                            not in config["providers"][provider]["available_models"]
                        ):
                            config["providers"][provider]["available_models"].append(
                                model
                            )
                    else:
                        model = new_model

                    config["providers"][provider]["model"] = model
                    save_config(config)
                    print(f"\nConfiguration saved: {provider} -> {model}")
                    input("Press Enter to continue...")
                    break

            case "features":
                clear_screen()
                f_conf = config.setdefault("features", {})

                selected = q.checkbox(
                    "Toggle Platform Features (Space to select, Enter to save):",
                    choices=[
                        Choice(
                            "System Snapshot (Include RAM/Disk in prompt)",
                            value="system_snapshot",
                            checked=f_conf.get("system_snapshot", True),
                        ),
                        Choice(
                            "Auto-Capture (Intercept script errors)",
                            value="auto_capture",
                            checked=f_conf.get("auto_capture", True),
                        ),
                        Choice(
                            "Autonomous Mode (AI executes without <PID>)",
                            value="autonomous_mode",
                            checked=f_conf.get("autonomous_mode", False),
                        ),
                        Choice(
                            "Auto-Summary (Generate short log titles)",
                            value="auto_summary",
                            checked=f_conf.get("auto_summary", True),
                        ),
                        Choice(
                            "Circuit Breaker (Limit attempts to 3, ask if needed more attempts)",
                            value="circuit_breaker",
                            checked=f_conf.get("circuit_breaker", True),
                        ),
                        # Choice(
                        #     "Smart Placeholders (Ask for missing values)",
                        #     value="smart_placeholders",
                        #     checked=f_conf.get("smart_placeholders", True),
                        # ),
                    ],
                ).ask()

                if selected is not None:
                    keys = [
                        "system_snapshot",
                        "auto_capture",
                        "autonomous_mode",
                        "auto_summary",
                        "circuit_breaker",
                    ]
                    for key in keys:
                        config["features"][key] = key in selected

                    save_config(config)
                    print("\n[+] Features updated successfully.")
                    input("Press Enter to continue...")

            case "system":
                while True:
                    clear_screen()
                    sys_opt = q.select(
                        "========== System Setup ==========",
                        choices=[
                            Choice("1. Max log length.", value="max_log"),
                            Choice(
                                "2. Choose the database type (Reset required after changing).",
                                value="db_type",
                            ),
                            Choice("3. Mode", value="mode"),
                            Choice("<- Back", value="back"),
                        ],
                    ).ask()

                    if not sys_opt or sys_opt == "back":
                        break

                    if sys_opt == "max_log":
                        new_len = q.text("Enter max log length: ").ask()
                        if new_len and new_len.isdigit():
                            config["system"]["max_log_length"] = int(new_len)
                            save_config(config)
                            print(f"\nMax log length changed to {new_len}.")
                            input("Press Enter to continue...")

                    if sys_opt == "db_type":
                        curr_db_type = config.get("db_type", "sqlite")
                        db_type = q.select(
                            "========== Choose DB type ==========",
                            choices=[
                                Choice("1. SQLite", value="sqlite"),
                                Choice("2. PostreSQl", value="postgres"),
                                Choice("<- Back", value="back"),
                            ],
                        ).ask()

                        if db_type == "back":
                            break
                        if db_type == curr_db_type:
                            pass
                        elif db_type == "sqlite":
                            config["db_type"] = "sqlite"
                        else:
                            config["db_type"] = "postgres"

                        save_config(config)
                        print(f"DB type changed to {db_type}.")
                        continue

                    if sys_opt == "mode":
                        while True:
                            clear_screen()
                            new_mode = q.select(
                                "========== Select mode ==========",
                                choices=[
                                    Choice("1. Manual call", value="manual"),
                                    Choice(
                                        "2. Auto call (choose interval)", value="auto"
                                    ),
                                    Choice("<- Back", value="back"),
                                ],
                            ).ask()

                            if not new_mode or new_mode == "back":
                                break

                            if new_mode == "manual":
                                config["system"]["interval"] = 0
                                save_config(config)
                                print("\nMode changed to Manual.")
                                input("Press Enter to continue...")
                                break
                            elif new_mode == "auto":
                                while True:
                                    new_interval = q.text(
                                        "Enter an interval in minutes (1 for constant checks): "
                                    ).ask()

                                    if not new_interval:
                                        break

                                    if new_interval.isdigit():
                                        config["system"]["interval"] = int(new_interval)
                                        save_config(config)
                                        print(
                                            f"\nInterval changed to {new_interval} minutes."
                                        )
                                        input("Press Enter to continue...")
                                        break
                                break


def main_menu():
    while True:
        clear_screen()

        config = load_config()
        db_type = config.get("db_type", "sqlite")
        provider = config["ai_provider"]
        model = config["providers"][provider].get("model", "Unknown")

        option = q.select(
            f"=========== AI system fixer [{db_type}] | Current model: {model} ==========\n",
            choices=[
                Choice(title="1. Fix issues", value="fix"),
                Choice(title="2. Configure", value="configure"),
                Choice(title="3. Exit", value="exit"),
            ],
        ).ask()

        match option:
            case "fix":
                fix_menu()
            case "configure":
                configure_menu(config)
            case _:
                clear_screen()
                print("Bye")
                break
