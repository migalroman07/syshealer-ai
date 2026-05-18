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

PROMPT_TEMPLATE = """You are an Expert Linux DevOps Engineer resolving a production incident.

CRITICAL SRE RULES:
1. DIAGNOSE FIRST: 
   - If a port is in use, DO NOT just restart the service. Write commands to find the blocking PID (e.g., `ss -tulpn` or `lsof`) and kill it.
   - If the disk is full, DO NOT install new packages. Write safe cleanup commands (e.g., `apt-get clean`, `journalctl --vacuum-time=1d`).
   - If a dpkg lock is held, write a command to find the blocking process before removing the lock.
2. AVOID DUPLICATION: Formulate ONE final cohesive bash script.

OUTPUT FORMAT INSTRUCTIONS (CRITICAL):
You MUST respond STRICTLY with a valid JSON object. Do NOT wrap the JSON in markdown code blocks (like ```json). Do NOT add any conversational text before or after the JSON.

The JSON object MUST contain exactly these 3 keys:
{
  "reasoning": "Step-by-step detailed analysis of the root cause. This is your scratchpad to think deeply. Be as detailed as you want here.",
  "short_desc": "A VERY brief summary of the issue (MAXIMUM 5-7 words). Example: 'Port 8080 is busy with some process.'.",
  "script": "#!/bin/bash\\n\\n# Your final executable bash script here. Put all explanations as bash comments (#) INSIDE this script."
}

System Log:
"""

DESC_PROMPT = """You are a Linux Server Monitor. Analyze the following system log snippet.
Provide a VERY SHORT summary of the problem.
RULES:
1. Maximum 5-7 words.
2. Only output the summary text, no conversational filler.
Example: 'Not enough space on the disk'.

System Log:
"""


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
