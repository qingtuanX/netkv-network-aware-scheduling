# paper/ —— 论文 LaTeX 工程

**目标档次**：CCF-B / SCI 二区期刊（测量 / 实证类论文，不是"又一个调度器"）。

## 两个版本（按目标期刊选一个编译）

| 目标期刊 | 用哪个入口 | 文献样式 | 当前产物 |
|---|---|---|---|
| **IEEE 系**（TPDS、ToN、TSC 等） | `main.tex` + `bash build.sh` | `IEEEtran.bst` | `main.pdf`，**14 页**双栏 |
| **Elsevier 系**（Computer Networks、JSA、FGCS、JPDC 等） | `main_elsevier.tex` + `bash build_elsevier.sh` | `elsarticle-num` | `main_elsevier.pdf`，**37 页**单栏 preproof |

两个版本的**正文节文件完全共用**（`sections/01..10`）与同一份 `refs.bib`，
差别只在三处：文档类、标题/作者/摘要写法、参考文献样式。**改内容只需改一次。**

> 投稿前请到目标期刊主页的 Author Guidelines 确认官方模板；若期刊提供自己的
> `.cls`/`.bst`，把它们放进本目录、改 `\documentclass` 与 `\bibliographystyle` 即可，正文不动。

## 怎么编译

```bash
cd paper
bash build.sh            # IEEE 版：pdflatex → bibtex → pdflatex ×2
bash build_elsevier.sh   # Elsevier 版
```

**为什么必须三轮**：第 1 轮生成 `.aux`，bibtex 据此产出 `.bbl`，第 2–3 轮才把
参考文献与交叉引用编号回填。只跑一轮会看到 `?` 和缺失的参考文献——最常见的
"编译成功但看着不对"。

本机已装 MiKTeX 25.12（`C:\Users\qiafe\AppData\Local\Programs\MiKTeX\miktex\bin\x64`，
已开启按需自动安装宏包）。换机器只需装任一 TeX 发行版；也可直接打包上传 Overleaf，无外部依赖。

## 目录

| 文件 | 作用 |
|---|---|
| `main.tex` | 标题 / 摘要 / 关键词 / `\input` 各节 / 参考文献样式 |
| `sections/01..10*.tex` | 正文十节（引言、相关工作、模型与方法、实验设置、RQ1、RQ2、条件降权、真机、局限、结论） |
| `refs.bib` | 参考文献（47 条，正文引用 47 条，一一对应） |
| `figures/` | 8 张论文图（`fig_*.pdf` 矢量 + `.png` 预览），全部**英文标签** |
| `build.sh` | 一键编译（三轮 + 打印未定义引用与 overfull 统计） |
| `check_tex.py` | **静态自检**：引用键、交叉引用、环境配对、图片是否存在、括号计数 |
| `verify_refs.py` | 用 Crossref 逐条核实文献的作者/标题/年份，报告核实不了的条目 |

## 两条必须守住的纪律

**1. 数字只能来自产物，不能手打。** 正文里每个数字都出自
`sim/results/paper_numbers.txt`（由 `sim/analysis/export_paper_numbers.py` 从三批分析产物自动导出，
620+ 行，按论文小节编号排列）。流程：

```
results/matrix_*          原始实验数据（append-only 证据）
  → analysis/analyze.py   → results/analysis_*/（表格 + report.txt）
  → export_paper_numbers.py → results/paper_numbers.txt（正文数字的唯一来源）
  → paper/sections/*.tex
```

改数字的正确顺序：**先改产物 → 再重跑导出 → 最后改正文**。直接改正文数字会让
"claim ↔ data" 的链条断掉，且没人能快速发现。

**2. 改完 tex 必须跑自检。**

```bash
sim/.venv/Scripts/python.exe paper/check_tex.py
```
它会抓出"引用了 bib 里没有的键""`\ref` 指向不存在的标签""图不存在""环境没配对"这类
只有编译器才知道的低级错误。编译一次（`bash build.sh`）也是必须的。

## 参考文献现状（投稿前必须补齐）

**已核实**（标题 / 场馆 / 年份 / 作者）：大部分条目。**已回填 DOI 的 5 条**
（Crossref 通过"文献类型 + 场馆名 + 年份"三重校验）：
`vllm`（SOSP'23）、`fattree`（SIGCOMM'08）、`timelyremote`（ToN'24）、
`delayedopen`（Stochastic Systems'19）、`staleloadold`（**ICDCS'99**）。

> ⚠️ **已修正的一处错**：`staleloadold` 原写成 TPDS 2000，Crossref 显示它其实是
> **ICDCS 1999**。已改正——这类错误只有查了才知道。

**作者字段现状（2026-10-12 复核）**：**46/46 条全部有真实作者**——此前"11 条留空"
的 `gensched`/`jitserve`/`psp`/`gorgo`/`staleguard`/`churnbench`/`controlplane`/
`elastickv`/`dlfp`/`howfar`/`poa` 均已补齐；`netkv` 的"未核实"备注已更正为
**经本地 PDF 首页核实**（单作者 Mubarak Adetunji Ojewale 属实）。
（`verify_refs.py` 会对 OSDI/NSDI/arXiv 类条目报"[?] 标题差异大"——那是 Crossref
收录不全的误配，如 Orca 配到生物学、Cassini 配到探测器，**非真缺陷**。）

