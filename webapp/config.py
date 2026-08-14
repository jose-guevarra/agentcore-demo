"""Configuration for the webapp, loaded from environment / .env.

Mirrors the values used by tools/test_agent.sh and tools/user_login.sh.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()

REQUIRED_VARS = ("ACCOUNT_ID", "AGENTCORE_RUNTIME_ID", "COGNITO_CLIENT_ID")


@dataclass(frozen=True)
class Config:
    region: str
    account_id: str
    runtime_id: str
    cognito_client_id: str

    @property
    def runtime_arn(self) -> str:
        return f"arn:aws:bedrock-agentcore:{self.region}:{self.account_id}:runtime/{self.runtime_id}"

    @property
    def invoke_url(self) -> str:
        encoded_arn = quote(self.runtime_arn, safe="")
        return f"https://bedrock-agentcore.{self.region}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"


def load_config() -> Config:
    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required webapp config: "
            + ", ".join(missing)
            + ". Copy webapp/.env.example to webapp/.env and fill it in "
            "(see webapp/README.md)."
        )

    return Config(
        region=os.environ.get("AWS_REGION", "us-east-1"),
        account_id=os.environ["ACCOUNT_ID"],
        runtime_id=os.environ["AGENTCORE_RUNTIME_ID"],
        cognito_client_id=os.environ["COGNITO_CLIENT_ID"],
    )
