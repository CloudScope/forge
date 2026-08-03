from __future__ import annotations

from typing import Callable, Iterable, Optional, Set

from .models import TaskNode, TaskStatus, Workflow


def descendants(wf: Workflow, roots: Iterable[str]) -> Set[str]:
    children: dict[str, list[str]] = {tid: [] for tid in wf.tasks}
    for tid, node in wf.tasks.items():
        for d in node.deps:
            if d in children:
                children[d].append(tid)
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(children.get(cur, []))
    return seen


def invalidate(
    wf: Workflow,
    changed: Iterable[str],
    *,
    protect: Optional[Callable[[TaskNode], bool]] = None,
) -> tuple[set[str], set[str]]:
    """
    Invalidate changed nodes and their descendants. Return (invalidated, preserved).

    `protect` shields matching nodes from being reset — their descendants are still
    invalidated. Used to keep human approvals frozen: an automated re-plan may redo
    the work behind a decision, but never silently reopens the decision itself.

    Artifact history is intentionally left untouched; lineage is append-only and
    consumers always read the latest version via `wf.artifacts`.
    """
    impact = descendants(wf, changed)
    preserved: set[str] = set()
    for tid, node in wf.tasks.items():
        if tid in impact:
            if protect is not None and protect(node):
                impact.discard(tid)
                if node.status == TaskStatus.SUCCEEDED:
                    preserved.add(tid)
                continue
            node.status = TaskStatus.PENDING
            node.error = None
            node.outputs = {}
        elif node.status == TaskStatus.SUCCEEDED:
            preserved.add(tid)
    return impact, preserved


def graft_nodes(wf: Workflow, nodes: list[TaskNode]) -> None:
    for n in nodes:
        wf.tasks[n.id] = n