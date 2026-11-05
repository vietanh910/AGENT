"""Human project labels without changing durable project/run identities."""

import re


def usable_name(value, project_id):
    value = " ".join(str(value or "").split()).strip()
    if not value or value == project_id or re.match(r"^(?:PRJ|PROJ|RUN|CHG)[-_]", value, re.I):
        return None
    return value[:255]


def name_from_brief(brief):
    line = next((line.strip(" #*-\t") for line in str(brief or "").splitlines() if line.strip()), "")
    explicit = re.match(r"^(?:tên\s+(?:dự án|ứng dụng|hệ thống)|project\s+name)\s*[:：]\s*(.+)", line, re.I)
    if explicit:
        line = explicit[1]
    else:
        line = re.sub(r"^(?:(?:tôi\s+)?(?:muốn|cần)\s+|hãy\s+|giúp\s+tôi\s+)+", "", line, flags=re.I)
        line = re.sub(r"^(?:xây dựng|phát triển|tạo|làm|build|create|develop)\s+(?:(?:cho tôi|giúp tôi|a|an|một)\s+)*", "", line, flags=re.I)
        line = re.sub(r"^(?:hệ thống|phần mềm|ứng dụng|website|web app|application|app|dự án)\s+", "", line, flags=re.I)
        line = re.split(r"\s+(?:bao gồm|gồm các|với các|có các|với chức năng)\b|[.!?;\n]", line, maxsplit=1, flags=re.I)[0]
    line = " ".join(line.split()).strip(" :.,;\"'")
    if len(line) > 80:
        line = line[:80].rsplit(" ", 1)[0] + "…"
    return line[:1].upper() + line[1:] if line else "Dự án chưa đặt tên"


def project_display_name(project_id, project=None, root=None, latest=None):
    for candidate in [getattr(project, "name", None),
                      (getattr(root, "plan", None) or {}).get("project_name"),
                      (getattr(latest, "plan", None) or {}).get("project_name")]:
        name = usable_name(candidate, project_id)
        if name:
            return name
    return name_from_brief(getattr(root, "brief", None) or getattr(latest, "brief", None))
