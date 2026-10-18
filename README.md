# AI交易机器人

> **核心目标**：构建一套面向 BTC/ETH/SOL 永续合约的 C++ 高频/中频交易系统。
>
> **交易所优先级**：Bybit V5（后续可扩展 Binance）。
>
> **设计哲学**：生存优先 > 风险可控 > 可验证正期望。不承诺“稳定收益”，但强调“可回滚、可审计、硬风控”。

---

当前状态（2026-10-18）：**数量资产桥已交付，双腿执行误差已量化，实际执行与盈利资格未通过。**
309回归无skip，原119时点的1428场景独立核验：934报价可比，478因所选
双腿时差不可比（非交易所拒单），其余初始不足/截尾保留。250ms现货先行
最大额外成本约1.03USDT；真实费用、抵押品就绪和成交仍未知。见
[执行桥、原始序列审计与生产缺口](docs/reviews/2026-10-18-dated-basis-execution-bridge-result.md)。
无新收益实验或账户/部署；Binance生产仍stub，不能拿旧线性账直接交易。
下一自主做手续费匹配对冲的零行情代数裁决，不重复采样/历史调参。
G4/候选0/确认0/Demo禁开仓保持，无后台等待。

此前状态（2026-10-18）：**三个完整事件重复正现金，但提前退出的增益未重复成立。**
固定四季独立事件实验中，3/9/12月压力净+84.63/+32.12/+17.03 USDT；
只有3月优于持有控制，故停止继续优化当前退出政策。6月UNKNOWN保留，
各季不拼成年收益，原连续账户不足不改。见
[四季度机制归因与圆桌结论](docs/reviews/2026-10-18-dated-basis-event-panel-result.md)。
240回归、288账/6295536风险bar独立复算通过。下一重心为执行和独立事件证据，
不继续刷历史参数；候选0、确认0、G4/Demo禁开仓保持，无账户/部署/后台等待。

此前状态（2026-10-18）：**2023条件开发支持保留；2024冻结转移前段正现金，完整证据不足。**
候选基础+234.74/压力+203.94 USDT，但6月mark缺5根导致退出、后两季阻止，
DATA_INSUFFICIENT，不能称全年盈利验证通过。月/日同缺，固定REST补证未恢复。
226回归、144账/3196488风险bar独立核验通过。见
[2024完整结果与缺口归因](docs/reviews/2026-10-18-dated-basis-frozen-2024-transfer-result.md)。
规则未再调参，不重置旧失败；研究内有限批次可自主推进，无需逐步索权。
独立确认0、候选0、G4/Demo禁开仓保持，无账户/部署或后台等待。

此前状态（2026-10-18）：**单一提前退出结构取得条件开发支持，尚非独立盈利资格。**
同入场/成本/风险，基础+105.40、压力+58.72 USDT，资金占用时间减少26.36%；
18压力格均正，但约99.44%压力净收益来自一季，去最大季仅+0.33。见
[提前双腿退出完整结果](docs/reviews/2026-10-18-dated-basis-convergence-exit-result.md)。
212回归及108账独立复算通过。冻结此规则，下一做跨行情和执行验证，
不继续在2023调参；独立确认0、候选0、G4/Demo锁不改，无账户或部署。

此前状态（2026-10-18）：**完整条件持仓已获中央正现金，压力稳健性仍未通过。**
基础+76.18/压力中央+35.33 USDT，三季度完整出清；最差压力−11.99，保持
NET_UNSUPPORTED。192回归及108账/2784096风险bar独立复算一致。见
[条件收益与三方圆桌复盘](docs/reviews/2026-10-18-dated-basis-conditional-development-result.md)。
用户已授权研究内自主连续推进；下一固定入场、检验提前双腿退出结构，
不扫描阈值或逐批索权。旧失败、独立确认0、候选0、G4/Demo锁不改，无部署。

此前状态（2026-10-18）：**观测分层已实现，盘口范围限制已解决于新诊断；经济资格未改。**
同一119时点中，31次退出深度拒绝来自初始快照边界，已有连续增量实际
能覆盖请求；新已知档位下界核验118双腿展示量、1时差排除，不冒称成交。
149回归与238方向扫单独立复算通过。见
[观测分层与盘口诊断](docs/reviews/2026-10-18-dated-basis-observability-result.md)。
未新增行情或收益回放；2023执行证据仍未知，旧负收益/撤回、G4/候选0/
Demo锁保持。下一统一条件收益与执行证据的研究口径，不调参刷绿或盲等。

此前状态（2026-10-18）：**机会政策开发已完成，尚无经济支持，最早瓶颈已定位。**
唯一回放基础现金−10.33/压力−25.61 USDT；首笔持仓21h35m后遇到完整但
零成交期货bar，spot/mark仍正常，合同撤回并锁存后续季度。零成交不等于
空盘口，不把这次早退当作长期机制否定。见
[完整开发结果与结构复盘](docs/reviews/2026-10-18-dated-basis-opportunity-development-result.md)。
102回归及36账独立核对通过；下一先区分成交、报价和估值的观测状态，
不调参重跑或默认等待。G4/候选0/Demo锁保持，无账户/交易/部署。

此前状态（2026-10-18）：**现金风险边界已核清，下一研究顺序待实质选择。**
入场预测不是锁定利润，8%停止不是最大亏损保证；四个合成场景与既有
核算/独立审计一致，69项回归通过。见
[风险边界与开发确认顺序](docs/reviews/2026-10-18-dated-basis-cash-risk-design.md)。
已询问是否允许新有界开发先估计效应、再裁决独立确认预算；不降低G4或
改变旧阶段结论，尚未执行新收益批次。无账户/部署/等待，Demo禁开仓保持。

此前状态（2026-10-18）：**实时双腿盘口可核验，观测报价尚无压力现金余量。**
固定120秒、119时点118有效；2500美元面值基差72.78至87.71bps，低于原
压力假设盈亏平衡115.58bps，预计现金净额−10.55至−6.86USDT。不必先等
交割才发现入场余量不足，但也不能把两分钟诊断当成方向永久否定。见
[实时盘口结果与下一步](docs/reviews/2026-10-18-dated-basis-live-quotes-result.md)。
60回归及118项独立复算通过；无新收益批次/交易/部署/后台等待。下一先绑定
现金风险下界与有限独立验证出口，不重复采样刷正；G4/候选0/Demo锁保持。

此前状态（2026-10-18）：**已接受并实现新实验双账核算，独立确认出口仍未成立。**
现金权益承担真实费用、应计融资义务和8%停止，6%/12%只作独立基准敏感性；
不再扣入风险峰值和跨期本金。44回归、11账51845风险bar独立Decimal一致。
见[交付结果与确认瓶颈](docs/reviews/2026-10-18-dated-basis-dual-ledger-result.md)。
单一机会规则已冻结，但既有2023四季全部已见；未绑定可承担的独立输入/
确认终点，按事前条件不启动新市场回测或默认等待。不是完整市场/生产适配器，
未证明盈利；旧结果、G4未达成、候选0和Demo禁开仓保持，无部署。

