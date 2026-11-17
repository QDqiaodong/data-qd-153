本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

- input.json：故障树概率枚举样例（v1 输入）。
- history/tree.json：同一棵含共享基本事件的故障树，作为 fault-history init 的 --tree 输入。
- history/batch1.json、history/batch2.json：实测状态批次样例，先上报跨日停机，
  再以撤回更正 B 的恢复观测，演示停机区间边界的跨代修正。
- diagnose/request.json：fault-diagnose 的 {tree,evidence,tests,budget} 自制样例，
  含共享 pump 子门与概率 0 的 interlock 初始证据，演示精确最优自适应检测策略。
- factor/request.json：fault-factor 的 {tree,factors,evidence,query,elimination_order}
  自制样例，含共享 pv_and/ps_and 子门、重叠 scope 的非归一势函数、门与基本事件混合证据，
  query 为两个相关门。旁注 fixture 无真实设备含义。
- calibrate/request.json：fault-calibrate 的 {tree,groups,observations,max_iterations,
  tolerance} 自制样例，含双成员共享组与单成员组两个都会更新的参数组、一个不在 root 祖先内
  的冻结事件、空证据及门/事件混合读数，演示逐轮 EM 轨迹与末轮概率表。
- calibrate/unsupported.json：同入口的 unsupported_data 自制样例，冻结为 0 的 interlock
  被要求失败，初始质量为零，正常结束且不做任何参数更新。
