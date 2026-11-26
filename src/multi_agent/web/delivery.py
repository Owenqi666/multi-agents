"""Build a copyable handoff from the receipt that authorized the delivery."""

import re
from dataclasses import asdict
from typing import Any
from urllib.parse import quote

from .actions import ActionConflict
from .resources import ArchiveUnsupported, _owned, _ResourceStore, _scope
from .workspace import Workspace


def _code(text: str) -> str:
    # Git refs and user-authored goals can themselves contain Markdown.
    fence = "`" * (1 + max((len(s) for s in re.findall(r"`+", text)), default=0))
    line = " ".join(text.splitlines())
    return f"{fence} {line} {fence}"


def delivery_summary(
    workspace: Workspace, identifier: str, origin: str
) -> dict[str, Any]:
    database, common = _scope(
        workspace, "integration", identifier, common_dir=workspace.git_common_dir
    )
    with _ResourceStore.open_readonly(database) as store:
        _owned(
            store,
            common,
            "integration",
            identifier,
            archive=workspace.archive is not None,
        )
        if not store.has_integration_verification():
            raise ArchiveUnsupported("This archive predates integration acceptance.")
        receipt = store.load_integration_acceptance(identifier)
        if receipt is None:
            raise ActionConflict("A delivery summary is available after acceptance.")
        record = store.load_integration(identifier)
        result = store.load_integration_test_result(identifier, receipt.test_attempt)
        tasks = [
            {"id": m.task_input.card.id, "goal": m.task_input.card.goal}
            for m in record.members
        ]
        commands = [
            {"name": c.command.name, "exit_code": c.exit_code} for c in result.commands
        ]
    links = {
        "integration": f"/integrations/{quote(identifier, safe='')}",
        "diff": f"/diffs/snapshot/{quote(receipt.snapshot_id, safe='')}",
        "review": f"/reviews/{quote(receipt.review_round_id, safe='')}",
    }
    lines = [
        "## 集成交付摘要",
        "",
        f"- 验收时间 {_code(receipt.accepted_at)}",
        f"- 候选提交 {_code(receipt.candidate.commit)}（{_code(receipt.candidate.ref)}）",
        f"- 目标分支 {_code(receipt.target_ref)}，验收基线 {_code(receipt.target_baseline)}",
        f"- 验收收据 {_code(receipt.acceptance_id)}",
        "",
        "### 交付内容",
        "",
        *(f"- {_code(task['id'])} {_code(task['goal'])}" for task in tasks),
        "",
        "### 测试与审核",
        "",
        f"- 测试第 {receipt.test_attempt} 次通过，证据 SHA-256 {_code(receipt.test_evidence_hash)}",
        *(f"- {_code(c.command.name)}，退出码 {c.exit_code}" for c in result.commands),
        f"- 通过的审核轮 {_code(receipt.review_round_id)}，冻结快照 {_code(receipt.snapshot_id)}",
        f"- 审核检查点 SHA-256 {_code(receipt.review_checkpoint_hash)}",
        "",
        (
            f"在本机工作台查看 [交付与测试]({origin}{links['integration']}) · "
            f"[冻结差异]({origin}{links['diff']}) · [审核依据]({origin}{links['review']})"
        ),
    ]
    return {
        "receipt": asdict(receipt),
        "tasks": tasks,
        "tests": commands,
        "links": links,
        "markdown": "\n".join(lines) + "\n",
    }
