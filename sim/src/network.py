"""网络状态模型：静态与不稳定（时变）链路。

RQ2 的前提是"网络状态会变，而调度器看到的可能是过期的"。
如果网络是静态的，staleness 就没有研究对象——所以时变网络是 RQ2 的必要条件。

设计要点：
  - 真实链路状态由 NetworkTrajectory.state(node_id, t) 给出，仿真用它算传输时间；
  - 调度器看到的是 state(node_id, t - lag)，lag 就是 staleness；
  - 两者分离，因此"决策错误"和"网络真的变差了"可以被区分开。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class LinkState:
    """某条链路在某一时刻的真实状态。"""

    latency: float      # 单程时延（秒）
    bandwidth: float    # 带宽（字节/秒）


class NetworkTrajectory(ABC):
    """链路状态随时间的变化。"""

    @abstractmethod
    def state(self, node_id: int, t: float) -> LinkState:
        ...

    @abstractmethod
    def describe(self) -> str:
        ...


class StaticNetwork(NetworkTrajectory):
    """链路状态恒定。用于"正常"和"退化"两种网络条件。"""

    def __init__(self, links: dict[int, LinkState], label: str = "static"):
        self.links = dict(links)
        self.label = label

    def state(self, node_id: int, t: float) -> LinkState:
        return self.links[node_id]

    def describe(self) -> str:
        return f"StaticNetwork[{self.label}]"


class UnstableNetwork(NetworkTrajectory):
    """链路状态随时间随机波动（抖动/不稳定）。

    模型：对数带宽做均值回复的 OU 过程，时延按同样方向小幅波动。
        bw(t)   = bw_mean * exp(sigma_b * x(t))
        lat(t)  = lat_mean * exp(sigma_l * x(t))     # 拥塞时延随带宽下降而上升
        dx = -theta * x * dt + sqrt(2*theta) * dW

    采用预先离散化 + 线性插值，保证同一 seed 完全可复现，
    并且调度器可以"回看"任意历史时刻的状态（staleness 需要这个能力）。

    简化之处：各节点独立抽样，没有建模共享链路的相关性。
    """

    def __init__(
        self,
        links: dict[int, LinkState],
        seed: int,
        theta: float = 0.5,          # 回复速率（1/秒）
        sigma_b: float = 0.6,        # 带宽对数波动幅度
        sigma_l: float = 0.3,        # 时延对数波动幅度
        dt: float = 0.2,             # 离散化步长（秒）
        horizon: float = 86400.0,    # 预生成时长（秒）
        label: str = "unstable",
    ):
        self.links = dict(links)
        self.label = label
        self.dt = dt
        self.theta = theta
        self.sigma_b = sigma_b
        self.sigma_l = sigma_l
        self.horizon = horizon
        self._build(seed)

    def _build(self, seed: int) -> None:
        n_steps = int(self.horizon / self.dt) + 2
        self.t_grid = np.arange(n_steps) * self.dt
        self._bw: dict[int, np.ndarray] = {}
        self._lat: dict[int, np.ndarray] = {}

        for i, link in sorted(self.links.items()):
            rng = np.random.default_rng(seed * 1000003 + i)
            x = np.zeros(n_steps)
            sqrt_term = np.sqrt(2.0 * self.theta * self.dt)
            for k in range(1, n_steps):
                x[k] = (x[k - 1] * (1.0 - self.theta * self.dt)
                        + sqrt_term * rng.standard_normal())
            self._bw[i] = link.bandwidth * np.exp(self.sigma_b * x)
            self._lat[i] = link.latency * np.exp(self.sigma_l * x)

    def state(self, node_id: int, t: float) -> LinkState:
        t = min(max(t, 0.0), self.horizon)
        return LinkState(
            latency=float(np.interp(t, self.t_grid, self._lat[node_id])),
            bandwidth=float(np.interp(t, self.t_grid, self._bw[node_id])),
        )

    def describe(self) -> str:
        return (f"UnstableNetwork[{self.label}, theta={self.theta}, "
                f"sigma_bw={self.sigma_b}, sigma_lat={self.sigma_l}]")
