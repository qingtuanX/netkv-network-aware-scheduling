"""负载来源：真实 trace 加载 + 合成负载生成。

设计原则：
  1. 真实 trace 和合成负载必须能被明确区分，绝不允许混为一谈；
  2. 加载器必须能处理"只下载了文件前 N MB"的截断 CSV；
  3. 同一份负载在四种策略之间必须完全一致（公平性前提）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

AZURE_COLUMNS = ["TIMESTAMP", "ContextTokens", "GeneratedTokens"]


@dataclass
class WorkloadStats:
    """一份负载的体检结果。inspect_trace.py 的第一手证据。"""

    source: str
    n_requests: int
    duration_s: float
    qps_mean: float
    qps_p95: float
    context_p50: float
    context_p95: float
    context_max: int
    generated_p50: float
    generated_p95: float
    generated_max: int
    n_dropped: int          # 因为 token 非法 / 行损坏被丢弃
    n_truncated_tail: int   # 疑似被截断的最后一行
    timestamp_monotonic: bool

    def report(self) -> str:
        lines = [
            f"来源: {self.source}",
            f"请求数: {self.n_requests:,}   覆盖时长: {self.duration_s/3600:.3f} 小时",
            f"到达率: 均值 {self.qps_mean:.2f} QPS, p95 秒级窗口 {self.qps_p95:.2f} QPS",
            f"ContextTokens:  p50={self.context_p50:.0f}  p95={self.context_p95:.0f}  max={self.context_max:,}",
            f"GeneratedTokens: p50={self.generated_p50:.0f}  p95={self.generated_p95:.0f}  max={self.generated_max:,}",
            f"丢弃非法行: {self.n_dropped}   疑似截断尾行: {self.n_truncated_tail}",
            f"时间戳单调递增: {self.timestamp_monotonic}",
        ]
        return "\n".join(lines)


def _summarize(df: pd.DataFrame, source: str, n_dropped: int,
               n_truncated: int, monotonic: bool) -> WorkloadStats:
    sec = df["arrival"].to_numpy()
    duration = float(sec[-1] - sec[0]) if len(sec) > 1 else 0.0

    # 用滑动窗口统计瞬时到达率，避免均值掩盖突发。
    # 窗口宽度自适应，防止超长 trace 生成天量分箱。
    if duration > 0:
        bin_w = max(1.0, duration / 20_000.0)
        bins = np.arange(0.0, duration + bin_w, bin_w)
        counts, _ = np.histogram(sec - sec[0], bins=bins)
        # 折算成每秒到达率，使不同窗口宽度可比
        counts = counts / bin_w
        qps_mean = float(counts.mean()) if len(counts) else float(len(sec))
        qps_p95 = float(np.percentile(counts, 95)) if len(counts) else float(len(sec))
    else:
        qps_mean = qps_p95 = float(len(sec))

    return WorkloadStats(
        source=source,
        n_requests=len(df),
        duration_s=duration,
        qps_mean=qps_mean,
        qps_p95=qps_p95,
        context_p50=float(df["context_tokens"].median()),
        context_p95=float(df["context_tokens"].quantile(0.95)),
        context_max=int(df["context_tokens"].max()),
        generated_p50=float(df["generated_tokens"].median()),
        generated_p95=float(df["generated_tokens"].quantile(0.95)),
        generated_max=int(df["generated_tokens"].max()),
        n_dropped=n_dropped,
        n_truncated_tail=n_truncated,
        timestamp_monotonic=monotonic,
    )


def load_azure_trace(
    path: str | Path,
    max_requests: int | None = None,
    duration_s: float | None = None,
    rate_scale: float = 1.0,
) -> tuple[list, WorkloadStats]:
    """读取 Azure LLM Inference Trace。

    参数
    ----
    max_requests : 只取前 N 个请求（调试和提速用）
    duration_s   : 只取前 N 秒内的请求
    rate_scale   : 到达率缩放。>1 表示把时间轴压缩、提高负载压力。
                   做法是把相邻到达间隔除以 rate_scale。
                   【这是一个实验杠杆，不是数据原貌，必须在论文中声明。】

    返回 (requests, stats)。requests 是 (arrival, context_tokens, generated_tokens) 列表。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 trace 文件: {path}\n"
            "请先下载（只需前 50 MB，不必下完整 660 MB）:\n"
            "  curl -L -C - --retry 3 -A \"Mozilla/5.0\" -r 0-51800516 \\\n"
            "    -o sim/data/raw/azure_code_first50MB.csv \\\n"
            "    https://github.com/Azure/AzurePublicDataset/releases/download/dataset-llm-2024/AzureLLMInferenceTrace_code_1week.csv"
        )

    # 截断下载会导致最后一行不完整；用 python 引擎跳过坏行，再单独统计。
    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    expected_fields = len(AZURE_COLUMNS)
    n_truncated = 0
    if raw_lines and raw_lines[-1].count(",") != expected_fields - 1:
        n_truncated = 1
        raw_lines = raw_lines[:-1]

    from io import StringIO
    df = pd.read_csv(
        StringIO("\n".join(raw_lines)),
        engine="python",
        on_bad_lines="skip",
    )

    missing = [c for c in AZURE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"trace 缺少字段 {missing}，实际列: {list(df.columns)}"
        )

    n_before = len(df)
    df = df[AZURE_COLUMNS].copy()
    df = df.dropna()
    df["ContextTokens"] = pd.to_numeric(df["ContextTokens"], errors="coerce")
    df["GeneratedTokens"] = pd.to_numeric(df["GeneratedTokens"], errors="coerce")
    df = df.dropna()
    # token 非正的行无法产生有效工作量，丢弃并计数
    df = df[(df["ContextTokens"] > 0) & (df["GeneratedTokens"] > 0)]

    # 2024 版带 +00:00，2023 版不带；统一按 UTC 解析后转相对秒
    ts = pd.to_datetime(df["TIMESTAMP"], utc=True, format="mixed")
    monotonic = bool(ts.is_monotonic_increasing)
    if not monotonic:
        ts = ts.sort_values()
        df = df.loc[ts.index]
    arrival = (ts - ts.iloc[0]).dt.total_seconds().to_numpy()

    df = pd.DataFrame({
        "arrival": arrival,
        "context_tokens": df["ContextTokens"].to_numpy(dtype=np.int64),
        "generated_tokens": df["GeneratedTokens"].to_numpy(dtype=np.int64),
    })
    n_dropped = n_before - len(df)

    if duration_s is not None:
        df = df[df["arrival"] <= duration_s]
    if max_requests is not None:
        df = df.iloc[:max_requests]
    df = df.reset_index(drop=True)

    if rate_scale != 1.0:
        if rate_scale <= 0:
            raise ValueError("rate_scale 必须为正")
        df["arrival"] = df["arrival"] / rate_scale

    stats = _summarize(df, str(path), n_dropped, n_truncated, monotonic)
    requests = list(df.itertuples(index=False, name=None))
    return requests, stats