此前状态（2026-10-18）：**资金成本语义已查明，下一需决定后续实验是否分账。**
6%/12%来自研究假设，未绑定实际融资，现行产品规则未指定这两个基准。
旧研究把机会成本扣入净值/8%回撤/跨季本金；固定合成中现金权益回撤7.63%，
经济效用账却触发8%停止。见[来源、合成证据及新口径建议](docs/reviews/2026-10-18-dated-basis-capital-result.md)。
旧合同算术/市场结果保留，不称取消基准即可盈利；建议账户账与资金基准账
分开，6%/12%保留敏感性。改变开发准入口径需要一次实质决定，尚未实施。
18回归/4账Decimal通过，0真实市场重跑；G4/候选0/Demo锁保持，无部署或等待。

此前状态（2026-10-18）：**机会容量诊断完成，当前压力经济口径下没有正报价余量。**
100712条有效报价、四交割季度逐根核查：压力费用加12%假设资金成本后
正余量0，并非只因固定日期取点；不计机会成本的压力现金仍有20603条
正预测，三个季度有持续段，不能称现金盈利不可能。见
[容量结果与成本约束](docs/reviews/2026-10-18-dated-basis-capacity-result.md)。
下一先核资本成本与产品收益基准的适用性，不再加期限或模型、不直接降门槛。
132回归/604272项Decimal复核，0新行情或收益实验；G4/候选0/Demo锁保持。

此前状态（2026-10-18）：**30/60/90日期限实验完成，有局部现金正收益，尚无经济支持。**
30/60日只在同一12月季度入场，基础现金+5.16/+17.69USDT，资金占用调整后
−7.07/−6.66；90日不入场。60日压力中央现金+4.03，但压力情景包络仍跨零。
见[完整经济结果与现金占用分解](docs/reviews/2026-10-18-dated-basis-term-result.md)。
同源日档案恢复月档案缺口后唯一计算，110回归/756账独立核对。G4/候选0/
Demo锁保持，无部署或等待；持续目标未完成，下一先诊断机会容量，不加第4期限。

此前状态（2026-10-18）：**费用与结构诊断完成，仅降手续费不足以解决固定7日政策。**
原中央价格/滑点/6%占用不变，即使期货费归零、现货均按7.5bps折扣，四季
成本下界仍超过毛空间；6月/9月差额仅0.94/1.85bps，对执行假设敏感。
见[费用依据与成本预算](docs/reviews/2026-10-18-dated-basis-fee-result.md)。
86相关回归无skip、四季独立有理数核对；0新收益实验/账户/ECS/部署/等待。
下一建议是期限与净基差完整开发实验，尚未启动；G4/候选0/Demo锁保持。

此前状态（2026-10-17）：**期限基差7日政策经济筛查完成，基础成本下四季均不入场。**
固定2023四季度、1政策/54敏感情景，裁决NO_GO_NO_TRADES；独立假想建仓
基础中央净额−13.50/−4.02/−4.24/−8.98USDT，不是实际策略亏损。见
[经济结果与成本归因](docs/reviews/2026-10-17-dated-basis-assumption-result.md)。
76回归无skip、216账独立复算；旧输入不足与风险UNKNOWN不改。G4未达成、
候选0、Demo禁开仓保持，无账户/ECS/部署/等待，不自动改期限或续研究。

此前状态（2026-10-17）：**期限基差双腿重开核查完成，历史结算与风险输入仍不足。**
取得2023四季度交割价格记录、两日原始成交；18小时×7字段与旧K线完全一致，
但实际结算钟、历史规格及保证金绑定尚缺。见
[输入结果与解释边界](docs/reviews/2026-10-17-dated-basis-research-result.md)。
INPUT_NOT_ADMITTED、0收益实验，不是机制经济否定。44回归无skip、6381旧绑定
保持；无账户/ECS/部署/等待，G4未达成、候选0、Demo锁不变。

此前状态（2026-10-16）：**手续费24小时归因完成，微弱改善不足以升级交易政策。**
改善主要来自价格线性空间之外的费用分量，但2025两半年各剔一个固定28日
块后总改善转负，成本覆盖亦未建立。见
[归因结果与下一阶段边界](docs/reviews/2026-10-16-fee-component-diagnosis-result.md)。
535条冻结预测独立核验、157回归无skip；0新收益拟合/交易回放/账户/ECS/
等待。G4未达成、候选0、Demo锁保持，不自动改持有期或追加模型。

此前状态（2026-10-16）：**研究方法已调整，同条件信息增量诊断完成。**
三组已有信息×三个预测期限，54拟合/14283预测独立核对；8格增量不稳定，
手续费24小时仅微弱描述性一致，不是收益或统计通过，不直接改持有期重跑。
见[完整诊断](docs/reviews/2026-10-16-information-attribution-result.md)和
[研究解释规则](docs/RESEARCH_EXPLANATION_PROTOCOL.md)。141回归无skip，
一次wrapper计数错误具名修复并唯一复验；无新交易路径、账户/ECS/部署或等待。
G4未达成、候选0、Demo禁开仓及旧失败保持，本批不自动扩模型或机制。

此前状态（2026-10-16）：**数据先行批次完成，活跃地址机制被训练风险否决。**
官方固定版本790日输入完整；4个机制参数全部触及8%研究回撤停止，无候选
进入2025评价。压力净收益−1.270727%至−7.822097%，不因基础微正忽略风险。
见[真实实验结果与关闭复盘](docs/reviews/2026-10-16-btc-address-participation-result.md)。
8训练向量/16独立账及120相关回归通过，无skip；无新交易所行情、账户/ECS/
部署或等待。G4未达成、候选0、Demo禁开仓保持；本批不自动追加参数或机制。

此前状态（2026-10-16）：**CME保证金调整新机制核查结束，事件输入未准入。**
官方日表金额不能当政策调整事件；完整公告目录、比例/制度和可知时间仍缺。
唯一原件GET为curl92/HTTP000，无正文、未重试；这是输入不足，不是经济否定。
见[立项结果与接续条件](docs/reviews/2026-10-16-cme-margin-mechanism-intake-result.md)。
36资料、9旧回归无skip、6217旧绑定保持；0收益/训练/新行情/账户/ECS/等待。
本批关闭，完整经济方案0份，不自动续搜索、换机制或采购；G4/候选0/Demo锁保持。

此前状态（2026-10-16）：**BTC手续费完整经济实验结束，有正收益但稳健性未通过。**
8训练向量、6冻结评价、28独立账已完成；2025基础+3.627621%/压力+2.019452%，
压力回撤2.720246%。但基础未优于价格对照、链上再延迟一天转亏，去最好一周
压力亦负，裁决CLOSED_NO_GO / NO_GO_ECONOMIC。见
[完整结果与关闭复盘](docs/reviews/2026-10-16-btc-fee-pressure-result.md)。
100相关回归无skip、6145旧绑定保持，一次收益前夹具错误复盘后唯一复验通过。
0新取数/账户/ECS/部署/等待；G4未达成、候选0、Demo锁保持。按预定出口关闭，
不追加参数或默认进入观察期，不把正开发收益宣称为独立盈利资格。

