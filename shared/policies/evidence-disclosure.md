# 证据选择与披露策略

## 三层分离

1. 原始材料库：收到的材料及其只读哈希记录；进入材料库不等于成为证据。
2. AI候选池：AI可以提出 `submit_now`、`reserve`、`internal_only`、`do_not_submit` 或 `needs_review` 建议，但不能改变律师决定。
3. 当前提交集：只有律师决定为 `submit_now` 且存在有效G2批准的证据才能进入。

“上传的证据全部进入目录”不是本系统规则。每项证据至少评估真实性、合法性、关联性、证明力、不利内容、是否引出新争点、对方利用路径、重复性、提交时机、不提交后果和替代材料。

## 披露字段

`disclosure_intent` 只表达希望如何披露；`intended_recipient` 只表达预期接收者；`service_obligation_status` 必须记录是否已核验送达或交换义务。“希望只给法院”不能被转换成“对方一定看不到”的承诺。含“保证对方看不到”“仅法院可见”“绝不送达对方”等保证性措辞是硬阻断项。

程序要求未核验时必须标为 `requires_verification` 并阻止提交包定稿。即使希望只作法院候选，也只有在当前案件为生产环境、`service_obligation_status=verified_not_required`，且绑定的程序法源同时为已核验 L1、`production_eligible=true`、`test_only=false` 时才可能继续；TEST-ONLY、摘要或模型推断均不能支撑该决定。组卷和打印生产单必须自行重跑这项语义校验。

优先提高证明力密度。允许同时维护简版提交候选、详细储备稿和待确认附表，但三者不得共用含糊的“已提交”状态。
