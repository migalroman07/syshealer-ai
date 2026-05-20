import json
import os
import shutil
import sys
from typing import Any, Dict

# Dynamically calculate absolute path to the project root.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CONFIG_BAK_PATH = os.path.join(BASE_DIR, "config.json.bak")

MAX_RETRIES: int = 3
DEFAULT_LOG_LENGTH: int = 8000
HTTP_TIMEOUT: int = 30

DEFAULT_CONFIG: Dict[str, Any] = {
    "db_type": "sqlite",
    "ai_provider": "groq",
    "max_log_length": DEFAULT_LOG_LENGTH,
    "max_retries": MAX_RETRIES,
    "http_timeout": HTTP_TIMEOUT,
    "providers": {
        "groq": {
            "base_url": "https://api.groq.com/openai/v1",
            "api_key": "GROQ_API_KEY",
            "model": "llama-3.3-70b-versatile",
            "available_models": [
                "llama-3.3-70b-versatile",
                "llama-3.1-8b-instant",
                "mixtral-8x7b-32768",
                "gemma2-9b-it",
                "deepseek-r1-distill-llama-70b",
            ],
        },
        "openai": {
            "base_url": "https://api.openai.com/v1",
            "api_key": "OPENAI_API_KEY",
            "model": "gpt-4o-mini",
            "available_models": [
                "gpt-4o-mini",
                "gpt-4o",
                "o1-mini",
                "o1-preview",
                "gpt-4-turbo",
            ],
        },
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "OPENROUTER_API_KEY",
            "model": "anthropic/claude-3.5-sonnet",
            "available_models": [
                "anthropic/claude-3.5-sonnet",
                "google/gemini-2.5-pro",
                "meta-llama/llama-3.3-70b-instruct",
                "deepseek/deepseek-chat",
                "x-ai/grok-2",
            ],
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "api_key": "DEEPSEEK_API_KEY",
            "model": "deepseek-chat",
            "available_models": ["deepseek-chat", "deepseek-reasoner"],
        },
        "ollama": {
            "base_url": "http://localhost:11434/v1",
            "api_key": "OLLAMA_API_KEY",
            "model": "qwen2.5:7b",
            "available_models": [
                "qwen2.5:7b",
                "qwen2.5:1.5b",
                "llama3.2",
                "mistral",
                "deepseek-coder-v2",
                "phi3",
                "gemma2",
            ],
        },
    },
    "system": {"interval": 30, "temperature": 0.1},
    "features": {
        "system_snapshot": True,
        "auto_capture": True,
        "autonomous_mode": False,
        "auto_summary": True,
        "circuit_breaker": True,
        "pause_resume_hint": True,
    },
}


def load_config() -> dict:
    """Loads config file. Creates a default one if it doesn't exist."""

    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)

            if "db_type" not in config:
                config["db_type"] = "sqlite"
            if "autonomous_mode" not in config:
                config["features"]["autonomous_mode"] = False
            return config
    except json.JSONDecodeError as e:
        print(f"\n[-] ERROR: Your config.json file is corrupted! Loading defaults.")
        return DEFAULT_CONFIG


def save_config(updated_config):
    """Saves changes to the config file. Backups config file."""
    if os.path.exists(CONFIG_PATH):
        shutil.copy2(CONFIG_PATH, CONFIG_BAK_PATH)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(updated_config, f, indent=4, ensure_ascii=False)
