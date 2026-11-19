"""Adapt the canonical task card to Codex's strict response schema subset."""

from typing import Any, cast

# The canonical validator still checks these constraints against the returned card.
_LOCAL_CONSTRAINTS = {
    "$schema",
    "$defs",
    "description",
    "pattern",
    "minItems",
    "uniqueItems",
}


def _project(value: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_project(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        name = value["$ref"].removeprefix("#/$defs/")
        return _project(definitions[name], definitions)
    schema = {
        key: _project(item, definitions)
        for key, item in value.items()
        if key not in _LOCAL_CONSTRAINTS
    }
    if "const" in schema:
        schema["type"] = "integer"
        schema["enum"] = [schema.pop("const")]
    return schema


def codex_task_card_schema(canonical: dict[str, Any]) -> dict[str, Any]:
    schema = cast(dict[str, Any], _project(canonical, canonical["$defs"]))
    # Strict schemas require each object's declared fields. The host accepts
    # both canonical question shapes, and proposed tests can be an empty list.
    schema["required"].append("proposed_tests")
    questions = schema["properties"]["open_questions"]
    with_resolution = questions["items"]
    without_resolution = {
        **with_resolution,
        "properties": {
            key: value
            for key, value in with_resolution["properties"].items()
            if key != "resolution"
        },
    }
    with_resolution["required"] = [*with_resolution["required"], "resolution"]
    questions["items"] = {"anyOf": [without_resolution, with_resolution]}
    return schema
