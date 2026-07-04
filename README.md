![Version](https://img.shields.io/github/v/release/Annincikee/hass-volvooncall-cn?color=green&label=Version)
[![GitHub all releases](https://img.shields.io/github/downloads/Annincikee/hass-volvooncall-cn/total?label=Downloads)](https://github.com/Annincikee/hass-volvooncall-cn/releases)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)


# Volvo On Call CN

Homeassistant volvooncall 中国区插件，通过中国版沃尔沃API连接车辆并将车辆数据和控制作为Home Assistant实体暴露。

## 功能特点

- 车辆状态监控（锁、引擎、车门、车窗等）
- 远程控制（锁定/解锁、引擎启动/停止、鸣笛、闪灯）
- 燃油和续航信息
- 纯电续航、电量和充电桩状态
- T8 满电续航采样与长期电池衰减趋势
- 车辆位置跟踪
- 车辆警告信息（保养、液位、胎压）
- 支持多车辆
- 使用gRPC高效通信

## 安装要求

- Home Assistant实例
- 沃尔沃在线账户（中国区）
- 具有联网服务的沃尔沃车辆

## HACS 安装集成

HACS -> 集成 -> 右上角三个点 -> 自定义存储库

配置集成时请选择车辆动力类型：

- `B4/B5/B6`：燃油/轻混车型，不创建动力电池、纯电续航、充电和 TM 电耗实体，也不请求电池接口。
- `T8`：插电混动车型，启用全部电池、纯电续航、充电及 TM 电耗实体。

已有配置会默认保留为 `T8` 以避免升级后实体突然消失，可在集成“配置”中修改并自动重载。
- 存储库：https://github.com/Annincikee/hass-volvooncall-cn
- 类别：集成

浏览并下载存储库 -> 搜索 Volvo On Call CN 并下载

## 手动安装

1. 从GitHub下载最新版本
2. 将文件解压到Home Assistant的`custom_components`目录
3. 重启Home Assistant

## Homeassistant 添加集成

设置 -> 设备与服务 -> 添加集成 -> 搜索品牌 Volvo On Call CN -> 填入手机号和密码
- 手机号：11 位纯数字
- 密码：即"沃尔沃APP"上的登录密码，需要提前设置好登录密码

提交稍等片刻后，即可看到拥有的车辆设备

## 内置车辆控制卡片（S90 T8 优先）

集成内置 `custom:volvo-car-card`，按最新 Sections 仪表盘的 12 列网格适配，集中显示车锁、四门、四窗、引擎盖、后备箱、天窗、燃油、电量、纯电续航、充电和常用远程控制。

Lovelace 使用“存储模式”时，集成会自动注册卡片资源。重启 Home Assistant 并强制刷新浏览器后，可直接在卡片选择器的“社区”区域添加“Volvo 车辆控制卡”。也可以手动添加：

```yaml
type: custom:volvo-car-card
vin: TESTVIN0000000001
name: S90 Polestar
model: s90_t8
show_controls: true
show_details: true
```

`vin` 不区分大小写。卡片会按 `{domain}.{vin}_{suffix}` 自动关联本集成的实体。若实体曾在 Home Assistant 中改名，可在 YAML 中覆盖单个实体：

```yaml
type: custom:volvo-car-card
vin: TESTVIN0000000001
model: s90_t8
entities:
  battery: sensor.s90_t8_battery
  electric_range: sensor.s90_t8_electric_range
  lock: lock.s90_t8_lock
```

如果 Lovelace 资源使用 YAML 模式，请手动添加模块资源：

```yaml
lovelace:
  resource_mode: yaml
  resources:
    - url: /volvooncall_cn/frontend/volvo-car-card.js?v=1.0.0
      type: module
```

### 本地 APK 车辆素材

卡片支持使用你本人合法取得的 Volvo Cars APK 中的俯视车辆素材进行本地学习。仓库不会提交或发布该专有图片。将 APK 放在仓库根目录后执行：

```bash
[removed legacy local-asset helper] base..apk
```

脚本会在本地生成：

```text
custom_components/volvooncall_cn/frontend/cartopview_complete_fallback.png
```

手动部署时，需要把该图片与 `volvo-car-card.js` 一起复制到 Home Assistant 的 `custom_components/volvooncall_cn/frontend/`。也可在卡片配置中用 `image:` 指向你自己的 `/local/...` 或 HTTP(S) 车辆俯视图，以适配其他车型。

## T8 满电续航趋势

T8 配置会创建 `sensor.{vin}_full_charge_electric_range`。集成在车辆电量第一次达到 `100%` 时记录当次服务端续航值；同一次满电停留期间不会重复采样，电量降到 `100%` 以下后才会等待下一次满电。最近样本、采样时间、累计次数和数据源会写入 Home Assistant 存储，重启或重载集成后仍保留。

该传感器使用距离设备类型和 `measurement` 状态类，可直接进入 Home Assistant Recorder 长期统计。例如：

```yaml
type: statistics-graph
title: T8 满电续航趋势
entities:
  - sensor.testvin0000000001_full_charge_electric_range
days_to_show: 365
period: month
stat_types:
  - mean
```

精度说明：主 gRPC 接口的 `estimatedDistanceToEmptyKm` 字段类型是 `int32`，因此只提供整公里；家充桩回退接口的 `estimatedDrivingKm` 若返回小数，本集成会原样保留为浮点值，不主动取整或四舍五入。

满电表显续航会受环境温度、空调负载、近期驾驶能耗和车辆估算策略影响，适合观察长期趋势，但不等同于电池管理系统的真实 SOH/可用容量。建议比较相近季节和使用条件下的多次样本，不要用单次变化直接判断电池衰减。

## 实体一览

`{vin}` 表示车架号

| ID | 名称 | 备注 |
|-----------------------------------------------|------------------|-----------------------------------------------------------|
| `lock.{vin}_lock` | 车锁 | 远程锁定或解锁车辆 |
| `binary_sensor.{vin}_engine` | 引擎 | |
| `switch.{vin}_engine_remote_control` | 远程启动 | 直接启动车辆，使用下方选择的 1–15 分钟时长 |
| `number.{vin}_engine_duration` | 远程启动持续时长 | 单位分钟，默认 5 分钟 |
| `switch.{vin}_climatization` | 温度调节 | 仅开启/关闭驻车空调，不启动车辆，不使用远程启动时长 |
| `sensor.{vin}_distance_to_empty` | 续航里程 | |
| `binary_sensor.{vin}_front_left_door` | 前左门 | 表示门是否打开 |
| `binary_sensor.{vin}_front_right_door` | 前右门 | |
| `binary_sensor.{vin}_rear_left_door` | 后左门 | |
| `binary_sensor.{vin}_rear_right_door` | 后右门 | |
| `lock.{vin}_window_lock` | 远程窗锁 | 远程开窗或关窗（新款车型支持） |
| `binary_sensor.{vin}_front_left_window_open` | 前左窗 | 表示窗是否打开, 属性`open_status_ajar`表示是否仅打开一条缝 |
| `binary_sensor.{vin}_front_right_window_open` | 前右窗 | |
| `binary_sensor.{vin}_rear_left_window` | 后左窗 | |
| `binary_sensor.{vin}_rear_right_window` | 后右窗 | |
| `sensor.{vin}_fuel_amount` | 油箱剩余油量 | |
| `binary_sensor.{vin}_hood` | 引擎盖 | 表示引擎盖是否打开 |
| `sensor.{vin}_odometer` | 总里程 | |
| `binary_sensor.{vin}_sunroof` | 天窗 | |
| `binary_sensor.{vin}_tail_gate` | 尾门 | |
| `device_tracker.{vin}_position` | 位置 | |
| `device_tracker.{vin}_position_wgs84` | 位置 wgs84 坐标 | 在 ha 默认地图上展示车辆时，请使用此实体 |
| `button.{vin}_flash` | 闪灯 | |
| `button.{vin}_honk_and_flash` | 闪灯鸣笛 | |
| `button.{vin}_honk` | 鸣笛 | |
| `switch.{vin}_sunroof_control` | 远程控制天窗 | 仅在遮阳帘已打开时支持远程打开天窗（新款车型支持） |
| `switch.{vin}_tailgate_control` | 远程控制尾箱 | 打开尾箱会同时解锁车辆,请注意及时锁车（新款车型支持） |
| `sensor.{vin}_fuel_average_consumption_liters_per_100_km` | 百公里油耗 | |
| `sensor.{vin}_tm_distance` | TM 里程 | 手动复位行程，单位 km |
| `sensor.{vin}_tm_fuel_consumption` | TM 平均油耗 | 单位 L/100km |
| `sensor.{vin}_tm_energy_consumption` | TM 平均电耗 | 单位 kWh/100km |
| `sensor.{vin}_tm_average_speed` | TM 平均速度 | 单位 km/h |
| `sensor.{vin}_ta_distance` | TA 里程 | 自动复位行程，单位 km |
| `sensor.{vin}_ta_fuel_consumption` | TA 平均油耗 | 单位 L/100km |
| `sensor.{vin}_ta_average_speed` | TA 平均速度 | 单位 km/h |
| `sensor.{vin}_battery_charge_level` | 动力电池电量 | 单位 % |
| `sensor.{vin}_electric_range` | 纯电续航里程 | 单位 km |
| `sensor.{vin}_full_charge_electric_range` | 最近满电续航 | 100% 电量时每个充电周期采样一次，单位 km，支持长期统计 |
| `sensor.{vin}_charging_status` | 充电状态 | 属性包含数据源和家充桩信息 |
| `sensor.{vin}_charger_connection_status` | 充电枪连接状态 | 属性包含数据源和家充桩信息 |
| `sensor.{vin}_estimated_charging_time` | 预计充满剩余时间 | 单位 min |
| `sensor.{vin}_charging_power` | 充电功率 | 单位 kW |
| `binary_sensor.{vin}_service_warning` | 保养警告 | |
| `sensor.{vin}_service_warning_msg` | 保养警告信息 | 无需保养、未知警告、定期保养即将到期、发动机工作时间即将需要保养、行驶里程即将需要保养、定期保养时间已到、发动机工作时间保养时间已到、行驶里程保养时间已到、定期保养已逾期、发动机工作时间保养已逾期、行驶里程保养已逾期 |
| `binary_sensor.{vin}_brake_fluid_level_warning` | 刹车液警告 | |
| `binary_sensor.{vin}_engine_coolant_level_warning` | 发动机冷却液警告 | |
| `binary_sensor.{vin}_oil_level_warning` | 机油警告 | |
| `binary_sensor.{vin}_washer_fluid_level_warning` | 玻璃水警告 | |
| `binary_sensor.{vin}_front_left_tyre_pressure_warning` | 左前胎压警告 | |
| `binary_sensor.{vin}_front_right_tyre_pressure_warning` | 右前胎压警告 | |
| `binary_sensor.{vin}_rear_left_tyre_pressure_warning` | 左后胎压警告 | |
| `binary_sensor.{vin}_rear_right_tyre_pressure_warning` | 右后胎压警告 | |

## 测试车型

- 2021 S60
- 2024 XC60

## 故障排查

如果您在使用集成时遇到问题，可以通过在`configuration.yaml`中添加以下内容来启用调试日志：

```yaml
logger:
  default: info
  logs:
    custom_components.volvooncall_cn: debug
```

## 效果预览

<img src="images/screenshot-20230729-011246.png" alt="沃尔沃仪表盘" width="50%"/>
<img src="images/screenshot-20230729-011320.png" alt="沃尔沃控制" width="50%"/>

## 特别鸣谢

- [@chliny](https://github.com/chliny) 实现了新版车机云端协议对接