此前状态（2026-10-15）：**BTC手续费压力输入已核实，完整经济实验提案已交付。**
固定版本两指标所需790日各连续正值，4GET全200；重建快照不等于历史时点
认证。见[完整8向量与六路径方案](docs/plans/2026-10-15-btc-fee-pressure-development.md)
及[输入核实结果](docs/reviews/2026-10-15-btc-fee-pressure-design-result.md)。
下一新范围复用既有价格，0新增行情/等待；尚未训练或回测，不重开旧关闭
策略。6115旧绑定保持、9旧边界回归通过，无生产修改/账户/ECS/部署。
G4未达成、候选0、Demo锁保持；新整体经济范围待接受，不逐内部步骤索权。

此前状态（2026-10-15）：**执行净收益已驱动本地学习对照，尚无新增盈利证据。**
真实应用订单/成交/资金费/WAL处理链接入原学习器，候选独立继承持仓，训练
锁定、留出拒绝、下一窗更新。14项新增验收、441828收据和完整CTest130/130
通过，7既有叶子skip保留。见
[四组结果、四次失败复盘及经济边界](docs/reviews/2026-10-15-runtime-net-learning-result.md)。
公平对齐时钟后，新旧方法本次执行净收益相同；初版不公平优势作废，不能
声称换目标已提高盈利。生产控制器未替换，无市场实验/账户/ECS/部署，
main skip-ci；G4未达成、候选0、Demo禁开仓保持。本工程批次结案，无需
等行情；下一真实经济批次须有具名机制与完整范围，不自动重开旧亏损路线。

此前状态（2026-10-15）：**完整运行时只读收益接线通过本地合成验收。**
真实BotApplication启动/主循环/异步成交/资金费/旧控制器已接入默认关闭的
观察器，接线开关及观察器失效前后交易行为对照一致。23原生场景、6独立
审计、94收据复算和完整CTest129/129通过，7个既有叶子skip保留。见
[阶段结果、持久化测试缺口与边界](docs/reviews/2026-10-15-runtime-reward-shadow-result.md)。
一次夹具身份遗漏导致WAL关闭记录失败，已补强日志与真实重载断言、具名复盘
并唯一复验；未改生产WAL规则。无行情/市场实验/账户/ECS或部署，main skip-ci。
线上学习目标未切换，G4未达成、候选0、Demo禁开仓保持。下一实质成果为
净收益目标驱动学习的完整对照范围，不自动重开旧策略或进入等待期。

此前状态（2026-10-15）：**原生执行净收益最小闭环通过合成验收。**
原有学习器已通过新适配器使用原生账户/订单/风险/执行账，候选继承持仓与
风险锁，方向增益和单纯敞口放大分开判断。见
[19项新增回归、独立账务和交付边界](docs/reviews/2026-10-15-native-execution-reward-result.md)。
完整本地CTest127/127通过，7个既有可选依赖/平台叶子skip；6014旧文件保持。
这不是线上控制器切换或市场盈利证明，G4未达成、候选0、Demo禁开仓保持。
0新行情/历史经济重跑/账户/ECS，main skip-ci不等于CI/CD成功；下一建议是
完整运行时的本地只读影子接线，尚未启动，当前不需等待行情积累。

此前状态（2026-10-15）：**学习架构审查完成，复现虚拟评分与持仓账差额。**
五项未改C++的合成诊断确认换桶成本/收益归因口径不同；研究账与运行调权
不是同一执行闭环，普通策略edge仍为启发式，不是已校准的条件期望收益。
见[证据、边界与最小纠偏计划](docs/reviews/2026-10-15-learning-architecture-review.md)。
诊断不是修复；最近BTC毛收益为负且不调用该评分，旧经济否决不变。
本轮0回测/训练/部署/账户，G4未达成、候选0、Demo禁开仓保持。
建议下一完整成果为同语义收益/执行的最小合成闭环，尚未实施，不再先换信号。

此前状态（2026-10-15）：**BTC老龄输出六路径冻结评价完成，经济否决，批次关闭。**
选中机制2025基础/压力净−6.834493%/−7.982139%，压力回撤8.042690%触发
硬停止；价格对照正收益，新机制增益为负，毛价格损益也为负。见
[完整比较及关闭复盘](docs/reviews/2026-10-15-btc-old-output-comparison-result.md)。
用户接受的对照身份修正只用于完成比较，未改变模型/风险/收益门槛。
66相关测试与本次12独立账通过（含训练共44账），0重训/新取数/账户/ECS；
NO_GO_RISK，CLOSED_NO_GO，G4未达成、候选0、Demo禁开仓保持。
不进入长期观察、不改延迟挑赢家、不自动续参数或新机制；以下为历史截面。

此前状态（2026-10-15）：**BTC老龄输出输入与训练完成，2025评价被对照资格阻断。**
16向量中选中机制训练基础/压力净+6.560838%/+4.862679%，压力回撤5.600206%，
13周期且全为多头；只能称训练现象，尚未证明方向。四个价格对照全部硬停止，
按原合同NO_GO_RISK，2025评价0条，不是新机制已被经济否定。见
[结果与比较设计复盘](docs/reviews/2026-10-15-btc-old-output-training-result.md)。
建议只澄清对照的无晋升实验身份，固定六路径完成后段，0重训/新取数；这是
待答复的合同边界决定，不自动执行。47新测试/9旧回归及32独立账通过，
5GET输入成功；G4未达成、候选0、Demo禁开仓不变，不进入长期市场等待。

此前状态（2026-10-15）：**快速方向实验提案已交付，尚未回测，新链上输入待验收。**
按用户“实验阶段先判断方向再打磨”的要求，两机制评审选出BTC老龄输出支出：
复用2021/2025价格，计划固定16向量选择后做成本后增益和延迟敏感性比较。
见[完整开发提案](docs/plans/2026-10-15-btc-old-output-direction.md)及
[评审依据与输入缺口](docs/reviews/2026-10-15-fast-mechanism-intake-result.md)。
四个链上CSV直接控制未成功，不冒称数据就绪；下一完整批次最多8工程小时，
按输入可计算／经济判断／去留三个成果推进，不先启动长期观察。当前0新收益
实验，G4未达成、候选0，旧路线关闭与Demo禁开仓保持。

此前状态（2026-10-15）：**完整ETH/SOL开发实验完成，经济否决，不进入等待期。**
9组Q1比较选中1.5／48h，连续路径基础／压力净−3.968891%／−7.803059%，
压力回撤8.109251%，延迟压力也触线；毛损益为负。96相关测试与24套独立账
通过，但不构成盈利资格。见[结果与关闭复盘](docs/reviews/2026-10-15-eth-sol-complete-economic-result.md)。
本批CLOSED_NO_GO，0新行情／账户／ECS，G4未达成、候选0、Demo禁开仓保持；
不自动等26周、续参数或换机制。已停止后的日历尾段不是持续活跃交易。

此前状态（2026-10-15）：**完整开发经济实验方案已交付，尚未启动。** 建议明确
重开一次ETH/SOL开发：复用现有全年数据，9向量选择、冻结后连续275日评价、
完整成本/退出和延迟压力、独立审计及一次经济去留。见
[完整实验方案](docs/plans/2026-10-15-eth-sol-complete-economic-experiment.md)。
20工程小时含设计，单批次；不要求基线先盈利，不重置触线路径。拟议性能
告警只作用于新策略，旧结果/运行Safety/Demo禁开仓不改。当前是方案交付，
不是获准重开或已回测；不自动等26周，G4未达成、合格候选0。

