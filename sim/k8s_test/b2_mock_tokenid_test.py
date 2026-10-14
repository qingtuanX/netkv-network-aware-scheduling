# -*- coding: utf-8 -*-
"""token ID 缺失拦截测试（作者裁定 2026-10-11 的验证）。

假服务故意不回 logprobs.tokens（只回 text），验证：
1) 严格模式（默认）：第一次出现就停该格、aborted_strict=true、退出码 2、
   n_token_source_fallback/n_token_source_invalid 计数正确、n_tokens 不被块数冒充；
2) 非严格模式（调试用）：继续跑完，但同样标记 invalid、退出码 1。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

sys.path.insert(0, r"E:\代码项目文件\论文\sim\k8s_test")
import b2_perf_client  # noqa: E402

TMP = Path(r"C:\Users\qiafe\AppData\Local\Temp\opencode")
PROMPTS = TMP / "mock_prompts.json"
PROMPTS.write_text(json.dumps([{"id": "m1", "prompt": "x"}]), encoding="utf-8")


class H(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        ln = int(self.headers.get("Content-Length", 0))
        self.rfile.read(ln)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for _ in range(6):
            self.wfile.write(b'data: {"choices":[{"text":"x"}]}\n\n')
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 18999), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

print("=== 测试 1：严格模式（应停该格、退出码 2）===")
rc1 = b2_perf_client.main([
    "--url", "http://127.0.0.1:18999", "--arm", "mock_strict",
    "--prompts-file", str(PROMPTS), "--max-tokens", "6",
    "--reps", "3", "--concurrency", "1",
    "--out", str(TMP / "mock_strict.json"),
])
out1 = json.loads((TMP / "mock_strict.json").read_text(encoding="utf-8"))
s1 = out1["summary"]
print(f"exit_code={rc1} n_requests={s1['n_requests']} "
      f"fallback={s1['n_token_source_fallback']} invalid={s1['n_token_source_invalid']} "
      f"aborted={out1['aborted_strict']} n_tokens={out1['requests'][0]['n_tokens']}")
assert rc1 == 2, "严格模式应返回 2"
assert out1["aborted_strict"] is True
assert s1["n_token_source_fallback"] == 1 and s1["n_token_source_invalid"] == 1
assert s1["n_requests"] == 1, "严格模式应停止提交后续请求"
assert out1["requests"][0]["n_tokens"] == 0, "不得用块数冒充 token 数"
assert out1["requests"][0]["avg_token_gap_s"] is None

print("=== 测试 2：非严格模式（继续跑完、退出码 1、同样标记无效）===")
rc2 = b2_perf_client.main([
    "--url", "http://127.0.0.1:18999", "--arm", "mock_nstrict",
    "--prompts-file", str(PROMPTS), "--max-tokens", "6",
    "--reps", "3", "--concurrency", "1", "--no-strict-token-ids",
    "--out", str(TMP / "mock_nstrict.json"),
])
out2 = json.loads((TMP / "mock_nstrict.json").read_text(encoding="utf-8"))
s2 = out2["summary"]
print(f"exit_code={rc2} n_requests={s2['n_requests']} "
      f"fallback={s2['n_token_source_fallback']} invalid={s2['n_token_source_invalid']} "
      f"aborted={out2['aborted_strict']}")
assert rc2 == 1
assert out2["aborted_strict"] is False
assert s2["n_requests"] == 3 and s2["n_token_source_invalid"] == 3

print("=== 拦截测试全部通过 ===")
