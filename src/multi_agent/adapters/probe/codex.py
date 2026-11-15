"""Minimal stdio dialogue control, using the checked-in Codex JSON schemas.

Sources: protocol/codex/schema/{ClientRequest,ClientNotification,ServerRequest,
ServerNotification,JSONRPCError}.json, v1/InitializeParams.json and the v2
ThreadStart, TurnStart, TurnInterrupt, ModelList, ThreadItemsList and CommandExec
request/response schemas.
All other output remains opaque to this recorder.
"""

import asyncio
from importlib.metadata import version
from typing import Any

from .config import BAD_MODEL, OUTPUT_SCHEMA, PROMPTS, Options
from .process import Process


class Conversation:
    def __init__(self, process: Process) -> None:
        self.process = process
        self.next_id = 0
        self.thread_id: str | None = None
        self.process_exited = False
        self.turn_completed = False
        self.cancel_skipped = False

    def skip_cancel(self) -> None:
        if not self.cancel_skipped:
            self.cancel_skipped = True
            self.process.observe("cancel_skipped", True)

    async def receive(self) -> dict[str, Any] | None:
        while True:
            message = await self.process.read()
            if message is None:
                self.process_exited = True
                return None
            if not isinstance(message, dict):
                continue
            method = message.get("method")
            if method is not None and "id" in message:
                response: dict[str, Any] = {"id": message["id"]}
                if method in (
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                ):
                    response["result"] = {"decision": "decline"}
                else:
                    response["error"] = {
                        "code": -32601,
                        "message": "Method not supported by the probe",
                    }
                await self.process.send(response)
            elif method == "turn/completed":
                params = message.get("params", {})
                if (
                    self.thread_id is not None
                    and params.get("threadId") == self.thread_id
                ):
                    self.turn_completed = True
            return message

    async def send_request(self, method: str, params: dict[str, Any]) -> int | None:
        self.next_id += 1
        sent = await self.process.send(
            {"id": self.next_id, "method": method, "params": params}
        )
        return self.next_id if sent else None

    async def request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        request_id = await self.send_request(method, params)
        if request_id is None:
            return None
        while not self.process_exited:
            message = await self.receive()
            if (
                message is not None
                and "method" not in message
                and message.get("id") == request_id
            ):
                return message
        return None

    async def wait_turn_end(self) -> None:
        while not self.turn_completed and not self.process_exited:
            await self.receive()

    async def collect_items(self) -> None:
        """Collect evidence after the turn, within the caller's step deadline."""
        if not self.turn_completed:
            return
        params: dict[str, Any] = {"threadId": self.thread_id}
        while not self.process_exited and self.process.child.returncode is None:
            response = await self.request("thread/items/list", params)
            if response is None or "error" in response:
                return
            cursor = response["result"].get("nextCursor")
            if cursor is None:
                return
            params = {"threadId": self.thread_id, "cursor": cursor}

    async def sandbox(self, cwd: str) -> None:
        for command, policy in (
            (["cat", "README.md"], {"type": "readOnly"}),
            (["touch", "probe-write-check.txt"], {"type": "readOnly"}),
            (
                ["touch", "probe-write-control.txt"],
                {"type": "workspaceWrite", "writableRoots": [cwd]},
            ),
        ):
            response = await self.request(
                "command/exec",
                {"command": command, "cwd": cwd, "sandboxPolicy": policy},
            )
            if response is None:
                return

    async def cancel_turn(self, turn_id: str, options: Options) -> bool:
        waiting = asyncio.create_task(self.wait_turn_end())
        try:
            done, _ = await asyncio.wait({waiting}, timeout=options.cancel_after)
            if done or self.turn_completed or self.process.child.returncode is not None:
                await waiting
                self.skip_cancel()
                return False
            request_id = await self.send_request(
                "turn/interrupt", {"threadId": self.thread_id, "turnId": turn_id}
            )
            await asyncio.wait_for(asyncio.shield(waiting), options.grace)
            if request_id is None:
                self.skip_cancel()
            return request_id is not None
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def settle(self, seconds: float) -> None:
        # Keep answering server requests while late notifications are recorded.
        try:
            async with asyncio.timeout(seconds):
                while await self.receive() is not None:
                    pass
        except TimeoutError:
            pass

    async def run(self, step: str, options: Options, cwd: str) -> bool:
        """Return whether cancellation was sent; completion is independent of success."""
        response = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "multi-agent-probe",
                    "version": version("multi-agent"),
                }
            },
        )
        if response is None:
            if step == "codex-cancel":
                self.skip_cancel()
            return False
        await self.process.send({"method": "initialized"})
        if step == "codex-sandbox":
            await self.sandbox(cwd)
            return False
        if step == "codex-env":
            await self.request("account/read", {"refreshToken": False})
            for hidden in (False, True):
                params: dict[str, Any] = {"includeHidden": True} if hidden else {}
                while not self.process_exited:
                    response = await self.request("model/list", params)
                    if response is None or "error" in response:
                        break
                    cursor = response["result"].get("nextCursor")
                    if cursor is None:
                        break
                    params = {**params, "cursor": cursor}
            if not self.process_exited:
                await self.request("account/rateLimits/read", {})
            return False

        suffix = step.removeprefix("codex-")
        model = BAD_MODEL if suffix == "bad-model" else options.codex_model
        response = await self.request(
            "thread/start",
            {
                "model": model,
                "cwd": cwd,
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "ephemeral": True,
            },
        )
        if response is None or "error" in response:
            if suffix == "cancel":
                self.skip_cancel()
            return False
        self.thread_id = response["result"]["thread"]["id"]
        params = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": PROMPTS[suffix]}],
            "model": model,
            "effort": options.codex_effort,
            "sandboxPolicy": {"type": "readOnly"},
            "approvalPolicy": "never",
        }
        if suffix == "structured":
            params["outputSchema"] = OUTPUT_SCHEMA
        response = await self.request("turn/start", params)
        if response is None or "error" in response:
            if suffix == "cancel":
                self.skip_cancel()
            return False
        cancelled = False
        if suffix == "cancel":
            cancelled = await self.cancel_turn(
                response["result"]["turn"]["id"], options
            )
        else:
            await self.wait_turn_end()
        await self.collect_items()
        return cancelled