此前状态（2026-10-15）：**CFTC比特币机构持仓单机制立项评审完成，暂不进入市场实验。**
有新的原作者研究线索，但报告可知时钟、完整成本及独立确认/功效/终点未绑定；
结论为NOT_ADMITTED_COMPLETE_SCOPE_UNBOUND，不是策略已亏损。见
[立项判断与具体缺口](docs/reviews/2026-10-15-cftc-position-intake.md)。本轮24次
公开资料操作，0行情API/回测/训练/账户/部署，无后台等待；不换第二候选或
恢复旧路线。G4未达成、合格候选0、Demo禁开仓保持。

此前状态（2026-10-15）：**产品与研究规则重设计已通过限定合成验收，尚无合格策略。**
新研究以[统一规则](docs/RESEARCH_PRODUCT_POLICY.md)为入口：先验冻结可部署
策略，再验在线学习增益；不要求对照先盈利才允许开发，不默认26周等待。
40项合成回归与16组精确概率独立复算通过，仅为DESIGN_VALIDATED_ONLY。
见[交付结果](docs/reviews/2026-10-15-product-research-policy-result.md)和
[多agent圆桌](docs/reviews/2026-10-15-project-multi-agent-roundtable.md)。真实统计
功效/期限未绑定，G4未达成、候选0；未改线上或旧合同，Demo禁开仓保持。
没有重开旧路线、新回测或后台等待；下一关为具名机制的完整立项判断。

此前状态（2026-10-15）：**ETH/SOL输入与参考执行验证完成，固定基线安全撤回，
本批CLOSED_NO_GO；不进入训练或26周等待。** 用户已接受完整有界范围，见
[结果、亏损拆解和阶段复盘](docs/reviews/2026-10-15-eth-sol-baseline-result.md)。
基础／压力参考净收益−0.174340%／−0.311563%，原连续两个亏损窗口规则触发，
不是8%回撤触线；3个双腿周期、各15笔参考成交，独立账务及风险复算通过。
G0开发输入通过、G1参考一致性通过，不代表真实成交获证；G2／G3未启动，
G4未达成。51项相关回归通过，无skip，旧失败保留；没有调参救基线、部署
ECS或操作账户，Demo禁开仓保持。剩余额度不自动换第二候选或恢复旧研究。

此前状态（2026-10-09）：**外部数据评估完成，暂不采购；G4 尚未达成。**
三家供应商分别提供历史版本或发行口径能力，但目标数据包尚未完整准入。
已见 BTC／ETH 行情也不能因换数据商变为独立确认。见
[供应商比较、确认出口与采购前置条件](docs/reviews/2026-10-09-external-data-feasibility-result.md)。
CryptoQuant 技术询问已备妥但未发送；未采购／经济实验／账户操作／部署，
Demo 禁开仓保持。此前[圆桌复盘](docs/reviews/2026-10-09-stablecoin-roundtable-result.md)及[恢复原件](docs/reviews/2026-10-09-stablecoin-access-followup.md)保留，不追溯改绿。

此前 S1 稳定币批次按历史时点证据不足结案，当前两机制研究范围未重开。
USDT／USDC 分别 3237／2951 日点可读，但历史可知版本尚未绑定；经济实验 0 次，
不冒称策略亏损。见[输入裁决和复盘](docs/reviews/2026-10-09-stablecoin-development-screen-result.md)。
8 新检查／9 旧回归通过，37 旧 gate 保持；G4 未达成、候选 0、Demo 禁开仓保持。
不自动换第三机制、续预算或进入市场等待。以下为此前 S2 实测和更早截面。

此前状态：**一次 ETH 队列开发筛查已完成，固定规则触及 8% 回撤而淘汰。**
用户明确接受开发数据独立性 UNKNOWN 的新边界后，取得 702 个历史版本与完整
小时行情，执行唯一经济实验。基础／双倍成本路径回撤下界 8.1177%／8.1757%，
独立 Decimal 复算一致。见[实测结果](docs/reviews/2026-10-09-eth-queue-development-screen-result.md)。
不调参续跑，不进入市场等待；G4 未达成，Demo 禁开仓保持。

以下为此前截面：**G0 未通过，选 0，G1 至 G4 未启动；本轮不是等待市场时间。**
最新[ETH 队列输入核对](docs/reviews/2026-10-09-eth-queue-input-admission-result.md)
已取得 702 个版本的部署／公开评论时间记录，完成 9 个正文控制和 ECS 只读取证。
用户已确认没有备份，本轮按历史使用记录无法核全结案；不再等待备份，也不把
缺记录当独立确认资格。见[结案状态](docs/CURRENT_STATE.md)。不自动续研究、
换机制或启动长期观察；未部署交易服务或解除 Demo 禁开仓。
以下为此前 G0 与历史补证截面。
用户已批准[有界重启](docs/plans/2026-10-08-perpetual-g0-restart.md)，固定检查
稳定币实际发行及 ETH 验证者退出。取得部分真实输入，但历史事件／可知时点
与独立确认出口未绑定；见[具体结果](docs/reviews/2026-10-08-perpetual-g0-restart-result.md)。
总 32 小时／5 主动工程工作日上限没有自动续批含义，不是短期盈利承诺。
旧稀疏反转路线及 16 小时／25 向量提案仍关闭，不新增训练或半年观察。
随后[只读补证](docs/reviews/2026-10-08-perpetual-input-diagnosis.md)找到 1226 日
队列历史及两个旧快照，完成单位差异／缺日审计；仍未获得 G0 或独立确认资格。

历史线索保留：冻结模型2021年363日基础+12.0684%、双倍成本+10.0978%，但自身
压力统计下界仍负，原安全资格仍拒绝，合格候选为0。见[完整历史结果](docs/reviews/2026-10-01-frozen-history-transfer-result.md)、
[当前状态](docs/CURRENT_STATE.md)及[离线工程资产](docs/OFFLINE_ENGINEERING_FREEZE.md)。
Demo 禁开仓由 `f6cc4ea` 上线，保留采集、对账和风险减仓；旧失败记录不改。
本批仅研究资料与诊断工具交付，不触发交易服务部署，不以材料通过冒称经济资格。下方
“已覆盖”仅指需求文档覆盖，不代表经济或当前运行验收通过。

## 需求符合性核对表

