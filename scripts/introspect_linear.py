import json
from pathlib import Path
import httpx

def introspect_type(type_name):
    query = """
    query Introspect($name: String!) {
        __type(name: $name) {
            name
            kind
            description
            fields {
                name
                type {
                    name
                    kind
                    ofType {
                        name
                        kind
                        ofType {
                            name
                            kind
                        }
                    }
                }
                args {
                    name
                    type {
                        name
                        kind
                        ofType {
                            name
                            kind
                        }
                    }
                }
            }
            inputFields {
                name
                type {
                    name
                    kind
                    ofType {
                        name
                        kind
                    }
                }
            }
            enumValues {
                name
                description
            }
        }
    }
    """
    resp = httpx.post("https://api.linear.app/graphql", json={"query": query, "variables": {"name": type_name}}, timeout=20.0)
    return resp.json().get("data", {}).get("__type", {})

types_to_check = [
    "Mutation",
    "Query",
    "ProjectCreateInput",
    "ProjectUpdateInput",
    "ProjectMilestoneCreateInput",
    "ProjectMilestoneUpdateInput",
    "IssueCreateInput",
    "IssueUpdateInput",
    "IssueRelationCreateInput",
    "IssueRelationType",
    "CommentCreateInput",
    "AttachmentCreateInput",
    "WorkflowState",
    "IssueLabel",
    "Issue",
    "Project",
    "ProjectMilestone"
]

results = {}
for t in types_to_check:
    data = introspect_type(t)
    results[t] = data
    print(f"Fetched {t}: kind={data.get('kind')}")

out_path = Path("control/app/integrations/linear_introspection.json")
out_path.parent.mkdir(parents=True, exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
print("Saved introspection to", out_path)
