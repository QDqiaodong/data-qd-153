# Exact bounded fault-tree probability v1

设备可靠性数据人员核验独立基本故障事件及AND/OR门DAG。基本事件最多16个唯一ID、精确有理概率0..1；门最多100个，输入非空唯一且引用已知事件/门，整图无环。root可为事件或门。
枚举全部2^n基本事件状态，按互相独立概率乘积加权。重复引用同一事件必须共享状态，不能错误地将门输出视独立后相乘。输出顶事件故障概率、可靠度、枚举状态数及逻辑故障状态数；后者包括概率0的状态，不代表观测故障数。
样例top=(A AND B) OR (A AND C)，P=0.1*(0.2+0.3-0.2*0.3)=11/250，可靠度239/250。无共因故障、时间模型、维修、相关事件拟合或安全认证；不是生存曲线估计。

# Measured downtime history v2

运维人员按实测状态发布可更正的日停机数据。`fault-history`（或 `python -m fault_tree.history`）
提供 init/apply/query 三个子命令；旧 `fault-tree` 概率枚举入口与语义不变，概率字段在实测时轴上不参与求值。

## 命令与退出码

```sh
fault-history init  --tree TREE --horizon H --dataset DIR   # 建立数据集，输出 {"generation": 0}
fault-history apply --dataset DIR --source BATCH            # 结算一批修订，输出 {"batch_id", "generation"}
fault-history query --dataset DIR --day D --output FILE     # 固定已提交代次读取一日，写 FILE
```

成功退出0；非法输入、非法数据状态或操作失败退出2，并保留已提交代次与既有输出。
apply 成功仅输出该回执；init 输出 `{"generation": 0}`；query 的报告写入 FILE 并同时打印到 stdout。

## init 输入契约

- TREE 为 v1 原格式故障树（events/gates/root），沿用全部节点、概率与无环校验；概率合法保留但不参与实测求值。
  树结构在数据集内不再变化：数据集保存原始字节 SHA256（tree_sha256）与字节级自足副本 tree.json。
- H 为 1..7776000 的整数秒。0 为数据集时间原点，每天固定 86400 秒，共 ceil(H/86400) 天，
  第 D 日覆盖半开区间 [D*86400, min((D+1)*86400, H))，末日可不足一天；不涉及时区/夏令时。
- DIR 必须不存在。init 在同级暂存目录构建完整数据集（含提交指针）后一次目录重命名到位；
  失败或被终止不会留下可被 query 当作已初始化的 DIR。

## BATCH 输入契约

- 严格 `{"batch_id", "records"}`，records 为 1..5000 项；输入沿用 8MB 限制、拒绝重复 JSON 键、
  未知字段与布尔冒充整数。各 ID 沿用 ident 规则（非空、去首尾空白、<=128 字符、无控制字符）。
- put 项严格 `{"sample_id","revision","op","event_id","time","failed"}` 且 op="put"；
  withdraw 项严格 `{"sample_id","revision","op"}` 且 op="withdraw"。
- revision 为 1..10^9 整数，允许跳号与首次即 withdraw；event_id 须为已声明基本事件而非门；
  time 为 0<=time<H 的整数（time=H 不接受）；failed 只能为 JSON 布尔。
- 同批 (sample_id, revision) 不得重复；同一样本可携带多个不同 revision；输入排列不影响结算。

## 事实结算契约

- 按 sample_id 持久保留每个成功接收 revision 的规范内容（put 的 event_id/time/failed 或 withdraw），
  即使已被更高版盖过；当前状态取最高 revision，withdraw 是保留身份的墓碑。
- 同版异内容无论新旧均拒绝整批；旧版不能覆盖新版；最高版 withdraw 后重送旧 put 不能恢复；
  未见的低版可登记但不改变最高版；已见同版同内容可再次出现。
- 批次先整体结算，再检查有效观测冲突；任何拒绝都不登记版本记忆或 batch 身份。
- batch_id 的规范身份由所有字段决定，仅忽略对象键顺序与 records 行顺序（逐字段规范化后 SHA256）。
  同 batch 同规范内容返回首次保存的 {batch_id, generation}，即使代次已前移；同 batch 异内容拒绝。
- 每个首次成功的 batch 恰推进 generation 一次（即使只补录不生效的旧版）；拒绝或重放不推进。
- 有效观测 = 每一样本最高版 put。最终活动集合中同事件同秒出现相反 failed 即拒绝整批
  （即使该事件不在 root 祖先中）；不同样本在同事件同秒提供相同 failed 合法，不构成多次跳转。
- 未被 root 引用的基本事件观测仍可接收与修订，但不改变停机结果。