| 用户核心要求                 | 文档落点                              | 覆盖情况  |
| ---------------------- | --------------------------------- | ----- |
| 标的：BTC/ETH/SOL（主流币）    | 首页项目目标、PRD范围、默认参数                 | ✅ 已覆盖 |
| 交易品类：永续合约              | 项目目标、PRD范围、模块设计（合约规则/资金费率/标记价）    | ✅ 已覆盖 |
| 风险可控、避免爆仓/清零           | 风控硬规则：逐仓、杠杆白名单、强平距离阈值、回撤三阈值、断连只减仓 | ✅ 已覆盖 |
| 最大回撤目标：20%（中等风险）       | PRD KRI、默认参数附录、风控设计               | ✅ 已覆盖 |
| 不承诺稳定收益，但强调可验证正期望      | 项目目标、KPI/KRI                      | ✅ 已覆盖 |
| 自进化（可控范围：策略权重/参数）      | 产品目标、KRI自进化验收、SDD Bandit/Regime   | ✅ 已覆盖 |
| 智能/AI筛选币对（多币种扩展）       | PRD范围、SAD模块设计、TRD接口设计             | ✅ 已覆盖 |
| 闸门与回滚（回测→纸交易→小实盘、自动回滚） | PRD 1.3.5/1.5、SDD 3.10、工程验收用例     | ✅ 已覆盖 |
| 语言偏好：C++               | SDD 技术栈、线程模型、Repo结构               | ✅ 已覆盖 |
| 可落地可验收（含工程验收）          | PRD KPI/KRI + 工程验收 + 指标规范 + 用例模板  | ✅ 已覆盖 |

---

## 📚 文档导航

### 1. 核心设计
- **[需求与验收标准 (PRD)](docs/需求描述.md)**: 详细的项目目标、KRI/KPI 指标及验收标准。
- **[系统架构设计 (SAD)](docs/架构设计.md)**: 系统工作原理、核心组件、架构视图、技术选型与可靠性设计。
- **[系统详细设计 (TRD)](docs/详细设计.md)**: 模块接口、状态机、线程模型、存储一致性与测试方案。
- **[数据与事件规范 (Specs)](docs/规范约束.md)**: 核心事件字段定义、幂等性设计与 C++ 结构参考。

### 2. 策略与风控
- **[交易策略 (Strategy)](docs/交易策略.md)**: Trend/MeanRev/VolTarget 策略逻辑、Regime 识别与 Bandit 自进化。
- **[风险控制与执行 (Risk & Execution)](docs/风险控制.md)**: **(核心)** 硬风控状态机、熔断机制、OMS 逻辑与 Gate 流程。

### 3. 工程落地
- **[开发指南 (Developer)](docs/开发指南.md)**: 环境搭建、编译构建、模拟器运行指南。
- **[配置手册 (Configuration)](docs/配置手册.md)**: 系统配置模板、风控参数硬约束说明。
- **[实现验收检查清单](docs/验收清单.md)**: P0 设计修复项与闭环保障的开发/测试打勾清单。
- **[AI 自闭环实现路径](docs/实现路径.md)**: 先跑通闭环、再基于报告优化的执行路线图。
- **[数据加速链路](docs/数据加速链路.md)**: 归档下载、增量同步、缺口回补、特征构建与 walk-forward 回测。
- **[全工程重构蓝图](docs/重构蓝图.md)**: 前瞻性重构路线、目标架构与阶段计划。

---

## 🛡️ 关键工程原则 (Must Follow)

### 硬边界（不可学习、不可越权）
- 逐仓强制（Isolated only）
- 杠杆白名单：BTC/ETH ≤ 3x；SOL ≤ 2x；event_window/EXTREME → 1x
- 回撤三阈值（账户级）：8%降档 / 12%强减仓+冷却 / 20%熔断（经济意义全平）+冷却+分段恢复
- 强平距离（单仓最小值）：<8% 只减仓；有持仓但风险数据未知时禁止加仓；P95 仅作历史诊断

### 自进化范围（可控）
- 允许进化：策略权重、策略参数（如 Trend/MeanRev/VolTarget 参数）
- 不允许进化：硬风控阈值与硬规则
- Gate 必须满足最小活跃度（信号与成交门槛），防止“零交易通过”
- 所有进化必须走 Gate：回放回归 → 模拟盘撮合回归 → Shadow →（可选）小实盘

## 🚀 快速开始

请参考 [docs/开发指南.md](docs/开发指南.md) 进行环境配置与构建。

### 项目结构概览
```text
ai-trade/
├── CMakeLists.txt       # CMake 构建入口
├── Makefile             # 统一工程命令入口（qa/build/test）
├── apps/
│   └── ai_trade_web/    # 控制平面服务（phase-1 只读）
├── build/               # 构建产物目录 (cmake build)
├── config/              # 配置文件 (yaml)
├── data/                # 本地回测数据/缓存
├── docs/                # 项目文档
├── src/
│   ├── core/            # 基础类型与日志
│   ├── market/          # 行情输入
│   ├── universe/        # 智能/AI筛币（最小可运行实现）
│   ├── strategy/        # 策略引擎
│   ├── risk/            # 风控引擎
│   ├── execution/       # 执行引擎
│   ├── oms/             # 账户状态/订单处理
│   ├── monitor/         # Gate/活跃度/闭环监控
│   └── system/          # 交易系统编排
├── tests/               # 基础集成测试 (CTest)
└── tools/               # 模拟器、回放工具
```

### 本地运行骨架

