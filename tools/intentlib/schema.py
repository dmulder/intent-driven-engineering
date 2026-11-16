"""Published schemas and a fail-closed evaluator for their deliberately small subset."""
import re


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def arr(item):
    return {"type": "array", "items": item}


S = {"type": "string"}
TEXT = {"type": "string", "minLength": 1}
N = {"type": "integer", "minimum": 0}
REV = {"type": "integer", "minimum": 1}
NULLS = {"type": ["string", "null"]}
NULLREV = {"type": ["integer", "null"], "minimum": 1}
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
VERSION = {"type": "integer", "enum": [1]}
ID = {"type": "string", "pattern": "^[IBE]-[0-9a-f]{64}$"}
REF = obj({"source": HASH, "mode": {"enum": ["100644", "100755", "120000"], "type": "string"}})
POINT = arr(N)
INTERVAL = obj({"before": POINT, "after": POINT})
TARGET = obj({"path": TEXT, "start": N, "end": N, "source": HASH,
              "mode": REF["properties"]["mode"]})
ACTIONS = ["preserve", "modify", "supersede", "retire", "unapply", "rebind", "introduce"]
TRANSITION = obj({"intent_id": ID, "binding_id": {"type": ["string", "null"]},
                  "action": {"type": "string", "enum": ACTIONS},
                  "from_revision": NULLREV, "to_revision": NULLREV,
                  "binding_revisions": arr(REV)})
COMMON = {"schema_version": VERSION, "id": ID, "revision": REV,
          "previous_hash": NULLS, "created_by_event": ID, "content_hash": HASH}
SCHEMAS = {
    "intent": obj({**COMMON, "status": {"type": "string", "enum": ["active", "retired", "superseded"]},
        "kind": TEXT, "title": TEXT, "description": TEXT, "rationale": TEXT,
        "requirements": arr(TEXT), "constraints": arr(TEXT), "related_intents": arr(ID)}),
    "binding": obj({**COMMON, "intent_id": ID, "intent_revision": REV,
        "relationship": {"type": "string", "enum": ["implements", "constrained-by", "motivated-by", "preserves", "verifies", "historically-derived-from"]},
        "status": {"type": "string", "enum": ["active", "ended", "unresolved", "historical"]},
        "target": TARGET, "rationale": TEXT,
        "predecessors": arr(ID)}),
    "event": obj({"schema_version": VERSION, "id": ID, "kind": TEXT,
        "description": TEXT, "rationale": TEXT,
        "changes": arr(obj({"path": TEXT,
            "before": {"anyOf": [REF, {"type": "null"}]},
            "after": {"anyOf": [REF, {"type": "null"}]}, "intervals": arr(INTERVAL)})),
        "intent_transitions": arr(TRANSITION),
        "provenance": {"type": "string", "enum": ["known", "unknown"]},
        "verification": arr(obj({"kind": TEXT, "reference": TEXT, "result": TEXT})),
        "records": arr(obj({"type": {"type": "string", "enum": ["intent", "binding"]}, "id": ID, "revision": REV})),
        "content_hash": HASH}),
    "manifest": obj({"schema_version": VERSION,
        "intents": {"type": "object", "additionalProperties": REV},
        "bindings": {"type": "object", "additionalProperties": REV}, "events": arr(ID)}),
    "object": obj({"schema_version": VERSION, "id": HASH,
        "encoding": {"type": "string", "enum": ["base64"]}, "data": S, "content_hash": HASH}),
    "config": obj({"schema_version": VERSION, "namespace": HASH,
        "coverage": {"type": "string", "enum": ["all-tracked-non-metadata"]}}),
    "baseline": obj({"schema_version": VERSION, "provenance": {"type": "string", "enum": ["unknown"]},
        "files": {"type": "object", "additionalProperties": REF}, "content_hash": HASH})}


def check(value, schema, path="$"):
    errors = []
    if "anyOf" in schema:
        if not any(not check(value, part, path) for part in schema["anyOf"]):
            errors.append(f"{path}: no allowed shape matches")
        return errors
    allowed = schema.get("type")
    if allowed:
        allowed = [allowed] if isinstance(allowed, str) else allowed
        types = {"object": dict, "array": list, "string": str, "integer": int, "null": type(None)}
        if not any(type(value) is types[t] for t in allowed):
            return [f"{path}: expected {allowed}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: invalid enum value")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: required")
        for key, child in value.items():
            if key in props:
                errors += check(child, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: unknown field")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors += check(child, schema["additionalProperties"], f"{path}.{key}")
    if isinstance(value, list):
        for i, child in enumerate(value):
            errors += check(child, schema["items"], f"{path}[{i}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: empty text")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: invalid identifier/hash")
    if type(value) is int and value < schema.get("minimum", value):
        errors.append(f"{path}: below minimum")
    return errors


def published(name):
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **SCHEMAS[name]}
