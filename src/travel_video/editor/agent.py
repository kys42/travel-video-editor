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
- Treat decision_policy.planning as a mandatory conversational gate before durable-action tools. A normal request to make an edit is plan-first; only the explicit bypass and small-active-edit exceptions declared there may mutate in the same turn.

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
        from openai_codex import ApprovalMode, AsyncCodex, Sandbox

        session = self.store.session(session_id)
        turn_payload = {
            "user_message": message,
            "ui_context": context,
            "tool_observations": observations,
            "active_edit_id": session.get("active_edit_id"),
        }
        prompt = (
            "Produce the next editor decision for this turn.\n"
            + json.dumps(turn_payload, ensure_ascii=False, separators=(",", ":"))
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
            result = await thread.run(
                prompt,
                output_schema=AGENT_DECISION_SCHEMA,
                sandbox=Sandbox.read_only,
                approval_mode=ApprovalMode.deny_all,
            )
        if not result.final_response:
            raise RuntimeError("Codex completed without a structured response")
        return AgentDecision.parse(result.final_response)


class DemoBackend:
    """Deterministic backend that exercises the real gateway and revision path."""

    name = "demo"

    async def decide(
        self,
        *,
        session_id: str,
        message: str,
        context: dict[str, Any],
        observations: list[dict[str, Any]],
    ) -> AgentDecision:
        del session_id, context
        by_tool = {item["tool"]: item for item in observations if item.get("ok")}
        wants_edit = any(word in message for word in ("만들", "편집", "하이라이트", "초안"))
        duration_match = re.search(r"(\d{1,4})\s*초", message)
        target_duration = float(duration_match.group(1)) if duration_match else 60.0
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
                ] if scene_ids else [],
                suggestions=["이 장면들로 60초 초안 만들기", "대화가 있는 장면만 보기"],
                done=True,
            )
        if "create_edit" not in by_tool:
            return AgentDecision(
                tool_calls=[
                    {
                        "name": "show_scene_refs",
                        "arguments_json": json.dumps(
                            {"scene_ids": scene_ids, "title": "초안 후보"},
                            ensure_ascii=False,
                        ),
                    },
                    {
                        "name": "create_edit",
                        "arguments_json": json.dumps(
                            {
                                "title": "여행 하이라이트 초안",
                                "brief": message,
                                "target_duration": target_duration,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
            )
        created = by_tool["create_edit"]["result"]
        if "apply_edit_operations" not in by_tool:
            operations = []
            per_scene = max(3.0, min(9.0, target_duration / max(1, len(scenes[:6]))))
            for item in scenes[:6]:
                source_in = float(item["source_in"])
                operations.append(
                    {
                        "op": "add_scene",
                        "scene_id": item["scene_id"],
                        "source_in": source_in,
                        "source_out": min(float(item["source_out"]), source_in + per_scene),
                        "label": item["title"],
                        "reason": "검토된 하이라이트와 특이 포인트를 우선한 데모 선택",
                    }
                )
            return AgentDecision(
                tool_calls=[
                    {
                        "name": "apply_edit_operations",
                        "arguments_json": json.dumps(
                            {
                                "edit_id": created["edit_id"],
                                "expected_revision_id": created["head_revision_id"],
                                "summary": "검토된 하이라이트 장면을 시간순으로 추가",
                                "operations": operations,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            )
        edited = by_tool["apply_edit_operations"]["result"]
        revision = edited["revision"]
        return AgentDecision(
            response=(
                f"검토된 장면 {revision['clip_count']}개로 {revision['timeline_duration']:.1f}초 초안을 만들었습니다. "
                "원본은 건드리지 않았고, 각 컷은 원본 타임코드에 연결된 새 revision입니다."
            ),
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
            ],
            suggestions=["주문 장면은 줄이고 반응을 더 길게", "첫 장면부터 소스 모니터로 재생"],
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
                    observations.append(observation)
                    if result.kind in {"card", "action"}:
                        yield AgentEvent(result.kind, result.payload)
                except (KeyError, ValueError, RevisionConflictError) as exc:
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
                raise RuntimeError("agent returned neither a final response nor tool calls")
        raise RuntimeError(f"agent exceeded the {self.max_steps}-step tool limit")


def _text_chunks(text: str, size: int = 48) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)] or [""]


def new_session_id() -> str:
    return f"chat_{uuid.uuid4().hex[:20]}"