当前请使用[离线工程回归入口](docs/OFFLINE_ENGINEERING_FREEZE.md#工程回归入口)，经验证门禁完成构建和合成测试。不要直接启动 `trade_bot`、覆盖绑定历史证据的旧 `build/`，或借新状态文件重跑已关闭策略。以下 Docker/运行说明是通用能力说明，不代表当前发布或交易授权。

### 工程质量门禁（推荐）
```bash
# 一键执行：编译 + 测试 + compose 配置校验 + 报告契约校验
make qa

# 或直接执行脚本
tools/quality_gate.sh
```

### Docker 运行（推荐统一编译环境）
```bash
# 构建镜像（构建阶段会自动执行 ctest）
docker build -t ai-trade:latest .

# 回放模式
docker run --rm \
  -v "$(pwd)/config:/app/config:ro" \
  -v "$(pwd)/data:/app/data" \
  ai-trade:latest --config=config/bybit.replay.yaml --exchange=bybit
```

使用 `docker compose`：
```bash
# 首次请先准备 .env（AK/SK 注入）
cp .env.example .env
# 编辑 .env，填入对应环境的 Bybit AK/SK

docker compose up --build ai-trade
```

`ai-trade-web` 控制平面（phase-C）：
```bash
# 本地启动控制平面 API（读取 reports/models/config）
docker compose --profile web up --build ai-trade-web
# 默认访问: http://localhost:8080/docs
```

开启 phase-C 治理写入能力（谨慎）：
```bash
export AI_TRADE_WEB_ENABLE_WRITE=true
export AI_TRADE_WEB_ADMIN_TOKEN=CHANGE_ME_STRONG_TOKEN
docker compose --profile web up -d ai-trade-web
```

说明：
- 写入接口需要请求头 `X-Admin-Token` 与环境变量令牌一致。
- 默认发布保护会校验 latest 报告为 `PASS` 才允许发布配置。
- 高风险发布默认启用双人审批与冷却：
  - `AI_TRADE_WEB_HIGH_RISK_TWO_MAN_RULE=true`
  - `AI_TRADE_WEB_HIGH_RISK_REQUIRED_APPROVALS=2`
  - `AI_TRADE_WEB_HIGH_RISK_COOLDOWN_SECONDS=180`
- 发布流程：`Preview -> Approve -> 输入 confirm phrase -> Publish`。
- 闭环产物回收默认按 3 天窗口回收（`CLOSED_LOOP_GC_MAX_AGE_HOURS=72`），并叠加数量上限：
  - `CLOSED_LOOP_GC_KEEP_RUN_DIRS`
  - `CLOSED_LOOP_GC_KEEP_DAILY_FILES`
  - `CLOSED_LOOP_GC_KEEP_WEEKLY_FILES`
  - `CLOSED_LOOP_GC_MAX_AGE_HOURS`
  - `CLOSED_LOOP_GC_LOG_FILE` / `CLOSED_LOOP_GC_LOG_MAX_BYTES` / `CLOSED_LOOP_GC_LOG_KEEP_BYTES`
- scheduler 每轮闭环后会执行 Docker 层垃圾回收（控制 `/var/lib/containerd` 增长）：
  - `DOCKER_GC_ENABLED`
  - `DOCKER_GC_UNTIL`（默认 `72h`，即仅保留最近 3 天）
  - `DOCKER_GC_PRUNE_IMAGES` / `DOCKER_GC_PRUNE_BUILD_CACHE`
  - `DOCKER_GC_KEEP_RECENT_TAGS`（按仓库额外保留最近 N 个镜像 tag，默认 `8`）
  - `DOCKER_GC_KEEP_REPO_MATCHERS`（按仓库名子串匹配清理范围，默认 `ai-trade,ai-trade-research,ai-trade-web`）
  - 默认不清理 volume（`DOCKER_GC_PRUNE_VOLUMES=false`）

可观测性（Prometheus + Grafana + Loki + Promtail）：
```bash
# 启动监控栈（obs profile）
docker compose --profile obs up -d loki promtail prometheus grafana

# 查看状态
docker compose ps
```

默认访问：
- Grafana: `http://<host>:3000`（默认账号密码见 `.env` 中 `GRAFANA_ADMIN_USER/PASSWORD`）
- Prometheus: `http://<host>:9090`
- Loki: `http://<host>:3100`

安全默认值（生产建议）：
- 监控端口默认仅绑定本机回环（`127.0.0.1`）；若需公网访问，请显式设置：
  - `GRAFANA_BIND_ADDRESS=0.0.0.0`
  - `PROMETHEUS_BIND_ADDRESS=0.0.0.0`
  - `LOKI_BIND_ADDRESS=0.0.0.0`
- `GRAFANA_ADMIN_PASSWORD` 必须改为强密码，并配合安全组白名单。

预置仪表盘：
- `ai-trade Runtime`（已自动导入）
- `ai-trade Metrics Runtime (Prometheus)`（已自动导入）
- 关键监控项：`equity/drawdown/notional`、`funnel_window.fills`、`throttle_total.hit_rate`、`Gate pass/fail`、`SELF_EVOLUTION_ACTION`、`OMS_RECONCILE_AUTORESYNC`。

生产编排下启用监控栈：
```bash
AI_TRADE_IMAGE=ghcr.io/<owner>/ai-trade:<tag> \
docker compose -f docker-compose.prod.yml --profile obs up -d ai-trade loki promtail prometheus grafana
```

生产编排下切换运行配置（不改 compose 文件）：
```bash
cd /opt/ai-trade
set -a && source .env.runtime && set +a
export AI_TRADE_CONFIG_PATH=config/bybit.demo.s5.yaml
docker compose -f docker-compose.prod.yml --env-file .env.runtime up -d ai-trade
```

生产编排下启动自闭环常驻服务（scheduler + watchdog）：
```bash
cd /opt/ai-trade
set -a && source .env.runtime && set +a
export AI_TRADE_PROJECT_DIR=/opt/ai-trade
export AI_TRADE_ENV_FILE=.env.runtime
docker compose -f docker-compose.prod.yml --env-file .env.runtime up -d ai-trade watchdog scheduler
```

数据加速链路（归档 + 增量 + 回补 + 特征 + 回测）：
```bash
tools/closed_loop_runner.sh data \
  --compose-file docker-compose.prod.yml \
  --env-file .env.runtime \
  --data-config config/data_pipeline.yaml
```

### GitHub Actions + 阿里云 ECS 自动部署
适用场景：主分支合并后自动构建镜像并发布到 ECS，提升迭代效率与一致性。

新增文件：
- `.github/workflows/ci.yml`：PR / 非 main 分支编译与测试
- `.github/workflows/cd.yml`：main 分支自动构建镜像并部署 ECS
- `.github/workflows/smoke.yml`：CD 成功后自动执行部署后短窗口健康验收，也支持手动触发
- `docker-compose.prod.yml`：生产部署编排（使用预构建镜像）
- `deploy/ecs-deploy.sh`：远端部署脚本（健康检查 + 闭环门禁失败自动回滚）

首次准备（ECS）：
```bash
sudo mkdir -p /opt/ai-trade/data
cd /opt/ai-trade

# 运行时密钥文件（仅示例，按需填写）
cat > .env.runtime <<'EOF'
AI_TRADE_IMAGE=ghcr.io/<owner>/ai-trade:<tag>
AI_TRADE_BYBIT_DEMO_API_KEY=your_demo_key
AI_TRADE_BYBIT_DEMO_API_SECRET=your_demo_secret
EOF
```

GitHub Secrets（仓库 Settings -> Secrets and variables -> Actions）：
- `ECS_HOST`：ECS 公网 IP
- `ECS_USER`：SSH 用户（如 `root` / `ubuntu`）
- `ECS_SSH_KEY`：SSH 私钥
- `ECS_PORT`：SSH 端口（可选，默认 22）
- `ECS_HOST_FINGERPRINT`：SSH 主机指纹（可选，建议配置；用于校验远端主机身份，格式示例：`SHA256:...`）
- `GHCR_USER`：GHCR 拉镜像账号（可选，私有镜像建议配置）
- `GHCR_TOKEN`：GHCR 拉镜像令牌（可选，私有镜像建议配置）

GitHub Variables（可选，用于覆盖 CD 强闭环默认值）：
- `CLOSED_LOOP_ENFORCE`（默认 `true`）
- `CLOSED_LOOP_ACTION`（默认 `assess`）
- `CLOSED_LOOP_STAGE`（默认 `DEPLOY`）
- `CLOSED_LOOP_SINCE`（默认 `30m`）
- `CLOSED_LOOP_MIN_RUNTIME_STATUS`（默认空，使用阶段内置阈值）
- `CLOSED_LOOP_OUTPUT_ROOT`（默认 `/opt/ai-trade/data/reports/closed_loop`）
- `CLOSED_LOOP_STRICT_PASS`（默认 `true`）
- `DEPLOY_SERVICES`（默认 `ai-trade watchdog scheduler`）
- `GATE_DEFER_SERVICES`（默认 `watchdog scheduler`，表示门禁通过后再启动的服务）
- `DOCKER_LOG_MAX_SIZE`（默认 `20m`，容器日志轮转大小）
- `DOCKER_LOG_MAX_FILE`（默认 `5`，容器日志轮转文件数）

触发方式：
- `CI`：PR 和非 main 分支 push 自动触发
- `CD`：push 到 `main` 或手动 `workflow_dispatch` 触发
- `Closed Loop Smoke`：CD 成功后自动触发，或手动触发；用于 5-15 分钟部署冒烟，不替代 4h/12h/24h S5 验收；报告写入 `/opt/ai-trade/data/reports/closed_loop_smoke`
- `Closed Loop Research`：与发布链解耦；每天运行一次 `research`，或手动选择 `research/assess/train/full/data`。`research` 只产出 Alpha 观察证据，不注册模型、不激活、不重启
- `Full`：仅在已有候选需要资格验收时手动触发；发布链固定为 `CD -> Smoke`，不再因运维改动自动运行长周期模型实验
- `Closed Loop Smoke` artifact 额外包含 `deploy_freshness.json`，用于核对当前 `ai-trade` 容器镜像 tag、启动时间、`boot_id/startup_utc` 是否与本次部署一致

当前状态、研究 Go/Stop 门槛和下一阶段权限以 [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md) 为唯一人工维护基线。

发布结果：
- 新镜像 tag：`ghcr.io/<owner>/ai-trade:<commit_sha>`
- 部署脚本会写入 `AI_TRADE_IMAGE` 到 `/opt/ai-trade/.env.runtime`
- 部署脚本默认会拉起闭环核心服务：`ai-trade + watchdog + scheduler`
- 门禁执行顺序为：先停 `GATE_DEFER_SERVICES`，仅启动非 defer 服务做门禁；门禁通过后再启动 defer 服务
- 启动健康检查失败时自动回滚到上一个镜像
- 健康检查通过后会立即执行闭环 `assess` 门禁，且仅当以下两项都为 `PASS` 才放行：
  - `data/reports/closed_loop/latest_runtime_assess.json` 的 `verdict`
  - `data/reports/closed_loop/latest_closed_loop_report.json` 的 `overall_status`
- CD 会自动上传部署门禁产物为 artifact：`deploy-closed-loop-<run_id>`

Bybit 回放配置示例：
```bash
./build/trade_bot --config=config/bybit.replay.yaml
```

Bybit 实盘最小闭环（Public WS 行情 + Private WS 成交 + REST 下单/撤单，需将 `system.mode` 设为 `paper` 或 `live`）：
```bash
# 推荐：按环境分离密钥（testnet）
export AI_TRADE_BYBIT_TESTNET_API_KEY=your_testnet_key
export AI_TRADE_BYBIT_TESTNET_API_SECRET=your_testnet_secret
./build/trade_bot --config=config/bybit.paper.yaml --exchange=bybit
```

Bybit Demo Trading（主网模拟盘）示例：
```bash
export AI_TRADE_BYBIT_DEMO_API_KEY=your_demo_key
export AI_TRADE_BYBIT_DEMO_API_SECRET=your_demo_secret
./build/trade_bot --config=config/bybit.demo.yaml --exchange=bybit
```

当前生产路径固定处于 Demo 孵化阶段：运行容器只注入 Demo/Testnet 密钥，
Bybit 主网实盘连接在适配器中硬性禁用。S5 Closed Loop 会把跨运行证据累积到
`data/models/demo_incubation_state.json`，并输出
`data/reports/closed_loop/latest_demo_incubation_report.json`。只有连续 14 天、
至少 7 个交易日/100 个完整闭合交易、成本后收益置信下界为正、自学习更新稳定且
无硬安全事件时，报告才会标记为 `ELIGIBLE_FOR_MANUAL_LIVE_TEST_REVIEW`；该状态
仍不会自动开启实盘。自进化分桶权重会校验后原子持久化到
`data/self_evolution_weights_v1.tsv`；配置或程序版本变化时不继承旧权重，进程重启后
未成功恢复权重也不能通过 Demo 孵化门槛。

稳态参数模板（更低频、更强防抖）：
```bash
# Demo 稳态模板
./build/trade_bot --config=config/bybit.demo.stable.yaml --exchange=bybit --run_forever

# Paper/Testnet 稳态模板
./build/trade_bot --config=config/bybit.paper.stable.yaml --exchange=bybit --run_forever
```

Demo 自进化保守模板（先跑这个再逐步放开）：
```bash
./build/trade_bot --config=config/bybit.demo.evolution.yaml --exchange=bybit --run_forever
```

运行控制示例：
```bash
# 持续运行（默认：system.max_ticks=0）
./build/trade_bot --config=config/bybit.demo.yaml --exchange=bybit --run_forever

# 仅运行固定 tick 数后退出
./build/trade_bot --config=config/bybit.demo.yaml --exchange=bybit --max_ticks=500
```

Docker 下运行 paper/demo 示例：
```bash
# 读取 .env 中的 AK/SK（不再临时 -e 注入）
docker compose run --rm ai-trade --config=config/bybit.paper.yaml --exchange=bybit

# 稳态模板示例
docker compose run --rm ai-trade --config=config/bybit.demo.stable.yaml --exchange=bybit --run_forever

# 自进化保守模板示例
docker compose run --rm ai-trade --config=config/bybit.demo.evolution.yaml --exchange=bybit --run_forever
```

离线研究（R1/R2）示例：
```bash
# R0: 抓取 Bybit 历史 K 线（本机 Python）
python3 tools/fetch_bybit_kline.py \
  --symbol=BTCUSDT \
  --interval=5 \
  --category=linear \
  --bars=5000 \
  --output=./data/research/ohlcv_5m.csv

# R0: 抓取 Bybit 历史 K 线（Docker）
docker compose run --rm --entrypoint python3 ai-trade-research \
  tools/fetch_bybit_kline.py \
  --symbol=BTCUSDT \
  --interval=5 \
  --category=linear \
  --bars=5000 \
  --output=./data/research/ohlcv_5m.csv

# R1: Miner（C++）
docker compose run --rm ai-trade \
  --run_miner \
  --miner_csv=./data/research/ohlcv_5m.csv \
  --miner_top_k=10 \
  --miner_generations=4 \
  --miner_population=32 \
  --miner_elite=8 \
  --miner_output=./data/research/miner_report.json

# R2: Integrator（Python + CatBoost）
docker compose run --rm ai-trade-research \
  --csv=./data/research/ohlcv_5m.csv \
  --miner_report=./data/research/miner_report.json \
  --output=./data/research/integrator_report.json \
  --model_out=./data/models/integrator_latest.cbm
```

自动闭环（训练/验收/汇总报告）：
```bash
# train: R0 + R1 + R2 + 模型注册 + 汇总报告
tools/closed_loop_runner.sh train

# assess: 部署后短窗口冒烟（不要求成交）
tools/closed_loop_runner.sh assess --stage SMOKE --since 15m

# assess: 运行态长窗口验收（S3/S5）+ 汇总报告
tools/closed_loop_runner.sh assess --stage S5 --since 4h

# full: train + assess
tools/closed_loop_runner.sh full --stage S5 --since 4h

# 产物回收（按数量保留 + 可选日志截断）
tools/recycle_artifacts.sh \
  --reports-root ./data/reports/closed_loop \
  --keep-run-dirs 120 \
  --keep-daily-files 120 \
  --keep-weekly-files 104
```

生产环境建议通过 `scheduler` 常驻执行闭环：
```bash
cd /opt/ai-trade
set -a && source .env.runtime && set +a
export AI_TRADE_PROJECT_DIR=/opt/ai-trade
export AI_TRADE_ENV_FILE=.env.runtime
# 可选：加速验证（每小时跑一次 assess/full）
# export SCHEDULER_ACTION=assess
# export SCHEDULER_INTERVAL_SECONDS=3600
docker compose -f docker-compose.prod.yml --env-file .env.runtime up -d scheduler watchdog
```

`train/full` 现在包含两道前置门禁：
- `D1 baseline freeze`：冻结当前 active 模型快照（输出 `baseline_report.json`）
- `D2 data quality gate`：训练前校验 CSV 质量（输出 `data_quality_report.json`）

闭环产物目录：
- 每次运行：`data/reports/closed_loop/<UTC_RUN_ID>/`
- 最新软链接：`data/reports/closed_loop/latest`
- 总结报告：`data/reports/closed_loop/latest/closed_loop_report.json`
- 策略原始边际诊断：`data/reports/closed_loop/latest/strategy_diagnose_report.json`
- 固定入口（无需解析软链接）：
  - `data/reports/closed_loop/latest_closed_loop_report.json`
  - `data/reports/closed_loop/latest_runtime_assess.json`
  - `data/reports/closed_loop/latest_run_meta.json`
  - `data/reports/closed_loop/latest_run_id`
  - `data/reports/closed_loop/latest_daily_summary.json`
  - `data/reports/closed_loop/latest_weekly_summary.json`

回收策略（默认开启）：
- `closed_loop_runner.sh` 每次结束后自动执行 `tools/recycle_artifacts.sh`
- 默认保留：
  - 最近 `120` 个 run 目录
  - 最近 `120` 份 `daily_*.json`
  - 最近 `104` 份 `weekly_*.json`
  - 最近 `72h` 产物（`CLOSED_LOOP_GC_MAX_AGE_HOURS=72`）
- 可通过环境变量覆盖：
  - `CLOSED_LOOP_GC_ENABLED`
  - `CLOSED_LOOP_GC_KEEP_RUN_DIRS`
  - `CLOSED_LOOP_GC_KEEP_DAILY_FILES`
  - `CLOSED_LOOP_GC_KEEP_WEEKLY_FILES`
  - `CLOSED_LOOP_GC_MAX_AGE_HOURS`
  - `CLOSED_LOOP_GC_LOG_MAX_BYTES`
  - `CLOSED_LOOP_GC_LOG_KEEP_BYTES`

手动重建日报/周报摘要（通常不需要，闭环脚本会自动生成）：
```bash
python3 tools/build_periodic_summary.py \
  --reports-root ./data/reports/closed_loop \
  --out-dir ./data/reports/closed_loop/summary
```

报告重点（账号盈亏）：
- `account_outcome.first_sample_utc` / `account_outcome.last_sample_utc`
- `account_outcome.first_equity_usd`
- `account_outcome.last_equity_usd`
- `account_outcome.equity_change_usd`
- `account_outcome.equity_change_pct`
- `account_outcome.day_start_equity_usd`
- `account_outcome.equity_change_vs_day_start_usd`
- `account_outcome.equity_change_vs_day_start_pct`
- `account_outcome.max_equity_usd_observed`
- `account_outcome.peak_to_last_drawdown_pct`
- `account_outcome.max_drawdown_pct_observed`

`runtime.warn_reasons` 中的 Gate 失败率告警已按“运行态影响”收敛：
- 仅当 Gate 失败率偏高且窗口内出现 `reduce_only/gate_halted/trading_halted` 时才给黄灯，
- 纯“低活跃但未触发限制”的情况不再误报黄灯。

ECS 一键验收（已部署 `docker-compose.prod.yml` 场景）：
```bash
cd /opt/ai-trade
set -a && source .env.runtime && set +a
tools/closed_loop_runner.sh assess \
  --compose-file docker-compose.prod.yml \
  --env-file .env.runtime \
  --stage S5 \
  --since 4h
cat data/reports/closed_loop/latest/closed_loop_report.json
cat data/reports/closed_loop/latest_daily_summary.json
cat data/reports/closed_loop/latest_weekly_summary.json
```

定时闭环（推荐：一键安装 cron）：
```bash
cd /opt/ai-trade
ops/cron/install_closed_loop_cron.sh install \
  --repo-dir /opt/ai-trade \
  --compose-file docker-compose.prod.yml \
  --env-file .env.runtime \
  --stage S5
```

说明：安装脚本会额外生成 `cleanup` 定时任务，默认每天回收一次历史报告，并对 `cron.log` 做按大小截断（输出到 `data/reports/closed_loop/gc.log`）。

仅保留 assess 定时任务（禁用 full）：
```bash
ops/cron/install_closed_loop_cron.sh install --disable-full
```

卸载定时任务：
```bash
ops/cron/install_closed_loop_cron.sh uninstall
```

模板仍保留：`ops/cron/closed-loop.cron.example`。

GitHub Actions 定时闭环：
- 工作流：`.github/workflows/closed-loop.yml`
- 支持 `workflow_dispatch` 手动触发（`train/full/assess`）
- 部署冒烟工作流：`.github/workflows/smoke.yml`，CD 成功后自动运行 `SMOKE` 阶段，报告根目录为 `data/reports/closed_loop_smoke`，避免覆盖长周期 S5 的 `latest` 指针；artifact 同步输出 `deploy_freshness.json`。
- 默认每 6 小时在 ECS 执行一次 `assess` 并输出闭环报告。
- `train` 只产出训练报告，不会刷新 `latest_*` 运行验收指针（避免覆盖最近一次可发布验收结果）。

说明：默认采用“WS优先、失败自动回退 REST”。
- 行情通道：`public_ws_enabled=true`，失败可回退 `/v5/market/tickers`
- 成交通道：`private_ws_enabled=true`，失败可回退 `/v5/execution/list`
- Docker 构建默认启用 `Boost.Beast` WebSocket 后端，规避部分环境下 `libcurl` 不支持 `wss` 的握手失败问题。

执行防抖参数（`execution`）：
- `min_order_interval_ms`：同一 symbol 最小下单间隔（毫秒）
- `reverse_signal_cooldown_ticks`：反向信号冷却 tick 数
- `min_rebalance_notional_usd`：最小调仓名义阈值（小于阈值不下单）

策略防抖参数（`strategy`）：
- `signal_deadband_abs`：价格变化绝对死区
- `signal_deadband_bps`：价格变化相对死区（bps，配置后优先于 `signal_deadband_abs`，更适合多币种）
- `min_hold_ticks`：反向信号最小持有 tick
- `signal_notional_usd`：信号输出目标名义值

运行态观测（`system`）：
- `status_log_interval_ticks`：周期状态日志频率（日志前缀：`RUNTIME_STATUS`）
- `max_ticks`：运行 tick 上限（`0` 表示不设上限）
- `reconcile.mismatch_confirmations`：连续对账不一致确认次数（避免单次抖动误熔断）

本地闭环演示（mock 行情/成交）：
```bash
./build/trade_bot --exchange=mock
```

## 📅 最小落地路径
1) 先做“可观测骨架”：配置/日志/指标/审计事件落地
2) 打通交易闭环：交易所适配 → OMS → 风控 → 执行（先支持 BTC/ETH）
3) 上线最小策略闭环：Feature → Trend + VolTarget → Portfolio → Risk → Execution
