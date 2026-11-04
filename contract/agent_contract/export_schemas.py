import io
import json
import sys
from pathlib import Path

# Đảm bảo in UTF-8 an toàn trên Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from agent_contract.models import (
    AgentError,
    AgentIdentity,
    AgentState,
    ArtifactRef,
    Constraints,
    NextAction,
    TaskRequest,
    TaskResult,
    Verdict,
)
from agent_contract.system_models import (
    Approval,
    AuditEvent,
    DispatchAck,
    DispatchRequest,
    Heartbeat,
    RunTask,
    WorkerRegistration,
    WorkflowRun,
)

MODELS = [
    AgentIdentity,
    AgentState,
    ArtifactRef,
    Constraints,
    TaskRequest,
    AgentError,
    NextAction,
    TaskResult,
    Verdict,
    WorkerRegistration,
    Heartbeat,
    DispatchRequest,
    DispatchAck,
    WorkflowRun,
    RunTask,
    Approval,
    AuditEvent,
]


def export_all_schemas(output_dir: Path | None = None) -> list[Path]:
    """Xuất JSON Schema của tất cả các model sang thư mục đích (mặc định contract/schemas/)."""
    if output_dir is None:
        # Đường dẫn mặc định: contract/schemas/
        output_dir = Path(__file__).resolve().parent.parent / "schemas"

    output_dir.mkdir(parents=True, exist_ok=True)
    generated_files: list[Path] = []

    for model_cls in MODELS:
        schema = model_cls.model_json_schema()
        schema_file = output_dir / f"{model_cls.__name__}.json"
        with open(schema_file, "w", encoding="utf-8") as f:
            json.dump(schema, f, indent=2, ensure_ascii=False)
            f.write("\n")
        generated_files.append(schema_file)
        print(f"Đã xuất schema: {schema_file.name} -> {schema_file}")

    return generated_files


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    exported = export_all_schemas(target)
    print(f"Hoàn thành xuất {len(exported)} JSON Schemas thành công.")
