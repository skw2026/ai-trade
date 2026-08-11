# 微结构目标架构公平对照

当前 `microstructure_alpha_learnability_v1` 已在固定的 6 个 OOS split 上证明：市场存在扣除压力成本后的非重叠机会，但现有独立二元盈利事件分类器没有学到可用信号。下一步在不改变数据、特征、成本、时间切分或晋级门槛的前提下，对照当前基线与三种预声明目标架构：直接压力净效用回归、机会识别加条件动作选择两阶段模型、联合动作排序模型。

对照结果只用于 development 诊断。任何架构即使在当前 OOS split 上优于基线，也只能形成下一轮独立 forward 验证的预注册候选，不得直接生成 frozen candidate、修改 `development_passed`，或进入 demo/live 路由。

