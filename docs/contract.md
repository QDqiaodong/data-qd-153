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

# Optimal adaptive fault-diagnosis policy v3

故障调查人员持有静态故障树、部分已知节点读数与可付费的准确检测清单，需要知道先测什么、
每个读数之后继续测什么。`fault-diagnose`（或 `python -m fault_tree.diagnose`）
生成精确最优的自适应检测决策树；它不拟合事件概率、不修改故障树、不接入设备、不读取停机历史。
旧 `fault-tree` 与 `fault-history` 入口及契约保持不变。

## 命令与退出码

```sh
fault-diagnose --source FILE --output FILE
python -m fault_tree.diagnose --source FILE --output FILE
```

成功退出 0：报告原子写入 FILE，并把同一份报告打印到 stdout。解析/计算/IO 失败退出 2 且保留原输出；
`inconsistent_evidence`、`zero_mass_evidence`、`infeasible` 都是正常分析结果，写完整报告且退出 0。

## 输入契约

严格 `{"tree","evidence","tests","budget"}`，沿用 8MB 读取上限，拒绝重复 JSON 键、未知字段、
非法类型/引用与超界输入；数组顺序不代表优先级，重排不影响 result。

- `tree` 恰为既有 events/gates/root 结构，整体交给既有 `parse_tree` 契约，不放宽任何旧校验：
  16 个基本事件、100 个门、概率 0..1、ident、引用合法、AND/OR DAG 无环，root 可为事件或门。
- `evidence` 为 0..116 项 `{"node","failed"}`；node 必须是已声明事件或门且不重复，
  failed 只能是 JSON 布尔（布尔不冒充整数）。
- `tests` 为 0..10 项 `{"id","node","cost"}`；id 沿用 ident 规则且在清单内唯一，
  node 可为任意已声明事件或门；不同 test_id 可检测同一 node，也可检测已有初始证据的 node。
  cost 是 1..10^9 的整数。
- `budget` 是 0..10^9 的整数。

## 逻辑世界与精确条件质量

- 逻辑世界是全部 16 个（或实际声明数）基本事件的真假赋值；门值按原 AND/OR DAG 求得。
  所有检测读取**同一个静态基本事件向量**下某节点的准确布尔，不改变设备状态；共享事件的门读数
  不是独立随机变量，条件相关性一律从联合世界拆分推导，绝不把清单节点边缘概率再作独立乘积。
- 符合全部初始 evidence 的世界构成初始支持；其先验质量是各独立基本事件概率（或 1-概率）的乘积。
  概率恰为 0 或 1 的事件仍保留其逻辑世界：概率 0 不等于逻辑不可能。
- `logical_worlds` 统计满足证据的逻辑世界数（含零质量世界）；`evidence_probability` 是其总先验质量，
  用约分 Fraction 字符串表示（整数写 `"0"`、`"1"`）。
- `root_failure_probability` 是给定初始证据的精确条件顶事件故障概率。

## 子问题、规范目标与预算可行性

子问题由（已见节点读数、已用 test_id 集合、剩余预算）共同决定，内部表示为
（可行世界位掩码、已用检测位掩码、剩余预算）。在逻辑支持非空且 root 未确定时：

1. 候选动作是尚未使用且 cost 不超过剩余预算的 test；检测必须在看到读数前选择，不能按真实世界
   事后挑最便宜路径，也不能只检查平均费用是否低于预算。
2. 把支持按该 test 的实际读数拆成 true/false 两个子分支；逻辑不可能的结果分支写 `null`，
   逻辑可行但零质量的分支必须保留完整合规策略。每个非空子分支都必须能在扣除本次 cost 的剩余
   预算内确定 root，否则该候选检测不可用于合规策略。
3. 正质量子问题：动作值 = 本次 cost + 各分支后续期望费用按该分支条件质量加权之和；零质量分支
   对期望贡献 0 但仍须有可行续策。按最小动作值选择，同值按 test_id 的 UTF-8 字节序取最小，
   不用浮点容差，不以最坏费用打破正质量同值。
4. 逻辑非空但质量为 0 的子问题：动作值 = 本次 cost + 非空子分支后续最坏费用的最大值；
   同值同样按 test_id UTF-8 字节序取最小，然后递归应用同一规则。
