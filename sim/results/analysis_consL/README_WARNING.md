# 警告：本报告只代表 BurstGPT 一批，不要引用

本目录的 report.txt 由 analyze.py 对 results/matrix_consL 生成。
但 matrix_consL 里同时放着合成、Azure、BurstGPT 三批产物，而 analyze.py 每个 mode
只取**最新 run_id**（= 最后跑完的 BurstGPT）。所以：

- 本报告的数字 **只代表 BurstGPT**，不是三批结论；
- 三批的正式报告在：
  - ../analysis_consL_synthetic/report.txt
  - ../analysis_consL_azure/report.txt
  - ../analysis_consL_burstgpt/report.txt
- 写论文/交接时请引用上面三份，不要引用本目录。

（2026-10-20 发现并记录，见 sim/CONS_BATTERY_CLEANUP_2026-10-20.md §6。）
