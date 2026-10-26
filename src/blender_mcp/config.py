"""Optional telemetry configuration for the self-hosted fork.

No telemetry destination is configured in this fork baseline. Enabling remote
telemetry requires a separate security/privacy review and explicit configuration.
This avoids import failures in upstream telemetry tests without transmitting
prompts, viewport images, or anonymous metadata by default.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TelemetryConfig:
    enabled: bool = False
    max_prompt_length: int = 4000
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_bucket: str = ""
    timeout: float = 10.0


telemetry_config = TelemetryConfig()
