"""PM's explicitly authorized fallback for missing documents and cumulative source."""

from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile

from pydantic import BaseModel, Field

from agent_contract.enums import ArtifactType, ErrorCode, TaskStatus
from agent_contract.models import TaskResult, blocked_result, make_error
from tools.artifact import save_artifact


RECOVERABLE_ACTIONS = {
    "define_backlog", "write_requirements", "write_user_stories", "design_architecture", "design_schema",
    "implement_project", "implement_feature", "fix_bugs", "create_test_cases",
    "run_sprint_review", "run_retrospective", "track_blockers", "report_status",
}


class RecoveryFile(BaseModel):
    path: str = Field(min_length=1)
    content: str


class RecoveryOutput(BaseModel):
    summary: str
    files: list[RecoveryFile] = Field(min_length=1, max_length=100)


def safe_name(name):
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or ":" in normalized or "\x00" in normalized:
        raise ValueError("Recovery output needs a safe relative file path")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("Recovery output path escapes its workspace")
    return str(PurePosixPath(normalized))


def recover(agent, request):
    original = request.context.get("recovery_original", {})
    action = original.get("action")
    if action not in RECOVERABLE_ACTIONS:
        raise ValueError("PM cannot replace a quality verdict, acceptance, gate or real deployment")
    if agent.llm.mock and not getattr(agent.llm, "mock_responder", None):
        return blocked_result(request, make_error(ErrorCode.LLM_ERROR,
            "PM recovery needs a configured real LLM; no simulated deliverable was generated.", retryable=True))
    kind = ArtifactType(request.context["output_artifact_type"])
    source, evidence = {}, []
    budget = 180000
    for ref in request.input_artifacts:
        name = Path(ref.uri.replace("\\", "/")).name
        path = Path(agent.config.artifact_dir) / "inbox" / name
        if not path.is_file():
            raise ValueError(f"Recovery input is missing: {ref.artifact_id}")
        if ref.type == ArtifactType.SOURCE_CODE:
            if path.suffix.lower() != ".zip":
                raise ValueError("PM source recovery requires the complete source ZIP")
            with ZipFile(path) as archive:
                infos = [info for info in archive.infolist() if not info.is_dir()]
                if len(infos) > 3000 or sum(i.file_size for i in infos) > 100 * 1024 * 1024:
                    raise ValueError("Source ZIP exceeds recovery workspace limits")
                for info in infos:
                    entry = safe_name(info.filename)
                    if entry.casefold() in {key.casefold() for key in source}:
                        raise ValueError("Duplicate source path in recovery ZIP")
                    source[entry] = archive.read(info)
            for entry, data in source.items():
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if len(text) <= budget:
                    evidence.append({"path": entry, "content": text})
                    budget -= len(text)
        else:
            data = path.read_bytes()
            if len(data) <= budget:
                evidence.append({"artifact": ref.artifact_id, "name": ref.name,
                                 "content": data.decode("utf-8", errors="replace")})
                budget -= len(data)
    prompt = {
        "original": original, "instruction": request.instruction,
        "reason": request.context.get("recovery_reason"),
        "acceptance_criteria": request.context.get("acceptance_criteria", []),
        "source_files": list(source), "available_evidence": evidence,
    }
    output = agent.llm.complete_json([
        {"role": "system", "content": "You are PM executing an authorized recovery task. Complete the original work "
            "using supplied evidence. Return real file contents, not a plan or placeholders. For source code return "
            "only added/modified text files; the runner preserves every other source file, including binaries. "
            "Do not fabricate test results, security verdicts, deployment or acceptance. Return JSON matching: "
            + json.dumps(RecoveryOutput.model_json_schema())},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ], RecoveryOutput)
    files = {safe_name(item.path): item.content.encode("utf-8") for item in output.files}
    if len({name.casefold() for name in files}) != len(output.files):
        raise ValueError("Duplicate recovery output paths")
    tag = re.sub(r"[^a-zA-Z0-9_-]", "_", request.task_id)
    artifacts = []
    if kind == ArtifactType.SOURCE_CODE:
        # Preserve exact spelling and bytes of every unchanged path.
        existing = {name.casefold(): name for name in source}
        for name, data in files.items():
            source[existing.get(name.casefold(), name)] = data
        content = BytesIO()
        with ZipFile(content, "w") as archive:
            for name, data in source.items():
                archive.writestr(name, data)
        artifacts.append(save_artifact(f"recovery-{tag}-source.zip", kind, content.getvalue(),
            agent.identity.agent_id, agent.config.artifact_dir))
    else:
        for name, data in files.items():
            artifacts.append(save_artifact(f"recovery-{tag}/{name}", kind, data,
                agent.identity.agent_id, agent.config.artifact_dir))
    return TaskResult(task_id=request.task_id, agent_id=request.agent_id, status=TaskStatus.COMPLETED,
        summary=output.summary, outputs=[{"recovered_action": action, "summary": output.summary}], artifacts=artifacts)
