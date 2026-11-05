"""Actual subprocess execution and artifact handoff, without LLM/network access."""

import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import httpx
import pytest

from agent_contract.enums import AgentRole, ArtifactType, TaskStatus
from agent_contract.models import ArtifactRef, TaskRequest
from agent_contract.system_models import DispatchRequest
from worker.host.outbox import OutboxService
from worker.host.process_agent import ProcessAgent
from worker.host.runner import AgentRunner


def test_pm_process_returns_business_modules_without_namespace_collisions(tmp_path, monkeypatch):
    import subprocess
    real_run = subprocess.run
    def checked_run(*args, **kwargs):
        process = real_run(*args, **kwargs)
        assert process.returncode == 0, process.stderr
        return process
    monkeypatch.setattr(subprocess, "run", checked_run)
    source = Path(__file__).resolve().parents[2] / "agents" / "pm"
    target = tmp_path / "pm"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".env", "artifacts", "data", "workspace", "__pycache__"))
    adapter = ProcessAgent(SimpleNamespace(agent_id="pm-01", role="PM", path=str(target)),
                           {"LLM_PROVIDER": "mock", "LLM_API_KEY": ""})
    module = {"module_id": "catalog", "name": "Catalog", "description": "Library book catalog", "tasks": [
        {"task_id": "search", "title": "Search books", "description": "Search catalog by title", "acceptance_criteria": ["Search returns matching books"]}]}
    result = adapter.handle(TaskRequest(task_id="TASK-PLAN-01", project_id="LIB", agent_id="pm-01", role=AgentRole.PM,
        action="create_plan", instruction="Plan the online library project by module",
        context={"execution_plan": True, "project_name": "Library", "modules": [module]}))
    assert result.status == TaskStatus.COMPLETED
    assert result.outputs[0]["module_plan"]["modules"] == [module]
    assert (target / "artifacts" / "module-plan.json").is_file()
    report = adapter.handle(TaskRequest(task_id="TASK-REPORT-01", project_id="LIB", agent_id="pm-01", role=AgentRole.PM,
        action="report_status", instruction="Report actual project progress",
        context={"execution_mode": "modules", "project_tasks": [
            {"task_id": "REAL-CATALOG", "title": "Catalog implementation", "role": "DEV", "status": "COMPLETED"},
            {"task_id": "REAL-LOAN", "title": "Loan implementation", "role": "DEV", "status": "QUEUED"}]}))
    assert "REAL-CATALOG" in report.outputs[0]["report"]["completed"][0]
    assert "REAL-LOAN" in report.outputs[0]["report"]["next"][0]
    fallback = adapter.handle(TaskRequest(task_id="TASK-FALLBACK-01", project_id="LIB", agent_id="pm-01", role=AgentRole.PM,
        action="create_plan", instruction="Plan the whole online library project",
        context={"execution_plan": True, "project_name": "Library"}))
    assert fallback.outputs[0]["planning_warning"]
    change = adapter.handle(TaskRequest(task_id="TASK-CHANGE-01", project_id="LIB", agent_id="pm-01", role=AgentRole.PM,
        action="create_plan", instruction="Plan changes to an existing online library",
        context={"execution_plan": True, "change_request": True, "modules": [module], "project_name": "Library",
                 "architecture_changed": True, "database_changed": True}))
    assert change.outputs[0]["module_plan"]["architecture_changed"] is True
    assert change.outputs[0]["module_plan"]["database_changed"] is True


def test_configured_agent_requires_real_code(tmp_path):
    with pytest.raises(ValueError, match="real path"):
        ProcessAgent(SimpleNamespace(agent_id="dev-01", role="DEV", path=str(tmp_path)))