5. root 在当前所有逻辑可行世界中已同值时立即停止为叶；未确定时不能提前给结论。
   一条路径同一 test_id 至多执行一次。无任何合规策略时返回 `infeasible`。

规划全程使用公共分母下的整数单位做精确比较，仅在序列化时还原 Fraction；不使用模拟采样、
近似概率、贪心信息增益或固定检测排序。

## 状态判定与输出契约

状态判断严格按以下先后顺序：

1. 逻辑世界数为 0 → `inconsistent_evidence`（初始证据逻辑矛盾）。
2. 否则先验总质量为 0 → `zero_mass_evidence`（逻辑非空但零质量，不规划，禁止除以 0）。
3. 否则按预算能否规划 → `optimal` 或 `infeasible`。

报告严格为 `{"schema_version":3,"source_sha256","result"}`，source_sha256 是完整输入原字节 SHA256。
result 恰含 `status,logical_worlds,evidence_probability,root_failure_probability,
expected_cost,worst_cost,policy`：

- 前两个状态（inconsistent/zero_mass）的后四个分析字段均为 null；zero_mass 仍给出
  logical_worlds 与 `"0"` 的 evidence_probability。
- infeasible 给出精确 root_failure_probability，但 expected_cost、worst_cost、policy 为 null。
- optimal 的 `expected_cost` 是初始证据条件下所选规范策略的期望累计费用（不含免费初始证据），
  `worst_cost` 是所选策略在所有逻辑可行执行路径（含零质量路径）上的最大累计费用，非负整数；
  最坏费用是所选规范策略的统计值，不是另一个优化目标。
- 初始支持已确定 root 且质量为正时立即为叶，expected_cost `"0"`、worst_cost 0；
  质量为 0 时即使 root 已确定仍按 zero_mass_evidence 返回。
- policy 叶节点 `{"kind":"leaf","failed":布尔}`；检测节点
  `{"kind":"test","test_id":ID,"on_false":子树或null,"on_true":子树或null}`，
  两个分支语义按实际读数固定，不能交换。

同语义输入重排 tree/evidence/tests 数组不改变 result，但源字节变化会改变 source_sha256；
同一输入报告字节稳定。输出沿用安全原子替换（临时文件 + os.replace），输入输出不能同路径，
拒绝输出符号链接；失败退出 2 并保留原输出。

# Joint conditional risk table v4

可靠性数据团队持有静态故障树、跨事件相关势函数、部分节点读数与一个待分析节点组，
需要一次得到该节点组的**完整条件联合概率表**，供下游风险组合分析直接使用。
`fault-factor`（或 `python -m fault_tree.factor`）按调用方给定的消元次序做精确局部因子
乘加，不展开全部基本事件世界；它不拟合事件概率、不修改故障树、不读取停机历史、
不接入设备。旧 `fault-tree`、`fault-history`、`fault-diagnose` 入口及结果契约保持不变。
本入口的树契约把基本事件上限放宽到 64，旧入口仍保持 16 事件接受上限。

## 命令与退出码

```sh
fault-factor --source FILE --output FILE
python -m fault_tree.factor --source FILE --output FILE
```

成功退出 0：报告原子写入 FILE，并把同一份报告打印到 stdout（报告字节确定）。
`zero_model`、`zero_evidence` 是正常业务结果，写完整报告且退出 0。
解析、结构超界（含 12 变量宽度拒绝）、计算或 IO 失败退出 2 且保留既有输出，
不留下半份新报告。沿用源字节 SHA256、输入/输出同路径拒绝、输出符号链接拒绝、
8MB 读取上限、重复 JSON 键拒绝、非有限数值拒绝与原子替换。

## 输入契约

严格 `{"tree","factors","evidence","query","elimination_order"}`，各层拒绝未知字段、
非法类型、重复 JSON 键与布尔冒充整数。数组顺序不代表优先级：tree/factors/evidence/query
的语义等价重排不改变 result，但 `elimination_order` 决定 audit 与宽度准入。

