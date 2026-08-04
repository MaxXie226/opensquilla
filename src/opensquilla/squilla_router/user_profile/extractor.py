"""Transcript rendering, batching, and the provider-neutral streaming loop.

The gateway injects the concrete LLM stream factory, keeping this package free
of the provider layer while preserving the same Dream provider/model selection.
Rendering preserves every non-empty transcript entry with its role and
truncates head+tail so a long session still fits a bounded prompt. Batching
drops a session that alone blows the batch budget rather than cutting it
mid-way — keeping every ``session_id`` the LLM sees honest.
"""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from opensquilla.squilla_router.user_profile.prompts import (
    SYSTEM_PROMPT,
    build_batch_prompt,
    parse_batch_response,
)
from opensquilla.squilla_router.user_profile.schema import (
    BatchAnalysis,
    SessionTranscript,
)

_TRUNCATION_MARKER = "\n[transcript truncated]\n"


class _TranscriptRow(Protocol):
    role: str
    content: str | None


class StreamFactory(Protocol):
    """Build the concrete provider stream without importing the provider layer."""

    def __call__(
        self,
        *,
        provider: Any,
        user_prompt: str,
        system_prompt: str,
        max_output_tokens: int,
        temperature: float,
        timeout: float,
    ) -> AsyncIterator[Any]: ...


def _truncate(text: str, max_chars: int, head_fraction: float = 0.5) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = _TRUNCATION_MARKER[:max_chars]
    retained = max_chars - len(marker)
    if retained <= 0:
        return marker
    head_chars = math.floor(retained * head_fraction)
    tail_chars = retained - head_chars
    tail = text[-tail_chars:] if tail_chars else ""
    return text[:head_chars] + marker + tail


def render_transcript(
    session_id: str,
    rows: Sequence[_TranscriptRow],
    *,
    per_session_max_chars: int,
) -> SessionTranscript:
    """Render one session to a role-prefixed, truncated plain-text blob."""

    head_budget = max(0, per_session_max_chars // 2)
    tail_budget = max(0, per_session_max_chars - head_budget - len(_TRUNCATION_MARKER))
    head_parts: list[str] = []
    tail_parts: deque[str] = deque()
    full_parts: list[str] | None = []
    head_chars = 0
    tail_chars = 0
    total_chars = 0
    for row in rows:
        role = getattr(row, "role", "") or ""
        if not role:
            continue
        content = getattr(row, "content", None)
        if not content:
            continue
        line = f"{role}: {content}"
        chunk = line if total_chars == 0 else "\n" + line
        total_chars += len(chunk)
        if full_parts is not None:
            full_parts.append(chunk)
            if total_chars > per_session_max_chars:
                full_parts = None
        if head_chars < head_budget:
            keep = chunk[: head_budget - head_chars]
            head_parts.append(keep)
            head_chars += len(keep)
        if tail_budget > 0:
            tail_parts.append(chunk)
            tail_chars += len(chunk)
            while tail_chars > tail_budget and tail_parts:
                overflow = tail_chars - tail_budget
                first = tail_parts[0]
                if overflow >= len(first):
                    tail_chars -= len(first)
                    tail_parts.popleft()
                else:
                    tail_parts[0] = first[overflow:]
                    tail_chars -= overflow
                    break
    if total_chars <= per_session_max_chars:
        text = "".join(full_parts or [])
    else:
        text = "".join(head_parts) + _TRUNCATION_MARKER + "".join(tail_parts)
        text = _truncate(text, per_session_max_chars)
    return SessionTranscript(session_id=session_id, text=text)


def batch_sessions(
    sessions: list[SessionTranscript],
    *,
    batch_size: int,
    batch_input_max_chars: int,
) -> list[list[SessionTranscript]]:
    """Group sessions into batches of ~``batch_size`` within a char budget.

    A session whose rendered text alone exceeds ``batch_input_max_chars`` is
    dropped (not split), so no batch references a session it only partially
    showed the model.
    """

    batches: list[list[SessionTranscript]] = []
    current: list[SessionTranscript] = []
    for session in sessions:
        candidate = [session]
        candidate_size = len(SYSTEM_PROMPT) + len(build_batch_prompt(candidate))
        if batch_input_max_chars > 0 and candidate_size > batch_input_max_chars:
            continue  # too big even alone -> drop
        current_candidate = [*current, session]
        current_candidate_size = len(SYSTEM_PROMPT) + len(
            build_batch_prompt(current_candidate)
        )
        would_overflow = (
            batch_input_max_chars > 0
            and current
            and current_candidate_size > batch_input_max_chars
        )
        if current and (len(current) >= batch_size or would_overflow):
            batches.append(current)
            current = []
        current.append(session)
    if current:
        batches.append(current)
    return batches


async def extract_batch(
    *,
    provider: Any,
    stream_factory: StreamFactory,
    batch: list[SessionTranscript],
    max_output_tokens: int,
    temperature: float,
    timeout: float,
    response_max_chars: int,
) -> BatchAnalysis:
    """Run one batch through the provider and parse its reply. Fail-open.

    Mirrors the task-analyzer consumption loop: bounded by an ``asyncio.timeout``,
    accumulates ``text_delta`` events under a size cap, stops on ``done``, raises
    on ``error``, and always closes the stream. Any failure returns a failed
    :class:`BatchAnalysis` so the run continues best-effort.
    """

    session_ids = tuple(s.session_id for s in batch)
    if not batch:
        return BatchAnalysis.failed(session_ids)
    stream: Any | None = None
    try:
        text_parts: list[str] = []
        total_chars = 0
        got_done = False
        async with asyncio.timeout(timeout):
            stream = stream_factory(
                provider=provider,
                user_prompt=build_batch_prompt(batch),
                system_prompt=SYSTEM_PROMPT,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
                timeout=timeout,
            )
            try:
                async for event in stream:
                    kind = getattr(event, "kind", None)
                    if kind == "text_delta":
                        text = str(getattr(event, "text", ""))
                        total_chars += len(text)
                        if response_max_chars > 0 and total_chars > response_max_chars:
                            raise ValueError("profile analyst response exceeded size limit")
                        text_parts.append(text)
                    elif kind == "done":
                        got_done = True
                        break
                    elif kind == "error":
                        code = getattr(event, "code", None) or "unknown"
                        raise RuntimeError(f"provider_error:{code}")
            finally:
                aclose = getattr(stream, "aclose", None)
                if callable(aclose):
                    await aclose()
        if not got_done:
            raise RuntimeError("profile analyst stream ended before DoneEvent")
        return parse_batch_response("".join(text_parts), session_ids)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a bad batch must not abort the run
        return BatchAnalysis.failed(session_ids)


__all__ = [
    "batch_sessions",
    "extract_batch",
    "render_transcript",
    "StreamFactory",
]
