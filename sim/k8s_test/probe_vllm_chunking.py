"""B0 追加探针：真 vLLM 服务下"一个输出片（chunk）到底是不是一个 token"的核对工具。

为什么单独写这个文件，而不是改 `measure_serving.py`：
    `measure_serving.py` 是**引擎无关的尺子**，它只从外面数"有多少个带内容的 SSE 片段"
    （`n_content_chunks`），并按"每个片段一个 token"折算逐字间隔（TPOT）。这个折算
    在假后端上一定成立（假后端就是按我们设定的片段数吐的），**在真服务上是一个未验证的假设**。
    要验证它，必须拿到服务端自报的 token 数——那是引擎相关的能力，不该塞进引擎无关的尺子里。
    所以新开一个探针，且**不改动任何已有文件**。

它做什么：
    对同一个 `（提示词, 采样参数）` 组合，同时取四个互相独立的"token 数"读数，再逐片对照：
      ① `n_chunks`            —— 收到的带 `choices` 的 SSE 片段总数（不看文本空不空）
      ② `n_chunks_with_text`  —— 其中**文本非空**的片段数（**这正是 measure_serving 数的那个量**）
      ③ `n_tokens_ids`        —— `return_token_ids=true` 时每片自报的 `token_ids` 长度之和
      ④ `n_usage_tokens`      —— `stream_options.include_usage=true` 时服务端自报的 completion_tokens
      ⑤ `n_tokens_tokenize`   —— 把生成文本送回服务端 `/tokenize`，用**同一个分词器**数出来的 token 数
    ①与②的差 = "被尺子静默丢掉的片段"；①与③的差 = "一片是否等于一个 token"。

为什么必须同时看②和③，而不是只看总数：
    一个多字节字符（中文、emoji）在分词后可能被切成**多个 token**，而 vLLM 的增量解码器
    要等字节凑齐才吐得出文字——中间那些片段的 `text` 是**空串**。于是
    `n_chunks_with_text < n_chunks == n_tokens`，而尺子只数前者。
    这是"一片是不是一个 token"这个问题在**命名层面**的后果：如果真出现这种情况，
    `n_content_chunks` 就不能叫"token 数"，`tpot_*` 也不能叫"逐 token 间隔"。

只依赖标准库（阶段 C 要能在云主机的系统 python3 上跑同一份脚本）。

用法（在 `sim/` 目录下执行；解释器可用 WSL 的 python3 或 `sim/.venv/Scripts/python.exe`）：
    python k8s_test/probe_vllm_chunking.py chunking --url http://127.0.0.1:8090
    python k8s_test/probe_vllm_chunking.py errors  --url http://127.0.0.1:8090
    python k8s_test/probe_vllm_chunking.py both    --url http://127.0.0.1:8090

产物（append-only：文件名带 UTC run_id，不覆盖；out-dir 相对仓库根下的 sim/ 解析）：
    results/b0_vllm/<run_id>_chunks.jsonl    每个收到的 SSE 片段的原文与对照读数
    results/b0_vllm/<run_id>_compare.csv     每个（提示词, 参数）组合的五路 token 数对照
    results/b0_vllm/<run_id>_errors.csv      每个错误场景的请求、HTTP 状态、原文、按尺子的分类
    results/b0_vllm/<run_id>_summary.json    结论汇总（含"片=token"是否成立的判定）
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Windows 控制台默认可能是 GBK，不重设会在中文输出上乱码（本仓库所有入口脚本都这么做）
# ---------------------------------------------------------------------------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent          # = sim/
DEFAULT_OUT_DIR = "results/b0_vllm"

# 与 measure_serving.py 保持一致的错误分类名。**不改这两个脚本的语义**：
# 这里的分类只是"按同一套名字给真实 vLLM 的响应贴标签"，用于回答"分类对不对"。
ERR_NONE = ""
ERR_TIMEOUT = "timeout"
ERR_CONNECT = "connect_error"
ERR_HTTP_5XX = "http_5xx"
ERR_HTTP_4XX = "http_4xx"
ERR_STREAM_BROKEN = "stream_broken"
ERR_STREAM_INCOMPLETE = "stream_incomplete"
ERR_BAD_JSON = "bad_json"
ERR_NO_CONTENT = "no_content"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _endpoint(url: str, path: str) -> tuple[str, int, str]:
    u = urlparse(url)
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if u.scheme == "https" else 80)
    base = u.path.rstrip("/")
    return host, port, base + path


# ---------------------------------------------------------------------------
# 一次流式请求：既拿读数，也把每个片段的原文原封不动带回来
# ---------------------------------------------------------------------------
def stream_once(host: str, port: int, path: str, body: dict, timeout_s: float
                ) -> dict:
    """发一次流式请求，返回逐片段明细。**任何异常都转成分类，不往上抛**。

    为什么要把每片的**原文**带回来：事后要核对"我数出来的片数"与"服务端自报的 token 数"
    是否一致，而一旦只留计数，任何不一致都无法定位（到底是漏收了一片、还是某片里有两个 token）。
    """
    out: dict = {
        "http_status": 0, "error_class": ERR_NONE, "error_detail": "",
        "chunks": [], "saw_done": 0, "saw_finish_reason": 0,
        "finish_reasons": [], "usage": None, "n_bytes": 0, "e2e_s": float("nan"),
        "t_first_content_s": float("nan"),
    }
    t0 = time.time()
    conn = None
    raw = json.dumps(body).encode()
    t_first_content = 0.0
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Accept", "text/event-stream")
        conn.putheader("Content-Length", str(len(raw)))
        conn.endheaders()
        conn.send(raw)
        resp = conn.getresponse()
        out["http_status"] = resp.status
        if resp.status >= 400:
            detail = resp.read(4000).decode("utf-8", "replace")
            out["error_class"] = (ERR_HTTP_5XX if resp.status >= 500 else ERR_HTTP_4XX)
            out["error_detail"] = f"HTTP {resp.status}: {detail[:2000]}"
            out["e2e_s"] = time.time() - t0
            return out
        idx = -1
        while True:
            line = resp.readline()
            if not line:
                break
            out["n_bytes"] += len(line)
            s = line.strip()
            if not s.startswith(b"data:"):
                continue          # SSE 的空行/注释行（vLLM 每片后面跟一个空行）
            payload = s[5:].strip()
            if payload == b"[DONE]":
                out["saw_done"] = 1
                break
            t_now = time.time()
            try:
                obj = json.loads(payload.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                out["error_class"] = out["error_class"] or ERR_BAD_JSON
                out["error_detail"] = f"{type(e).__name__}: {e}"
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("usage"):
                out["usage"] = obj["usage"]
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices:
                # 只有 usage 的收尾片：它是 SSE 片段，但**不是输出片段**，两回事，分开记。
                out["chunks"].append({
                    "kind": "usage_only", "raw": obj, "text": "",
                    "finish_reason": None, "token_ids": None,
                    "t_s": round(t_now - t0, 6), "logprob_tokens": None,
                })
                continue
            idx += 1
            c0 = choices[0] or {}
            text = c0.get("text")
            if not isinstance(text, str):
                d = c0.get("delta")
                text = (d or {}).get("content") if isinstance(d, dict) else ""
            if not isinstance(text, str):
                text = ""
            fr = c0.get("finish_reason")
            if fr:
                out["saw_finish_reason"] = 1
                out["finish_reasons"].append(fr)
            tid = c0.get("token_ids")
            lp = c0.get("logprobs")
            lp_tokens = lp.get("tokens") if isinstance(lp, dict) else None
            if text and t_first_content == 0.0:
                t_first_content = t_now
                out["t_first_content_s"] = round(t_now - t0, 6)
            out["chunks"].append({
                "kind": "choice", "index": idx, "raw": obj, "text": text,
                "finish_reason": fr,
                "token_ids": tid if isinstance(tid, list) else None,
                "logprob_tokens": lp_tokens if isinstance(lp_tokens, list) else None,
                "t_s": round(t_now - t0, 6),
            })
        try:
            resp.read()
        except Exception:
            pass
        if t_first_content == 0.0 and out["http_status"] == 200:
            out["error_class"] = out["error_class"] or ERR_NO_CONTENT
        out["e2e_s"] = time.time() - t0
    except socket.timeout:
        out["error_class"] = out["error_class"] or ERR_TIMEOUT
        out["error_detail"] = f"socket timeout after {timeout_s}s"
        out["e2e_s"] = time.time() - t0
    except ConnectionRefusedError as e:
        out["error_class"] = out["error_class"] or ERR_CONNECT
        out["error_detail"] = repr(e)
        out["e2e_s"] = time.time() - t0
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
        out["error_class"] = out["error_class"] or (
            ERR_STREAM_BROKEN if out["http_status"] else ERR_CONNECT)
        out["error_detail"] = repr(e)
        out["e2e_s"] = time.time() - t0
    except (http.client.HTTPException, http.client.IncompleteRead) as e:
        out["error_class"] = out["error_class"] or ERR_STREAM_BROKEN
        out["error_detail"] = repr(e)
        out["e2e_s"] = time.time() - t0
    except OSError as e:
        out["error_class"] = out["error_class"] or (
            ERR_STREAM_BROKEN if out["http_status"] else ERR_CONNECT)
        out["error_detail"] = repr(e)
        out["e2e_s"] = time.time() - t0
    except Exception as e:  # noqa: BLE001
        out["error_class"] = out["error_class"] or ERR_STREAM_BROKEN
        out["error_detail"] = f"{type(e).__name__}: {e}"
        out["e2e_s"] = time.time() - t0
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    # 读完了但两个语义终止信号一个都没有 ⇒ 按 measure_serving 的口径是"流不完整"
    if (out["http_status"] == 200 and out["chunks"]
            and not (out["saw_done"] or out["saw_finish_reason"])
            and not out["error_class"]):
        out["error_class"] = ERR_STREAM_INCOMPLETE
    return out


def post_json(host: str, port: int, path: str, body: dict, timeout_s: float = 30.0
              ) -> tuple[int, object]:
    """发一次非流式 JSON 请求，返回 (状态码, 解析后的 JSON 或原文串)。"""
    conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    try:
        raw = json.dumps(body).encode()
        conn.request("POST", path, body=raw,
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        data = r.read()
        try:
            return r.status, json.loads(data.decode("utf-8"))
        except Exception:
            return r.status, data.decode("utf-8", "replace")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 子命令 1：片 vs token
# ---------------------------------------------------------------------------
# 为什么这几个提示词/参数：它们分别冲着"一片可能不等于一个 token"的三种已知来源。
CASES: list[dict] = [
    # ① ASCII 贪心：最常见的稳态，预期 1 片 = 1 token
    {"label": "ascii_greedy_32", "prompt": "The capital of France is",
     "max_tokens": 32, "temperature": 0.0},
    # ② ASCII 贪心、长一点：看片数是否随 token 数线性增长（排除"只有首片特殊"）
    {"label": "ascii_greedy_128", "prompt": "Write a short paragraph about rain:",
     "max_tokens": 128, "temperature": 0.0},
    # ③ 中文：多字节字符，最容易出现"某片的 text 是空串"（增量解码等字节凑齐）
    {"label": "cjk_greedy_32", "prompt": "请用中文简单介绍一下长江。",
     "max_tokens": 32, "temperature": 0.0},
    # ④ emoji/符号：每个码点的字节数更极端
    {"label": "emoji_greedy_32", "prompt": "Repeat this exactly: 🚀🌏🔥✨ ",
     "max_tokens": 32, "temperature": 0.0},
    # ⑤ 非贪心采样：官方提醒"多 token 一片时逐 token 间隔会不一致"，
    #    所以必须验证结论对哪些采样参数成立，不能只测 temperature=0。
    {"label": "ascii_sampled_32", "prompt": "The capital of France is",
     "max_tokens": 32, "temperature": 1.0, "top_p": 0.95, "seed": 1234},
    {"label": "cjk_sampled_32", "prompt": "请用中文简单介绍一下长江。",
     "max_tokens": 32, "temperature": 1.0, "top_p": 0.95, "seed": 1234},
]


def _extra(mod: str) -> dict:
    """每个组合都要开的三样"取证开关"。

    `return_token_ids` 与 `logprobs` 是**两条互不依赖的逐片 token 证据**：
    只信一条的话，万一那条被实现忽略（不报错，只是字段不出现），结论会静默变空。
    `stream_options.include_usage` 给出服务端自报的 completion_tokens，是第三条。
    """
    return {
        "model": mod,
        "stream": True,
        "return_token_ids": True,
        "logprobs": 1,
        "stream_options": {"include_usage": True},
    }


def cmd_chunking(args: argparse.Namespace, out_dir: Path, run_id: str) -> dict:
    host, port, path = _endpoint(args.url, "/v1/completions")
    tok_host, tok_port, tok_path = _endpoint(args.url, "/tokenize")
    rows: list[dict] = []
    chunk_lines: list[dict] = []
    summary_cases: list[dict] = []

    for case in CASES:
        body = {"prompt": case["prompt"], "max_tokens": case["max_tokens"],
                "temperature": case.get("temperature", 0.0)}
        for k in ("top_p", "seed"):
            if k in case:
                body[k] = case[k]
        body.update(_extra(args.model))

        res = stream_once(host, port, path, body, 120.0)
        ch = res["chunks"]
        choice_chunks = [c for c in ch if c["kind"] == "choice"]
        usage_chunks = [c for c in ch if c["kind"] == "usage_only"]
        with_text = [c for c in choice_chunks if c["text"]]
        n_tok_ids = sum(len(c["token_ids"]) for c in choice_chunks
                        if isinstance(c["token_ids"], list))
        n_lp_toks = sum(len(c["logprob_tokens"]) for c in choice_chunks
                        if isinstance(c["logprob_tokens"], list))
        n_lp_null = sum(1 for c in choice_chunks if c["logprob_tokens"] is None)
        text = "".join(c["text"] for c in choice_chunks)
        usage = res["usage"] or (usage_chunks[-1]["raw"].get("usage") if usage_chunks else None)
        n_usage = (usage or {}).get("completion_tokens")

        # 服务端自己的分词器数一遍生成文本：这是"token 数"的第五条独立读数
        st_tok, tok_obj = post_json(tok_host, tok_port, tok_path,
                                    {"model": args.model, "prompt": text})
        n_tokenize = len(tok_obj.get("tokens", [])) if (
            st_tok == 200 and isinstance(tok_obj, dict)) else None

        # 逐片 token 数直方图：一片是不是一个 token，看这个就一目了然
        per_chunk_sizes = [len(c["token_ids"]) for c in choice_chunks
                           if isinstance(c["token_ids"], list)]
        hist: dict[str, int] = {}
        for s in per_chunk_sizes:
            hist[str(s)] = hist.get(str(s), 0) + 1

        for c in ch:
            chunk_lines.append({
                "run_id": run_id, "case": case["label"],
                "kind": c["kind"], "index": c.get("index"),
                "t_s": c["t_s"], "text": c.get("text", ""),
                "text_len": len(c.get("text", "") or ""),
                "finish_reason": c.get("finish_reason"),
                "n_token_ids": (len(c["token_ids"])
                                if isinstance(c.get("token_ids"), list) else None),
                "token_ids": c.get("token_ids"),
                "n_logprob_tokens": (len(c["logprob_tokens"])
                                     if isinstance(c.get("logprob_tokens"), list) else None),
                "raw": c["raw"],
            })

        row = {
            "run_id": run_id, "case": case["label"], "prompt": case["prompt"],
            "max_tokens": case["max_tokens"],
            "temperature": case.get("temperature", 0.0),
            "error_class": res["error_class"], "error_detail": res["error_detail"][:200],
            "http_status": res["http_status"],
            "n_chunks": len(choice_chunks),
            "n_chunks_with_text": len(with_text),
            "n_chunks_text_empty": len(choice_chunks) - len(with_text),
            "n_tokens_ids": n_tok_ids or None,
            "n_usage_tokens": n_usage,
            "n_tokenize_tokens": n_tokenize,
            "n_logprob_tokens": n_lp_toks or None,
            "n_chunks_without_logprobs": n_lp_null,
            "per_chunk_token_hist": json.dumps(hist, ensure_ascii=False),
            "saw_done": res["saw_done"],
            "saw_finish_reason": res["saw_finish_reason"],
            "finish_reasons": "|".join(res["finish_reasons"]),
            "text": text,
            "text_len": len(text),
            "ttft_s": res["t_first_content_s"], "e2e_s": round(res["e2e_s"], 6),
        }
        rows.append(row)
        summary_cases.append({k: v for k, v in row.items() if k not in ("text",)})
        print(f"[chunking] {case['label']:<20} chunks={len(choice_chunks):>4} "
              f"with_text={len(with_text):>4} empty={len(choice_chunks)-len(with_text):>3} "
              f"token_ids={n_tok_ids:>4} usage={n_usage} tokenize={n_tokenize} "
              f"hist={hist} err={res['error_class'] or '-'}")

    (out_dir / f"{run_id}_chunks.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in chunk_lines) + "\n",
        encoding="utf-8")
    _write_csv(out_dir / f"{run_id}_compare.csv", rows)
    return {"cases": summary_cases}


# ---------------------------------------------------------------------------
# 子命令 2：真实错误场景
# ---------------------------------------------------------------------------
def cmd_errors(args: argparse.Namespace, out_dir: Path, run_id: str) -> dict:
    host, port, path = _endpoint(args.url, "/v1/completions")
    rows: list[dict] = []
    base = {"model": args.model, "prompt": "hello", "max_tokens": 4,
            "temperature": 0.0}

    def scen(label: str, body: object, timeout_s: float = 30.0,
             raw_body: bytes | None = None, why: str = "") -> None:
        """跑一个错误场景。`raw_body` 用于构造连 JSON 都不合法的请求。"""
        if raw_body is not None:
            res = _raw_request(host, port, path, raw_body, timeout_s)
        else:
            res = stream_once(host, port, path, body, timeout_s)  # type: ignore[arg-type]
        rows.append({
            "run_id": run_id, "scenario": label, "why": why,
            "request": (raw_body.decode("utf-8", "replace") if raw_body is not None
                        else json.dumps(body, ensure_ascii=False))[:400],
            "http_status": res["http_status"],
            "error_class": res["error_class"],
            "error_detail": res["error_detail"][:400],
            "n_chunks": len([c for c in res["chunks"] if c.get("kind") == "choice"]),
            "saw_done": res["saw_done"], "saw_finish_reason": res["saw_finish_reason"],
            "e2e_s": res["e2e_s"],
        })
        print(f"[errors] {label:<28} status={res['http_status']:>3} "
              f"class={res['error_class'] or '-':<18} {(res['error_detail'] or '')[:90]}")

    scen("valid_baseline", dict(base, stream=True), why="正对照：这一条必须成功")
    scen("bad_json_body", None, raw_body=b"{not json at all",
         why="连 JSON 都不合法")
    scen("bad_type_max_tokens", dict(base, max_tokens="eight", stream=True),
         why="类型不对（字符串当整数）")
    scen("negative_max_tokens", dict(base, max_tokens=-1, stream=True),
         why="取值越界（负数）")
    scen("zero_max_tokens", dict(base, max_tokens=0, stream=True),
         why="取值边界（0）")
    scen("temperature_negative", dict(base, temperature=-1.0, stream=True),
         why="取值越界（负温度）")
    scen("n_negative", dict(base, n=-1, stream=True),
         why="取值越界（请求条数为负）")
    scen("unknown_model", dict(base, model="definitely-not-a-real-model",
                               stream=True),
         why="模型名不存在 ⇒ 看它回 400 还是 404")
    scen("prompt_too_long", dict(base, prompt="x " * 200000, max_tokens=8,
                                 stream=True),
         why="超出上下文长度 ⇒ 服务端应拒绝而不是截断")
    scen("unknown_field", dict(base, stream=True, not_a_real_field=1),
         why="多余字段 ⇒ 看服务端是忽略还是报错（决定尺子能不能打真服务）")
    scen("client_timeout_1ms", dict(base, max_tokens=64, stream=True),
         timeout_s=0.001,
         why="客户端超时设成 1 ms ⇒ 期望 socket timeout 分类")
    scen("missing_content_length", None,
         raw_body=b"", why="空请求体 ⇒ 期望 400 而不是崩")

    _write_csv(out_dir / f"{run_id}_errors.csv", rows)
    got = sorted({r["error_class"] or ERR_NONE for r in rows})
    return {"error_classes_seen": got, "scenarios": len(rows)}


def _raw_request(host: str, port: int, path: str, raw: bytes, timeout_s: float) -> dict:
    """发一个**刻意构造**的请求体（可以不是合法 JSON），只回状态码与响应原文。"""
    out: dict = {"http_status": 0, "error_class": ERR_NONE, "error_detail": "",
                 "chunks": [], "saw_done": 0, "saw_finish_reason": 0,
                 "e2e_s": float("nan")}
    t0 = time.time()
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(raw)))
        conn.endheaders()
        conn.send(raw)
        resp = conn.getresponse()
        out["http_status"] = resp.status
        detail = resp.read(4000).decode("utf-8", "replace")
        if resp.status >= 500:
            out["error_class"] = ERR_HTTP_5XX
        elif resp.status >= 400:
            out["error_class"] = ERR_HTTP_4XX
        out["error_detail"] = f"HTTP {resp.status}: {detail[:1500]}"
    except socket.timeout:
        out["error_class"] = ERR_TIMEOUT
        out["error_detail"] = f"socket timeout after {timeout_s}s"
    except ConnectionRefusedError as e:
        out["error_class"] = ERR_CONNECT
        out["error_detail"] = repr(e)
    except Exception as e:  # noqa: BLE001
        out["error_class"] = out["error_class"] or ERR_STREAM_BROKEN
        out["error_detail"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        out["e2e_s"] = time.time() - t0
    return out


# ---------------------------------------------------------------------------
def _write_csv(path: Path, rows: list[dict]) -> None:
    """按键的并集写 CSV（不同场景的列可能不一样，缺的留空）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def main() -> int:
    ap = argparse.ArgumentParser(
        description="真 vLLM 下的片/token 核对与错误场景探针（只用标准库）")
    ap.add_argument("role", choices=["chunking", "errors", "both"])
    ap.add_argument("--url", default="http://127.0.0.1:8090")
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct",
                    help="要写进请求体的模型名（vLLM 单模型服务下也能用它校验）")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"probe_{_now_utc()}"
    print(f"run_id={run_id}  url={args.url}  model={args.model}  out={out_dir}")

    summary: dict = {"run_id": run_id, "url": args.url, "model": args.model,
                     "utc": datetime.now(timezone.utc).isoformat()}
    if args.role in ("chunking", "both"):
        summary["chunking"] = cmd_chunking(args, out_dir, run_id)
    if args.role in ("errors", "both"):
        summary["errors"] = cmd_errors(args, out_dir, run_id)

    (out_dir / f"{run_id}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"产物 → {out_dir}/{run_id}_*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