def test_duplicate_srs_names_keep_both_versions(tmp_path):
    def respond(request):
        content = b"global requirements" if "GLOBAL" in request.url.path else b"module requirements"
        return httpx.Response(200, content=content, headers={"X-Checksum-SHA256": hashlib.sha256(content).hexdigest()})
    agent = SimpleNamespace(config=SimpleNamespace(artifact_dir=str(tmp_path / "artifacts")))
    runner = AgentRunner("dev-01", AgentRole.DEV, agent, OutboxService(str(tmp_path / "outbox.db")),
                         "test-secret", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    from datetime import datetime, timezone
    refs = [ArtifactRef(artifact_id=identifier, name="srs.md", type=ArtifactType.REQUIREMENT,
            uri=f"artifact://{identifier}", created_by="ba-01", created_at=datetime.now(timezone.utc))
            for identifier in ["ART-GLOBAL", "ART-MODULE"]]
    request = DispatchRequest(task_id="TASK-DEV-1", project_id="RUN-1", agent_id="dev-01", role=AgentRole.DEV,
        action="implement_feature", instruction="Implement library", callback_url="http://control/result", input_artifacts=refs)
    assert runner._download_input_artifacts(request) is None
    files = list((tmp_path / "artifacts" / "inbox").glob("*.md"))
    assert {file.read_bytes() for file in files} == {b"global requirements", b"module requirements"}
    assert len({ref.name for ref in request.input_artifacts}) == 2


def test_upload_ids_are_unique_per_task_and_stable_on_retry(tmp_path):
    from datetime import datetime, timezone
    from agent_contract.models import TaskResult
    received = []
    def upload(request):
        # Parse metadata from the multipart request without running a network server.
        body = request.read().decode()
        start = body.index('{"artifact_id"')
        data = json.JSONDecoder().raw_decode(body[start:])[0]
        received.append(data)
        return httpx.Response(201, json={"artifact_id": data["artifact_id"], "name": data["name"],
            "type": data["type"], "uri": "artifact://" + data["artifact_id"], "checksum": data["checksum"],
            "created_by": data["created_by"], "created_at": datetime.now(timezone.utc).isoformat()})
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "source.zip").write_bytes(b"project code")
    agent = SimpleNamespace(config=SimpleNamespace(artifact_dir=str(root)))
    runner = AgentRunner("dev-01", AgentRole.DEV, agent, OutboxService(str(tmp_path / "outbox.db")), "test-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(upload)))
    for tid in ["TASK-1", "TASK-2", "TASK-2"]:
        request = DispatchRequest(task_id=tid, project_id="RUN-1", agent_id="dev-01", role=AgentRole.DEV,
            action="implement_feature", instruction="Implement", callback_url="http://control/result")
        result = TaskResult(task_id=tid, agent_id="dev-01", status=TaskStatus.COMPLETED, summary="Code", artifacts=[
            ArtifactRef(artifact_id="ART-001", name="source.zip", type=ArtifactType.SOURCE_CODE,
                        uri="source.zip", created_by="dev-01", created_at=datetime.now(timezone.utc))])
        assert runner._upload_agent_artifacts(request, result).status == TaskStatus.COMPLETED
    assert received[0]["artifact_id"] != received[1]["artifact_id"]
    assert received[1]["artifact_id"] == received[2]["artifact_id"]


def test_incomplete_index_does_not_drop_declared_output_files(tmp_path):
    from datetime import datetime, timezone
    from agent_contract.models import TaskResult
    uploads = []
    def upload(request):
        body = request.read().decode()
        data = json.JSONDecoder().raw_decode(body[body.index('{"artifact_id"'):])[0]
        uploads.append(data)
        return httpx.Response(201, json={"artifact_id": data["artifact_id"], "name": data["name"],
            "type": data["type"], "uri": "artifact://" + data["artifact_id"], "checksum": data["checksum"],
            "created_by": data["created_by"], "created_at": datetime.now(timezone.utc).isoformat()})
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    for name in ("srs.md", "stories.json"):
        (artifact_dir / name).write_text(name)
    refs = [ArtifactRef(artifact_id=identifier, name=name, type=ArtifactType.REQUIREMENT,
        uri=name, created_by="ba-01", created_at=datetime.now(timezone.utc))
        for identifier, name in (("ART-1", "srs.md"), ("ART-2", "stories.json"))]
    (artifact_dir / "index.json").write_text(json.dumps([refs[0].model_dump(mode="json")]))
    agent = SimpleNamespace(config=SimpleNamespace(artifact_dir=str(artifact_dir)))
    runner = AgentRunner("ba-01", AgentRole.BA, agent, OutboxService(str(tmp_path / "outbox.db")), "test-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(upload)))
    request = DispatchRequest(task_id="TASK-BA-1", project_id="RUN-1", agent_id="ba-01", role=AgentRole.BA,
        action="write_requirements", instruction="Write SRS and stories", callback_url="http://control/result")
    result = TaskResult(task_id=request.task_id, agent_id="ba-01", status=TaskStatus.COMPLETED, summary="Done", artifacts=refs)
    result = runner._upload_agent_artifacts(request, result)
    assert result.status == TaskStatus.COMPLETED
    assert {u["name"] for u in uploads} == {"srs.md", "stories.json"}
    assert all(ref.uri.startswith("artifact://") for ref in result.artifacts)


def test_missing_declared_output_file_prevents_completed_callback(tmp_path):
    from datetime import datetime, timezone
    from agent_contract.models import TaskResult
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    agent = SimpleNamespace(config=SimpleNamespace(artifact_dir=str(artifact_dir)))
    runner = AgentRunner("ba-01", AgentRole.BA, agent, OutboxService(str(tmp_path / "outbox.db")), "test-secret",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("Missing files must not be uploaded"))))
    request = DispatchRequest(task_id="TASK-BA-1", project_id="RUN-1", agent_id="ba-01", role=AgentRole.BA,
        action="write_requirements", instruction="Write SRS", callback_url="http://control/result")
    result = TaskResult(task_id=request.task_id, agent_id="ba-01", status=TaskStatus.COMPLETED, summary="Done", artifacts=[
        ArtifactRef(artifact_id="ART-MISSING", name="srs.md", type=ArtifactType.REQUIREMENT, uri="srs.md",
                    created_by="ba-01", created_at=datetime.now(timezone.utc))])
    result = runner._upload_agent_artifacts(request, result)
    assert result.status == TaskStatus.FAILED and result.error.code.value == "ARTIFACT_ERROR"