- `tree` 恰为 `{"events","gates","root"}`：
  - `events` 为 1..64 项 `{"id","numerator","denominator"}`，id 唯一且沿用 ident 规则，
    0<=numerator<=10^9、1<=denominator<=10^9、numerator<=denominator（概率 0..1）。
  - `gates` 为 0..100 项 `{"id","op","inputs"}`，op 为 `AND`/`OR`，inputs 为 1..5 个
    不同且已声明的节点；门 id 不得与事件或其他门重复；整图无环。
  - `root` 为已声明事件或门。**所有**声明的基本事件（含不在 root 祖先中的事件）与
    所有门都是完整模型与结构审计的一部分。
  - 其余校验（ident、引用、无环）与 v1 树契约相同；仅事件上限与本入口绑定为 64。
- `factors` 为 0..128 项 `{"id","scope","values"}`：
  - `id` 唯一，仅用于输入身份校验，**不参与数值权重**。
  - `scope` 为 1..8 个不同基本事件 ID 的**有序**数组；其顺序是势表的列坐标。
  - `values` 恰有 `2**len(scope)` 项 `{"numerator","denominator"}`，非负有理数，
    分子 0..10^9、分母 1..10^9；允许大于 1 的势值，也允许 scope 重叠。
  - 行按 scope 原序的二进制字典序解释，**False 先于 True**（首列变化最慢）。
    只重排 scope 却原样解释 values 会得到不同结果；scope 重排并同步重排 values 结果不变。
- `evidence` 为 0..164 项 `{"node","failed"}`：node 唯一且为已声明事件或门，
  failed 为 JSON 布尔。证据只约束输出质量，不改变既定结构宽度准入。
- `query` 为 1..8 个不同已声明节点（事件或门，不要求含 root，也不要求是叶子）。
- `elimination_order` 须恰好列出 query 之外的全部事件和门各一次（不漏、不重、不越界）。

## 联合质量与归一化

对基本事件赋值 `x`：未归一化质量 `W(x)` = 各事件 Bernoulli 先验质量之积
（失败取 p、健康取 1-p）× 全部势函数在 `x` 上的值；门值由同一个 `x` 沿 DAG 确定
（AND=全真、OR=任一真）。共享门不是独立变量，重复引用共享同一状态，未观测门也不是
新的自由随机事件。`Z = Σ_x W(x)`；`N = Σ_{x 符合 evidence} W(x)`；
`Q(q) = Σ_{x 同时符合 evidence 与 query 赋值 q} W(x)`。

- `Z = 0` → `zero_model`：`evidence_probability` 为 null，`joint` 为 `[]`。
- `Z > 0` 且 `N = 0` → `zero_evidence`：`evidence_probability` 为字符串 `"0"`，`joint` 为 `[]`。
- 其余 → `ok`：`evidence_probability = N/Z`，每行 `probability = Q(q)/N`；
  无证据时 `evidence_probability` 为字符串 `"1"`（只要 Z>0）。
- 两种零状态都仍完整给出 `variables` 与 `audit`；`normalizer` 始终是 Z。

禁止把势函数分别归一化、把共享门当独立变量相乘、把未观测门当成自由随机变量，
也禁止用各节点边缘概率的列表冒充整组联合分布。

## 消元与结构审计

必须按给定 `elimination_order` 以精确局部因子运算（乘法 + 对变量求和）求出 Z、N 与
完整联合表，不得展开全部基本事件世界。数值与审计使用同一秩序：

- 审计结构由每事件一元先验因子、每个输入势函数 scope、每门含门及其全部输入的确定性
  因子建立；所有 scope 内部按 ID 的 **UTF-8 字节序**规范化。
- 在任何证据约简或数值化简**之前**，依次取含待消元变量的全部因子的 scope 并集为
  `bucket_scope`；删除这些因子并加入 `bucket_scope` 去掉该变量后的 `residual_scope`；
  残余为空 scope 时保留为标量因子。最后剩余 scope 的并集为 `final_scope`。
- `peak_variables` 取初始 scope、每步 `bucket_scope`、每步 `residual_scope` 与
  `final_scope` 的最大变量数，**不得超过 12**；即使证据使变量固定、势值全零或因子为
  常数也不缩 scope。超界输入退出 2 并保留旧输出。
