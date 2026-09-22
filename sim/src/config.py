"""配置加载与节点构造。"""

from __future__ import annotations

from pathlib import Path

import yaml

from .model import NodeSpec


def load_config(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_nodes(spec: list[dict]) -> list[NodeSpec]:
    """从配置构造节点列表。

    配置示例:
        nodes:
          - {node_id: 0, compute_rate: 16000, slots: 4, latency: 0.005, bandwidth: 1.25e7}
          - {node_id: 1, compute_rate: 8000,  slots: 4, latency: 0.002, bandwidth: 1.25e8}

    未给出 latency / bandwidth 时使用 NodeSpec 的默认值（0.5 ms / 10 Gbps）。
    """
    return [
        NodeSpec(
            node_id=int(item["node_id"]),
            compute_rate=float(item["compute_rate"]),
            slots=int(item.get("slots", 1)),
            latency=float(item.get("latency", 0.0005)),
            bandwidth=float(item.get("bandwidth", 1.25e9)),
        )
        for item in spec
    ]
