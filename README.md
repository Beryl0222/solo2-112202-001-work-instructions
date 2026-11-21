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