- 消元次序改变桶宽但不改变数值结果；audit 依给定次序逐项记录，因此次序变化会改变 audit。

## 输出契约

报告严格为 `{"schema_version":4,"source_sha256","result"}`，`source_sha256` 是完整输入
原字节 SHA256。`result` 严格为 `{status,normalizer,evidence_probability,variables,joint,audit}`：

- `variables` 将 query 按 ID 的 UTF-8 字节序排序。
- `joint` 依此序、False 先于 True 列出全部 `2^len(query)` 行
  `{"failed", "probability"}`；`failed` 是与 `variables` 对齐的布尔数组；零概率行不可省略。
- `normalizer` 始终是 Z；概率与 normalizer 均用约分 Fraction 字符串，整数不写分母。
- `audit` 严格为 `{steps,final_scope,peak_variables}`；`steps` 依给定次序每项为
  `{variable,bucket_scope,residual_scope}`，所有 scope 均规范排序。

同语义输入重排 tree/factors/evidence/query 数组不改变 result 且报告字节稳定；
`elimination_order` 仍决定 audit 与宽度准入。输出沿用安全原子替换
（临时文件 + `os.replace`），输入输出不能同路径，拒绝输出符号链接；失败退出 2 并保留原输出。

# 共享参数组批量 EM 校准 v5

可靠性数据团队只有若干次独立试验的**部分节点读数与重复次数**，需要把它们转成可追踪的
基本事件概率校准参数表。`fault-calibrate`（或 `python -m fault_tree.calibrate`）在原
16 事件故障树、共享 AND/OR DAG 与全图校验之上，对**共享参数组**运行题目定义的定点 EM
迭代；它不识别唯一真实参数、不寻找全局最优、不采样、不调用外部优化库。旧 `fault-tree`、
`fault-history`、`fault-diagnose`、`fault-factor` 四个入口及结果契约保持不变。

## 命令与退出码

```sh
fault-calibrate --source FILE --output FILE
python -m fault_tree.calibrate --source FILE --output FILE
```

成功退出 0：报告原子写入 FILE，并把同一份报告打印到 stdout（同输入字节确定）。
`unsupported_data`、`converged`、`max_iterations` 都是正常结束，退出 0；
解析、计算或 IO 失败退出 2 且保留既有输出。沿用源字节 SHA256、输入/输出同路径拒绝、
输出符号链接拒绝、8MB 读取上限、重复 JSON 键拒绝、非有限数值拒绝与原子替换。

## 输入契约

顶层严格为 `{"tree","groups","observations","max_iterations","tolerance"}`，各层拒绝
未知/缺失字段、重复 JSON 键、布尔冒充整数。tree 的 events/gates、门 inputs、groups 及
其 events、observations 及其 evidence 的语义等价重排不改变 result。

- `tree` 完整交给原 `parse_tree`：基本事件 1..16 个，门 0..100 个，概率为 0..1 的精确
  有理数，ident、引用、门输入唯一与整图无环规则全部不变。**所有**声明事件（含不在 root
  祖先中的事件）都参与世界枚举。
- `groups` 为 1..16 项 `{"id","events","alpha","beta"}`：
  - `id` 唯一且沿用 ident；`events` 为非空、无重复的已声明**基本事件** ID 数组（不能引用
    门），所有组的成员互不重叠；允许只覆盖部分事件，组外事件冻结在 tree 原概率（可为 0/1）。
  - 同组成员初始概率以输入有理数**精确比较相等**，且严格在 (0,1) 内（拒绝 0、1 边界）。
  - `alpha`、`beta` 为 1..10^6 整数，是**每组各一份**的正伪计数，不是每个成员各一份。
- `observations` 为 1..128 项 `{"id","count","evidence"}`：`id` 唯一且沿用 ident；
  `count` 为 1..10^6 整数（拒绝布尔）；`evidence` 为 0..116 项 `{"node","failed"}`，
  node 为任意已声明事件或门且同一行不重复，`failed` 只能为 JSON 布尔。相同 evidence 可按
  不同 id 出现，各按自身 count 计权；空 evidence 合法。各行代表不同试验，**不在行间合并
  证据**；未提及的节点是缺测而非健康。
