本目录为原创虚构合成数据，使用MIT许可。没有真实个人、线上实验、生产日志、交易、项目进度或搜索用户信息；不来自外部平台。

- input.json：故障树概率枚举样例（v1 输入）。
- history/tree.json：同一棵含共享基本事件的故障树，作为 fault-history init 的 --tree 输入。
- history/batch1.json、history/batch2.json：实测状态批次样例，先上报跨日停机，
  再以撤回更正 B 的恢复观测，演示停机区间边界的跨代修正。
- diagnose/request.json：fault-diagnose 的 {tree,evidence,tests,budget} 自制样例，
  含共享 pump 子门与概率 0 的 interlock 初始证据，演示精确最优自适应检测策略。
