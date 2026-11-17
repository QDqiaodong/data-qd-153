# data-qd-153 · 故障树系统可靠性

Exact independent basic-event fault-tree probability enumeration。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

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
