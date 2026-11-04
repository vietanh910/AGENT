import json

with open('control/app/integrations/linear_introspection.json', encoding='utf-8') as f:
    data = json.load(f)

print('IssueRelationType enum values:')
for ev in data['IssueRelationType']['enumValues']:
    print(' ', ev['name'], ':', ev.get('description'))

mutation_fields = {field['name']: field for field in data['Mutation']['fields']}
interesting_mutations = [
    'projectCreate', 'projectUpdate',
    'projectMilestoneCreate', 'projectMilestoneUpdate',
    'issueCreate', 'issueUpdate', 'issueArchive', 'issueDelete',
    'issueRelationCreate',
    'commentCreate',
    'attachmentCreate', 'attachmentLinkURL'
]
print('\nMutations:')
for m in interesting_mutations:
    if m in mutation_fields:
        f = mutation_fields[m]
        args = [f"{a['name']}: {a['type'].get('name') or a['type'].get('ofType', {}).get('name')}" for a in f['args']]
        ret = f['type'].get('name') or f['type'].get('ofType', {}).get('name')
        print(f"  {m}({', '.join(args)}) -> {ret}")
    else:
        print(f"  {m} NOT FOUND")

# Inputs
print('\nInput fields for IssueCreateInput:')
for f in data['IssueCreateInput']['inputFields']:
    print(f"  {f['name']}: {f['type'].get('name') or f['type'].get('ofType', {}).get('name')}")

print('\nInput fields for IssueRelationCreateInput:')
for f in data['IssueRelationCreateInput']['inputFields']:
    print(f"  {f['name']}: {f['type'].get('name') or f['type'].get('ofType', {}).get('name')}")

print('\nInput fields for AttachmentCreateInput:')
for f in data['AttachmentCreateInput']['inputFields']:
    print(f"  {f['name']}: {f['type'].get('name') or f['type'].get('ofType', {}).get('name')}")

print('\nProjectCreateInput fields:')
for f in data['ProjectCreateInput']['inputFields']:
    print(f"  {f['name']}: {f['type'].get('name') or f['type'].get('ofType', {}).get('name')}")

print('\nProjectMilestoneCreateInput fields:')
for f in data['ProjectMilestoneCreateInput']['inputFields']:
    print(f"  {f['name']}: {f['type'].get('name') or f['type'].get('ofType', {}).get('name')}")

query_fields = {field['name']: field for field in data['Query']['fields']}
interesting_queries = [
    'workflowStates', 'workflowState',
    'issueLabels', 'issueLabel',
    'issues', 'issue',
    'projects', 'project',
    'projectMilestones',
    'comments', 'comment',
    'team', 'teams'
]
print('\nQueries:')
for q in interesting_queries:
    if q in query_fields:
        f = query_fields[q]
        args = [f"{a['name']}: {a['type'].get('name') or a['type'].get('ofType', {}).get('name')}" for a in f['args']]
        ret = f['type'].get('name') or f['type'].get('ofType', {}).get('name')
        print(f"  {q}({', '.join(args)}) -> {ret}")
    else:
        print(f"  {q} NOT FOUND")

def introspect_filter(filter_name):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from introspect_linear import introspect_type
    f_data = introspect_type(filter_name)
    print(f"\n{filter_name} fields:")
    for f in f_data.get('inputFields', []):
        print(f"  {f['name']}: {f['type'].get('name') or f['type'].get('ofType', {}).get('name')}")

introspect_filter('IssueFilter')
introspect_filter('CommentFilter')
