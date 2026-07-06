# Volvo 原生风格统计卡设计依据

本文件记录 `custom:volvo-car-card` 的设计来源和边界。参考资料来自用户本机的 `/Users/monet/Downloads/volvo_ui_study`，只使用设计规律，不提交 APK、字体或专有图片。

## 参考结论

- Volvo Cars App 主要使用 Jetpack Compose；真实页面不在传统 XML layout 中。
- 可复用组件集中在 `com/volvocars/volvoappui/`，其中 `card`、`soc`、`batterylevel`、`cartopview`、`trip` 对本卡片最相关。
- 车辆状态页采用俯视车辆图叠加局部状态层，不是实时 3D。
- App 的行程/统计信息强调 TM、TA、续航、电量、充电状态这些可扫描数据，而不是大量装饰图形。

## 视觉令牌

来自 `volvo_ui_study/design_summary/README.md` 和 `assets_export/04_color_system`：

| 用途 | Token / Hex |
| --- | --- |
| 主文字 | `#141414` |
| 次文字 | `#707070` |
| Volvo accent blue | `#1c6eba` |
| Volvo brand blue | `#284e80` |
| 浅色高层背景 | `#ffffff` |
| 浅色中层背景 | `#fafafa` |
| 浅色低层背景 | `#f5f5f5` |
| 分隔/边框 | 约 12%-16% 黑色透明度 |
| 成功 | `#04721c` |
| 警告/错误 | `#cd2314` / `#eb7400` |

实现时优先使用 Home Assistant 主题变量作为运行时背景和文字色，Volvo token 只作为默认值和强调色。字体不嵌入 Volvo Novum、Centum 或 Sans Pro。

## 信息架构

1. 顶部：品牌、车辆昵称/车型、总里程、连接状态、车锁快捷操作。
2. 双能源续航：纯电续航与燃油续航并列，贴近原生 App 对续航的首屏优先级。
3. 能源状态：动力电池、燃油余量、充电功率、预计充满时间。
4. 俯视车辆状态：四门、四窗、引擎盖、尾门、天窗直接覆盖到车身图上；未关闭状态只用警告红。
5. 行程统计：TM 手动复位和 TA 自动复位分组展示；T8 才展示 TM 电耗。
6. 远程控制：锁、远程启动、尾门、天窗、闪灯、鸣笛闪灯；危险操作二次确认。

## Home Assistant 边界

- 卡片是 Lovelace 前端模块，资源由集成静态路径提供。
- 默认按 `{domain}.{vin}_{suffix}` 绑定实体，并允许 `entities:` 覆盖被用户改名的实体。
- `sensor.{vin}_full_charge_electric_range` 使用 `measurement` 状态类，供原生 `statistics-graph` 长期趋势卡使用。
- 本仓库不会提交从 App 提取的 `cartopview_complete_fallback.png`；脚本只在用户本机从合法 APK 提取。
