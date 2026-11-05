"""Build the PM's final Word report from accepted artifacts and recorded evidence."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.shared import Cm, Pt
from lxml.etree import XMLSyntaxError
from sqlalchemy import select

from agent_contract.quality import quality_verdict
from control.app.database.models import ApprovalModel, ArtifactModel, ProjectReleaseModel, TaskModel, WorkflowRunModel
from control.app.services.artifact_store import ArtifactStoreError, ArtifactStoreService, model_to_artifact_ref


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_CHARS = 24000
MAX_REPORT_CHARS = 350000


class ProjectReportError(ValueError):
    pass


def report_download_url(project_id, release_id=None):
    from urllib.parse import quote
    base = f"/api/v1/projects/{quote(project_id, safe='')}"
    return f"{base}/releases/{quote(release_id, safe='')}/report" if release_id else f"{base}/report"


def artifact_path(store, session, artifact):
    """Resolve server-owned metadata as well as the store's conventional location."""
    path = (artifact.metadata_json or {}).get("file_path")
    if path:
        return Path(path)
    try:
        return store.get_artifact_file_path(session, artifact.artifact_id)[0]
    except ArtifactStoreError:
        return None


def _clean(value):
    # XML 1.0 cannot encode control characters sometimes found in logs.
    return re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", str(value or ""))


def _table(document, headers, rows):
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Light Shading Accent 1"
    for cell, title in zip(table.rows[0].cells, headers):
        cell.text = _clean(title)
    for values in rows:
        for cell, value in zip(table.add_row().cells, values):
            cell.text = _clean(value)


def _text(document, text):
    """Render basic Markdown while preserving code and the agent's original wording."""
    in_code = False
    for line in _clean(text).splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)", line) if not in_code else None
        if heading:
            document.add_heading(heading[2], level=min(len(heading[1]) + 2, 4))
        elif in_code:
            paragraph = document.add_paragraph(line)
            for run in paragraph.runs:
                run.font.name = "Consolas"
                run.font.size = Pt(9)
        elif re.match(r"^\s*[-*]\s+", line):
            document.add_paragraph(re.sub(r"^\s*[-*]\s+", "", line), style="List Bullet")
        else:
            document.add_paragraph(line)


