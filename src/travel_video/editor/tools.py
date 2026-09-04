from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .catalog import TimelineCatalog
from .contracts import EditorAgentContract, ToolDefinition
from .evidence import SceneEvidenceService
from .store import EditStore


@dataclass(frozen=True, slots=True)
class ToolResult:
    kind: str
    payload: dict[str, Any]
    model_payload: dict[str, Any] | None = None
    local_image_paths: tuple[str, ...] = ()

    def for_model(self) -> dict[str, Any]:
        if self.model_payload is not None:
            return self.model_payload
        if self.kind in {"card", "action"}:
            return {
                "delivered_to_ui": True,
                "kind": self.kind,
                "type": self.payload.get("type"),
            }
        return self.payload


class ToolGateway:
    def __init__(
        self,
        catalog: TimelineCatalog,
        store: EditStore,
        contract: EditorAgentContract,
        evidence: SceneEvidenceService,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.contract = contract
        self.evidence = evidence
        self._handlers: dict[str, Callable[[dict[str, Any], str], ToolResult]] = {
            "list_assets": self._list_assets,
            "search_scenes": self._search_scenes,
            "get_scene_evidence": self._get_scene_evidence,
            "inspect_scene_range": self._inspect_scene_range,
            "create_edit_proposal": self._create_edit_proposal,
            "get_edit_proposal": self._get_edit_proposal,
            "apply_edit_proposal": self._apply_edit_proposal,
            "create_edit": self._create_edit,
            "get_edit": self._get_edit,
            "apply_edit_operations": self._apply_edit_operations,
            "show_scene_refs": self._show_scene_refs,
            "show_edit_revision": self._show_edit_revision,
            "play_source_range": self._play_source_range,
            "focus_scene": self._focus_scene,
            "open_inspector_tab": self._open_inspector_tab,
        }
        missing = contract.names - self._handlers.keys()
        extra = self._handlers.keys() - contract.names
        if missing or extra:
            raise RuntimeError(
                f"tool contract/handler mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
            )

    def definition(self, name: str) -> ToolDefinition:
        return self.contract.get(name)

    def invoke(
        self, name: str, arguments: dict[str, Any], session_id: str
    ) -> ToolResult:
        definition = self.definition(name)
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        started = time.monotonic()
        status = "completed"
        result_summary: dict[str, Any]
        try:
            definition.validate_input(arguments)
            result = self._handlers[name](arguments, session_id)
            result_summary = self._summarize(result)
            return result
        except Exception as exc:
            status = "failed"
            result_summary = {
                "error_type": type(exc).__name__,
                "message": str(exc)[:300],
            }
            raise
        finally:
            self.store.log_tool_run(
                run_id=f"toolrun_{uuid.uuid4().hex[:16]}",
                session_id=session_id,
                tool_name=name,
                category=definition.category,
                status=status,
                duration_ms=round((time.monotonic() - started) * 1000),
                arguments=arguments,
                result_summary=result_summary,
            )

    @staticmethod
    def _summarize(result: ToolResult) -> dict[str, Any]:
        payload = result.payload
        summary: dict[str, Any] = {"kind": result.kind}
        for key in (
            "type",
            "count",
            "edit_id",
            "head_revision_id",
            "revision_id",
            "proposal_id",
            "evidence_id",
        ):
            if key in payload:
                summary[key] = payload[key]
        if isinstance(payload.get("items"), list):
            summary["item_count"] = len(payload["items"])
        return summary

    def _list_assets(self, args: dict[str, Any], _: str) -> ToolResult:
        items = self.catalog.list_assets(
            capture_date=args.get("capture_date"), limit=int(args.get("limit", 30))
        )
        return ToolResult("data", {"items": items, "count": len(items)})

    def _search_scenes(self, args: dict[str, Any], _: str) -> ToolResult:
        items = self.catalog.search_scenes(
            query=str(args.get("query", "")),
            asset_ids=[str(item) for item in args.get("asset_ids", [])] or None,
            highlight_only=bool(args.get("highlight_only", False)),
            limit=int(args.get("limit", 12)),
        )
        return ToolResult("data", {"items": items, "count": len(items)})

    def _get_scene_evidence(self, args: dict[str, Any], _: str) -> ToolResult:
        return ToolResult("data", self.catalog.scene_evidence(str(args["scene_id"])))

    def _inspect_scene_range(self, args: dict[str, Any], _: str) -> ToolResult:
        inspection = self.evidence.inspect(
            scene_id=str(args["scene_id"]),
            source_in=(
                float(args["source_in"]) if args.get("source_in") is not None else None
            ),
            source_out=(
                float(args["source_out"])
                if args.get("source_out") is not None
                else None
            ),
            visual_mode=str(args.get("visual_mode", "auto")),
            frame_count=int(args.get("frame_count", 8)),
            include_raw_stt=bool(args.get("include_raw_stt", False)),
            context_seconds=float(args.get("context_seconds", 4.0)),
        )
        return ToolResult(
            "card",
            inspection.card,
            model_payload=inspection.model,
            local_image_paths=inspection.local_image_paths,
        )

    def _create_edit_proposal(
        self, args: dict[str, Any], session_id: str
    ) -> ToolResult:
        proposal = self.store.create_proposal(
            catalog=self.catalog,
            session_id=session_id,
            title=str(args["title"]),
            objective=str(args["objective"]),
            candidates=[dict(item) for item in args["candidates"]],
            target_duration=(
                float(args["target_duration"])
                if args.get("target_duration") is not None
                else None
            ),
            duration_rationale=str(args.get("duration_rationale") or ""),
            assumptions=[str(item) for item in args.get("assumptions", [])],
            uncertainties=[str(item) for item in args.get("uncertainties", [])],
            base_edit_id=(
                str(args["base_edit_id"]) if args.get("base_edit_id") else None
            ),
            base_revision_id=(
                str(args["base_revision_id"]) if args.get("base_revision_id") else None
            ),
            application_mode=str(args.get("application_mode", "replace_all")),
            created_by=f"agent:{session_id}",
        )
        return ToolResult(
            "card",
            self.proposal_card(proposal, session_id=session_id),
            model_payload=self._proposal_model_view(proposal),
        )

    def _get_edit_proposal(self, args: dict[str, Any], session_id: str) -> ToolResult:
        proposal = self.store.get_proposal(
            str(args["proposal_id"]), expected_session_id=session_id
        )
        return ToolResult("data", self._proposal_model_view(proposal))

    def _apply_edit_proposal(self, args: dict[str, Any], session_id: str) -> ToolResult:
        result = self.store.apply_proposal(
            catalog=self.catalog,
            proposal_id=str(args["proposal_id"]),
            selected_candidate_ids=[
                str(item) for item in args.get("selected_candidate_ids", [])
            ]
            or None,
            expected_session_id=session_id,
            created_by=f"agent:{session_id}",
        )
        return ToolResult("data", result)

    @staticmethod
    def _proposal_model_view(proposal: dict[str, Any]) -> dict[str, Any]:
        return {
            key: proposal.get(key)
            for key in (
                "schema_version",
                "proposal_id",
                "status",
                "title",
                "objective",
                "target_duration",
                "duration_rationale",
                "estimated_duration",
                "base_edit_id",
                "base_revision_id",
                "application_mode",
                "candidates",
                "assumptions",
                "uncertainties",
                "applied_edit_id",
                "applied_revision_id",
            )
        }

    @staticmethod
    def proposal_card(
        proposal: dict[str, Any], *, session_id: str | None = None
    ) -> dict[str, Any]:
        card = {
            "type": "edit_proposal_ref",
            "proposal_id": proposal["proposal_id"],
            "status": proposal["status"],
            "title": proposal["title"],
            "objective": proposal["objective"],
            "target_duration": proposal.get("target_duration"),
            "duration_rationale": proposal.get("duration_rationale"),
            "estimated_duration": proposal["estimated_duration"],
            "base_edit_id": proposal.get("base_edit_id"),
            "base_revision_id": proposal.get("base_revision_id"),
            "application_mode": proposal.get("application_mode", "replace_all"),
            "assumptions": proposal.get("assumptions", []),
            "uncertainties": proposal.get("uncertainties", []),
            "candidates": proposal["candidates"],
        }
        if session_id is not None:
            card["session_id"] = session_id
        return card

    def _create_edit(self, args: dict[str, Any], session_id: str) -> ToolResult:
        edit = self.store.create_edit(
            title=str(args["title"]),
            brief=str(args["brief"]),
            target_duration=(
                float(args["target_duration"])
                if args.get("target_duration") is not None
                else None
            ),
            created_by=f"agent:{session_id}",
        )
        self.store.update_session(session_id, active_edit_id=edit["edit_id"])
        return ToolResult("data", edit)

    def _get_edit(self, args: dict[str, Any], _: str) -> ToolResult:
        return ToolResult(
            "data",
            self.store.get_edit(
                str(args["edit_id"]),
                str(args["revision_id"]) if args.get("revision_id") else None,
            ),
        )

    def _apply_edit_operations(
        self, args: dict[str, Any], session_id: str
    ) -> ToolResult:
        edit = self.store.apply_operations(
            catalog=self.catalog,
            edit_id=str(args["edit_id"]),
            expected_revision_id=str(args["expected_revision_id"]),
            summary=str(args["summary"]),
            operations=[dict(item) for item in args["operations"]],
            created_by=f"agent:{session_id}",
        )
        self.store.update_session(session_id, active_edit_id=edit["edit_id"])
        return ToolResult("data", edit)

    def _show_scene_refs(self, args: dict[str, Any], _: str) -> ToolResult:
        scene_ids = [str(item) for item in args["scene_ids"]]
        if not 1 <= len(scene_ids) <= 12:
            raise ValueError("scene_ids must contain 1 to 12 items")
        items = [self.catalog.scene(scene_id).compact() for scene_id in scene_ids]
        return ToolResult(
            "card",
            {
                "type": "scene_refs",
                "title": str(args.get("title") or "장면 후보"),
                "items": items,
            },
        )

    def _show_edit_revision(self, args: dict[str, Any], _: str) -> ToolResult:
        edit = self.store.get_edit(
            str(args["edit_id"]),
            str(args["revision_id"]) if args.get("revision_id") else None,
        )
        revision = edit["revision"]
        return ToolResult(
            "card",
            {
                "type": "edit_revision_ref",
                "edit_id": edit["edit_id"],
                "title": edit["title"],
                "revision_id": revision["revision_id"],
                "sequence": revision["sequence"],
                "clip_count": revision["clip_count"],
                "duration": revision["timeline_duration"],
                "target_duration": edit["target_duration"],
                "changes": revision["change_summary"],
                "clips": [
                    {
                        "clip_id": item["id"],
                        "scene_id": item["metadata"].get("scene_id"),
                        "label": item["label"],
                        "source_in": item["source_in"],
                        "source_out": item["source_out"],
                        "duration": round(item["source_out"] - item["source_in"], 3),
                    }
                    for item in revision["plan"].get("clips", [])
                ],
            },
        )

    def _scene_action(
        self, action_type: str, scene_id: str, **extra: Any
    ) -> ToolResult:
        scene = self.catalog.scene(scene_id)
        return ToolResult(
            "action",
            {
                "type": action_type,
                "scene_id": scene.scene_id,
                "asset_id": scene.asset_id,
                "dom_id": f"scene-{scene.asset_id}-{scene.group_id}",
                "source_in": scene.source_in,
                "source_out": scene.source_out,
                **extra,
            },
        )

    def _play_source_range(self, args: dict[str, Any], _: str) -> ToolResult:
        scene = self.catalog.scene(str(args["scene_id"]))
        source_in = float(args.get("source_in", scene.source_in))
        source_out = float(args.get("source_out", scene.source_out))
        if (
            source_in < scene.source_in - 0.001
            or source_out > scene.source_out + 0.001
            or source_out <= source_in
        ):
            raise ValueError(
                "playback range must be positive and stay inside the reviewed scene"
            )
        return self._scene_action(
            "play_source_range",
            scene.scene_id,
            source_in=source_in,
            source_out=source_out,
            autoplay=bool(args.get("autoplay", True)),
        )

    def _focus_scene(self, args: dict[str, Any], _: str) -> ToolResult:
        return self._scene_action("focus_scene", str(args["scene_id"]))

    @staticmethod
    def _open_inspector_tab(args: dict[str, Any], _: str) -> ToolResult:
        tab = str(args["tab"])
        if tab not in {"monitor", "ai"}:
            raise ValueError("tab must be monitor or ai")
        return ToolResult("action", {"type": "open_inspector_tab", "tab": tab})
