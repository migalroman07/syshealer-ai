import json
import os
import re
import subprocess
from typing import Any

import httpx
from dotenv import load_dotenv
from openai import OpenAI

from src.config import BASE_DIR

env_path = os.path.join(BASE_DIR, ".env")
load_dotenv(env_path, override=True)


def build_prompt(config: dict, raw_log: str) -> str:
    # Prompt core.
    base_rules = """You are an Elite Linux SRE resolving a critical production incident.
Your goal is to write a highly reliable bash script to fix the system error shown in the logs.

CRITICAL SECURITY RULES (STRICT BLACKLIST):
1. NEVER stop, kill, disable, or restart `sshd`, `ssh`, `networkd`, or `systemd`. Do not break remote access!
2. NEVER use destructive commands like `rm -rf /` or format disks. If the disk is full, use safe cleanup (`apt-get clean`).
3. If a port is blocked, DO NOT blindly kill processes. Write commands to find the blocking PID (e.g., `ss -tulpn` или `lsof`) and handle it safely.
"""
    # Mode switch
    if config["features"].get("autonomous_mode", False):
        mode_rules = """
SCRIPT GENERATION RULES (AUTONOMOUS MODE - ON):
1. You have FULL AUTOMATION rights. You MUST NOT use placeholders.
2. Resolve missing variables (PIDs, IPs, ports) dynamically inside bash pipelines (e.g., `TARGET_PID=$(lsof -t -i:80)`).
3. The script must be self-executing without human input. Add safety checks before destructive actions (e.g., `if [ -n "$TARGET_PID" ]; then kill -9 $TARGET_PID; fi`).
"""
    else:
        mode_rules = """
SCRIPT GENERATION RULES (SAFE MODE - OFF):
1. You are strictly FORBIDDEN from guessing or dynamically resolving PIDs, ports, IPs, or passwords inside bash.
2. If you lack a specific value, you MUST use a SAFE PLACEHOLDER in brackets (e.g., `<BLOCKED_PID>`).
3. Exactly ONE line above the placeholder, you MUST add a bash comment containing the exact terminal command the admin should run to find this value (e.g., `# Run 'ss -tulpn | grep 80' to find the PID`).
"""

    # Answer rules.
    format_rules = """
OUTPUT FORMAT (STRICT EXECUTABLE JSON):
You MUST respond with a valid JSON object. No markdown blockticks.
{
  "reasoning": "Step-by-step root cause analysis.",
  "short_desc": "Summary of the issue.",
  "script": "#!/bin/bash\\nset -e\\n\\n<YOUR_COMMANDS>"
}

System Log:
"""

    return f"{base_rules}\n{mode_rules}\n{format_rules}\n{raw_log}"


def _get_ai_client(config: dict) -> tuple[OpenAI, str]:
    """Initializes and returns OpenAI client and chosen model."""
    provider = config["ai_provider"]
    provider_settings = config["providers"][provider]
    base_url = provider_settings.get("base_url")
    model = provider_settings["model"]
    key_placeholder = provider_settings.get("api_key")

    actual_api_key = os.getenv(key_placeholder)
    timeout = config.get("http_timeout", 30)

    if not actual_api_key and provider != "ollama":
        raise ValueError(
            f"API Key not found! Please ensure {key_placeholder} is set in .env"
        )

    custom_client = httpx.Client(trust_env=False, timeout=timeout)

    client = OpenAI(
        base_url=base_url,
        api_key=actual_api_key,
        http_client=custom_client,
    )
    return client, model


def extract_json_data(text: str) -> tuple[str, str]:
    """Safely gets log desc and solution from JSON-formatted response."""
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)

    if not match:
        if "MANUAL_INTERVENTION_REQUIRED" in text:
            return "Manual intervention required", "MANUAL_INTERVENTION_REQUIRED"
        return "AI response parsing error", text.strip()

    try:
        data = json.loads(match.group(0))
        desc = data.get("short_desc", "No decs")
        script = data.get("script", "")
        return desc.strip(), script.strip()
    except json.JSONDecodeError:
        return "JSON reading error", text.strip()


def _get_system_snapshot() -> str:
    """Gathers real-time system metrics (RAM, Disk space) for AI context."""
    try:
        ram = subprocess.run(
            ["free", "-m"], capture_output=True, text=True
        ).stdout.strip()
        disk = subprocess.run(
            ["df", "-h", "/"], capture_output=True, text=True
        ).stdout.strip()
        return f"\n\n--- SYSTEM SNAPSHOT ---\nRAM Usage (MB):\n{ram}\n\nDisk Usage (/):\n{disk}\n-----------------------"
    except Exception:
        return ""


def generate_solution(
    raw_log: str, config: dict, prev_error: Any | None | str = ""
) -> tuple[str, str]:
    """Sends a request to AI and return a tuple."""
    client, model = _get_ai_client(config)

    # build promt depending on features anabled or not.
    prompt = build_prompt(config, raw_log)

    if prev_error:
        prompt += (
            f"\n\n[CRITICAL FAILURE] The bash script you generated previously CRASHED during execution!\n"
            f"--- BASH ERROR OUTPUT ---\n{prev_error}\n-------------------------\n\n"
            f"YOUR TASK FOR THIS ATTEMPT: Analyze the bash error. Output a COMPLETELY REVISED script in JSON."
        )

    max_len = config["system"].get("max_log_length", 2000)
    trimmed_log = raw_log[-max_len:]

    features = config.get("features", {})

    # System Snapshot
    sys_snapshot = ""
    if features.get("system_snapshot", True):
        sys_snapshot = _get_system_snapshot()

    prompt = f"{PROMPT_TEMPLATE}\nSystem Log:\n{trimmed_log}{sys_snapshot}"

    # Autonomous Mode
    if features.get("autonomous_mode", False):
        prompt += (
            "\n\nRULE: AUTONOMOUS MODE ON. DO NOT use placeholders. "
            "Write dynamic bash logic (e.g., use lsof/awk to find values automatically)."
        )
    else:
        prompt += (
            "\n\nRULE: SAFE MODE. Use placeholders like <PID> or [IP]. "
            "CRITICAL REQUIREMENT: If you use a placeholder, you MUST add a bash comment (#) "
            "right above the command, explaining EXACTLY what terminal command the user "
            "should run to find this missing value."
        )

    # Handle previous error if needed.
    if prev_error:
        prompt += f"\n\n[USER FEEDBACK] The previous generated script failed with the following error:\n{prev_error}\n\nAnalyze this error, update your JSON output to provide a fully corrected bash script."

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        raw_response = response.choices[0].message.content

        desc, script = extract_json_data(raw_response)

        return desc, script

    except Exception as e:
        return f"API Error: {str(e)}", ""


def generate_log_desc(raw_log: str, config: dict) -> str:
    """Generates description for the log to display before fix call."""
    if not config.get("features", {}).get("auto_summary", True):
        return "No summary."

    client, model = _get_ai_client(config)

    trimmed_log = raw_log[:800]
    prompt = f"{DESC_PROMPT}\n{trimmed_log}"

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=30,
        )
        return response.choices[0].message.content.strip().strip("'\"")
    except Exception:
        return "Description generation error"
