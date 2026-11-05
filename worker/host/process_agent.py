"""Run each configured agent through its existing CLI in an isolated process."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from dotenv import dotenv_values

from agent_contract.models import TaskRequest, TaskResult


class ProcessAgent:
    def __init__(self, config, environment=None):
        self.root = Path(config.path or "").resolve()
        if not config.path or not (self.root / "cli.py").is_file():
            raise ValueError(f"Agent {config.agent_id} needs a real path containing cli.py; use runner: fake only for simulation")
        self.agent_id, self.role = config.agent_id, config.role
        self.environment = environment or {}
        values = dotenv_values(self.root / ".env")
        artifact_dir = self.root / (values.get("ARTIFACT_DIR") or "artifacts")
        self.config = SimpleNamespace(artifact_dir=str(artifact_dir.resolve()))
        self.port = values.get("PORT") or "8001"

    def handle(self, request):
        payload = TaskRequest.model_validate(request.model_dump(mode="json"))
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "AGENT_ID": self.agent_id, "AGENT_ROLE": self.role,
                    "ARTIFACT_DIR": self.config.artifact_dir, "PORT": self.port})
        env.update(self.environment)
        # The project contract is available even when cwd changes to the agent folder.
        root = Path(__file__).resolve().parents[2]
        env["PYTHONPATH"] = os.pathsep.join([str(root / "contract"), str(root), env.get("PYTHONPATH", "")])
        with tempfile.TemporaryDirectory(prefix="agent-task-") as temporary:
            task_file = Path(temporary) / "task.json"
            task_file.write_text(payload.model_dump_json(), encoding="utf-8")
            timeout = request.constraints.timeout_seconds if request.constraints else 1000
            result = subprocess.run([sys.executable, str(self.root / "cli.py"), "run", str(task_file)],
                cwd=self.root, env=env, capture_output=True, encoding="utf-8", timeout=timeout, check=False)
            if result.returncode:
                # Avoid copying process output, which can contain credentials, into task errors.
                raise RuntimeError(f"Agent {self.agent_id} CLI failed with exit code {result.returncode}")
            return TaskResult.model_validate_json(result.stdout.strip())