def generate_synthetic(
    n_requests: int = 20_000,
    base_qps: float = 5.0,
    burstiness: float = 2.0,
    context_mu: float = 7.0,
    context_sigma: float = 1.4,
    context_max: int = 32_768,
    generated_mu: float = 4.5,
    generated_sigma: float = 1.3,
    generated_max: int = 2_048,
    seed: int = 12345,
) -> tuple[list, WorkloadStats]:
    """合成负载 —— 明确标记为 synthetic，仅用于打通代码链路。

    【严禁】把 synthetic 的结果当作 Azure trace 的结论写进论文。
    这个函数存在的唯一目的：在真实 trace 下载完成前，让整条管线可运行、可测试。
    """
    rng = np.random.default_rng(seed)

    # 到达过程：非齐次泊松，用慢变正弦调制速率制造可观测的突发。
    # 速率用乘性调制 lam = base * exp(burstiness*0.5*sin(...))，
    # 保证恒为正且有界（burstiness=2 时速率在 base 的 0.37x–2.72x 之间）。
    t = 0.0
    arrivals = []
    period = max(1.0, n_requests / 8.0)
    for i in range(n_requests):
        phase = np.sin(2 * np.pi * i / period)
        lam = base_qps * float(np.exp(burstiness * 0.5 * phase))
        t += rng.exponential(1.0 / lam)
        arrivals.append(t)

    context = np.clip(rng.lognormal(context_mu, context_sigma, n_requests), 1, context_max)
    generated = np.clip(rng.lognormal(generated_mu, generated_sigma, n_requests), 1, generated_max)

    df = pd.DataFrame({
        "arrival": arrivals,
        "context_tokens": context.astype(np.int64),
        "generated_tokens": generated.astype(np.int64),
    })
    stats = _summarize(df, "SYNTHETIC (非真实数据，仅用于打通管线)", 0, 0, True)
    requests = list(df.itertuples(index=False, name=None))
    return requests, stats


