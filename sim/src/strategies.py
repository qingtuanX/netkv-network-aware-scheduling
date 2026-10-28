"""调度策略。

四个策略共用同一个接口：给一份【调度器可见的状态】，返回一个 node_id。

关键设计：策略只看到 NodeView（"调度器以为的状态"），
看不到 NodeSpec（"真实状态"）。因此 staleness 是观测层的事，
策略代码完全不需要知道它存在，也不可能绕过它偷看真实状态。
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .model import NodeView, Request, network_time, request_bytes


@dataclass
class Observation:
    """调度器在一次决策时能看到的状态快照。

    注意语义：这不是"真实状态"，而是"调度器以为的状态"。
    当 lag > 0 时，这个快照来自过去，并且它自己知道这一点
    （staleness 属性），因此可以据此设计回退逻辑。
    """

    nodes: list[NodeView]
    observed_at: float              # 快照对应的仿真时刻（两条通道里更早的那个，供统一口径使用）
    true_time: float                # 当前真实时刻
    bytes_per_context_token: float = 4.0
    bytes_per_generated_token: float = 4.0
    # 【细变量①】两条通道各自的快照时刻。
    # 为什么必须分开告诉策略：实测（§16 二维扫描）表明
    # **队列消息旧 5 秒 ≈ 网线消息旧 15–60 秒**——两者的耐旧度差一个数量级，
    # 用同一个年龄阈值去门控必然做错其中一条（早先的 fallback 就是这么失效的）。
    # 缺省值 = observed_at，保证旧调用方行为不变。
    observed_at_queue: float | None = None
    observed_at_network: float | None = None

    @property
    def staleness(self) -> float:
        return self.true_time - self.observed_at

    @property
    def age_queue(self) -> float:
        t = self.observed_at_queue if self.observed_at_queue is not None else self.observed_at
        return self.true_time - t

    @property
    def age_network(self) -> float:
        t = (self.observed_at_network if self.observed_at_network is not None
             else self.observed_at)
        return self.true_time - t


class Strategy(ABC):
    """所有策略的共同基类。"""

    name: str = "strategy"

    def __init__(self, seed: int = 0):
        self.rng = random.Random(seed)
        # 决策日志：论文里的"决策解释记录"，也是调试 staleness 的唯一手段
        self.decisions: list[dict] = []
        self.log_decisions = False

    @abstractmethod
    def choose(self, req: Request, obs: Observation) -> int:
        """返回选中的 node_id。"""

    def describe(self) -> str:
        return self.name

    def stats_columns(self) -> dict:
        """可选的运行期诊断列，会被 run_matrix 追加进 runs.csv（默认无）。

        存在的理由：自适应的效果全靠"实际施加了多大权重"说话，而这个数**只有策略自己知道**——
        权重可能被降权因子压低，也可能被"网络占比"那道门整个关掉。只报公式里那个数会被
        读成"它一定用了 0.29 的权重"，实际可能一次都没用上。
        """
        return {}

    def _log(self, req: Request, obs: Observation, chosen: int,
             scores: dict[int, float] | None = None) -> None:
        if self.log_decisions:
            self.decisions.append({
                "req_id": req.req_id,
                "true_time": obs.true_time,
                "observed_at": obs.observed_at,
                "staleness": obs.staleness,
                "chosen": chosen,
                "scores": scores,
            })


class RandomStrategy(Strategy):
    """随机选择。作为下界参考，不是强基线。"""

    name = "Random"

    def choose(self, req: Request, obs: Observation) -> int:
        node = self.rng.choice(obs.nodes)
        self._log(req, obs, node.node_id)
        return node.node_id


class LeastQueueStrategy(Strategy):
    """选择当前未完成请求数最少的节点。"""

    name = "LeastQueue"

    def choose(self, req: Request, obs: Observation) -> int:
        best = min(obs.nodes, key=lambda n: (n.pending, n.node_id))
        self._log(req, obs, best.node_id)
        return best.node_id


class LeastLoadStrategy(Strategy):
    """选择积压排空时间估计最短的节点。

    load 的定义见 NodeView.backlog_time。
    注意这是"资源余量"视角，不等于"这个请求会最快完成"——
    Sia (SOSP'23) 正是靠这个区别说明朴素负载感知会损失 goodput。
    """

    name = "LeastLoad"

    def choose(self, req: Request, obs: Observation) -> int:
        best = min(obs.nodes, key=lambda n: (n.backlog_time, n.node_id))
        self._log(req, obs, best.node_id)
        return best.node_id


class NetworkAwareStrategy(Strategy):
    """LeastLoad + 网络代价项。

    评分函数：
        score(node) = backlog_time(node) + w_net * network_cost(node, req)

    其中 network_cost = 输入传输时间 + 输出传输时间，用【观测到的】链路状态计算。

    【关键设计】w_net = 0 时，这个策略的排序与 LeastLoad 完全一致。
    因此消融实验里可以直接验证这一点：如果 w_net=0 的结果与 LeastLoad
    不是逐位相同，说明实现里有 bug。任何"网络感知的收益"也就都能
    干净地归因到网络项，而不是被别的差异污染。
    """

    name = "NetworkAware"

    def __init__(self, seed: int = 0, w_net: float = 1.0,
                 fallback_staleness: float | None = None,
                 fallback_w_net: float = 0.0):
        super().__init__(seed=seed)
        self.w_net = w_net
        # RQ2 的自适应回退：观测过期超过 fallback_staleness 秒时，
        # 把网络项权重降到 fallback_w_net（默认 0，即退回纯 LeastLoad）。
        self.fallback_staleness = fallback_staleness
        self.fallback_w_net = fallback_w_net

    def effective_w_net(self, obs: Observation) -> float:
        if self.fallback_staleness is None:
            return self.w_net
        return (self.fallback_w_net if obs.staleness > self.fallback_staleness
                else self.w_net)

    def choose(self, req: Request, obs: Observation) -> int:
        bpc = obs.bytes_per_context_token
        bpg = obs.bytes_per_generated_token
        in_bytes, out_bytes = request_bytes(
            req.context_tokens, req.generated_tokens, bpc, bpg)
        req.in_bytes, req.out_bytes = in_bytes, out_bytes

        w = self.effective_w_net(obs)
        scores: dict[int, float] = {}
        for n in obs.nodes:
            t_in, t_out = network_time(in_bytes, out_bytes, n)
            scores[n.node_id] = n.backlog_time + w * (t_in + t_out)

        chosen = min(scores, key=lambda k: (scores[k], k))
        self._log(req, obs, chosen, scores)
        return chosen

    def describe(self) -> str:
        if self.fallback_staleness is not None:
            return (f"{self.name}(w={self.w_net:g},fallback@{self.fallback_staleness:g}s"
                    f"->w={self.fallback_w_net:g})")
        return f"{self.name}(w_net={self.w_net:g})"


class NetworkAwareContentionStrategy(NetworkAwareStrategy):
    """V2 机制贡献：把**算力队列**与**链路队列**两条积压放在一起看。

    评分函数（**看结果前冻结**）：
        score(node) = backlog_time(node)                  # 算力队列的排空时间
                    + w_net × (link_drain_time(node)      # 链路队列的排空时间
                             + t_in(node, req) + t_out(node, req))

    其中 `link_drain_time = 链路上积压的字节 / 带宽`，与 `backlog_time = 未完成工作量 /
    算力×槽位` **完全对称**——一个看算力队列，一个看链路队列。

    与 v1 的 `NetworkAware` 唯一的区别就是多了 `link_drain_time` 这一项。
    这不是调参：它对应一个可解释的机制（"链路也是被争用的资源，别人已经压在这条
    链路上的字节同样会推迟我这次传输"），而且它用的是**观测到**的链路积压，
    因此天然会被陈旧污染——这正好是 RQ2 要研究的对象。

    【门 2】w_net = 0 时评分退化为 backlog_time，排序与 LeastLoad 逐位相同。
    """

    name = "NetworkAware2"

    def choose(self, req: Request, obs: Observation) -> int:
        bpc = obs.bytes_per_context_token
        bpg = obs.bytes_per_generated_token
        in_bytes, out_bytes = request_bytes(
            req.context_tokens, req.generated_tokens, bpc, bpg)
        req.in_bytes, req.out_bytes = in_bytes, out_bytes

        w = self.effective_w_net(obs)
        scores: dict[int, float] = {}
        for n in obs.nodes:
            t_in, t_out = network_time(in_bytes, out_bytes, n)
            scores[n.node_id] = n.backlog_time + w * (
                n.link_drain_time + t_in + t_out)

        chosen = min(scores, key=lambda k: (scores[k], k))
        self._log(req, obs, chosen, scores)
        return chosen


def _decay_linear(age: float, tau: float) -> float:
    """线性降权因子：年龄 0 → 1.0，年龄 ≥ tau → 0.0（只作消融对照臂）。"""
    if tau <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - max(age, 0.0) / tau))


def _decay_exp(age: float, tau: float) -> float:
    """指数降权因子：年龄 0 → 1.0，tau → 1/e ≈ 0.37，**永不精确归零**。

    为什么用指数而不是"到某个年龄就归零"：§16 实测的"优势−年龄"曲线本身近似指数——
    队列通道在 0 / 1 / 5 s 时优势为 −23% / −17% / −6%（mixed），而 exp(−age/4) 的
    值是 1.00 / 0.78 / 0.29，乘上 −23% 得 −23% / −18% / −7%，与实测差 1 个百分点以内。
    而且指数永不把权重压成 0："旧消息仍有一部分用"正是 §16 的结论；
    一刀切归零（硬开关）已经被实测证明是过度回退。
    """
    if tau <= 0:
        return 0.0
    return math.exp(-max(age, 0.0) / tau)


class NetworkAwareAdaptiveStrategy(NetworkAwareContentionStrategy):
    """V3 方法贡献：**按"信息自身的时变尺度"给网络项降权**，而不是按"通道年龄"一刀切。

    【一句人话】看到的状态越旧，就越不能全信——但"少信一点"和"完全不信"是两回事。
    更要紧的是：网络信息里其实有两块内容，**哪条网线粗**（物理属性，几分钟内不变）
    和**此刻谁在挤这条链路**（秒级就变样）。旧的快照毁掉的是后者，前者照样管用。

    【设计是怎么收敛到这个形态的——三次失败 + 一处实测，别重犯】
      第一版：让"队列通道不可信"时把队列项归零、只留网络项 → **灾难**
        （同一负载 p95 从 57 s 涨到 522 s）：可用的排序信息一少，所有请求涌向
        "快网线 + 慢算力"那台，直接把它压爆。
        → 教训：自适应只能决定"网络项用多少"，**绝不能改写队列项**——
          队列是唯一的负载信号，把它拿掉不是回退，是自残。
      第二版：用变异系数（CV）判抖动 → **失效**。链路积压是**尖刺型**信号
        （全新鲜时 CV 中位 0.03、p90 2.23），任何 CV 阈值都等价于"这条链路忙不忙"，
        66% 的决策被误关，策略退化成 LeastLoad（p95 14.5 s → 71.8 s）。
      第三版：硬开关 `1[年龄 ≤ 阈值]` → **过度回退**。新鲜时确实好，但队列消息一旧 5 s
        就 `w=0` 完全关掉网络项，p95 掉回 85.6 s（同一格 NA2 还有 57.1 s）。
      于是有了现在这一版：衰减尺度直接**从 §16 实测曲线拟合**，不再拍一个阈值。

    【最终形态】（评估前写定）
    ```
        w_q   = exp(−年龄_队列 / TAU_Q)        # 队列通道：整块网络信息的可信度
        dyn   = exp(−年龄_链路 / TAU_N_DYN)    # 链路的"忙闲"（动态部分）
        stat  = 1                              # 链路的"粗细"（结构部分，不随年龄衰减）
        score_i = backlog_i + w_q × (dyn × link_drain_i + stat × (t_in_i + t_out_i))
    ```
    | 参数 | 取值 | 依据（全部来自本仓库已跑完的实验） |
    |---|---|---|
    | `TAU_Q = 4 s` | 队列通道衰减尺度 | §16：队列消息 0/1/5 s 时优势 −23%/−17%/−6%（mixed），正好是 exp(−age/4)=1.00/0.78/0.29 乘以 −23% |
    | `TAU_N_DYN = 15 s` | 链路"忙闲"衰减尺度 | §16 链路通道 0→15 s：−23% → −12%，落差的一半在 15 s 附近 |
    | 结构部分不衰减（`stat = 1`） | —— | §16：链路通道旧到 60 s 仍有 −13%，**看不到继续衰减**——因为带宽是物理属性，旧快照依然准确回答"哪条网线粗"（这一条也解释了 §16 的"链路比队列耐旧"） |
    | `网络占比 ≥ 0.10` | 关掉网络项 | §13 带宽敏感性：机内档（占比≈0）时收益消失 |

    **当 `w_q = 0`（或 `W_NET=0`，或网络占比不足）时，评分退化为 `backlog_time`，
    与 LeastLoad 逐位相同**——门 2 因此仍然成立，网络项的贡献依然可以干净归因。

    【门 2 相容性】`W_NET=0` ⇒ 与 LeastLoad 逐位相同 ✓。
    【参数冻结声明】上表的数与规则在评估前写定；`decay="hard"/"linear"` 两个对照臂、
    漂移门控开关、阈值网格都会作为**独立消融**报出，不做"调好再报"。
    """

    name = "NetworkAware2Adapt"

    # —— 冻结参数：主方法（decay="split"），全部由 §16 实测曲线拟合 ——
    TAU_Q = 4.0             # 队列通道的指数衰减尺度（秒）
    TAU_N_DYN = 15.0        # 链路"忙闲"（动态部分）的指数衰减尺度（秒）
    NET_SHARE_FLOOR = 0.10  # 本请求网络占比低于此 → 关掉网络项
    # —— decay="channel"（2026-10-01 修订版，见类 docstring 末尾的修订记录）——
    # 按 §16 的 10 种子二维扫描：链路通道旧到 60 s 仍有 −24%(data_heavy)/−17%(mixed) 的收益，
    # 即**几乎不随年龄衰减**；队列通道则在 5 s 内掉到 −12%/−4%。所以正确的时间常数是
    # 一条快（队列）、一条极慢并且带下限（链路）。
    TAU_N = 60.0            # 链路通道的指数衰减尺度（秒）
    LINK_FLOOR = 0.7        # 链路通道的权重下限（§16：旧到 60 s 仍有 65%~90% 的收益）
    # —— 消融对照臂的参数（不是主方法的一部分）——
    TAU_Q_HARD = 1.0
    TAU_N_HARD = 10.0
    TAU_Q_LIN = 5.0
    TAU_N_LIN = 60.0
    VOL_DRIFT = 0.5         # 漂移容忍量（秒）；见 DRIFT_GATE
    DRIFT_GATE = False      # 第三版遗留的二值漂移门控，默认关闭；消融里可开
    W_NET = 1.0
    HIST_MAX = 64
    DECAY = "split"         # split（第一版，已被实测否掉）| channel（修订版）| hard | linear

    def __init__(self, seed: int = 0, w_net: float = W_NET,
                 decay: str = DECAY, **kw):
        # 参数覆盖项：只允许覆盖下面这些门控参数（类属性即默认值）。
        # 必须在这里 pop 掉，不能透传给父类——父类的签名里没有它们。
        for key in ("tau_q", "tau_n", "tau_n_dyn", "tau_q_hard", "tau_n_hard",
                    "tau_q_lin", "tau_n_lin", "vol_drift", "net_share_floor",
                    "link_floor"):
            if key in kw:
                setattr(self, key.upper(), float(kw.pop(key)))
        if "drift_gate" in kw:
            self.DRIFT_GATE = bool(kw.pop("drift_gate"))
        super().__init__(seed=seed, w_net=w_net, **kw)
        if decay not in ("split", "channel", "hard", "linear", "linkonly"):
            raise ValueError(
                f"未知 decay 模式: {decay}（可选 split / channel / hard / linear / linkonly）")
        self.decay = decay
        # 每个节点的 (链路排空时间, 观测时刻) 历史——只有漂移门控会用到
        self._hist: dict[int, list[tuple[float, float]]] = {}
        # 运行期诊断：实际施加的权重（落进 runs.csv，见 Strategy.stats_columns）
        self._acc_n = 0
        self._acc_dyn = 0.0
        self._acc_stat = 0.0
        self._acc_share = 0.0
        self._acc_gated = 0

    def _drift_risk(self, node_id: int, age_network: float) -> float:
        """按观测到的变化速率，推算"从上次看到现在"链路积压可能漂了多少（秒）。

        【为什么不用变异系数（CV）】第一版用的是"最近 K 次观测的 CV > 0.5 就不信"，
        实测直接失效：链路积压是**尖刺型**信号（全新鲜场景下 CV 中位 0.03、p90 2.23），
        任何固定 CV 阈值实际等价于"这条链路忙不忙"，于是**66% 的决策把网络项关掉**，
        策略退化成 LeastLoad（p95 从 14.5 s 变 71.8 s）。CV 对尖刺分布没有意义。
        换成与**时间尺度**挂钩的量：变化速率 × 观测年龄 = 可能的漂移量。
        新鲜时 age≈0 → 风险≈0 → 永远信任；只有"又旧又在动"才不信——这才符合直觉。
        """
        h = self._hist.get(node_id)
        if not h or age_network <= 0:
            return 0.0
        d2, t2 = h[-1]
        if len(h) < 2:
            return 0.0
        d1, t1 = h[-2]
        dt = max(t2 - t1, 1e-9)
        rate = abs(d2 - d1) / dt
        return rate * age_network

    def net_share(self, req: Request, obs: Observation) -> float:
        """本请求"网络时间占端到端时间"的比例（只用可观测量：字节数、算力、带宽）。"""
        mean_bw = sum(max(n.bandwidth, 1e-9) for n in obs.nodes) / max(len(obs.nodes), 1)
        t_xfer = (req.in_bytes + req.out_bytes) / max(mean_bw, 1e-9)
        t_svc = max(req.work, 0.0) / max(
            sum(n.compute_rate * n.slots for n in obs.nodes) / max(len(obs.nodes), 1),
            1e-9)
        total = t_xfer + t_svc
        return t_xfer / total if total > 0 else 0.0

    def weights(self, req: Request, obs: Observation) -> tuple[float, float]:
        """返回 `(动态部分权重, 结构部分权重)`——`choose` 用它们给网络项的两个分量加权。

        分成两块的理由见类 docstring：链路的**忙闲**是秒级易变的，链路的**粗细**是物理属性。
        陈旧的快照误导前者，对后者几乎无损。

        - `decay="split"`（主方法）：动态部分乘 `exp(−年龄_链路/TAU_N_DYN)`，
          结构部分**不衰减**；整块再乘队列通道的 `exp(−年龄_队列/TAU_Q)`。
        - `decay="hard"` / `"linear"`：两个分量同权，只用于消融对照。
        """
        w = self.w_net
        if w == 0.0:
            return (0.0, 0.0)
        share = self.net_share(req, obs)
        # 门：本请求几乎不经网络（机内档）→ 网络项没有意义
        if share < self.NET_SHARE_FLOOR:
            return self._tally(0.0, 0.0, share, gated=True)
        # 门（可选，默认关）：观测到的链路积压在剧烈变化时，快照本身误导。
        # 连续降权的主方法已用 dyn 因子覆盖了这件事，这一条留给消融验证。
        if self.DRIFT_GATE and any(
                self._drift_risk(n.node_id, obs.age_network) > self.VOL_DRIFT
                for n in obs.nodes):
            return self._tally(0.0, 0.0, share, gated=True)
        if self.decay == "hard":
            if obs.age_queue > self.TAU_Q_HARD or obs.age_network > self.TAU_N_HARD:
                return self._tally(0.0, 0.0, share, gated=True)
            return self._tally(w, w, share)
        if self.decay == "linear":
            f = (_decay_linear(obs.age_queue, self.TAU_Q_LIN)
                 * _decay_linear(obs.age_network, self.TAU_N_LIN))
            return self._tally(w * f, w * f, share, gated=(f <= 0.0))
        if self.decay == "channel":
            # 【修订版】按**通道**降权，两个分量同权：
            # 队列通道掉得快（§16：5 s 内从 −27% 掉到 −12%），链路通道几乎不掉
            # （§16：60 s 仍有 −24%），所以链路一侧只做轻微降权并留一个下限。
            # ⚠️ 已知缺陷（2026-10-23 校准模型实验发现）：exp(−age_q/τ_q) 把队列陈旧
            # 的惩罚施加到了网络项上，但队列陈旧影响的是 backlog 项的可靠性，不是网络项。
            # 在静态异构网络上这会把有用的链路信息丢掉（q5_n0 时网络项只剩 28%）。
            f = (_decay_exp(obs.age_queue, self.TAU_Q)
                 * max(self.LINK_FLOOR, _decay_exp(obs.age_network, self.TAU_N)))
            return self._tally(w * f, w * f, share, gated=(f <= 0.0))
        if self.decay == "linkonly":
            # 【修正版】只按链路状态年龄降权，不按队列年龄：
            # 队列陈旧影响 backlog 项（已在 obs 里变旧），链路陈旧才影响网络项可靠性。
            # 链路新鲜时保持满权重（不管队列多旧），链路陈旧时降权到下限。
            f = max(self.LINK_FLOOR, _decay_exp(obs.age_network, self.TAU_N))
            return self._tally(w * f, w * f, share, gated=(f <= 0.0))
        w_q = _decay_exp(obs.age_queue, self.TAU_Q)
        dyn = _decay_exp(obs.age_network, self.TAU_N_DYN)
        return self._tally(w * w_q * dyn, w * w_q, share)

    def _tally(self, w_dyn: float, w_stat: float, share: float,
               gated: bool = False) -> tuple[float, float]:
        """记录一次决策实际施加的权重（见 Strategy.stats_columns）。"""
        self._acc_n += 1
        self._acc_dyn += w_dyn
        self._acc_stat += w_stat
        self._acc_share += share
        self._acc_gated += int(gated)
        return (w_dyn, w_stat)

    def stats_columns(self) -> dict:
        n = max(self._acc_n, 1)
        return {
            "w_dyn_mean": round(self._acc_dyn / n, 6),
            "w_stat_mean": round(self._acc_stat / n, 6),
            "net_share_obs_mean": round(self._acc_share / n, 6),
            "w_gated_frac": round(self._acc_gated / n, 6),
        }

    def choose(self, req: Request, obs: Observation) -> int:
        bpc = obs.bytes_per_context_token
        bpg = obs.bytes_per_generated_token
        in_bytes, out_bytes = request_bytes(
            req.context_tokens, req.generated_tokens, bpc, bpg)
        req.in_bytes, req.out_bytes = in_bytes, out_bytes

        w_dyn, w_stat = self.weights(req, obs)
        scores: dict[int, float] = {}
        for n in obs.nodes:
            t_in, t_out = network_time(in_bytes, out_bytes, n)
            # 动态部分只管"这条链路现在多挤"，结构部分管"这条网线多粗 / 多快"。
            # 两个权重相等时（hard / linear 臂，或 dync=stat 的情形）本式与 NA2 完全同形。
            scores[n.node_id] = (n.backlog_time
                                 + w_dyn * n.link_drain_time
                                 + w_stat * (t_in + t_out))
            h = self._hist.setdefault(n.node_id, [])
            h.append((n.link_drain_time, obs.true_time))
            if len(h) > self.HIST_MAX:
                del h[:-self.HIST_MAX]

        chosen = min(scores, key=lambda k: (scores[k], k))
        self._log(req, obs, chosen, scores)
        return chosen

    def describe(self) -> str:
        drift = f",drift={self.VOL_DRIFT:g}" if self.DRIFT_GATE else ""
        head = (f"{self.name}(w={self.w_net:g},decay={self.decay}"
                f",floor={self.NET_SHARE_FLOOR:g}{drift}")
        if self.decay == "split":
            return f"{head},tau_q={self.TAU_Q:g}s,tau_n_dyn={self.TAU_N_DYN:g}s)"
        if self.decay == "linear":
            return f"{head},tau_q={self.TAU_Q_LIN:g}s,tau_n={self.TAU_N_LIN:g}s)"
        if self.decay == "linkonly":
            return f"{head},tau_n={self.TAU_N:g}s,floor={self.LINK_FLOOR:g})"
        return f"{head},tau_q={self.TAU_Q_HARD:g}s,tau_n={self.TAU_N_HARD:g}s)"


class NetworkAwareHardSwitchStrategy(NetworkAwareAdaptiveStrategy):
    """硬开关版自适应（`decay="hard"`），**只作方法消融的对照臂**。

    留着它的理由：论文要论证"连续降权"这个设计选择本身值多少钱。
    把硬开关版和软降权版放**同一个 cell 的同一个策略列表**里跑，比较就是逐请求配对的，
    不是跨 cell 的聚合对比（和回退机制的对照口径一致）。
    """

    name = "NetworkAware2AdaptHard"

    def __init__(self, seed: int = 0,
                 w_net: float = NetworkAwareAdaptiveStrategy.W_NET, **kw):
        super().__init__(seed=seed, w_net=w_net, decay="hard", **kw)


class P2CStrategy(Strategy):
    """Power-of-Two-Choices：随机抽两台，选积压小的那台。

    【为什么要补这条】P2C 是负载均衡里最经典的强基线，几乎所有 LLM 服务调度论文都会拿它当对照。
    之前只跟 `LeastQueue`/`LeastLoad`/`Random` 比，审稿人第一个问题就是"你的基线为什么这么弱、
    是不是只赢了最朴素的策略"。P2C 只需要**局部信息**（看两台而不是全部），
    因此它也是"少看一点状态能不能达到同样效果"的自然对照。

    注意：它用随机数，所以**种子对它有方差**（`LeastQueue`/`LeastLoad`/`NetworkAware*` 都是确定性的，
    多种子对它们不产生方差——见 AGENTS 门 3 的推论）。
    """

    name = "P2C"

    def choose(self, req: Request, obs: Observation) -> int:
        n = obs.nodes
        if len(n) <= 2:
            pick = list(n)
        else:
            pick = self.rng.sample(list(n), 2)
        best = min(pick, key=lambda v: (v.backlog_time, v.node_id))
        self._log(req, obs, best.node_id)
        return best.node_id


class P2CNetStrategy(NetworkAwareContentionStrategy):
    """P2C + 网络项：在随机抽出的两台里，按 v2 评分（积压 + 链路排空 + 传输）选。

    存在的意义：把"看全部节点"与"只看两台"的差别，和"看不看网络"的差别**解耦**。
    如果 P2CNet ≈ NetworkAware2，说明网络感知的价值不依赖于全局视野；
    如果差很多，说明"多看的那些节点"本身也值钱。
    """

    name = "P2CNet"

    def choose(self, req: Request, obs: Observation) -> int:
        bpc = obs.bytes_per_context_token
        bpg = obs.bytes_per_generated_token
        in_bytes, out_bytes = request_bytes(
            req.context_tokens, req.generated_tokens, bpc, bpg)
        req.in_bytes, req.out_bytes = in_bytes, out_bytes
        w = self.effective_w_net(obs)
        n = obs.nodes
        pick = list(n) if len(n) <= 2 else self.rng.sample(list(n), 2)
        scores = {}
        for v in pick:
            t_in, t_out = network_time(in_bytes, out_bytes, v)
            scores[v.node_id] = (v.backlog_time + w * (v.link_drain_time + t_in + t_out))
        chosen = min(scores, key=lambda k: (scores[k], k))
        self._log(req, obs, chosen, scores)
        return chosen


class CongestionOnlyStrategy(Strategy):
    """只看**链路拥塞**，不看带宽粗细：`score = backlog + w × link_drain`。

    【对应哪篇工作】Aegis（SIGCOMM 2026）的做法是把"机柜交换机前的排队字节数 + ECN 计数"
    打包成一个**拥塞压力**项加进打分。它用的是"现在堵不堵"，**不含**"这条网线本身多粗"。
    本策略就是那个机制的最小复现：只加链路排空时间，不加静态传输时间。

    【与 NetworkAware2 的关系】v2 = `backlog + link_drain + (t_in + t_out)`，
    也就是本策略**多了一项静态传输时间**。两者对比能回答：
    "带宽那一项到底有没有用，还是拥塞一项就够了"——这正是审稿人会问的消融。
    """

    name = "CongestionOnly"

    def __init__(self, seed: int = 0, w_net: float = 1.0):
        super().__init__(seed=seed)
        self.w_net = w_net

    def choose(self, req: Request, obs: Observation) -> int:
        scores = {v.node_id: v.backlog_time + self.w_net * v.link_drain_time
                  for v in obs.nodes}
        chosen = min(scores, key=lambda k: (scores[k], k))
        self._log(req, obs, chosen, scores)
        return chosen


class BottleneckStrategy(Strategy):
    """瓶颈最小化：`score = max(backlog, link_drain + t_in + t_out)`。

    【对应哪篇工作】Helix（ASPLOS 2025）在异构 GPU+异构网络上求最大流，本质是
    **不让任何一条资源成为瓶颈**。本策略是它的逐请求一阶近似：把"算力排队"与"网络时间"
    当两条串联资源，选**较大者最小**的那台，而不是像 v2 那样取**和**。

    【为什么这条对比重要】"求和"隐含了"两条资源可以互相补偿"（慢算力可以用快网线抵消）；
    "取大"隐含"瓶颈决定一切"。这两者的差别在异构集群里可能很大——实测会告诉我们哪种更对。
    """

    name = "Bottleneck"

    def choose(self, req: Request, obs: Observation) -> int:
        bpc = obs.bytes_per_context_token
        bpg = obs.bytes_per_generated_token
        in_bytes, out_bytes = request_bytes(
            req.context_tokens, req.generated_tokens, bpc, bpg)
        req.in_bytes, req.out_bytes = in_bytes, out_bytes
        scores = {}
        for v in obs.nodes:
            t_in, t_out = network_time(in_bytes, out_bytes, v)
            scores[v.node_id] = max(v.backlog_time, v.link_drain_time + t_in + t_out)
        chosen = min(scores, key=lambda k: (scores[k], k))
        self._log(req, obs, chosen, scores)
        return chosen


STRATEGIES: dict[str, type[Strategy]] = {
    RandomStrategy.name: RandomStrategy,
    LeastQueueStrategy.name: LeastQueueStrategy,
    LeastLoadStrategy.name: LeastLoadStrategy,
    NetworkAwareStrategy.name: NetworkAwareStrategy,
    NetworkAwareContentionStrategy.name: NetworkAwareContentionStrategy,
    NetworkAwareAdaptiveStrategy.name: NetworkAwareAdaptiveStrategy,
    NetworkAwareHardSwitchStrategy.name: NetworkAwareHardSwitchStrategy,
    P2CStrategy.name: P2CStrategy,
    P2CNetStrategy.name: P2CNetStrategy,
    CongestionOnlyStrategy.name: CongestionOnlyStrategy,
    BottleneckStrategy.name: BottleneckStrategy,
}
