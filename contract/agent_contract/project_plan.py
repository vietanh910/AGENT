"""Project/module planning shared by PM and the control node.

The model chooses business work. Code supplies delivery steps and artifact bindings.
"""

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ModuleTask(BaseModel):
    task_id: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)


class ProjectModule(BaseModel):
    module_id: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tasks: list[ModuleTask] = Field(min_length=1)


class ModulePlan(BaseModel):
    project_name: str = Field(min_length=1)
    overview: str = Field(min_length=1)
    modules: list[ProjectModule] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        module_ids = [m.module_id for m in self.modules]
        if len(set(module_ids)) != len(module_ids):
            raise ValueError("Duplicate module_id")
        for module in self.modules:
            ids = [t.task_id for t in module.tasks]
            if len(set(ids)) != len(ids):
                raise ValueError(f"Duplicate task_id in module {module.module_id}")
        return self


class ChangePlan(ModulePlan):
    """Only affected modules; shared design is revised when the impact requires it."""
    impact_summary: str = "Review the requested change against the existing release"
    architecture_changed: bool = False
    database_changed: bool = False


def compile_module_plan(base_plan: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    """Compile the entire backlog before dispatch; each feature must pass its own test."""
    business = ModulePlan.model_validate(proposal)
    plan = deepcopy(base_plan)
    plan.update(business.model_dump())
    plan["execution_mode"] = "modules"
    tasks = plan["tasks"]
    by_action = {t["action"]: t["task_id"] for t in tasks}
    foundation = by_action["implement_project"]
    global_srs = by_action["write_requirements"]
    architecture = by_action["design_architecture"]
    database = by_action["design_schema"]
    stories = by_action["write_user_stories"]
    previous = foundation
    code_source = foundation
    generated = []
    epics = list(plan.get("epics", []))

    def add(tid, role, action, title, instruction, deps, output, sources, module, item=None):
        generated.append({
            "task_id": tid, "role": role, "action": action, "title": title,
            "instruction": instruction, "depends_on": list(dict.fromkeys(deps)),
            "input_artifact_types": list(sources), "output_artifact_type": output,
            "acceptance_criteria": item.acceptance_criteria if item else ["Complete module deliverable"],
            "context": {
                "module_id": module.module_id, "module_name": module.name,
                "milestone_key": f"module-{module.module_id}",
                "work_item_id": item.task_id if item else None,
                "input_artifact_sources": sources,
                "cumulative_source": role == "DEV",
            },
        })

    for mi, module in enumerate(business.modules, 1):
        # Short deterministic IDs leave room for the run prefix in MySQL VARCHAR(64).
        prefix = f"m{mi:02d}"
        srs, cases = f"{prefix}_srs", f"{prefix}_cases"
        epics.append({"id": f"module-{module.module_id}", "name": module.name, "description": module.description})
        add(srs, "BA", "write_requirements", f"{module.name}: SRS",
            f"Specify the whole module: {module.description}. Tasks: " +
            "; ".join(f"{t.title}: {t.description}" for t in module.tasks),
            [previous], "REQUIREMENT", {"REQUIREMENT": [global_srs], "BACKLOG": [by_action["define_backlog"]]}, module)
        requirements = {"REQUIREMENT": [global_srs, srs]}
        add(cases, "TESTER", "create_test_cases", f"{module.name}: test cases",
            "Create test cases for every acceptance criterion in this module: " +
            "; ".join(c for t in module.tasks for c in t.acceptance_criteria),
            [srs], "TEST_CASE", {**requirements, "ARCHITECTURE": [architecture],
            "DATABASE_SCHEMA": [database], "USER_STORY": [stories]}, module)
        previous = cases
        for ti, item in enumerate(module.tasks, 1):
            dev, test = f"{prefix}_t{ti:02d}_dev", f"{prefix}_t{ti:02d}_test"
            add(dev, "DEV", "implement_feature", item.title,
                f"{item.description}\nExtend the supplied source; preserve existing modules. "
                "Return the complete cumulative project source, not only a patch.",
                [previous], "SOURCE_CODE", {**requirements, "ARCHITECTURE": [architecture],
                "DATABASE_SCHEMA": [database], "USER_STORY": [stories],
                "SOURCE_CODE": [code_source], "TEST_CASE": [cases]}, module, item)
            add(test, "TESTER", "run_test_suite", f"Test: {item.title}",
                "Test the exact supplied code against the module SRS and acceptance criteria. "
                "Run regression tests for earlier modules. Return verdict PASS or FAIL with findings.",
                [dev], "TEST_REPORT", {**requirements, "SOURCE_CODE": [dev], "TEST_CASE": [cases]}, module, item)
            previous, code_source = test, dev
        review = f"{prefix}_review"
        add(review, "SCRUM_MASTER", "run_sprint_review", f"Review: {module.name}",
            "Review completed module and test evidence before the next module starts.",
            [previous], "DOCUMENTATION", {"TEST_REPORT": [previous], "SOURCE_CODE": [code_source]}, module)
        previous = review

    index = next(i for i, t in enumerate(tasks) if t["task_id"] == foundation) + 1
    tasks[index:index] = generated
    for task in tasks[index + len(generated):]:
        task["depends_on"] = [previous if d == foundation else d for d in task.get("depends_on", [])]
    task_map = {t["task_id"]: t for t in tasks}
    for task in tasks:
        context = task.setdefault("context", {})
        context["execution_mode"] = "modules"
        if "input_artifact_sources" in context:
            continue
        sources = {}
        for kind in task.get("input_artifact_types", []):
            queue, visited = list(task.get("depends_on", [])), set()
            while queue:
                source = queue.pop(0)
                if source in visited:
                    continue
                visited.add(source)
                producer = task_map[source]
                if producer.get("output_artifact_type") == kind:
                    sources[kind] = [source]
                    break
                queue.extend(producer.get("depends_on", []))
        context["input_artifact_sources"] = sources
    plan["epics"] = epics
    return plan


def compile_change_plan(base_plan: dict[str, Any], proposal: dict[str, Any], baseline: dict[str, Any],
                        release_id: str) -> dict[str, Any]:
    """Compile incremental delivery using pinned baseline artifacts, without scaffolding."""
    change = ChangePlan.model_validate(proposal)
    affected = {m.module_id for m in change.modules}
    artifacts = baseline["artifacts"]
    tasks = []

    def inherited(kind, module_id=None):
        return [a["artifact_id"] for a in artifacts if a["type"] == kind and a.get("module_id") == module_id]

    def global_or_new(kind, producer):
        return ({kind: [producer]}, {}) if producer else ({}, {kind: inherited(kind)})

    def add(tid, role, action, title, instruction, deps, output, sources=None, base=None,
            module=None, item=None):
        sources, base = sources or {}, {k: list(dict.fromkeys(v)) for k, v in (base or {}).items() if v}
        tasks.append({"task_id": tid, "role": role, "action": action, "title": title,
            "instruction": instruction, "depends_on": deps, "output_artifact_type": output,
            "input_artifact_types": list(dict.fromkeys([*sources, *base])),
            "acceptance_criteria": item.acceptance_criteria if item else ["Preserve unaffected behavior and deliver the requested change"],
            "context": {"execution_mode": "modules", "epic": "change", "change_request": True,
                "module_id": module.module_id if module else None, "module_name": module.name if module else None,
                "milestone_key": f"module-{module.module_id}" if module else "change",
                "work_item_id": item.task_id if item else None, "input_artifact_sources": sources,
                "baseline_artifact_ids": base, "cumulative_source": role == "DEV"}})

    add("change_backlog", "PO", "define_backlog", "Update project backlog",
        "Update the existing backlog for this change; preserve completed and unaffected scope: " + change.overview,
        [], "BACKLOG", base={"BACKLOG": inherited("BACKLOG"), "REQUIREMENT": inherited("REQUIREMENT")})
    add("change_srs", "BA", "write_requirements", "Update shared SRS",
        "Revise the supplied full project SRS for the requested change. Preserve unaffected requirements. " + change.overview,
        ["change_backlog"], "REQUIREMENT", {"BACKLOG": ["change_backlog"]}, {"REQUIREMENT": inherited("REQUIREMENT")})
    add("change_stories", "BA", "write_user_stories", "Update project stories",
        "Update the existing user stories and acceptance criteria for the change, retaining unrelated stories.",
        ["change_srs"], "USER_STORY", {"REQUIREMENT": ["change_srs"]}, {"USER_STORY": inherited("USER_STORY")})
    add("change_scope", "GATE", "approve_scope", "Approve change scope", "Approve impact and changed requirements",
        ["change_stories"], None)
    previous, architecture, database, code = "change_scope", None, None, None
    if change.architecture_changed:
        architecture = "change_architecture"
        add(architecture, "SA", "design_architecture", "Revise architecture",
            "Revise the supplied architecture for the change; preserve unaffected components.", [previous], "ARCHITECTURE",
            {"REQUIREMENT": ["change_srs"], "USER_STORY": ["change_stories"]}, {"ARCHITECTURE": inherited("ARCHITECTURE")})
        previous = architecture
    if change.database_changed:
        database = "change_schema"
        src, old = global_or_new("ARCHITECTURE", architecture)
        add(database, "DBA", "design_schema", "Revise database and migrations",
            "Revise the existing schema and supply a migration preserving existing data and a rollback plan.",
            [previous], "DATABASE_SCHEMA", {**src, "REQUIREMENT": ["change_srs"]},
            {**old, "DATABASE_SCHEMA": inherited("DATABASE_SCHEMA")})
        previous = database
    architecture_src, architecture_base = global_or_new("ARCHITECTURE", architecture)
    database_src, database_base = global_or_new("DATABASE_SCHEMA", database)
    design_sources = {**architecture_src, **database_src}
    design_baseline = {**architecture_base, **database_base}
    add("change_test_cases", "TESTER", "create_test_cases", "Update shared regression cases",
        "Revise the supplied shared regression suite against the updated project SRS and stories. "
        "Preserve unaffected cases, update expectations explicitly changed by the new requirements, "
        "and retain coverage of existing behavior.",
        [previous], "TEST_CASE", {**design_sources, "REQUIREMENT": ["change_srs"], "USER_STORY": ["change_stories"]},
        {**design_baseline, "TEST_CASE": inherited("TEST_CASE")})
    previous = "change_test_cases"
    regression_baseline = [a["artifact_id"] for a in artifacts
                           if a["type"] == "TEST_CASE" and a.get("module_id") is not None and a.get("module_id") not in affected]
    module_cases = []
    for mi, module in enumerate(change.modules, 1):
        prefix = f"c{mi:02d}"
        srs, cases = f"{prefix}_srs", f"{prefix}_cases"
        add(srs, "BA", "write_requirements", f"{module.name}: update SRS",
            f"Update module {module.module_id}: {module.description}. Tasks: " +
            "; ".join(f"{t.title}: {t.description}" for t in module.tasks) + ". Preserve unchanged requirements.",
            [previous], "REQUIREMENT", {"REQUIREMENT": ["change_srs"]},
            {"REQUIREMENT": inherited("REQUIREMENT", module.module_id)}, module)
        add(cases, "TESTER", "create_test_cases", f"{module.name}: update test cases",
            "Revise existing module tests for the new SRS; retain unaffected tests and cover every acceptance criterion: " +
            "; ".join(c for t in module.tasks for c in t.acceptance_criteria),
            [srs], "TEST_CASE", {**design_sources, "REQUIREMENT": ["change_srs", srs]},
            {**design_baseline, "TEST_CASE": inherited("TEST_CASE", module.module_id)}, module)
        module_cases.append(cases)
        previous = cases
        for ti, item in enumerate(module.tasks, 1):
            dev, test = f"{prefix}_t{ti:02d}_dev", f"{prefix}_t{ti:02d}_test"
            code_src, code_base = global_or_new("SOURCE_CODE", code)
            add(dev, "DEV", "implement_feature", item.title,
                item.description + "\nModify the supplied existing source. Preserve unrelated modules and data. "
                "Return the complete cumulative project source; do not create a replacement project.",
                [previous], "SOURCE_CODE", {**design_sources, **code_src, "REQUIREMENT": ["change_srs", srs],
                    "USER_STORY": ["change_stories"], "TEST_CASE": [cases]}, {**design_baseline, **code_base}, module, item)
            add(test, "TESTER", "run_test_suite", f"Test: {item.title}",
                "Verify the changed task against its SRS and acceptance criteria using the exact supplied source. "
                "Run inherited regression tests too. Return verdict PASS or FAIL with findings.",
                [dev], "TEST_REPORT", {"SOURCE_CODE": [dev], "REQUIREMENT": ["change_srs", srs], "TEST_CASE": ["change_test_cases", cases]},
                {"TEST_CASE": regression_baseline}, module, item)
            previous, code = test, dev
        review = f"{prefix}_review"
        add(review, "SCRUM_MASTER", "run_sprint_review", f"Review: {module.name}",
            "Review change evidence before continuing to the next affected module.", [previous], "DOCUMENTATION",
            {"TEST_REPORT": [previous], "SOURCE_CODE": [code]}, module=module)
        previous = review
    add("change_regression", "TESTER", "run_test_suite", "Full project regression",
        "Run the complete regression suite against the final cumulative source: updated tests for affected modules "
        "and inherited tests for all unaffected modules. Return verdict PASS or FAIL and list tested modules.",
        [previous], "TEST_REPORT", {"SOURCE_CODE": [code], "TEST_CASE": ["change_test_cases", *module_cases], "REQUIREMENT": ["change_srs"]},
        {"TEST_CASE": regression_baseline})
    add("change_security", "SECURITY", "security_review", "Review changed release security",
        "Review the final cumulative source and report verdict PASS or FAIL with findings.",
        ["change_regression"], "SECURITY_REPORT", {**architecture_src, "SOURCE_CODE": [code]}, architecture_base)
    previous = "change_security"
    # Keep the existing deployment/acceptance policy, after successful regression and security.
    for template in base_plan["tasks"]:
        if template["action"] not in {"generate_ci_config", "build_image", "deploy", "health_check",
                                      "approve_prod", "accept_release", "run_retrospective", "report_status"}:
            continue
        tid = template["task_id"]
        sources, old = {}, {}
        for kind in template.get("input_artifact_types", []):
            producer = next((t["task_id"] for t in reversed(tasks) if t.get("output_artifact_type") == kind), None)
            if producer:
                sources[kind] = [producer]
            else:
                old[kind] = inherited(kind)
        add(tid, template["role"], template["action"], template["title"], template["instruction"],
            [previous], template.get("output_artifact_type"), sources, old)
        previous = tid
    return {**change.model_dump(), "execution_mode": "modules", "run_kind": "change",
        "baseline_release_id": release_id, "baseline_artifacts": deepcopy(artifacts),
        "regression_modules": sorted({m["module_id"] for m in baseline.get("modules", [])} | affected),
        "epics": [{"id": "change", "name": "Change delivery", "description": change.impact_summary}] +
                 [{"id": f"module-{m.module_id}", "name": m.name, "description": m.description} for m in change.modules],
        "tasks": tasks}
