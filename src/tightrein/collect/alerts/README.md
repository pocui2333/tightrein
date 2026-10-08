# 业务告警(collect/alerts)

**只对接 Alertmanager(API v2)：Prometheus Alertmanager、Grafana 内置的 Alertmanager、Grafana Cloud 与 Mimir 用同一套
接口。没有这类监控系统的项目用不了这个模块，在接入清单里设为 disabled 即可。**

## 是什么

读取监控系统中已触发、未静默、未被抑制的告警，排除基础设施类(磁盘、内存、重启等)，其余每条告警一条信号。只收集
监控系统已经报出的告警，自己不判断、不重复实现告警规则。

## 流程

`source.collect(runtime)`：按接入清单的 `method`(`alertmanager`)读告警(`alert_source/alertmanager.py`) → 按名称正则或
标签取值排除基础设施类 → 每条告警转成信号。不按时间窗口读，没有读取位置。

## 输入与输出

- 信号：check_type 为 `alert`，location 为告警名，message 依次取注解 summary、description、message，都没有时用告警名；
  occurred_at 为告警开始时间；group_key(指纹)为 `alertmanager:<告警的 fingerprint>`；evidence 带 sourceName、severity
  标签、全部标签与注解、来源链接；
- 覆盖范围：`alertmanager`(本次读到了告警列表)。

## 配置

- 接入清单：`collect.alerts` 为 enabled，`method: "alertmanager"`；
- `sites.json`：`alertmanager.url`(Grafana 内置告警为 `<Grafana 地址>/api/alertmanager/grafana`)、可选 `alertmanager.user`；
- `secrets.json`：`alertmanager.token`(Grafana 服务账号只需 Viewer 角色；没有时不带认证)；
- `controls."collect.alerts".exclude`：`names`(告警名正则)、`labels`(标签 → 取值清单)，命中任一即视为基础设施类排除。

## 设计依据

- 只取 `active=true&silenced=false&inhibited=false` 的告警：静默与被抑制的是人已处理或由上级告警覆盖的。
- 告警自带的 fingerprint 作为平台分组编号：同一条告警规则的同一组标签每次相同，去重不必再猜。
- 平台给的开始时间带 9 位纳秒，Python 解析不了，先去掉秒以下再换成 UTC。
- 基础设施类按配置排除(名称或标签)：不同项目的标签约定不同，写在配置里而不是代码里。
- 出处：旧 `sources/alerts/`、`extensions/methods/alert_source/alertmanager.py`。

## 不做什么

- 两次运行之间触发又恢复的告警读不到(只读当前在触发的)；
- 不判断告警是否是缺陷、不定严重度(severity 标签只放进证据)。