BURSTGPT_COLUMNS = ["Timestamp", "Model", "Request tokens", "Response tokens",
                    "Total tokens", "Log Type"]


def load_burstgpt(path: str | Path,
                  max_requests: int | None = None,
                  duration_s: float | None = None,
                  model_filter: str | None = None,
                  ) -> tuple[list, WorkloadStats]:
    """读取 BurstGPT（HPMLL，CC BY 4.0）。第二条真实 trace，用来检验结论是否只对一条 trace 成立。

    与 Azure trace 的差别都是**实测确认过的**，不能想当然：

    * 字段名不同：`Timestamp` / `Request tokens` / `Response tokens`（而不是 `TIMESTAMP` / `ContextTokens`）。
      本仓库拿到的 `BurstGPT_1.csv` 里**没有** README 提到的 `Session ID` / `Elapsed time`，
      所以不建模会话结构（论文里若要谈 prefix/会话局部性必须换文件）。
    * `Timestamp` 的单位是**秒**（整数、1 秒分辨率：中位间隔 1，54% 的行与相邻行同刻）。
      判定依据：按秒解读时 24 小时折叠曲线有清晰昼夜形态（白天/夜间 ≈ 2.7）；
      按毫秒解读则整段只覆盖约 88 分钟，不存在日周期。
    * 负载形状与 Azure 相反：输出更长（`Response tokens` p50≈34，Azure 是 8），
      输入更短（`Request tokens` p50≈251，Azure 是 1930）。两条 trace 的结论一致才有说服力。

    `model_filter` 可只取某个模型（数据里是 `ChatGPT` 与 `GPT-4`）；默认全部保留。
    【真实 trace，不是合成】来源/许可/字段见 文献/07-数据集与trace/README.md。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 BurstGPT 文件: {path}\n"
            "下载（需反代，GitHub 直连不通）:\n"
            "  curl -L --retry 5 -A \"Mozilla/5.0\" -o sim/data/raw/burstgpt_1.csv \\\n"
            "    \"https://gh-proxy.com/https://raw.githubusercontent.com/HPMLL/BurstGPT/main/data/BurstGPT_1.csv\""
        )

    raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    expected_fields = len(BURSTGPT_COLUMNS)
    n_truncated = 0
    if raw_lines and raw_lines[-1].count(",") != expected_fields - 1:
        n_truncated = 1
        raw_lines = raw_lines[:-1]

    from io import StringIO
    df = pd.read_csv(StringIO("\n".join(raw_lines)), engine="python",
                     on_bad_lines="skip")

    missing = [c for c in BURSTGPT_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"BurstGPT 缺少字段 {missing}，实际列: {list(df.columns)}")

    n_before = len(df)
    df = df[BURSTGPT_COLUMNS].copy()
    if model_filter is not None:
        df = df[df["Model"] == model_filter]
    for col in ("Timestamp", "Request tokens", "Response tokens"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna()
    # token 非正的行无法产生有效工作量；实测有 25443 行（约 1.8%）
    df = df[(df["Request tokens"] > 0) & (df["Response tokens"] > 0)]

    ts = df["Timestamp"].to_numpy(dtype=float)
    monotonic = bool(np.all(np.diff(ts) >= 0))
    if not monotonic:
        df = df.sort_values("Timestamp")
        ts = df["Timestamp"].to_numpy(dtype=float)
    arrival = ts - ts[0]

    df = pd.DataFrame({
        "arrival": arrival,
        "context_tokens": df["Request tokens"].to_numpy(dtype=np.int64),
        "generated_tokens": df["Response tokens"].to_numpy(dtype=np.int64),
    })
    n_dropped = n_before - len(df)

    if duration_s is not None:
        df = df[df["arrival"] <= duration_s]
    if max_requests is not None:
        df = df.iloc[:max_requests]
    df = df.reset_index(drop=True)

    labels = f"{path.name}"
    if model_filter is not None:
        labels += f"（仅 {model_filter}）"
    stats = _summarize(df, f"BurstGPT {labels}", n_dropped, n_truncated, monotonic)
    return list(df.itertuples(index=False, name=None)), stats
