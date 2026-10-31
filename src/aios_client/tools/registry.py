"""One source for advertised schemas and strict host-side argument validation."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import get_type_hints

from pydantic import ConfigDict, ValidationError, create_model


class ToolArguments(ValueError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    handler: object
    arguments: type
    capability: str
    effect: str

    def schema(self):
        parameters = self.arguments.model_json_schema()
        parameters.pop("title", None)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


class Registry:
    def __init__(self, guard=lambda: None, bound=lambda result: result):
        self.tools = {}
        self.guard, self.bound = guard, bound
        self.read_only = False

    def permitted(self, tool):
        if not self.read_only:
            return True
        # Arbitrary shell, stdin, HTTP and Git subprocesses can all mutate the
        # host. A fixed inspection tool replaces shell probes in this mode.
        return ((tool.effect == "read" and tool.capability in {
            "workspace.files", "workspace.artifacts", "host.inspect",
        }) or tool.name in {
            "publish_result", "update_plan", "task_blocked", "aios_recall", "aios_progress",
        })

    def add(self, name, handler, description, capability="coding", effect="read"):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or name in self.tools:
            raise ValueError("Invalid or duplicate tool name")
        hints = get_type_hints(handler, include_extras=True)
        fields = {}
        for key, parameter in inspect.signature(handler).parameters.items():
            if key not in hints or parameter.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                raise ValueError(f"Tool {name} requires explicit typed arguments")
            default = (
                ...
                if parameter.default is inspect.Parameter.empty
                else parameter.default
            )
            fields[key] = (hints[key], default)
        arguments = create_model(
            name + "_arguments",
            __config__=ConfigDict(extra="forbid", strict=True),
            **fields,
        )
        if effect not in {"read", "write", "execute", "control"}:
            raise ValueError("Unknown tool effect")
        self.tools[name] = Tool(
            name, description, handler, arguments, capability, effect
        )

    def catalog(self):
        return [tool.schema() for tool in self.tools.values() if self.permitted(tool)]

    def execute(self, name, arguments):
        self.guard()
        if name not in self.tools or not isinstance(arguments, dict):
            raise ToolArguments("Unknown tool or invalid arguments")
        tool = self.tools[name]
        if not self.permitted(tool):
            raise ToolArguments("Read-only inspection policy forbids this tool; use inspect_environment and return findings directly")
        try:
            values = tool.arguments.model_validate(arguments)
        except ValidationError as exc:
            # Provider/model supplied values may contain secrets; never echo them.
            errors = [
                {"field": ".".join(map(str, e["loc"])), "code": e["type"]}
                for e in exc.errors(include_input=False, include_url=False)
            ]
            raise ToolArguments(f"Invalid tool arguments: {errors}") from None
        result = tool.handler(**values.model_dump())
        if not isinstance(result, dict):
            raise TypeError("Tool executor must return an object")
        return self.bound({"ok": True, **result})
