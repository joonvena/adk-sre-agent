"""Centralized configuration for the SRE agent."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

if os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    import google.auth

    _, project_id = google.auth.default()
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"


@dataclass
class SreAgentConfig:
    """SRE agent configuration."""

    model: str = field(
        default=os.getenv("MODEL_NAME", "gemini-3.7-flash"),
        metadata={"help": "Gemini model to use for diagnosis"},
    )
    diagnosis_output_topic: str = field(
        default=os.getenv("DIAGNOSIS_OUTPUT_TOPIC", ""),
        metadata={"help": "Pub/Sub topic to publish diagnosis reports to"},
    )
    default_project: str = field(
        default=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        metadata={"help": "Default GCP project (overridden per-alert)"},
    )
    max_log_entries: int = field(
        default=20,
        metadata={"help": "Max log entries to fetch per query"},
    )
    max_time_window_minutes: int = field(
        default=60,
        metadata={"help": "Max time window for log/metric queries (minutes)"},
    )


config = SreAgentConfig()
