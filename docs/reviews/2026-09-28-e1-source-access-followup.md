# E1原版访问诊断与资料请求交接

范围：接续be83850后的“继续推进”，仅穷尽公开方法资料入口和准备资料请求。
0行情请求、0实验/训练/采购/账户操作、0对外消息；旧结果和全部gate不变。
本页不续研究预算、不重开E1，不将资料访问诊断写成策略资格通过。

## 本轮新增证据

| 入口 | 实际结果 | 能说明什么 |
| --- | --- | --- |
| Crossref登记的Elsevier纯文本链接 | HTTP 400；`INVALID_INPUT`，正文说明view参数不合法 | 上轮仅记录400，尚未区分请求格式与访问权限；本次补齐错误正文 |
| Elsevier全文视图（XML、`view=FULL`） | HTTP 401；`AUTHENTICATION_ERROR / Invalid API Key` | 此次匿名全文API路径受认证阻断；不是证明论文不存在、必须付费或本地代码故障 |
| ScienceDirect PDF入口 | 本次web访问HTTP 403 | 当前自动读取未取得PDF；不推断用户浏览器也必然失败 |
| OpenAlex DOI记录 | 当前仅列出版社DOI，`any_repository_has_fulltext=false` | 此索引未提供第二个全文位置；不声称互联网不存在副本 |
| Dustin Weiss公开ORCID | 无研究主页链接，作品列表为空 | 此公开记录没有可继续追踪的原文/代码入口 |
| UOW合作者官方资料及作品页 | 确认Ivy Zhou、相同ORCID和论文DOI；本论文仅链接DOI | 找到可核验的工作联系入口，没有取得独立全文/代码附件 |

访问依据：

- [Crossref登记](https://api.crossref.org/works/10.1016%2Fj.frl.2026.110340)
- [登记纯文本接口](https://api.elsevier.com/content/article/PII:S1544612326008688?httpAccept=text/plain)
- [本次全文视图请求](https://api.elsevier.com/content/article/pii/S1544612326008688?httpAccept=text%2Fxml&view=FULL)
- [出版社PDF入口](https://www.sciencedirect.com/science/article/pii/S1544612326008688/pdfft)
- [OpenAlex位置记录](https://api.openalex.org/works/https://doi.org/10.1016/j.frl.2026.110340)
- [作者公开ORCID](https://orcid.org/0009-0001-3658-5771)
- [UOW官方作者页](https://scholars.uow.edu.au/ivy-zhou)与[论文列表](https://scholars.uow.edu.au/ivy-zhou/publications)

UOW作者页ORCID为0000-0001-9880-7731，与出版登记一致；公开邮箱为
`ivy_zhou@uow.edu.au`，另列工作邮箱`izhou@uow.edu.au`。只选择前者作为拟议收件人，
不群发、不使用同名人员或联系人聚合站提供的地址。

已检查当前可用工具及插件目录：无已连接邮件通道；Gmail可用但未安装/连接。
已提供连接建议，尚未核实其发送能力。未登录、未建远端草稿、未发送邮件，
也未启动“等待作者回复”的时钟或监控。连接不是获得论文或收到回信的证明。

## 本地邮件草稿——尚未发送

To: ivy_zhou@uow.edu.au

Subject: Methods / replication materials for FRL 110340 (Bitcoin option expiration)

Dear Dr Zhou,

I am reviewing the reproducibility of your paper, “Bitcoin option expiration, gamma
exposure, and intraday price reversals” (DOI: 10.1016/j.frl.2026.110340).
Could you please share an author-approved manuscript and any available replication
code or data dictionary, or point me to their public location?

In particular, I would appreciate clarification of:

1. How the final version constructs the market-maker position proxy: trade-side
   convention, quantity/notional units, accumulation start and initial inventory,
   and treatment of block or combination trades.
2. Which option set and observation timestamp define ATM open interest, and whether
   high-OI thresholds are retrospective sample classifications or known before a
   trading decision. I do not want to confuse explanatory regressors with signals.
3. Whether the final version specifies a trading example and its exact entry,
   reversal and exit times. A publicly reposted 2024 draft appears inconsistent
   between the exit time in section 4.2 and the caption of Table 6; I have not
   verified that copy against your original.
4. Which historical trade/OI/Greek inputs are essential, their sources and units,
   and any licensing restrictions on replication materials.

Public links or a short clarification would be very helpful. I am not requesting
private account records or any paid service.

Thank you for your time.

[Sender name]

## 接续条件与停止边界

当前缺件是原版方法资料或可用的资料请求通道，不是运行满两周即可解决。
若提供原版PDF/作者实现，先核验DOI和版本，再逐项回答上面的四组问题；无法确认的
项目继续标unknown，不替作者选择平仓时间或使用全期分位数生成历史交易日。
若连接邮件，先核实可用发送能力和发件身份，再按确认范围处理本草稿；此刻不承诺
已自动发出或后台跟进。只索取方法资料，不发仓库、账户、密钥或本地证据包。

方法解释完整后，仍须检查时点可得性、数据覆盖和向Bybit永续迁移的成本口径；
这些检查不等于有盈利证据，也不自动重开关闭实验。没有新的输入时不重复同一
全文检索、生成重复结案或启动采集器。E1未准入、E2 REJECT、选0候选保持。