## 时轴求值与物化契约

- 每个已声明基本事件在 0 时默认健康；有效 put 的 failed 从 time 持续到该事件下一有效观测，
  最后一条持续至 H；time=0 的观测立即参与 [0,...)；首条观测前健康，无观测全程健康。
- 不同事件在同秒的全部变化先一起生效，再对共享基本事件布尔向量求 root（AND/OR 语义，
  不用概率乘积、不枚举概率世界），因此不产生瞬时或零长故障；root 可为事件或门。
- 全局停机区间为 root 持续为真的最大半开区间 [start,end)，按 start、end 升序，非空且在 [0,H) 内；
  在 H 仍故障的区间结束于 H。root 连续为真即同属一个区间，即使内部见证事件切换；相接区间不拆开。
- 区间身份即完整 [start,end] 二元组；边界改变则身份改变，不维护跨更正的永久故障 ID。
- 每日片段为全局区间与该日的非空交集：`{"incident":[全局start,全局end],"start","end"}`；
  跨日不增加全局故障数。日 downtime_seconds 为片段长度之和；全局 incident_count 为最大区间数、
  downtime_seconds 为其总长度；各日时长之和等于全局时长。每一天（含空结果）都有独立分区。

## 存储布局（公开契约）

```
DIR/meta.json                   {"schema_version":2,"kind":"meta","tree_sha256","root",
                                 "horizon","num_days","events":[...],"gates":{id:{"op","inputs"}},"order":[...]}
DIR/tree.json                   init 树文件的字节级副本（sha256 == tree_sha256）
DIR/current                     提交指针 {"generation": N}，唯一承诺代次来源
DIR/generations/<N>/facts.json  {"schema_version":2,"kind":"facts","generation":N,"tree_sha256",
                                 "samples":{sid:{rev:内容}},"batches":{bid:{"generation","fingerprint"}}}
DIR/generations/<N>/summary.json {"schema_version":2,"kind":"summary","generation":N,"tree_sha256",
                                 "incident_count","downtime_seconds","incidents":[[start,end],...]}
DIR/generations/<N>/days/<D>.json {"schema_version":2,"kind":"day","generation":N,"tree_sha256",
                                 "index","start","end","downtime_seconds","segments":[...]}
```

facts.json 可恢复全部版本判等记忆、当前最高版与 batch 接收身份；汇总与各日分区记录所属
generation 与树 SHA。数据集自足，init 树文件与 BATCH 文件删除后仍可重开查询。

## 提交协议与并发读取

- apply 每批重算全时间轴，在 generations/<N+1>/ 内逐个完整写入 facts、summary 与全部分区
  （每文件先写临时文件再原子替换），全部就绪后才原子替换 current 指针（临时文件 + rename）。
  指针翻转是该批次唯一的提交点：只有新事实与该代全部物化成果一起可见，批次才算接收成功。
- 读者只读一次 current 指针，随后的事实、汇总与日分区全部取自该代目录；已发布代次目录不可变、
  从不复用或回收，因此任何查询只能观察完整旧代或完整新代，跨文件引用（generation 与树 SHA）
  在读取时逐一校验。不能靠文件名最大代次、未发布目录或暂存文件推断批次已提交。
- 写进程在任意持久化写入或替换之后被终止：重开仍只能查询完整旧代或完整新代；重放原 batch
  由 facts 中的接收身份识别，返回首次回执，不重复推进代次。未发布残留目录在下一次同号发布前清除，
  允许保留全部历史代次；不支持断电恢复，不承诺多写者并发；一个写进程可与多个查询进程交错。
- 测试钩子：设置环境变量 FAULT_HISTORY_BARRIER_DIR 后，写进程在每个持久化边界
  （init.pre_rename、publish.begin/facts/summary/days/pre_commit/post_commit）写 <label>.ready
  并等待 <label>.ack，用于以真实子进程做中断与交错验收；未设置时完全不影响行为。

## query 输出契约

固定为 `{"schema_version":2,"tree_sha256","root","horizon","generation","current_samples",
"summary","day"}`。current_samples 按 sample_id 排序，每项
`{"sample_id","revision","op","event_id","time","failed"}`，列出每一样本最高版，
withdraw 的后三项为 null。summary 为 `{"incident_count","downtime_seconds"}`；
day 为 `{"index","start","end","downtime_seconds","segments"}`，segments 按片段 start/end 排列。
同一数据集状态下输出字节确定，经临时文件原子替换 FILE；FILE 必须在 dataset 目录外，
拒绝符号链接与覆盖输入；失败保留既有输出与已提交代次。
