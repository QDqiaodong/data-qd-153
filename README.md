# data-qd-153 · 故障树系统可靠性

Exact independent basic-event fault-tree probability enumeration，以及基于实测状态的可更正日停机数据集。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/fault-tree --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/fault_tree/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 当前能力和样例
# Exact bounded fault-tree probability v1

设备可靠性数据人员核验独立基本故障事件及AND/OR门DAG。基本事件最多16个唯一ID、精确有理概率0..1；门最多100个，输入非空唯一且引用已知事件/门，整图无环。root可为事件或门。
枚举全部2^n基本事件状态，按互相独立概率乘积加权。重复引用同一事件必须共享状态，不能错误地将门输出视独立后相乘。输出顶事件故障概率、可靠度、枚举状态数及逻辑故障状态数；后者包括概率0的状态，不代表观测故障数。
样例top=(A AND B) OR (A AND C)，P=0.1*(0.2+0.3-0.2*0.3)=11/250，可靠度239/250。无共因故障、时间模型、维修、相关事件拟合或安全认证；不是生存曲线估计。

# Measured downtime history v2

运维人员按实测状态发布可更正的日停机数据。init 把数据集绑定到原格式故障树与固定范围
[0,horizon)，apply 结算修订批次（put/withdraw，按样本最高版本生效，同批幂等），
query 固定一个已提交代次读取某日分区。完整契约见 docs/contract.md。

```sh
.venv/bin/fault-history init --tree fixtures/history/tree.json --horizon 200000 --dataset run/ds
.venv/bin/fault-history apply --dataset run/ds --source fixtures/history/batch1.json
.venv/bin/fault-history query --dataset run/ds --day 2 --output run/day2.json
.venv/bin/fault-history apply --dataset run/ds --source fixtures/history/batch2.json   # 迟到更正：撤回B的恢复观测
.venv/bin/fault-history query --dataset run/ds --day 2 --output run/day2b.json
.venv/bin/python -m fault_tree.history query --dataset run/ds --day 0 --output run/day0.json
```

样例树 top=(A AND B) OR (A AND C)。batch1 上报 A 在 [100,190000)、B 在 [80000,175000) 故障，
全局停机区间 [80000,175000) 跨三天，第2日片段 2200 秒、总停机 95000 秒；batch2 撤回 B 的恢复观测后，
区间身份变为 [80000,190000)，第2日片段 17200 秒、总停机 110000 秒。树文件与批次文件删除后数据集仍可查询。

# Optimal adaptive fault-diagnosis policy v3

故障调查人员在静态故障树、部分已知节点读数和可付费准确检测清单下，需要先测什么、每个读数后继续测什么。
`fault-diagnose` 枚举全部基本事件逻辑世界（概率 0 的世界仍是逻辑公民），从联合世界拆分推导检测的
条件相关性，逐子问题求精确最优自适应策略：正质量子问题最小化精确条件期望费用，零质量子问题最小化
最坏费用，同值按 test_id 的 UTF-8 字节序决胜；每条逻辑可行路径都必须在预算内确定顶事件，否则
正常返回 infeasible。完整数据契约见 [docs/contract.md](docs/contract.md)。

```sh
.venv/bin/fault-diagnose --source fixtures/diagnose/request.json --output run/diagnose.json
.venv/bin/python -m fault_tree.diagnose --source fixtures/diagnose/request.json --output run/diagnose.json
```

自制样例含共享 pump 子门 pv_and/ps_and 与概率 0 的 interlock 证据。规划器先测 ps_and：读数为真即
判定顶事件故障；为假再测 pv_and。报告给出可执行 policy 决策树、精确条件期望费用（247/50）、
最坏路径费用（5）与条件顶事件故障概率（11/250）。所有概率与期望为约分 Fraction 字符串，
无采样、无近似、无贪心；输入输出不同路径、原子替换、拒绝符号链接，失败退出 2 且保留旧报告。

