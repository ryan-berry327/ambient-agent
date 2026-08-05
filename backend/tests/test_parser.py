from app.services.distiller import DistillerService

d = DistillerService()
cases = [
    ('{"changes": [], "spec": []}', True),
    ('```json\n{"changes": [], "spec": []}\n```', True),
    ('Here is the spec:\n{"changes": [{"id": "abc", "action": "add", "reason": "x"}], "spec": []}', True),
]
for raw, expect in cases:
    out, ok = d._parse_distill_output(raw, [])
    print(f"case ok={ok} expect={expect} changes={len(out.changes)}")