- `max_iterations` 为 1..100 整数；`tolerance` 为有限 JSON 数，范围 [1e-12,1e-3]。

## 概率模型与 EM 迭代

组 g 的共享失败概率记为 theta_g，成员数 m_g。每个基本事件仍条件独立：共享参数只约束概率
相同，**不代表这些事件必须同时失败**（独立重复事件，而非单一共因）；组外事件用原先验。
对完整基本事件布尔向量 x 取独立 Bernoulli 乘积 P_theta(x)，所有门沿现有 DAG 在同一向量上
求值。S_i 是满足第 i 行全部 evidence 的世界集合，L_i(theta)=sum_{x in S_i} P_theta(x)，
C_g(x) 是组 g 中失败基本事件的个数。求和覆盖全部声明事件，不能只检查 root 或仅用观测门的
边缘概率。

给定旧 theta，E 步对**全部逻辑世界**（含零质量世界，不删除、不夹 epsilon、不把缺测当健康）
求条件期望：

  T_g = sum_i count_i * sum_{x in S_i} (P_theta(x) * C_g(x)) / L_i(theta)

M 步所有组在同一旧参数向量上算完分子后**同时**替换：

  theta'_g = (alpha_g + T_g) / (alpha_g + beta_g + m_g * sum_i count_i)

组外事件概率全程冻结。

## 初始质量门与 unsupported_data

所有输入语义校验完成后，先在初始 theta 下求每一行 L_i；任一为 0 即正常返回
`unsupported_data`，列出**全部**零质量行（按 id 的 UTF-8 字节序），`iterations=0`、
`groups`/`events` 为 null、`observations`/`trace` 为 []，且不进行任何参数更新。
行内逻辑矛盾、被冻结 0/1 事件排除的读数都属于此状态，不是格式错误。不在 root 祖先内的
冻结事件若带显式矛盾证据，同样使整行 unsupported，不能因其与 root 无关而忽略。

## 轨迹、收敛与输出契约

每个轨迹点评估
`ell(theta)=sum_i count_i*log(L_i(theta))`、
`J(theta)=ell(theta)+sum_g[alpha_g*log(theta_g)+beta_g*log(1-theta_g)]`（自然对数，
健康支用 log1p 稳定求值）。包含 iteration=0 初值点（其 max_delta 为 null），之后先同时
更新再按新 theta 重算 ell、J，`delta=max_g abs(theta'_g-theta_g)`。首轮起若 delta<=
tolerance 即停为 `converged`，该判定优先于同轮达到 max_iterations；否则到轮数上限为
`max_iterations`。不得仅因目标变化很小、似然下降或四舍五入相等而提前停。逐轮 J 不下降。

报告严格为 `{"schema_version":5,"source_sha256","result"}`，result 恰含
`status,iterations,groups,events,observations,trace,unsupported_observations`：

- 正常拟合时 iterations 为已执行更新数；`groups` 按 id 的 UTF-8 字节序排列，每项
  `{"id","events","probability"}`，events 成员也同序；`events` 列全部已声明事件（含冻结
  项）按 id 同序，每项 `{"id","probability"}`；`observations` 按 id 同序，每项
  `{"id","probability"}` 给末轮 L_i；`unsupported_observations` 为 []。
- `trace` 有 iterations+1 项，每项
  `{"iteration","parameters","log_likelihood","objective","max_delta"}`，parameters 为按
  组 id 排序的 `{"id","probability"}` 数组。末轮 trace 参数、groups、events 与观测概率
  必须一致。所有概率、目标与 delta 均为有限 JSON 数（不用 Fraction 字符串）。

数值界是契约的一部分：sum(count)<=128*10^6、每组 m<=16 且 alpha/beta>=1，因此一次更新后
theta 与 1-theta 均至少为 1/(2*10^6+16*128*10^6)；初始内部概率由原分子分母界也远离
binary64 舍入端点。E 步对全部逻辑世界求和（binary64 顺序求和即可，不采样、不近似后验、
不用硬归因代替条件期望）；同 evidence 拆成不同 id、count 总和相等的行给出相同数值轨迹；
固定 alpha/beta 下整体放大 count 会改变惩罚与数据的相对作用，count 不归一化到和为 1。
