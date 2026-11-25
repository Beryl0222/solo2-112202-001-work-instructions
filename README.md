# 工艺卡例外与在制批次的作业许可

业务资料使用虚构编号和合成叙述。文本保持来件拼写和措辞。各表是独立的来源记录。
TSV 文件采用 UTF-8，首行为列名。行首 id 是该表的稳定记录号，空格和标点属于原文，空单元格表示来件未填。
日期时间采用所在地本地时间。资料内注明为编号列表的字段用竖线分隔。

## 资料字段

- `materials/cards.tsv`。card 是卡片修订号，parent 是本卡上承接范围的段号，空值表示没有父段。authority 是来文单位。effective 为车间本地时间。
- `materials/tickets.tsv`。物件共享 assembly 时一起转合流。part 是物件在构件中的安装位置，station 记录工位和工段。
- `materials/events.tsv`。事件按 at 的车间本地时间录入。piece 关联物件。review 事件的 detail 含被确认或撤回的段号。
- `materials/assembly_rules.tsv`。assembly_family 是物件所属构件前缀，handoff 是工序来源规定的交接条件。

## 运行资料检查

使用 CPython 3.11.2 和标准库。资料检查仅读取输入。

```sh
python3 pack_check.py
python3 -m unittest -v
```

## 班长作业板

```sh
# 文本作业板；不传 --at 时回放资料中的最新时刻
python3 -m workshop --at 2026-10-06T10:50
# 或：python3 main.py --at 2026-10-06T10:50

# 同一结果的 JSON，便于接企业微信、看板或打印服务
python3 -m workshop board --at 2026-10-06T10:50 --json

# 单件作业与暂缓依据
python3 -m workshop piece K21 --at 2026-10-06T10:50 --json

# 历史领单原文和作业单版本
python3 -m workshop history K23 --at 2026-10-06T10:50 --json

# 质检追溯：构件 -> 物件 -> 事件 -> 当时有效的原句
python3 -m workshop trace K2-87 --at 2026-10-06T10:50 --json

# 合流前覆盖和检验冲突检查
python3 -m workshop handoff K2-87 --at 2026-10-06T10:50 --json
```

Python API：

```python
from workshop import load_workshop

shop = load_workshop("materials")
board = shop.board("2026-10-06T10:50")
trace = shop.quality_trace("K2-87", "2026-10-06T10:50")
handoff = shop.handoff_check("K2-87", "2026-10-06T10:50")

# 工艺员在内存回放中确认/撤回/否决；可接持久化事件表或接口
shop.confirm_interpretation(
    "P11", option_key="named_only", at="2026-10-06T11:00"
)
shop.withdraw_confirmation("P11", at="2026-10-06T11:05")
shop.reject_permit("P21", at="2026-10-06T11:10", reason="对象不足")
shop.record_event("K24", "完工", "乙，首遍", at="2026-10-06T11:20")
```

## 当前合成资料在 10:50 的现场结论

- `K21`（北侧弧段）：08:15 只领了甲料、尚未涂覆，因此 09:00 新版生效后必须按乙底涂施工；W05 已登记乙料首遍。W06 已开工第二遍后，W07 撤回 `P11` 解释，故第二遍作业单进入 `engineer_disposition_required`，保留 W05/W06 凭证并报工艺员处置。
- `K22`（门板）：W02 已在旧卡 A-r1 下完成甲料首遍。B11 明确允许该类件沿用甲料至第二遍，因此下一道是可领的“甲底涂第二遍”，之后仍须逐件实测膜厚；R3 禁止把北侧弧段旧料错用到门板。
- `K23`（连接件）：W03 是新版后领取的乙底涂首遍单，状态为已领未开工。W08 的原文“等待用料确认”必须原样回读；在该等待凭证解除前，不把工人自行猜料的单子标成可执行。`P21` 给 K23 的合流试配许可是膜厚要求的最小范围例外。
- `K24`（外缘）：可先执行无歧义的乙底涂首遍。`P11` 中“靠边的也可照办”是否扩大到 K24 有两种现场读法；W04 曾确认仅指北侧弧段，W07 又撤回该确认，因此只暂缓依赖旧料解释的可选第二遍，不阻止首遍继续领单。
- `K25`（南线北侧弧段）：`P11` 继承 B10 的北线/K2 范围，且 R2 明确不得用北线许可偏离南线甲料标准；K25 按南线甲料标准执行，不显示 P11 旧料路线。

## 建模边界

- 标题、父段、工位、机型和生效时间共同决定来源适用范围；只有原句出现“全单”时才扩大到整张单。
- 许可只放宽其文字和确认解释明确覆盖的工序/对象；膜厚等未提及要求仍由原卡和交接规则支持。
- 时间许可按行为发生时判断合法性。`permit_opinion(..., operation_at=..., observed_at=...)` 会同时给出发作时状态和事后状态；许可到期本身不把已依有效许可完成并登记的工序追溯为违规。
- 找不到兼容下一步时，作业单状态为 `no_compatible_next_step`，并通过 `preserved_evidence_event_id` 保留完成凭证。
- 合流检查输出每个必需件的依据、许可覆盖矩阵和 `minimum_affected_scope`。检验方式冲突只作用于明确点到的分件，其他分件仍可领单。
- `receipt_readback` 永远返回事件表中的原始 `detail`；作业单版本更新不会改写当班人员曾经领到的文字。
