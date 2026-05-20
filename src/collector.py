import hashlib
import re
import subprocess
from collections import defaultdict

from src.config import load_config
from src.database import Incident, SessionLocal


def sanitize_and_compress(log_text: str) -> str:
    dangerous_patterns = [
        r"(?i)ignore previous",
        r"(?i)system prompt",
        r"(?i)forget all instructions",
        r"(?i)jailbreak",
    ]
    for pat in dangerous_patterns:
        log_text = re.sub(pat, "[MALICIOUS_PROMPT_REMOVED]", log_text)

    log_text = re.sub(r"0x[0-9a-fA-F]{5,}", "[HEX_DUMP]", log_text)
    log_text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP_REDACTED]", log_text)
    log_text = re.sub(
        r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\S+\s+",
        "",
        log_text,
        flags=re.MULTILINE,
    )
    log_text = re.sub(r"\s+", " ", log_text)
    return log_text.strip()


def generate_log_hash(log_text: str) -> str:
    """Unique hash for incident deduplication."""
    clean_text = re.sub(r"\d+", "", log_text)
    return hashlib.sha256(clean_text.encode("utf-8")).hexdigest()


def collect_logs(custom_since: str | None = None):
    print("[*] Polling journalctl for new errors...")

    cmd = ["journalctl", "-p", "3", "-x", "-n", "100", "--no-pager"]

    if custom_since:
        if custom_since == "boot":
            cmd.append("-b")
        else:
            cmd.extend(["--since", custom_since])
    else:
        cmd.extend(["-n", "100"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=False)
        raw_output = result.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[-] Failed to run journalctl: {e}")
        return

    if not raw_output.strip() or "-- No entries --" in raw_output:
        return

    config = load_config()
    max_len = config.get("max_log_length", 8000)

    service_logs = defaultdict(list)
    for line in raw_output.split("\n"):
        if not line.strip():
            continue
        match = re.search(r"(\S+\[\d+\]|\S+):\s*(.*)", line)
        if match:
            service = match.group(1).split("[")[0]
            service_logs[service].append(match.group(2))
        else:
            service_logs["unknown"].append(line)

    db = SessionLocal()
    try:
        for service, messages in service_logs.items():
            raw_log = "\n".join(messages)

            clean_log = sanitize_and_compress(raw_log)[-max_len:]

            if len(clean_log) < 10:
                continue

            log_hash = generate_log_hash(clean_log)
            existing = (
                db.query(Incident)
                .filter(Incident.log_hash == log_hash)
                .order_by(Incident.id.desc())
                .first()
            )

            if existing:
                if existing.status == "ignored":
                    continue
                if not existing.executed and existing.status != "resolved":
                    existing.occurrences += 1
            else:
                new_incident = Incident(
                    raw_log=f"Service: {service}\nDetails:\n{clean_log}",
                    log_hash=log_hash,
                    status="pending",
                )
                db.add(new_incident)
        db.commit()
    except Exception as e:
        db.rollback()
    finally:
        db.close()
