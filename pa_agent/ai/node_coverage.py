"""Record which program-filled decision nodes were overridden by the AI.

Diagnostic only: the result is attached to the stage JSON as node_coverage
so that saved records can be aggregated later (which judge is the program
always wrong about?).  Nothing downstream reads it, so a missing or malformed
entry can never affect a trading decision.
"""
from __future__ import annotations

from typing import Any


def _answer_of(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return str(node.get("answer", "") or "").strip()


def _branch_of(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return str(node.get("branch", "") or "").strip()


def build_node_coverage(
    program_nodes: list[dict[str, Any]],
    final_nodes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compare program-filled nodes against the final (post-override) nodes.

    A node counts as overridden when its answer or branch changed.  Nodes that
    only received appended reason text are NOT counted - reason supplements do
    not change the decision.
    """
    final_by_id = {
        str(n.get("node_id", "")).strip(): n
        for n in final_nodes
        if isinstance(n, dict) and n.get("node_id")
    }
    coverage: dict[str, dict[str, Any]] = {}
    for node in program_nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("node_id", "")).strip()
        if not node_id:
            continue
        final = final_by_id.get(node_id)
        prog_answer = _answer_of(node)
        final_answer = _answer_of(final) if final is not None else ""
        prog_branch = _branch_of(node)
        final_branch = _branch_of(final) if final is not None else ""
        coverage[node_id] = {
            "program": prog_answer,
            "program_branch": prog_branch,
            "final": final_answer,
            "final_branch": final_branch,
            "overridden": bool(
                final is not None
                and (final_answer != prog_answer or final_branch != prog_branch)
            ),
        }
    return coverage