**其余缺失项**：部分条目缺页码；会议论文缺出版地与出版者（IEEE/ACM 常要求）；
预印本目前是 `@misc` + `eprint`，两家都能接受，但部分期刊更希望
`@article` + `journal = {arXiv preprint arXiv:...}`。

> **不要用 `backfill_dois.py` 的"只看标题"模式回填**。实测过：标题相似度 1.00 的三条
> （`mooncake`/`orca`/`libra`）分别指向 Springer 图书章节、Bloomsbury 图书和
> AccessScience 百科条目——**同名不等于同一篇**。脚本正因此加了
> 「类型必须是期刊/会议论文 + 必须有场馆名 + 年份相差 ≤1」的三重校验；
> 其中 `libra` 的标题无法核实，已**整条删除**（而不是留着可能错的引用）。

## 投稿前必须人工处理的事（**不要漏**）

1. ~~补齐文献作者~~ ✅ 2026-10-12 复核：46/46 全部有真实作者。**残余**：部分条目缺
   页码/出版地/出版者（格式性，投稿前按目标期刊要求过一遍）。
2. **`Aegis` 的 DOI 未二次核对**（作者与场馆来自 SIGCOMM 2026 官方 program 页，
   DOI 未打开 ACM DL 核）；`gensched` 的 DOI 来自检索记录，未打开正文。
   → 若期刊要求 DOI 全核，投稿前逐条打开一次。
3. **署名**：**作者裁定（2026-10-12）暂用占位符**（`[Author Name]`、`[Affiliation]`、
   `[City], [Country]`）。待作者提供：姓名（英文、含顺序）、单位、通讯作者、
   （可选）ORCID/基金/数据与代码可用性声明/AI 使用披露（按目标期刊政策）。
   文件里已注释给出**双盲评审**的匿名写法。
   ⚠️ 注意 `\IEEEauthorblockA{...\\{}[...]}` 里的 `\\{}` **不能删**：`\\` 后直接跟 `[`
   会被 LaTeX 当成长度参数而报 `Missing number`。
4. **通讯作者标记 / 基金号 / 作者简介**（多数期刊终稿要求）。
5. **确认目标期刊模板**（当前按 Computer Networks / Elsevier 方向准备），
   必要时替换 `.cls`/`.bst`（正文不用动）。


## 选刊决定（用数据做的，不是凭印象）

数据取自开源项目 `hitfyd/ShowJCR` 随仓库发布的**原始表**（已下载到 `文献/期刊分区数据/`）：
中科院分区表升级版 2025 全量（`FQBJCR2025-UTF8.csv`）、
**国际期刊预警名单 2020/2021/2023/2024/2025 五版**（`GJQKYJMD20xx.csv`）、
CCF 推荐目录 2026（`CCF2026-UTF8.csv`）、JCR 2024 影响因子（`JCR2024-UTF8.csv`）。

复算命令：

```bash
sim/.venv/Scripts/python.exe paper/check_venue.py                      # 内置候选清单
sim/.venv/Scripts/python.exe paper/check_venue.py "Computer Networks"  # 查指定刊
```

| 期刊 | 预警 | CCF | 中科院大类 | 是否 Top | IF(2024) |
|---|---|---|---|---|---|
| **IEEE Trans. Cloud Computing** | 未列入(5 版) | **B** | **2 区** | 否 | 5.0 |
| Computer Networks | 未列入 | B | 3 区 | 否 | 4.6 |
| Cluster Computing | 未列入 | B | 3 区 | 否 | 4.1 |
| Journal of Systems Architecture | 未列入 | B | 3 区 | 否 | 4.1 |
| J. Parallel and Distributed Computing | 未列入 | B | 3 区 | 否 | 4.0 |
| Performance Evaluation | 未列入 | B | 4 区 | 否 | 0.8 |
| **IEEE TPDS** | 未列入 | **A** | 2 区 | **是** | 6.0 |
| IEEE Trans. Services Computing | 未列入 | A | 2 区 | 是 | 5.8 |
| IEEE Trans. Network and Service Mgmt | 未列入 | C | 2 区 | 否 | 5.4 |
| Future Generation Computer Systems | 未列入 | C | 2 区 | 否 | 6.1 |

**结论**：同时满足「CCF-B + 中科院 2 区 + 不在任何一版预警名单」的**只有 IEEE TCC 一本**。
`Computer Networks` 是 CCF-B 但中科院 3 区；`FGCS`/`TNSM` 是中科院 2 区但 CCF-C。

**⚠️ 三条限制（必须在投稿前自行确认）**：
1. 预警名单校方可能**另加条目**，最终以**你学校科研处的当年文件**为准；
2. 中科院分区用的是 **2025 版**，2026 版可能调整；
3. CCF 用的是 **2026 版**目录。

## 已知的内容缺口（写在正文里，不是 bug）

- 第二版降权规则只测了 $\rho=0.6$ 一个负载强度档（正文已声明为最大覆盖缺口）。
- `$v_2$` 的强度扫描没做；现有扫描用的是较弱的 `$v_1$` 打分。
- "何时该打折"的机制未解释成功（突发性、持续性两个假设都被自身数据否掉），正文列为开放问题。
- 真机是**单机** WSL2 容器 + 应用层限速，只能支持方向，不支持量级（正文 §VIII 有量化）。
