from __future__ import annotations

import importlib.util
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from .contracts import AGENT_DECISION_SCHEMA, EditorAgentContract
from .store import EditStore, RevisionConflictError
from .tools import ToolGateway


@dataclass(frozen=True, slots=True)
class AgentEvent:
    event: str
    data: dict[str, Any]


@dataclass(slots=True)
class AgentDecision:
    response: str = ""
    tool_calls: list[dict[str, str]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    done: bool = False

    @classmethod
    def parse(cls, value: str) -> "AgentDecision":
        text = value.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
        raw = json.loads(text)
        calls = raw.get("tool_calls")
        if not isinstance(calls, list):
            raise ValueError("agent decision tool_calls must be an array")
        return cls(
            response=str(raw.get("response", "")),
            tool_calls=[
                {
                    "name": str(item["name"]),
                    "arguments_json": str(item["arguments_json"]),
                }
                for item in calls
            ],
            suggestions=[str(item) for item in raw.get("suggestions", [])][:4],
            done=bool(raw.get("done", False)),
        )


class AgentBackend(Protocol):
    name: str

    async def decide(
        self,
        *,
        session_id: str,
        message: str,
        context: dict[str, Any],
        observations: list[dict[str, Any]],
    ) -> AgentDecision: ...


EDITOR_AGENT_INSTRUCTIONS = """## Runtime adapter
You are the AI Editor described by the authoritative Editor Agent Contract below. Its identity, context policy, capabilities, tool authority, boundaries, decision policy, runtime policy, event contract, and change process are the single source of truth. Follow them as mandatory instructions and do not invent capabilities outside them.

Return only JSON matching the supplied output schema.
- response: concise user-facing text; leave empty while more data is needed.
- tool_calls: calls to exact tool names declared by the contract. arguments_json must contain one valid JSON object.
- suggestions: zero to four short follow-ups, normally only when done is true.
- done: false if tool results are required before the answer is reliable.
- Treat decision_policy.planning as a mandatory conversational gate before durable-action tools. A normal request to make an edit produces create_edit_proposal first; only a later confirmation, the explicit bypass, or the small-active-edit exception may create a revision.
- A contact sheet attached after inspect_scene_range is actual visual evidence for only that declared source range. Ground descriptions in its labeled cells and keep raw STT secondary to reviewed captions.

## Authoritative Editor Agent Contract
{agent_contract}
"""


class CodexBackend:
    name = "codex-sdk"

    def __init__(
        self,
        *,
        store: EditStore,
        contract: EditorAgentContract,
        project_root: Path,
        model: str = "gpt-5.6-terra",
    ) -> None:
        if importlib.util.find_spec("openai_codex") is None:
            raise RuntimeError("openai-codex is not installed")
        self.store = store
        self.contract = contract
        self.project_root = project_root.resolve()
        self.model = model
        self._delivered_image_artifacts: dict[str, set[str]] = {}
        self.developer_instructions = EDITOR_AGENT_INSTRUCTIONS.format(
            agent_contract=contract.prompt_json()
        )

    async def decide(
        self,
        *,
        session_id: str,
        message: str,
        context: dict[str, Any],
        observations: list[dict[str, Any]],
    ) -> AgentDecision:
        from openai_codex import (
            ApprovalMode,
            AsyncCodex,
            LocalImageInput,
            Sandbox,
            TextInput,
        )

        session = self.store.session(session_id)
        turn_payload = {
            "user_message": message,
            "ui_context": context,
            "tool_observations": [
                {key: value for key, value in item.items() if key != "attachments"}
                for item in observations
            ],
            "active_edit_id": session.get("active_edit_id"),
            "active_proposal_id": session.get("active_proposal_id"),
        }
        prompt = "Produce the next editor decision for this turn.\n" + json.dumps(
            turn_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        async with AsyncCodex() as codex:
            thread_id = session.get("codex_thread_id")
            common = {
                "cwd": str(self.project_root),
                "model": self.model,
                "sandbox": Sandbox.read_only,
                "approval_mode": ApprovalMode.deny_all,
                "developer_instructions": self.developer_instructions,
            }
            if thread_id:
                thread = await codex.thread_resume(str(thread_id), **common)
            else:
                thread = await codex.thread_start(ephemeral=False, **common)
                self.store.update_session(session_id, codex_thread_id=thread.id)
            delivered = self._delivered_image_artifacts.setdefault(session_id, set())
            pending_images: list[tuple[str, str]] = []
            pending_artifacts: set[str] = set()
            for observation in observations:
                for attachment in observation.get("attachments", []):
                    if attachment.get("type") != "local_image":
                        continue
                    artifact_id = str(
                        attachment.get("artifact_id") or attachment.get("path") or ""
                    )
                    path = str(attachment.get("path") or "")
                    if (
                        artifact_id
                        and path
                        and artifact_id not in delivered
                        and artifact_id not in pending_artifacts
                    ):
                        pending_images.append((artifact_id, path))
                        pending_artifacts.add(artifact_id)
            run_input = [TextInput(prompt)]
            run_input.extend(LocalImageInput(path) for _, path in pending_images)
            result = await thread.run(
                run_input,
                output_schema=AGENT_DECISION_SCHEMA,
                sandbox=Sandbox.read_only,
                approval_mode=ApprovalMode.deny_all,
            )
            delivered.update(artifact_id for artifact_id, _ in pending_images)
        if not result.final_response:
            raise RuntimeError("Codex completed without a structured response")
        return AgentDecision.parse(result.final_response)


class DemoBackend:
    """Deterministic backend that exercises the real gateway and revision path."""

    name = "demo"

    def __init__(self, store: EditStore) -> None:
        self.store = store

    async def decide(
        self,
        *,
        session_id: str,
        message: str,
        context: dict[str, Any],
        observations: list[dict[str, Any]],
    ) -> AgentDecision:
        del context
        by_tool = {item["tool"]: item for item in observations if item.get("ok")}
        session = self.store.session(session_id)
        active_proposal_id = session.get("active_proposal_id")
        if "apply_edit_proposal" in by_tool:
            applied = by_tool["apply_edit_proposal"]["result"]
            edit = applied["edit"]
            if "show_edit_revision" not in by_tool:
                return AgentDecision(
                    tool_calls=[
                        {
                            "name": "show_edit_revision",
                            "arguments_json": json.dumps(
                                {
                                    "edit_id": edit["edit_id"],
                                    "revision_id": edit["revision"]["revision_id"],
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ]
                )
            return AgentDecision(
                response=(
                    f"제안에서 선택한 장면 {edit['revision']['clip_count']}개를 "
                    f"{edit['revision']['timeline_duration']:.1f}초 revision으로 적용했습니다. "
                    "원본은 변경하지 않았습니다."
                ),
                suggestions=["첫 컷을 검토", "선택 근거와 대화 다시 보기"],
                done=True,
            )
        normalized_message = re.sub(r"\s+", " ", message.strip()).strip(".!?~ ")
        approval = bool(
            re.fullmatch(
                r"(?:좋아,?\s*)?"
                r"(?:(?:이\s+)?제안\s+)?"
                r"(?:전체\s+적용|그대로\s+(?:적용|진행)|적용|승인(?:해|할게)?)"
                r"(?:\s*(?:해줘|하자|할게))?",
                normalized_message,
            )
        )
        if active_proposal_id and approval:
            if "get_edit_proposal" not in by_tool:
                return AgentDecision(
                    tool_calls=[
                        {
                            "name": "get_edit_proposal",
                            "arguments_json": json.dumps(
                                {"proposal_id": active_proposal_id}, ensure_ascii=False
                            ),
                        }
                    ]
                )
            if "apply_edit_proposal" not in by_tool:
                return AgentDecision(
                    tool_calls=[
                        {
                            "name": "apply_edit_proposal",
                            "arguments_json": json.dumps(
                                {"proposal_id": active_proposal_id}, ensure_ascii=False
                            ),
                        }
                    ]
                )
        wants_edit = any(
            word in message for word in ("만들", "편집해", "초안", "구성해", "이어 붙")
        )
        wants_deep = any(
            word in message for word in ("자세히", "딥", "원문", "이미지 시트", "촘촘")
        )
        immediate = wants_edit and any(
            word in message for word in ("바로", "확인 생략", "묻지 말고")
        )
        duration_match = re.search(r"(\d{1,4})\s*초", message)
        requested_duration = float(duration_match.group(1)) if duration_match else None
        if "search_scenes" not in by_tool:
            return AgentDecision(
                tool_calls=[
                    {
                        "name": "search_scenes",
                        "arguments_json": json.dumps(
                            {"query": "", "highlight_only": True, "limit": 8},
                            ensure_ascii=False,
                        ),
                    }
                ]
            )
        scenes = by_tool["search_scenes"]["result"].get("items", [])
        scene_ids = [item["scene_id"] for item in scenes[:6]]
        natural_clip_durations = [
            min(9.0, float(item["source_out"]) - float(item["source_in"]))
            for item in scenes[:6]
        ]
        target_duration = requested_duration or max(5.0, sum(natural_clip_durations))
        if wants_deep and scene_ids and "inspect_scene_range" not in by_tool:
            return AgentDecision(
                tool_calls=[
                    {
                        "name": "inspect_scene_range",
                        "arguments_json": json.dumps(
                            {
                                "scene_id": scene_ids[0],
                                "visual_mode": "auto",
                                "frame_count": 8,
                                "include_raw_stt": True,
                                "context_seconds": 4,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            )
        if wants_deep and not wants_edit:
            return AgentDecision(
                response=(
                    "데모 모드에서 첫 후보의 제한된 구간 증거를 열었습니다. "
                    "검토 자막과 원시 STT는 출처를 구분해 표시되며 실제 의미 판단은 Codex SDK에서 수행됩니다."
                ),
                suggestions=["이 장면을 편집 후보로 구성", "다른 후보도 자세히 보기"],
                done=True,
            )
        if not wants_edit:
            return AgentDecision(
                response=(
                    f"데모 모드 결과입니다. 규칙에 따라 검토된 장면 {len(scene_ids)}개를 표시했습니다. "
                    "실제 내용 해석은 Codex SDK 백엔드에서만 수행됩니다."
                ),
                tool_calls=[
                    {
                        "name": "show_scene_refs",
                        "arguments_json": json.dumps(
                            {"scene_ids": scene_ids, "title": "관련 장면"},
                            ensure_ascii=False,
                        ),
                    }
                ]
                if scene_ids
                else [],
                suggestions=[
                    "이 장면들로 하이라이트 구성 제안",
                    "대화가 있는 장면만 보기",
                ],
                done=True,
            )
        if "create_edit_proposal" not in by_tool:
            candidates = []
            requested_clip_duration = (
                max(3.0, min(9.0, target_duration / max(1, len(scenes[:6]))))
                if requested_duration is not None
                else None
            )
            for index, item in enumerate(scenes[:6]):
                source_in = float(item["source_in"])
                clip_duration = requested_clip_duration or natural_clip_durations[index]
                candidates.append(
                    {
                        "scene_id": item["scene_id"],
                        "source_in": source_in,
                        "source_out": min(
                            float(item["source_out"]), source_in + clip_duration
                        ),
                        "label": item["title"],
                        "role": "chronological_highlight",
                        "reason": "검토된 하이라이트와 특이 포인트를 우선한 데모 후보",
                    }
                )
            return AgentDecision(
                tool_calls=[
                    {
                        "name": "create_edit_proposal",
                        "arguments_json": json.dumps(
                            {
                                "title": "여행 하이라이트 제안",
                                "objective": message,
                                "target_duration": target_duration,
                                "duration_rationale": "후보 장면의 자연스러운 길이 합계",
                                "candidates": candidates,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
            )
        proposal = by_tool["create_edit_proposal"]["result"]
        if immediate and "apply_edit_proposal" not in by_tool:
            return AgentDecision(
                tool_calls=[
                    {
                        "name": "apply_edit_proposal",
                        "arguments_json": json.dumps(
                            {"proposal_id": proposal["proposal_id"]},
                            ensure_ascii=False,
                        ),
                    }
                ]
            )
        if immediate:
            edited = by_tool["apply_edit_proposal"]["result"]["edit"]
            revision = edited["revision"]
            if "show_edit_revision" not in by_tool:
                return AgentDecision(
                    tool_calls=[
                        {
                            "name": "show_edit_revision",
                            "arguments_json": json.dumps(
                                {
                                    "edit_id": edited["edit_id"],
                                    "revision_id": revision["revision_id"],
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ]
                )
            return AgentDecision(
                response=(
                    f"즉시 실행 요청에 따라 제안의 장면 {revision['clip_count']}개를 "
                    f"{revision['timeline_duration']:.1f}초 revision으로 적용했습니다."
                ),
                suggestions=["첫 컷을 검토", "다른 구성 제안"],
                done=True,
            )
        return AgentDecision(
            response=(
                f"검토된 장면 {len(proposal['candidates'])}개로 "
                f"약 {proposal['estimated_duration']:.1f}초 편집 제안을 만들었습니다. "
                "아직 revision은 생성하지 않았습니다. 카드에서 장면을 빼거나 그대로 적용할 수 있습니다."
            ),
            suggestions=["이 제안 전체 적용", "후보 하나를 빼고 다시 제안"],
            done=True,
        )


class AgentOrchestrator:
    def __init__(
        self,
        backend: AgentBackend,
        gateway: ToolGateway,
        store: EditStore,
        *,
        max_steps: int = 6,
    ) -> None:
        self.backend = backend
        self.gateway = gateway
        self.store = store
        self.max_steps = max_steps

    async def stream_turn(
        self,
        *,
        session_id: str,
        message: str,
        context: dict[str, Any],
    ) -> AsyncIterator[AgentEvent]:
        self.store.session(session_id)
        observations: list[dict[str, Any]] = []
        for _ in range(self.max_steps):
            decision = await self.backend.decide(
                session_id=session_id,
                message=message,
                context=context,
                observations=observations,
            )
            for call in decision.tool_calls:
                name = call["name"]
                try:
                    definition = self.gateway.definition(name)
                    yield AgentEvent(
                        "status",
                        {"message": definition.status_message, "tool": name},
                    )
                    arguments = json.loads(call["arguments_json"])
                    result = self.gateway.invoke(name, arguments, session_id)
                    observation = {
                        "tool": name,
                        "ok": True,
                        "result": result.for_model(),
                    }
                    if result.local_image_paths:
                        observation["attachments"] = [
                            {
                                "type": "local_image",
                                "artifact_id": result.payload.get("evidence_id"),
                                "path": path,
                            }
                            for path in result.local_image_paths
                        ]
                    observations.append(observation)
                    if result.kind in {"card", "action"}:
                        yield AgentEvent(result.kind, result.payload)
                except (
                    KeyError,
                    PermissionError,
                    ValueError,
                    RevisionConflictError,
                ) as exc:
                    observations.append(
                        {
                            "tool": name,
                            "ok": False,
                            "error": type(exc).__name__,
                            "message": str(exc)[:500],
                        }
                    )
            if decision.done:
                if decision.response:
                    for chunk in _text_chunks(decision.response):
                        yield AgentEvent("text", {"content": chunk})
                if decision.suggestions:
                    yield AgentEvent("suggestions", {"items": decision.suggestions})
                active_edit = self.store.session(session_id).get("active_edit_id")
                yield AgentEvent(
                    "done",
                    {
                        "session_id": session_id,
                        "edit_id": active_edit,
                        "backend": self.backend.name,
                    },
                )
                return
            if not decision.tool_calls:
                raise RuntimeError(
                    "agent returned neither a final response nor tool calls"
                )
        raise RuntimeError(f"agent exceeded the {self.max_steps}-step tool limit")


def _text_chunks(text: str, size: int = 48) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]


def new_session_id() -> str:
    return f"chat_{uuid.uuid4().hex[:20]}"
