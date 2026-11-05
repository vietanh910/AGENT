"""Exercise real Word files through delivery, versioning, failures and authenticated downloads."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from docx import Document
import httpx
import pytest
from sqlalchemy import func, select

from control.app.config import settings
from control.app.database.models import ArtifactModel, ProjectModel, ProjectReleaseModel, TaskModel, WorkflowRunModel
from control.app.services import project_report
from control.app.services.project_releases import publish_release
from control.tests.test_change_requests import project, request, drive, change_plan


def report_file(env, run_id):
    with env["factory"]() as session:
        run = session.get(WorkflowRunModel, run_id)
        ref = deepcopy(run.run_metadata["project_report"])
        artifact = session.get(ArtifactModel, ref["artifact_id"])
        return Path(artifact.metadata_json["file_path"]), ref


def word_text(path):
    document = Document(path)
    return "\n".join([*(p.text for p in document.paragraphs),
        *(cell.text for table in document.tables for row in table.rows for cell in row.cells)])


def test_final_word_contains_whole_project_and_is_a_pm_deliverable(project):
    path, ref = report_file(project, "RUN-BASE")
    assert path.name.endswith("project-report-v1.docx")
    assert sha256(path.read_bytes()).hexdigest() == ref["checksum"]
    text = word_text(path)
    for value in ["BÁO CÁO TỔNG KẾT DỰ ÁN", "Library", "Search books", "Borrow books",
                  "Kiến trúc và công nghệ", "Thiết kế cơ sở dữ liệu", "Kiểm thử và bảo mật",
                  "Triển khai và vận hành", "Danh sách sản phẩm bàn giao", "PASS", "APPROVED",
                  "SOURCE_CODE", "ARCHITECTURE", "DATABASE_SCHEMA"]:
        assert value in text
    assert "Nội dung trích đã rút gọn" not in text
    with project["factory"]() as session:
        release = session.scalar(select(ProjectReleaseModel).where(ProjectReleaseModel.run_id == "RUN-BASE"))
        task = session.get(TaskModel, ref["task_id"])
        assert task.role == "PM" and task.action == "report_status"
        assert ref["artifact_id"] in {a["artifact_id"] for a in task.result["artifacts"]}
        assert ref["artifact_id"] in {a["artifact_id"] for a in release.manifest["artifacts"]}
        assert release.manifest["report"] == ref
        assert ref["download_url"].endswith(f"/releases/{release.release_id}/report")


def test_change_report_covers_inherited_modules_and_preserves_old_word(project):
    old_path, old_ref = report_file(project, "RUN-BASE")
    old_bytes = old_path.read_bytes()
    run = request(project, change_plan())
    with project["factory"]() as session:
        assert not session.get(WorkflowRunModel, run.run_id).run_metadata.get("project_report")
    assert drive(project, run.run_id).status == "COMPLETED"
    new_path, new_ref = report_file(project, run.run_id)
    text = word_text(new_path)
    for value in ["Search books", "Borrow books", "Renew loan", "First and second renewals succeed",
                  "RUN-BASE", run.run_id, "v1", "v2", "ARCHITECTURE", "DATABASE_SCHEMA"]:
        assert value in text
    assert old_path.read_bytes() == old_bytes
    assert old_ref["artifact_id"] != new_ref["artifact_id"]
    assert new_path.name.endswith("project-report-v2.docx")


def test_report_failure_blocks_promotion_and_can_resume(project, monkeypatch):
    original = project_report.create_project_report
    run = request(project, change_plan())

    def fail(*args, **kwargs):
        raise OSError("Disk unavailable for Word report")

    monkeypatch.setattr(project_report, "create_project_report", fail)
    blocked = drive(project, run.run_id)
    assert blocked.status == "BLOCKED"
    assert "Final project report could not be created" in blocked.run_metadata["block_reason"]
    with project["factory"]() as session:
        assert session.get(ProjectModel, "LIB").release_counter == 1
        assert session.scalar(select(func.count()).select_from(ProjectReleaseModel)) == 1
        current = session.get(WorkflowRunModel, run.run_id)
        assert not current.run_metadata.get("project_report")
        current.status = "RUNNING"
        session.commit()
    monkeypatch.setattr(project_report, "create_project_report", original)
    assert drive(project, run.run_id).status == "COMPLETED"
    assert report_file(project, run.run_id)[0].is_file()


def test_changed_source_document_cannot_be_silently_summarized(project, monkeypatch):
    original = project_report.create_project_report
    run = request(project, change_plan())

    def corrupt_then_render(session, current, manifest, *args):
        ref = next(a for a in manifest["artifacts"] if a["type"] == "ARCHITECTURE")
        artifact = session.get(ArtifactModel, ref["artifact_id"])
        Path(artifact.metadata_json["file_path"]).write_text("Tampered architecture", encoding="utf-8")
        return original(session, current, manifest, *args)

    monkeypatch.setattr(project_report, "create_project_report", corrupt_then_render)
    result = drive(project, run.run_id)
    assert result.status == "BLOCKED"
    assert "Report source checksum mismatch" in result.run_metadata["block_reason"]
    with project["factory"]() as session:
        assert session.get(ProjectModel, "LIB").release_counter == 1


def test_publish_retry_reuses_the_same_report(project):
    path, ref = report_file(project, "RUN-BASE")
    before = path.read_bytes()
    with project["factory"]() as session:
        count = session.scalar(select(func.count()).select_from(ArtifactModel))
        run = session.get(WorkflowRunModel, "RUN-BASE")
        first = publish_release(session, run, project["store"])
        again = publish_release(session, run, project["store"])
        assert first.release_id == again.release_id
        assert session.scalar(select(func.count()).select_from(ArtifactModel)) == count
        assert again.manifest["report"]["artifact_id"] == ref["artifact_id"]
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_report_downloads_require_auth_and_support_current_and_old_versions(project, monkeypatch):
    from fastapi import FastAPI
    from control.app.api import api_v1_router
    from control.app.api.dependencies import get_db

    monkeypatch.setattr(settings, "ADMIN_TOKEN", "offline-admin-token")
    app = FastAPI()
    app.include_router(api_v1_router)
    from control.app.main import security_exception_handler
    from control.app.security.permissions import SecurityException
    app.add_exception_handler(SecurityException, security_exception_handler)

    def db():
        with project["factory"]() as session:
            yield session

    app.dependency_overrides[get_db] = db
    old_path, old_ref = report_file(project, "RUN-BASE")
    run = request(project, change_plan())
    assert drive(project, run.run_id).status == "COMPLETED"
    new_path, _ = report_file(project, run.run_id)
    headers = {"Authorization": "Bearer offline-admin-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/projects/LIB/report")).status_code == 401
        latest = await client.get("/api/v1/projects/LIB/report", headers=headers)
        assert latest.status_code == 200
        assert latest.headers["content-type"] == project_report.DOCX_MEDIA_TYPE
        assert latest.content == new_path.read_bytes()
        assert latest.headers["x-checksum-sha256"] == sha256(latest.content).hexdigest()
        assert ZipFile(BytesIO(latest.content)).testzip() is None
        old = await client.get(old_ref["download_url"], headers=headers)
        assert old.status_code == 200 and old.content == old_path.read_bytes()
        assert (await client.get(old_ref["download_url"].replace("/LIB/", "/OTHER/"), headers=headers)).status_code == 404
        assert (await client.get("/api/v1/projects/UNKNOWN/report", headers=headers)).status_code == 404
        with project["factory"]() as session:
            session.add(ProjectModel(project_id="NEW", name="New", root_run_id="PENDING", release_counter=0))
            session.commit()
        assert (await client.get("/api/v1/projects/NEW/report", headers=headers)).status_code == 409
        new_path.write_bytes(b"modified report")
        assert (await client.get("/api/v1/projects/LIB/report", headers=headers)).status_code == 409


def test_reads_real_word_sql_and_readme_archives_without_extracting(tmp_path):
    source = tmp_path / "source.zip"
    with ZipFile(source, "w") as archive:
        archive.writestr("project/README.md", "# Cài đặt\npython app.py\nHướng dẫn sử dụng tiếng Việt")
        archive.writestr("project/docs/usage.md", "Cách đăng nhập và mượn sách")
        archive.writestr("project/app.py", "THIS_CODE_SHOULD_NOT_BE_IN_THE_REPORT")
        archive.writestr("../outside.py", "Do not extract")
    text, note = project_report._read_document(source, source_code=True)
    assert "python app.py" in text and "Cách đăng nhập" in text
    assert "THIS_CODE_SHOULD_NOT_BE_IN_THE_REPORT" not in text
    assert not (tmp_path.parent / "outside.py").exists()
    assert note
    word = Document()
    word.add_paragraph("SRS tiếng Việt")
    word.add_table(1, 1).cell(0, 0).text = "Tiêu chí nghiệm thu"
    path = tmp_path / "srs.docx"
    word.save(path)
    text, note = project_report._read_document(path)
    assert "SRS tiếng Việt" in text and "Tiêu chí nghiệm thu" in text and note is None
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE books (id INT);", encoding="utf-8")
    assert "CREATE TABLE books" in project_report._read_document(schema)[0]
