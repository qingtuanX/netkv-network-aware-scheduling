# `paper/refs.bib` 补字段台账

**口径**：只补核实到的字段；核不到就留空并写明原因——本项目的硬规矩是「编作者是学术不端，留空只是不好看」。

## 一、总览

- `refs.bib` 条目总数：**46**
- 本次补了字段的条目：**31**
- 一个字都没补的条目：**15**（其中 14 条本就是 arXiv 预印本 / 软件项目 / 数据集 / 结果网页，不存在页码与 DOI；`psp` 是**刻意不补**，理由见第三节）
- **仍缺 `pages` 的条目共 16 条** ＝ 上面 15 条 ＋ `burstgpt`（它只补了 `author`：`pages`/`doi` 属于 KDD'25 论文版，写进这个数据集条目会造成版本混用，理由见第二节该行）

## 二、逐条台账

「补了什么字段」一列只列**实际写进文件**的字段（已有同名字段时跳过）。

| bib key | 补了什么字段 | 填进去的值 | 出处 | 校验方式 | 可信度 |
|---|---|---|---|---|---|
| `distserve` | pages, publisher, address | `pages`={193--210}；`publisher`={USENIX Association}；`address`={Santa Clara, CA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin | USENIX 官方 BibTeX 逐字抄录；标题/作者/年份与本地 PDF 首页一致 | 高 |
| `splitwise` | pages, doi, publisher | `pages`={118--132}；`doi`={10.1109/ISCA59077.2024.00019}；`publisher`={IEEE} | Crossref（api.crossref.org） /works/10.1109/isca59077.2024.00019 | 三重校验：proceedings-article ✓ / ISCA 2024 ✓ / 年份 2024 ✓；作者与 bib 逐字一致。⚠️ 同题名还有 IEEE Micro 2025 期刊版（45(3):54-59），**不采用**（版本不同） | 高 |
| `mooncake` | pages, publisher, address | `pages`={155--170}；`publisher`={USENIX Association}；`address`={Santa Clara, CA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/fast25/presentation/qin | 同上。⚠️ FAST'25 的正式题名与 bib 现值不同，见台账「发现的问题」 | 高 |
| `vllm` | doi, publisher | `doi`={10.1145/3600006.3613165}；`publisher`={ACM} | Crossref（api.crossref.org） /works/10.1145/3600006.3613165 | 三重校验：类型 proceedings-article ✓ / 场馆 SOSP ✓ / 年份 2023 ✓；页码 611--626 与 bib 一致 | 高 |
| `sglang` | pages, doi, publisher | `pages`={62557--62583}；`doi`={10.52202/079017-2000}；`publisher`={Neural Information Processing Systems Foundation, Inc. (NeurIPS)} | Crossref（api.crossref.org） /works/10.52202/079017-2000 | 三重校验：proceedings-article ✓ / 场馆=Advances in NeurIPS 37 ✓ / 年份 2024 ✓；作者与 bib 逐字一致 | 高 |
| `sarathi` | pages, publisher, address | `pages`={117--134}；`publisher`={USENIX Association}；`address`={Santa Clara, CA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/osdi24/presentation/agrawal | 同上；OSDI 2024 官方页码 | 高 |
| `cachegen` | pages, doi, publisher | `pages`={38--56}；`doi`={10.1145/3651890.3672274}；`publisher`={ACM} | Crossref（api.crossref.org） /works/10.1145/3651890.3672274；本地 PDF 页脚亦印有该 DOI（`sim/data/processed/pdf_text/CacheGen-SIGCOMM2024.txt`） | 三重校验：proceedings-article ✓ / SIGCOMM 2024 ✓ / 年份 2024 ✓；页码与 Zotero 库互为独立佐证。⚠️ **不采用** arXiv 预印本 DOI 10.48550/arXiv.2310.07240（同一条目的不同版本） | 高 |
| `llumnix` | pages, publisher, address | `pages`={173--191}；`publisher`={USENIX Association}；`address`={Santa Clara, CA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/osdi24/presentation/sun-biao | 同上 | 高 |
| `cassini` | pages, publisher, address | `pages`={1403--1420}；`publisher`={USENIX Association}；`address`={Santa Clara, CA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/nsdi24/presentation/rajasekaran | 同上；作者列表与本地 PDF 首页一致 | 高 |
| `vclos` | pages, doi, publisher | `pages`={111285}；`doi`={10.1016/j.comnet.2025.111285}；`publisher`={Elsevier} | Crossref（api.crossref.org） /works/10.1016/j.comnet.2025.111285 | 三重校验：journal-article ✓ / Computer Networks ✓ / 年份 2025 ✓；volume 268 与 bib 一致。⚠️ 111285 是 Elsevier 的**文章编号**（非连续页码）；作者列表见台账「发现的问题」第 3 条 | 高 |
| `helix` | doi, publisher | `doi`={10.1145/3669940.3707215}；`publisher`={ACM} | Crossref（api.crossref.org） /works/10.1145/3669940.3707215 | 三重校验：proceedings-article ✓ / ASPLOS 2025 ✓ / 年份 2025 ✓；页码 586--602 与 bib 一致 | 高 |
| `alpaserve` | pages, publisher, address | `pages`={663--679}；`publisher`={USENIX Association}；`address`={Boston, MA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/osdi23/presentation/li-zhouhan | 同上；作者列表与本地 PDF 首页一致 | 高 |
| `serverlessllm` | pages, publisher, address | `pages`={135--153}；`publisher`={USENIX Association}；`address`={Santa Clara, CA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/osdi24/presentation/fu | 同上 | 高 |
| `topoopt` | publisher, address | `publisher`={USENIX Association}；`address`={Boston, MA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/nsdi23/presentation/wang-weiyang | 官方 BibTeX 的页码 739--767 与 bib 现值一致，故只补出版者/出版地 | 高 |
| `orca` | publisher, address | `publisher`={USENIX Association}；`address`={Carlsbad, CA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/osdi22/presentation/yu | 官方 BibTeX 的页码 521--538 与 bib 现值一致，故只补出版者/出版地 | 高 |
| `cloudabstractions` | doi, publisher | `doi`={10.1145/3725783.3764395}；`publisher`={ACM} | Crossref（api.crossref.org） /works/10.1145/3725783.3764395 | 三重校验：proceedings-article ✓ / APSys 2025 ✓ / 年份 2025 ✓；页码 98--105 与 bib 一致 | 高 |
| `aegis` | pages, doi, publisher | `pages`={1062--1074}；`doi`={10.1145/3789240.3829167}；`publisher`={ACM} | Crossref（api.crossref.org） /works/10.1145/3789240.3829167 | 三重校验：proceedings-article ✓ / SIGCOMM 2026 ✓ / 年份 2026 ✓；作者 Rui Li, Shuang Cao 与 bib 一致。（本条正是 `文献/08-竞争定位` §8.9 列为「DOI 待补」的那一条） | 高 |
| `gensched` | pages, publisher | `pages`={1--6}；`publisher`={IEEE} | Crossref（api.crossref.org） /works/10.1109/infocom59046.2026.11571651 | 三重校验：proceedings-article ✓ / INFOCOM 2026 ✓ / 年份 2026 ✓；DOI 与 bib 现值完全一致（同一条记录） | 高 |
| `jitserve` | author, pages, publisher, address, note(改写) | `author`={Wei Zhang and Zhiyu Wu and Yi Mu and Rui Ning and Banruo Liu and Nikhil Sarda and Myungjin Lee and Fan Lai}；`pages`={825--848}；`publisher`={USENIX Association}；`address`={Renton, WA} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/nsdi26/presentation/zhang-wei；arXiv:2504.20068（作者交叉核对） | NSDI 2026 官方 BibTeX；作者与 arXiv:2504.20068 完全一致，双向互证 | 高 |
| `staleloadold` | pages, publisher | `pages`={285--296}；`publisher`={IEEE} | Crossref（api.crossref.org） /works/10.1109/icdcs.1999.776530 | 三重校验：proceedings-article ✓ / 19th IEEE ICDCS ✓ / DOI 中的 .1999. 对上年份 1999 ✓（Crossref 的 issued 字段为空，故用 DOI 字符串佐证年份） | 高 |
| `delayedopen` | publisher | `publisher`={INFORMS} | Crossref（api.crossref.org） /works/10.1287/stsy.2019.0045 | 三重校验：journal-article ✓ / Stochastic Systems ✓ / 年份 2019 ✓；DOI 与 bib 现值一致、页码 305--306 一致。作者全名 David Lipshutz（bib 现作 D. Lipshutz，缩写无误） | 高 |
| `stalelinkqos` | pages, volume, doi, publisher | `pages`={162--176}；`volume`={9}；`doi`={10.1109/90.917073}；`publisher`={IEEE} | Crossref（api.crossref.org） /works/10.1109/90.917073 | 三重校验：journal-article ✓ / IEEE-ACM ToN ✓ / 年份 2001 ✓；作者 A. Shaikh, J. Rexford, K.G. Shin 与 bib 逐字一致（**题名只是缩写**：正式题名是 “…on Quality-of-Service Routing”） | 高 |
| `parsync` | author, pages, publisher | `author`={Yihui Feng and Zhi Liu and Yunjian Zhao and Tatiana Jin and Yidi Wu and Yang Zhang and James Cheng and Chao Li and Tao Guan}；`pages`={81--97}；`publisher`={USENIX Association} | USENIX 官方 BibTeX（论文页内嵌）：https://www.usenix.org/conference/atc21/presentation/feng-yihui | ★ 见台账「发现的问题」第 1 条：bib 题名是**系统名**而非论文名；真实论文是 ATC'21 《Scaling Large Production Clusters with Partitioned Synchronization》，**作者/页码取自该论文的官方 BibTeX**（题名本任务不许改） | 中 |
| `freshint` | author, pages, doi, publisher | `author`={Haeun Kim and Heewon Kim and Chanbin Bae and Sangheon Pack}；`pages`={1--6}；`doi`={10.1109/INFOCOM59046.2026.11571376}；`publisher`={IEEE} | Crossref（api.crossref.org） /works/10.1109/infocom59046.2026.11571376 | 标题相似度 1.00；三重校验：proceedings-article ✓ / 场馆=IEEE INFOCOM 2026 ✓ / 年份 2026 ✓ | 高 |
| `aoisurvey` | author, pages, volume, doi, publisher | `author`={Roy D. Yates and Yin Sun and D. Richard Brown and Sanjit K. Kaul and Eytan Modiano and Sennur Ulukus}；`pages`={1183--1210}；`volume`={39}；`doi`={10.1109/JSAC.2021.3065072}；`publisher`={IEEE} | Crossref（api.crossref.org） /works/10.1109/JSAC.2021.3065072 | 三重校验：journal-article ✓ / IEEE JSAC ✓ / 年份 2021 ✓；期号 39(5)，卷 39 与页 1183-1210 经 OpenAlex 独立复核一致 | 高 |
| `timelyremote` | publisher | `publisher`={IEEE} | Crossref（api.crossref.org） /works/10.1109/tnet.2024.3408673 | 三重校验：journal-article ✓ / IEEE-ACM ToN ✓ / 年份 2024 ✓；DOI 与 bib 现值一致、页码 3824--3839 一致 | 高 |
| `fattree` | publisher | `publisher`={ACM} | Crossref（api.crossref.org） /works/10.1145/1402958.1402967 | 三重校验：proceedings-article ✓ / SIGCOMM 2008 ✓ / 年份 2008 ✓；DOI 与 bib 现值一致、页码 63--74 一致 | 高 |
| `dcqcn` | doi, publisher | `doi`={10.1145/2785956.2787484}；`publisher`={ACM} | Crossref（api.crossref.org） /works/10.1145/2785956.2787484；本地 PDF 页脚亦印有该 DOI | 三重校验：proceedings-article ✓ / SIGCOMM 2015 ✓ / 年份 2015 ✓；页码 523--536 与 bib 一致 | 高 |
| `dctcp` | doi, publisher | `doi`={10.1145/1851182.1851192}；`publisher`={ACM} | Crossref（api.crossref.org） /works/10.1145/1851182.1851192 | 三重校验：proceedings-article ✓ / SIGCOMM 2010 ✓ / 年份 2010 ✓；页码 63--74 与 bib 一致 | 高 |
| `hpcc` | pages, doi, publisher | `pages`={44--58}；`doi`={10.1145/3341302.3342085}；`publisher`={ACM} | Crossref（api.crossref.org） /works/10.1145/3341302.3342085；本地 PDF 页脚亦印有该 DOI | 三重校验：proceedings-article ✓ / SIGCOMM 2019 ✓ / 年份 2019 ✓；页码与 Zotero 库一致 | 高 |
| `burstgpt` | author | `author`={Yuxin Wang and Yuhan Chen and Zeyu Li and Xueze Kang and Yuchu Fang and Yeju Zhou and Yang Zheng and Zhenheng Tang and Xin He and Rui Guo and Xin Wang and Qiang Wang and Amelie Chi Zhou and Xiaowen Chu} | arXiv:2401.17644（出口 arXiv 官方 API）+ Crossref（api.crossref.org） /works/10.1145/3711896.3737413 | 两条独立来源的 14 人作者列表**逐字一致**（arXiv API 与 Crossref KDD'25 记录）。该 bib 条目是 `@misc`（数据集/GitHub 仓库），故**不补 pages/doi**——论文版是 KDD 2025, pp. 5831--5841, DOI 10.1145/3711896.3737413，若改用 @inproceedings 可补 | 高 |

## 三、**没有补上**的条目与原因（重点，不藏）

| bib key | 留空字段 | 为什么留空 |
|---|---|---|
| `dynamo` | pages/doi | NVIDIA 开源项目（GitHub 仓库），**不是论文**，不存在页码或 DOI。已注明 howpublished |
| `llmd` | pages/doi | CNCF Sandbox 软件项目（llm-d.ai），非论文；同上 |
| `flowkv` | pages/doi | 仅 arXiv:2504.03775 预印本，无正式场馆 → 无页码、无 Crossref DOI。⚠️ 其作者列表与 arXiv 不符，见台账「发现的问题」第 5 条 |
| `netkv` | pages/doi | 仅 arXiv:2606.03910 预印本（Crossref/OpenAlex 均无正式场馆记录） |
| `gorgo` | pages/doi | 仅 arXiv:2602.11688 预印本 |
| `psp` | pages/doi | ★ **刻意不补**：该条目是 `@misc` + `eprint`（arXiv:2608.08425 **预印本**）形态，而正式版是 IEEE LCN 2026, pp. 1--12, DOI `10.1109/LCN67947.2026.11660801`（已用 Crossref 三重校验核实）。把正式版的页码/DOI 写进预印本条目＝**同一条目的两个版本混用**，本项目明令禁止（见任务书第 3 条），故留空。作者 Jiaqi Liu, Chunyang Zhang, Heng Pan, Yanbiao Li 与 bib 现值逐字一致。**若作者把该条改成 `@inproceedings`+`booktitle={Proc. IEEE LCN}`，即可直接补上这三个字段** |
| `staleguard` | pages/doi | 仅 arXiv:2609.29522 预印本（正文自称投 IEEE，尚无场馆） |
| `churnbench` | pages/doi | 仅 arXiv:2609.11515 预印本 |
| `controlplane` | pages/doi | 仅 arXiv:2609.23130 预印本（自称 research agenda） |
| `elastickv` | pages/doi | 仅 arXiv:2608.23658 预印本 |
| `dlfp` | pages/doi | 仅 arXiv:2609.38386 预印本 |
| `howfar` | pages/doi | 仅 arXiv:2605.28302 预印本 |
| `poa` | pages/doi | 仅 arXiv:2606.17081 预印本 |
| `mlperf` | pages/doi | 引的是 MLCommons 的**结果网页**（v5.0 datacenter），不是论文；无页码/DOI |
| `azuretrace` | pages/doi | 引的是 Microsoft 的**数据集仓库**（GitHub），非论文；无页码/DOI |

统计：这些条目的 `pages` 全部留空，因为**预印本 / 软件项目 / 数据集 / 网页本身没有页码**；`doi` 也留空，因为 arXiv 预印本的 DOI（`10.48550/arXiv.*`）与正式发表版本不是同一条记录，混用会制造出实际不存在的出处（本项目已有明确的版本混用禁令）。

## 四、唯一一处**改写既有字段**（以及为什么只改这一处）

任务书要求「只填确实查到的字段，不动其它内容」。本次只破例一处，原因是**这个 note 是被本次改动自己弄成错话的**：

| bib key | 字段 | 原文 | 改后 |
|---|---|---|---|
| `jitserve` | `note` | `Proc. USENIX NSDI (in press); author list not verified in this repository` | `Proc. USENIX NSDI; author list, pages and publisher verified against the USENIX official BibTeX` |

原 note 的两句话在改动后**都是错的**：① 作者列表已从 USENIX 官方 BibTeX 与 arXiv:2504.20068 双向核实并写入 `author`；② NSDI 2026 已开过会、有正式页码 825--848，不再是 `in press`。若不改，同一条目内会出现「note 说没核实作者」与「author 字段已填」直接矛盾。**其余所有既有内容（含别的 `note`）一律未动。**

## 五、发现的问题（**不在本任务授权范围内，未改动，交作者决定**）

补字段过程中做交叉核对时，发现 `refs.bib` 里若干条的**既有内容与权威记录不符**。任务书要求「只填确实查到的字段，不动其它内容」，所以**一处都没有改**，只在此登记。
第 2--6 条是**姓名与实际不符**（不是「留空」，是「写错」），建议投稿前逐条订正。

### 1. `parsync`：题名是**系统名**，不是论文名

USENIX ATC 2021 的真实论文是 **《Scaling Large Production Clusters with Partitioned Synchronization》**（Yihui Feng 等，pp. 81--97）。该论文正文里把这个设计**叫作** `partitioned synchronization (ParSync)`——原文：*"…staleness-aware state sharing design, called partitioned synchronization (ParSync)"*。

也就是说 `ParSync: Parallel Synchronization for Scheduling Simplicity and Scalability` 这个题名**在任何库里都不存在**（arXiv 题名检索 0 命中、Crossref `query.title=ParSync` 0 命中、OpenAlex `title.search:ParSync` 0 命中、USENIX ATC'21 议程页无此文）。

**本次处置**：作者/页码/出版者按**那篇真实论文**的官方 BibTeX 填了（引用意图明确：本仓库 `文献/08-竞争定位（2025-2026）.md` 第 220 行把它描述为「唯一把 staleness 当设计变量的顶会工作」，与该论文内容吻合）。**题名没有改**（任务书要求不动其它内容）——建议作者把题名改为正式题名。

### 2. `llumnix` 的作者姓名有误

| | 姓名 |
|---|---|
| bib 现值 | `Zhendong Huang`、`Hailong Zhao`、`Wei Xiao`、`Xicheng Zhang`、`Wenxiao Lin` |
| 权威记录 | `Ziming Huang`、`Hanyu Zhao`、`Wencong Xiao`、`Xinyi Zhang`、`Wei Lin` |

出处：本仓库已下载的原始 PDF `文献/03-LLM推理服务/Llumnix-OSDI2024.pdf` 第 1 页（抽好的文本见 `sim/data/processed/pdf_text/Llumnix-OSDI2024.txt`），以及 OpenAlex `https://doi.org/10.48550/arxiv.2406.03243`。首作者 `Biao Sun` 两处一致。

### 3. `vclos` 的作者姓名有误（13 个里 9 个姓氏以外的名字不符）

权威记录（Crossref `/works/10.1016/j.comnet.2025.111285`，Computer Networks 268:111285）：`Xinchi Han; Shizhen Zhao; Yongxi Lv; Peirui Cao; Weihao Jiang; Qinwei Yang; Yunzhuo Liu; Shengkai Lin; Bo Jiang; Ximeng Liu; Yong Cui; Chenghu Zhou; Xinbing Wang`。
bib 现值：`Xin Han; Shihan Zhao; Yong Lv; Peng Cao; Wenjie Jiang; Qi Yang; Youhui Liu; Sen Lin; Bing Jiang; Xin Liu; Yongqiang Cui; Chuan Zhou; Xiaowen Wang`。
**人数相同、姓氏大致对得上，但名字几乎全部不同**——像是被改写过的版本。

### 4. `mooncake` 的作者姓名与**题名**都有问题

- 作者：bib 的 `Haoze Tang / Shuo Cai / Yongwei Zhang / Yuxin Wu / Xin Xu`，权威记录（ACM TOS 22(1):1--38, DOI 10.1145/3773772）为 `Heyi Tang / Shangming Cai / Yineng Zhang / Yongwei Wu / Xinran Xu`（其余 8 人一致）。
- 题名：bib 用的是 **ACM TOS 期刊版/arXiv 版**的题名（*A KVCache-centric Disaggregated Architecture for LLM Serving*），但 `booktitle` 写的是 **USENIX FAST 2025**；FAST'25 的正式题名是 *Mooncake: Trading More Storage for Less Computation — A KVCache-centric Architecture for Serving LLM Chatbot*。**这是同一条目的两个版本被混在一起**（本项目明令禁止）。本次补的页码 155--170 是 **FAST'25 的**页码。

### 5. `flowkv` 的作者姓名有误

bib：`Wei Li; Guoyu Jiang; Xin Ding; Zhen Tao; Chenyu Hao; Chao Xu; Yuqi Zhang; Hao Wang`；arXiv:2504.03775（arXiv 官方 API 直查）：`Weiqing Li; Guochao Jiang; Xiangyong Ding; Zhangcheng Tao; Chuzhan Hao; Chenfeng Xu; Yuewei Zhang; Hao Wang`。姓与人数一致，名字全部不同。

### 6. `helix` 的作者姓名有误（3 个名字）

bib：`Yixiao Mei … Yifan Zhuang … Jinyang Yang`；Crossref `/works/10.1145/3669940.3707215`（ASPLOS'25）：`Yixuan Mei; Yonghao Zhuang; Xupeng Miao; Juncheng Yang; Zhihao Jia; Rashmi Vinayak`。姓氏一致，名字不同（Yixiao/Yixuan、Yifan/Yonghao、Jinyang/Juncheng）。

### 7. `serverlessllm` 的题名是 arXiv 版而非 OSDI'24 版

bib：*ServerlessLLM: Locality-Enhanced Serverless Inference for Large Language Models*（这是 arXiv:2401.14351 的题名）；OSDI'24 正式题名（USENIX 官方 BibTeX）是 *ServerlessLLM: Low-Latency Serverless Inference for Large Language Models*。页码 135--153 取自 OSDI'24。

### 8. `howfar` 的作者列表被截断（不算错，仅登记）

bib 列了 4 位，arXiv:2605.28302 共 12 位；前 4 位与实际**逐字一致**，属有意截断。

## 六、方法论与可复现路径

**怎么做的**（不依赖任何一次性脚本，按下面三步任何人都能重做一遍）：

1. **从本地 PDF 取第一手信息**。`文献/` 下 70 篇 PDF 的作者/场馆/年份/DOI 与首页一致；已抽好的文本缓存在 `sim/data/processed/pdf_text/*.txt`（可 `Grep` 复核），抽取脚本是 `sim/extract_pdf_text.py`。
2. **USENIX 官方 BibTeX（USENIX 论文页码与出版地的权威来源）**：USENIX 每篇论文页内嵌官方 BibTeX，含 `author / booktitle / year / pages / address / publisher`。入口是「会议议程页 → 论文页」，例如 `https://www.usenix.org/conference/<conf>/technical-sessions` → `https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin`。**USENIX 不为论文注册 DOI**，所以 Crossref/OpenAlex 都查不到它们——本任务涉及的 **11 篇 USENIX 论文**（OSDI'22/23/24、NSDI'23/24/26、FAST'25、ATC'21）页码全部来自这里。
3. **Crossref（ACM / IEEE / Elsevier / NeurIPS 的页码、DOI、出版者）**：`https://api.crossref.org/works/{doi}` 与 `https://api.crossref.org/works?query.bibliographic=...`；OpenAlex（`https://api.openalex.org/works?search=...`）作为**独立第二来源**复核卷期页；arXiv 官方 API（`https://export.arxiv.org/api/query?id_list=...`）复核预印本作者。

**本机不可达 / 不可用的通道**（如实记录，**未绕行**）：

| 通道 | 实测结果 |
|---|---|
| DBLP（`dblp.org/search/publ/api`） | 被 Anubis 反爬挑战页拦截，返回 `Making sure you're not a bot!`，取不到 JSON |
| Semantic Scholar API | 全天 429 限流 |
| Google / Bing / DuckDuckGo | 本机不通（无可用搜索入口） |
| ACM DL / IEEE Xplore 主页 | 403 / 202 空壳页；**未尝试任何绕过** |

**校验口径**：沿用 `paper/backfill_dois.py` 的三重一致性校验——① 类型必须是会议论文或期刊论文；② 必须有场馆名；③ 条目年份与记录年份相差 ≤1。本任务**没有放宽**该口径。另加两条本项目自己的硬规矩：

- **同一条目的不同版本不许混**：`cachegen` 用的是 SIGCOMM 2024 会议版 DOI，**没有**用 Zotero 库里的 arXiv 版 DOI（`10.48550/arXiv.2310.07240`）；`splitwise` 用的是 ISCA 2024，**没有**用同题名的 IEEE Micro 2025 期刊版。同理 `psp` 因为是预印本形态而**刻意不补**正式版页码（见第三节）。
- **台账覆盖全部 46 条**：每一条要么在第二节（补了），要么在第三节（写明为何没补）——本次生成台账的临时脚本里有一条断言（`F ∪ NOFILL == refs.bib 的全部 key`），漏掉任何一条都会直接报错，不会静默略过；该脚本是一次性的、已删除，**本台账就是留存的记录**。

**产物自检**：改动前后各跑一次 `sim/.venv/Scripts/python.exe paper/check_tex.py`，两次都通过（引用键 46/46、交叉引用、环境配对、图片存在、括号计数）；另用「按行比较」证明本次改动**只增加行**——去掉行尾逗号后比对，原文件里只有 1 行消失（就是第四节那条被改写的 `note`），其余全部是新增。
