from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

ToolCategory = Literal["data_query", "durable_action", "display", "ui_action"]
TOOL_CATALOG_SCHEMA = "editor-tool-catalog/v1"
SSE_EVENT_TYPES = frozenset(
    {"text", "card", "action", "status", "suggestions", "done", "error"}
)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    category: ToolCategory
    intent: str
    side_effect: str
    approval: str
    status_message: str
    input_schema: dict[str, Any]
    output: str

    def prompt_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "when_to_use": self.intent,
            "side_effect": self.side_effect,
            "approval": self.approval,
            "parameters": self.input_schema,
        }


class ToolCatalog:
    def __init__(
        self,
        definitions: list[ToolDefinition],
        principles: list[str],
        *,
        identity: dict[str, str] | None = None,
        capabilities: list[dict[str, Any]] | None = None,
        boundaries: list[dict[str, str]] | None = None,
    ):
        if not definitions:
            raise ValueError("tool catalog must define at least one tool")
        names = [item.name for item in definitions]
        if len(names) != len(set(names)):
            raise ValueError("tool catalog contains duplicate tool names")
        self._definitions = {item.name: item for item in definitions}
        self.principles = tuple(principles)
        self.identity = dict(identity or {})
        self.capabilities = tuple(dict(item) for item in (capabilities or []))
        self.boundaries = tuple(dict(item) for item in (boundaries or []))
        capability_ids = [str(item.get("id", "")) for item in self.capabilities]
        if not all(capability_ids) or len(capability_ids) != len(set(capability_ids)):
            raise ValueError("capabilities must have unique non-empty IDs")
        for capability in self.capabilities:
            required_fields = ("title", "summary", "examples", "tools", "outcome")
            if any(not capability.get(field) for field in required_fields):
                raise ValueError(
                    f"capability {capability.get('id')} is missing required content"
                )
            if not isinstance(capability["examples"], list) or not isinstance(
                capability["tools"], list
            ):
                raise ValueError(
                    f"capability {capability.get('id')} examples and tools must be arrays"
                )
            unknown = set(capability.get("tools", [])) - self.names
            if unknown:
                raise ValueError(
                    f"capability {capability.get('id')} references unknown tools: "
                    + ", ".join(sorted(unknown))
                )
        boundary_ids = [str(item.get("id", "")) for item in self.boundaries]
        if not all(boundary_ids) or len(boundary_ids) != len(set(boundary_ids)):
            raise ValueError("boundaries must have unique non-empty IDs")
        if any(
            not item.get("title") or not item.get("description")
            for item in self.boundaries
        ):
            raise ValueError("boundaries must define title and description")

    @classmethod
    def load(cls, path: Path | None = None) -> "ToolCatalog":
        contract_path = path or default_tool_catalog_path()
        raw = json.loads(contract_path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != TOOL_CATALOG_SCHEMA:
            raise ValueError(f"unsupported tool catalog: {raw.get('schema_version')}")
        definitions = []
        valid_categories = {"data_query", "durable_action", "display", "ui_action"}
        for item in raw.get("tools", []):
            category = item.get("category")
            if category not in valid_categories:
                raise ValueError(f"invalid category for {item.get('name')}: {category}")
            definitions.append(
                ToolDefinition(
                    name=str(item["name"]),
                    category=category,
                    intent=str(item["intent"]),
                    side_effect=str(item["side_effect"]),
                    approval=str(item["approval"]),
                    status_message=str(item["status_message"]),
                    input_schema=dict(item["input_schema"]),
                    output=str(item["output"]),
                )
            )
        return cls(
            definitions,
            list(raw.get("principles", [])),
            identity=dict(raw.get("identity", {})),
            capabilities=list(raw.get("capabilities", [])),
            boundaries=list(raw.get("boundaries", [])),
        )

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._definitions)

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise KeyError(f"unknown editor tool: {name}") from exc

    def public_document(self) -> dict[str, Any]:
        return {
            "schema_version": TOOL_CATALOG_SCHEMA,
            "identity": self.identity,
            "principles": list(self.principles),
            "capabilities": list(self.capabilities),
            "boundaries": list(self.boundaries),
            "tools": [item.prompt_view() for item in self.definitions],
        }

    def prompt_json(self) -> str:
        return json.dumps(
            self.public_document(), ensure_ascii=False, separators=(",", ":")
        )


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_tool_catalog_path() -> Path:
    source_path = project_root() / "docs" / "contracts" / "editor-tools.v1.json"
    if source_path.is_file():
        return source_path
    packaged_path = Path(
        str(files("travel_video.editor").joinpath("editor-tools.v1.json"))
    )
    if packaged_path.is_file():
        return packaged_path
    raise FileNotFoundError("editor tool contract is missing from source and package")


AGENT_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "response": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments_json": {"type": "string"},
                },
                "required": ["name", "arguments_json"],
                "additionalProperties": False,
            },
        },
        "suggestions": {
            "type": "array",
            "maxItems": 4,
            "items": {"type": "string"},
        },
        "done": {"type": "boolean"},
    },
    "required": ["response", "tool_calls", "suggestions", "done"],
    "additionalProperties": False,
}
