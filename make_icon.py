#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标 app.ico"""
import os
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 背景：蓝色圆角方块
    margin = size // 10
    bg_color = (5, 122, 171)  # #057AAB
    # 圆角矩形
    r = size // 5
    d.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=r, fill=bg_color)

    # 网络波纹（三层同心弧线，白色半透明）
    cx, cy = size // 2, size // 2 + size // 12
    for i, (alpha, width) in enumerate([(90, max(1, size // 32)),
                                        (160, max(1, size // 28)),
                                        (255, max(1, size // 22))]):
        r_outer = size // 4 + i * (size // 8)
        bbox = [cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer]
        d.arc(bbox, start=200, end=340, fill=(255, 255, 255, alpha), width=width)

    # 中心点
    dot_r = max(2, size // 14)
    d.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill="white")

    # 顶部 "HUST" 文字
    try:
        font = ImageFont.truetype("C:\\Windows\\Fonts\\arialbd.ttf", max(8, size // 7))
    except Exception:
        font = ImageFont.load_default()
    text = "HUST"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text((cx - tw // 2, margin + size // 18), text, fill="white", font=font)

    return img


def main():
    # 生成多种尺寸，ICO 会自动包含全部
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw(s) for s in sizes]
    # 第一个作为主图，其余作为额外尺寸
    imgs[0].save(OUT, format="ICO",
                 sizes=[(s, s) for s in sizes])
    print(f"图标已生成：{OUT}（{len(sizes)} 种尺寸）")

    # 同时导出 PNG 预览
    png = OUT.replace(".ico", ".png")
    draw(256).save(png)
    print(f"预览图：{png}")


if __name__ == "__main__":
    main()