def _read_document(path, source_code=False):
    """Read text, Word, or README files in archives without extracting/executing code."""
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        return "", "Tệp lớn hơn 2 MB; xem bản gốc trong danh sách bàn giao."
    data = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".docx":
        with ZipFile(BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_DOCUMENT_BYTES * 5:
                return "", "Nội dung Word vượt giới hạn đọc; xem bản gốc."
        source = Document(BytesIO(data))
        text = "\n".join(p.text for p in source.paragraphs)
        text += "\n" + "\n".join(" | ".join(c.text for c in row.cells)
            for table in source.tables for row in table.rows)
        return text, None
    if suffix == ".zip":
        with ZipFile(BytesIO(data)) as archive:
            names = [info for info in archive.infolist() if not info.is_dir()
                and (Path(info.filename).name.lower().startswith("readme")
                     or "/docs/" in f"/{info.filename.lower()}")
                and Path(info.filename).suffix.lower() in {".md", ".txt"}]
            sections, total = [], 0
            for info in sorted(names, key=lambda i: i.filename)[:12]:
                if info.file_size > MAX_DOCUMENT_BYTES or total + info.file_size > MAX_DOCUMENT_BYTES:
                    continue
                sections.append(f"{info.filename}\n{archive.read(info).decode('utf-8', errors='replace')}")
                total += info.file_size
            return "\n\n".join(sections), "Chỉ trích README và tài liệu hướng dẫn trong gói mã nguồn; xem gói gốc để đọc đầy đủ."
    if suffix not in {".md", ".txt", ".json", ".yaml", ".yml", ".sql", ".csv", ".log", ".rst"}:
        return "", "Định dạng không hỗ trợ trích nội dung; xem tệp gốc."
    if source_code and suffix not in {".md", ".txt", ".json"}:
        return "", "Mã nguồn được bàn giao riêng; xem tệp gốc."
    text = data.decode("utf-8-sig", errors="replace")
    if suffix == ".json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            pass
    return text, None


def create_project_report(session, run, manifest, version, parent=None, store=None):
    """Persist a versioned report on the final PM task; the caller owns the transaction."""
    store = store or ArtifactStoreService()
    current_tasks = list(session.scalars(select(TaskModel).where(TaskModel.run_id == run.run_id)
        .order_by(TaskModel.created_at, TaskModel.task_id)))
    owner = next((t for t in reversed(current_tasks) if t.role == "PM" and (
        t.action == "report_status" or ((t.context or {}).get("recovery_original") or {}).get("action") == "report_status")), None)
    if not owner:
        raise ProjectReportError("Final PM report_status task is missing")
    for artifact in session.scalars(select(ArtifactModel).where(ArtifactModel.run_id == run.run_id,
                                                               ArtifactModel.task_id == owner.task_id)):
        if (artifact.metadata_json or {}).get("project_report_version") == version:
            path = artifact_path(store, session, artifact)
            if not path or not path.is_file() or sha256(path.read_bytes()).hexdigest() != artifact.checksum:
                raise ProjectReportError("The stored final project report is missing or changed")
            return artifact

    history, seen = [], set()
    previous = parent
    while previous:
        if previous.release_id in seen or previous.project_id != run.project_id:
            raise ProjectReportError("Invalid project release history")
        seen.add(previous.release_id)
        history.append(previous)
        previous = session.get(ProjectReleaseModel, previous.base_release_id) if previous.base_release_id else None
    history.reverse()
    runs = [session.get(WorkflowRunModel, item.run_id) for item in history] + [run]
    if any(item is None for item in runs):
        raise ProjectReportError("Project history refers to a missing run")
    run_ids = [item.run_id for item in runs]
    tasks = list(session.scalars(select(TaskModel).where(TaskModel.run_id.in_(run_ids))
        .order_by(TaskModel.created_at, TaskModel.task_id)))
    artifacts = list(session.scalars(select(ArtifactModel).where(ArtifactModel.run_id.in_(run_ids))
        .order_by(ArtifactModel.created_at, ArtifactModel.artifact_id)))
    artifacts = [a for a in artifacts if not (a.metadata_json or {}).get("project_report_version")]
    task_map = {task.task_id: task for task in tasks}
    artifacts = [a for a in artifacts if not isinstance((task_map[a.task_id].result or {}).get("artifacts"), list)
        or a.artifact_id in {ref.get("artifact_id") for ref in task_map[a.task_id].result["artifacts"] if isinstance(ref, dict)}]
    selected_ids = {a["artifact_id"] for a in manifest["artifacts"]}
    source_ids = {a.artifact_id for a in artifacts}
    if any(a["artifact_id"] not in source_ids and a["artifact_id"] != (manifest.get("report") or {}).get("artifact_id")
           for a in manifest["artifacts"]):
        raise ProjectReportError("A report source artifact is missing from the project history")
    # Include earlier deployment stages, acceptance and retrospective documents too:
    # the release manifest deliberately keeps only the latest producer per slot.
    evidence_types = {"TEST_REPORT", "SECURITY_REPORT", "DEPLOYMENT", "DOCUMENTATION"}
    documents = [a for a in artifacts if a.artifact_id in selected_ids
                 or (a.run_id == run.run_id and a.type in evidence_types)]
    versions = {item.run_id: item.version for item in history}
    versions[run.run_id] = version

    document = Document()
    document.core_properties.title = f"Báo cáo tổng kết dự án — {manifest['project_name']} — v{version}"
    document.core_properties.author = f"Agent PM ({owner.agent_id})"
    document.core_properties.subject = "Tổng hợp từ tài liệu bàn giao và kết quả thực thi đã lưu"
    normal = document.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(11)
    document.sections[0].top_margin = Cm(2)
    document.sections[0].bottom_margin = Cm(2)
    document.sections[0].left_margin = Cm(2)
    document.sections[0].right_margin = Cm(2)
    document.sections[0].page_width = Cm(21)
    document.sections[0].page_height = Cm(29.7)
    document.add_heading("BÁO CÁO TỔNG KẾT DỰ ÁN", 0)
    document.add_paragraph(_clean(manifest["project_name"]), style="Subtitle")
    _table(document, ["Thông tin", "Giá trị"], [
        ["Dự án", run.project_id], ["Phiên bản bàn giao", f"v{version}"], ["Lần thực hiện", run.run_id],
        ["Tổng hợp bởi", f"Agent PM — {owner.agent_id}"], ["Bắt đầu (UTC)", runs[0].created_at.isoformat()],
        ["Điều kiện tổng kết", "Các task đã hoàn tất; PO đã nghiệm thu; kiểm tra chất lượng đã đạt hoặc đã sửa và kiểm tra lại."],
    ])
    document.add_paragraph("Báo cáo tổng hợp dữ liệu đã lưu của dự án. Các phần trích tài liệu giữ nội dung của agent tạo ra; "
        "kết quả kiểm thử và triển khai được ghi theo bằng chứng thực thi, kể cả thông tin mô phỏng nếu có trong tài liệu.")

    document.add_heading("1. Mục tiêu và phạm vi dự án", 1)
    _text(document, runs[0].brief)
    if run.run_id != runs[0].run_id:
        document.add_heading("Yêu cầu thay đổi của phiên bản này", 2)
        _text(document, run.brief)
    _text(document, (run.plan or {}).get("overview", ""))
    document.add_heading("2. Module và chức năng bàn giao", 1)
    for module in manifest.get("modules", []):
        document.add_heading(_clean(module["name"]), 2)
        _text(document, module.get("description", ""))
        _table(document, ["Task / chức năng", "Mô tả", "Tiêu chí nghiệm thu"], [
            [task.get("title", task["task_id"]), task.get("description", ""),
             "\n".join(task.get("acceptance_criteria", []))] for task in module.get("tasks", [])])

    document.add_heading("3. Tiến độ và lịch sử thực hiện", 1)
    _table(document, ["Phiên bản", "Run", "Yêu cầu"], [[f"v{versions[r.run_id]}", r.run_id, r.brief] for r in runs])
    _table(document, ["Phiên bản / task", "Agent", "Công việc", "Trạng thái"], [
        [f"v{versions[t.run_id]} / {t.task_id}", t.agent_id, (t.context or {}).get("title") or t.action,
         t.dispatch_state] for t in tasks])

    warnings, remaining = [], MAX_REPORT_CHARS

    def include(kinds):
        nonlocal remaining
        matching = [a for a in documents if a.type in kinds]
        if not matching:
            document.add_paragraph("Chưa có tài liệu thuộc nhóm này trong dữ liệu bàn giao.")
        for artifact in matching:
            document.add_heading(_clean(f"{artifact.name} — {artifact.created_by} (v{versions[artifact.run_id]})"), 2)
            document.add_paragraph(f"Nguồn: {artifact.artifact_id} | {artifact.uri}")
            path = artifact_path(store, session, artifact)
            note, text = None, ""
            if not path or not path.is_file():
                if (artifact.metadata_json or {}).get("file_path"):
                    raise ProjectReportError(f"Report source file is missing: {artifact.artifact_id}")
                note = "Chỉ có metadata, chưa có tệp nguồn để trích nội dung."
            else:
                if sha256(path.read_bytes()).hexdigest() != artifact.checksum:
                    raise ProjectReportError(f"Report source checksum mismatch: {artifact.artifact_id}")
                try:
                    text, note = _read_document(path, artifact.type == "SOURCE_CODE")
                except (ValueError, KeyError, BadZipFile, PackageNotFoundError, XMLSyntaxError) as exc:
                    note = f"Không đọc được nội dung ({type(exc).__name__}); xem tệp gốc."
            limit = min(MAX_DOCUMENT_CHARS, remaining)
            if len(text) > limit:
                text = text[:limit]
                note = (note + " " if note else "") + "Nội dung trích đã rút gọn; xem tệp gốc để đọc đầy đủ."
            remaining -= len(text)
            if text:
                _text(document, text)
            if note:
                document.add_paragraph(note)
                warnings.append(f"{artifact.name} ({artifact.artifact_id}): {note}")

    document.add_heading("4. Yêu cầu nghiệp vụ và user story", 1)
    include({"BACKLOG", "REQUIREMENT", "USER_STORY"})
    document.add_heading("5. Kiến trúc và công nghệ", 1)
    include({"ARCHITECTURE"})
    document.add_heading("6. Thiết kế cơ sở dữ liệu", 1)
    include({"DATABASE_SCHEMA"})
    document.add_heading("7. Mã nguồn và hướng dẫn cài đặt, sử dụng", 1)
    include({"SOURCE_CODE"})
    document.add_heading("8. Kiểm thử và bảo mật", 1)
    _table(document, ["Phiên bản / task", "Kiểm tra", "Kết quả", "Xử lý"], [
        [f"v{versions[t.run_id]} / {t.task_id}", t.action, quality_verdict(t.result) or "Chưa có verdict",
         "Đã sửa và kiểm tra lại" if (t.context or {}).get("quality_repaired") else (t.result or {}).get("summary", "")]
        for t in tasks if t.action in {"run_test_suite", "security_review"}])
    include({"TEST_CASE", "TEST_REPORT", "SECURITY_REPORT"})
    document.add_heading("9. Triển khai và vận hành", 1)
    include({"DEPLOYMENT"})
    document.add_heading("10. Nghiệm thu và tổng kết của các agent", 1)
    approvals = session.scalars(select(ApprovalModel).where(ApprovalModel.run_id.in_(run_ids))
        .order_by(ApprovalModel.created_at, ApprovalModel.approval_id)).all()
    _table(document, ["Run / cổng", "Quyết định", "Người quyết định"], [
        [f"{a.run_id} / {a.gate}", a.status, a.decided_by or ""] for a in approvals])
    for task in tasks:
        if task.action in {"accept_release", "run_retrospective"}:
            document.add_paragraph(_clean(f"{task.task_id}: {(task.result or {}).get('summary', '')}"))
            _text(document, json.dumps((task.result or {}).get("outputs", []), ensure_ascii=False, indent=2))
    include({"DOCUMENTATION"})
    document.add_heading("11. Giới hạn dữ liệu và công việc tiếp theo", 1)
    document.add_paragraph("Các hạn chế sản phẩm, lỗi còn biết, đề xuất cải tiến và hướng dẫn vận hành nằm trong "
        "các tài liệu kiểm thử, bảo mật, triển khai và retrospective ở trên. Nếu agent chưa ghi nhận nội dung nào, "
        "báo cáo không tự suy đoán thay cho bằng chứng.")
    for warning in warnings:
        document.add_paragraph(_clean(warning), style="List Bullet")
    if not warnings:
        document.add_paragraph("Đã trích được nội dung các tài liệu được chọn trong giới hạn tổng hợp.")
    document.add_heading("12. Danh sách sản phẩm bàn giao của phiên bản", 1)
    _table(document, ["Loại / module", "Tệp / agent", "Nguồn và tải bản gốc"], [
        [f"{a['type']} / {a.get('module_id') or 'Toàn dự án'}", f"{a['name']} / {a['created_by']}",
         f"{a['artifact_id']}\n/api/v1/artifacts/{a['artifact_id']}/content\nSHA-256: {a['checksum']}"]
        for a in manifest["artifacts"] if a["artifact_id"] in source_ids])

    output = BytesIO()
    document.save(output)
    artifact = store.save_artifact(session, output.getvalue(), f"project-report-v{version}.docx", run.run_id,
        owner.task_id, "DOCUMENTATION", owner.agent_id, version=str(version),
        metadata={"project_report_version": version, "project_id": run.project_id,
                  "source_artifact_ids": [a.artifact_id for a in documents], "report_warnings": warnings})
    result = dict(owner.result or {})
    result["artifacts"] = [*(result.get("artifacts") or []), model_to_artifact_ref(artifact).model_dump(mode="json")]
    owner.result = result
    return artifact
