# 业务资料约定

minutes是有向边基础时间，one_way=false表示可反向；mode为出行方式。units为事项件数，people为对应人口，不能跨事项直接求和。网点pool引用共享人员资源，每件办理消耗minutes_per_unit分钟。

每行id在该表内唯一，文件顺序不代表业务事件顺序。清单schema=1，各表登记文件名、字节长度、SHA256、行数及完整字段集合，未知版本或内容变化应重新登记。金额及测量数值均为独立资料，不代表任何现实机构的经营数据。

- nodes：id。
- roads：id、source、target、mode、minutes、one_way。
- demand：id、resident_group、node、area、people、service、units。
- sites：id、node、service、pool、minutes_per_unit。
- pools：id、weekly_minutes。

normal与boundary是相互独立的资料包，不能直接拼接重复id。引用字段按本项目domain.py的REFERENCES登记。读取检查只检查完整性、身份及这些直接引用，不替代领域分析中的可识别性、时点可见性或可行性判断。
