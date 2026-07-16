# 车型图片素材来源

当前三张车辆卡片图片于 2026-07-16 使用 OpenAI 图像生成工具独立生成，没有上传、描摹或引用原仓库图片、厂商官网图片或其他第三方参考图。它们是无品牌标识的通用车辆渲染，不是 Volvo Cars 官方素材，也不表示任何具体车型的准确外观。

现有文件名只为保持前端车型映射兼容：

- `car-s90-black-card.webp`：通用黑色四门轿车；
- `car-xc60-black-card.webp`：通用黑色中型跨界 SUV；
- `car-xc90-black-card.webp`：通用黑色大型三排 SUV。

## 生成提示词

三张被采用的素材分别使用以下核心提示词生成：

1. “Brand-new non-branded generic premium black four-door sedan, perfectly vertical top-down orthographic view, nose up, centered and symmetrical, clean body without logos, badges, emblems, ornaments, antennas, lettering, model names, watermark, or manufacturer-specific cues; full vehicle on uniform #00ff00 chroma-key background, no floor, shadow, reflection, gradient, texture, or scenery.”
2. “Brand-new non-branded generic black midsize crossover SUV, perfectly vertical top-down orthographic view, nose up, centered and symmetrical, no logos, badges, emblems, lettering, model names, watermark, antenna ornament, or manufacturer-specific cues; full vehicle on uniform #00ff00 chroma-key background, no floor, shadow, reflection, gradient, texture, or scenery.”
3. “Brand-new non-branded generic black large three-row SUV, perfectly vertical top-down orthographic view, nose up, centered and symmetrical, broad upright generic proportions, no logos, badges, emblems, lettering, model names, watermark, antenna ornament, or manufacturer-specific cues; full vehicle on uniform #00ff00 chroma-key background, no floor, shadow, reflection, gradient, texture, or scenery.”

生成结果通过本地色键工具移除 `#00ff00` 背景并输出带 alpha 通道的 WebP。最终文件经过人工外观检查和透明通道检查；四角透明，未发现文字、水印或品牌徽标。

## 使用边界

这些图片随本仓库代码一同按 MIT 许可证提供。由于生成式图像在不同司法辖区的权利认定可能不同，复用者应结合自己的发布地区和使用方式自行评估。请勿将这些图片描述为官方素材、官方车型照片或获得 Volvo Cars 认可的内容。
