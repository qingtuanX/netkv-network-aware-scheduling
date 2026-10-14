# 说明：attempt3 里三个 `*_DRYRUN_EMPTY.log` 是怎么来的（不是实验数据）

## 是什么

`attempt3/` 下有**三个被改名**的空日志：

```
model_revision_DRYRUN_EMPTY.log
model_hash_A_DRYRUN_EMPTY.log
model_hash_B_DRYRUN_EMPTY.log
```

它们在 **2026-10-07T13:22:37–38Z** 由我的一次**干跑自检**

```bash
ATTEMPT=3 MODEL_WAIT_MIN=0 B1_DRY=1 bash sim/k8s_test/run_b1.sh model
```

生成。**干跑不执行任何远端命令**，所以文件里只有一行头：

```
# ---- model_hash_A @ A (8.130.72.109)  start=2026-10-07T13:22:38Z ----
```

**没有任何远端输出**。

## 为什么要改名（而不是留着原文件名）

`run_b1.sh` 用 `no_clobber` 保护产物：**文件名已存在就拒绝再写**。
干跑抢先占用了 `model_revision.log` / `model_hash_A.log` / `model_hash_B.log` 这三个**正式步骤会用到的名字**，
于是正式的 `model` 阶段跑到第 3 步时会被**拒写**，而这三个文件里又是空的 ——
会让人误以为"模型哈希算过了，结果是空的"。

**改名是处置方式**：内容一个字节没动，只是换成自解释的名字（`_DRYRUN_EMPTY`），
把正式文件名还回去。**没有删除任何东西。**

## 顺带影响（照实说）

因为这三个名字被占，正式的 `model` 阶段**没能记录**"官方 revision"与"两端目录哈希"。
- **官方 revision 已经补记**：见 `dependency_digests.txt` 末尾
  （`Qwen/Qwen2.5-7B-Instruct`，sha = `a09a35458c702b33eeacc393d103063234e8bc28`）。
- **两端目录哈希拿不到**：下载没完成、实例随后失联，所以"两端模型一致"这一条**未验证**。
