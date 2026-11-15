"""Minimal stdio dialogue control, using the checked-in Codex JSON schemas.

Sources: protocol/codex/schema/{ClientRequest,ClientNotification,ServerRequest,
ServerNotification,JSONRPCError}.json, v1/InitializeParams.json and the v2
ThreadStart, TurnStart, TurnInterrupt and ModelList request/response schemas.
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
        self.ended = False

    async def receive(self) -> dict[str, Any] | None:
        while True:
            message = await self.process.read()
            if message is None:
                self.ended = True
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
                    self.ended = True
            return message

    async def send_request(self, method: str, params: dict[str, Any]) -> int:
        self.next_id += 1
        await self.process.send(
            {"id": self.next_id, "method": method, "params": params}
        )
        return self.next_id

    async def request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any] | None:
        request_id = await self.send_request(method, params)
        while not self.ended:
            message = await self.receive()
            if (
                message is not None
                and "method" not in message
                and message.get("id") == request_id
            ):
                return message
        return None

    async def wait_end(self) -> None:
        while not self.ended:
            await self.receive()

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
            return False
        await self.process.send({"method": "initialized"})
        if step == "codex-env":
            await self.request("account/read", {"refreshToken": False})
            for hidden in (False, True):
                params: dict[str, Any] = {"includeHidden": True} if hidden else {}
                while not self.ended:
                    response = await self.request("model/list", params)
                    if response is None or "error" in response:
                        break
                    cursor = response["result"].get("nextCursor")
                    if cursor is None:
                        break
                    params = {**params, "cursor": cursor}
            if not self.ended:
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
            return False
        if suffix != "cancel":
            await self.wait_end()
            return False

        waiting = asyncio.create_task(self.wait_end())
        try:
            done, _ = await asyncio.wait({waiting}, timeout=options.cancel_after)
            if done or self.ended or self.process.child.returncode is not None:
                await waiting
                return False
            await self.send_request(
                "turn/interrupt",
                {
                    "threadId": self.thread_id,
                    "turnId": response["result"]["turn"]["id"],
                },
            )
            await asyncio.wait_for(asyncio.shield(waiting), options.grace)
            return True
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
