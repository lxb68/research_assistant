"""受限工具协议与注册表，只允许执行显式注册的能力。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """描述一个可供 Agent 选择的受限工具。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def public_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


class ToolRegistry:
    """集中管理工具白名单、参数校验与执行入口。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        name = str(definition.name).strip()
        if not name:
            raise ValueError("工具名称不能为空")
        if name in self._tools:
            raise ValueError(f"工具已注册：{name}")
        self._tools[name] = definition

    def definitions(self) -> list[dict[str, Any]]:
        return [definition.public_schema() for definition in self._tools.values()]

    def prompt_catalog(self) -> str:
        return json.dumps(self.definitions(), ensure_ascii=False, separators=(",", ":"))

    def has(self, name: str) -> bool:
        return str(name).strip() in self._tools

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized_name = str(name).strip()
        definition = self._tools.get(normalized_name)
        if definition is None:
            raise ValueError(f"未注册工具：{normalized_name or '空'}")
        normalized_arguments = dict(arguments or {})
        self._validate_arguments(definition.parameters, normalized_arguments)
        result = definition.handler(normalized_arguments)
        if not isinstance(result, dict):
            raise TypeError(f"工具 {normalized_name} 必须返回对象")
        return result

    @staticmethod
    def _validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ValueError(f"工具缺少必填参数：{', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            unknown = [name for name in arguments if name not in properties]
            if unknown:
                raise ValueError(f"工具包含未知参数：{', '.join(unknown)}")
        type_map: dict[str, tuple[type, ...]] = {
            "string": (str,), "integer": (int,), "number": (int, float),
            "boolean": (bool,), "array": (list,), "object": (dict,),
        }
        for name, value in arguments.items():
            property_schema = properties.get(name)
            if not isinstance(property_schema, dict):
                continue
            expected = str(property_schema.get("type") or "")
            expected_types = type_map.get(expected)
            invalid_number = expected in {"integer", "number"} and isinstance(value, bool)
            if expected_types and (not isinstance(value, expected_types) or invalid_number):
                raise ValueError(f"工具参数 {name} 类型应为 {expected}")
            if isinstance(value, str) and not value.strip() and name in required:
                raise ValueError(f"工具参数 {name} 不能为空")
            allowed_values = property_schema.get("enum")
            if isinstance(allowed_values, list) and value not in allowed_values:
                allowed_text = ", ".join(str(item) for item in allowed_values)
                raise ValueError(f"工具参数 {name} 只能是：{allowed_text}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                minimum = property_schema.get("minimum")
                maximum = property_schema.get("maximum")
                if minimum is not None and value < minimum:
                    raise ValueError(f"工具参数 {name} 不能小于 {minimum}")
                if maximum is not None and value > maximum:
                    raise ValueError(f"工具参数 {name} 不能大于 {maximum}")


__all__ = ["ToolDefinition", "ToolRegistry"]
