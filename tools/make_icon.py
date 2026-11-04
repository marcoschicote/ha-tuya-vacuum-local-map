"""Draw the integration icon: coverage cells and a cleaning path, nothing real.

    python tools/make_icon.py

Writes brand/icon.png (256 px) and brand/icon@2x.png (512 px) in the integration.
"""

import pathlib

from PIL import Image, ImageDraw

OUT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "tuya_vacuum_local_map"
    / "brand"
)
SIZE = 2048  # drawn large, then scaled down for smooth edges
BACKGROUND = (31, 52, 82, 255)
COVERAGE = (74, 144, 217)
PATH = (232, 89, 12, 255)
CELLS = 8
LEVELS = {1: 95, 2: 150, 3: 215}  # alpha by how often a cell was cleaned

# An L-shaped "home", row 0 at the top: the digit is how many runs covered the cell.
HOME = [
    "11100000",
    "12100000",
    "12200000",
    "12333321",
    "13333321",
    "12333321",
    "11222211",
    "00111110",
]


def main() -> None:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), SIZE // 5, fill=BACKGROUND)
    margin = SIZE * 0.17
    cell = (SIZE - 2 * margin) / CELLS
    for row, line in enumerate(HOME):
        for col, digit in enumerate(line):
            if digit == "0":
                continue
            x0 = margin + col * cell
            y0 = margin + row * cell
            draw.rectangle(
                (x0 + 3, y0 + 3, x0 + cell - 3, y0 + cell - 3),
                fill=(*COVERAGE, LEVELS[int(digit)]),
            )

    def centre(col: float, row: float) -> tuple[float, float]:
        return margin + (col + 0.5) * cell, margin + (row + 0.5) * cell

    route = [(1, 6), (6, 6), (6, 5), (1, 5), (1, 4), (6, 4), (6, 3), (2, 3), (2, 2),
             (1, 2), (1, 1), (1, 0.6)]  # fmt: skip
    points = [centre(c, r) for c, r in route]
    draw.line(points, fill=PATH, width=SIZE // 36, joint="curve")
    for x, y in points[:: len(points) - 1]:
        radius = SIZE // 30
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=PATH)
    x, y = points[-1]
    radius = SIZE // 17
    draw.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        fill=PATH,
        outline=(255, 255, 255, 255),
        width=SIZE // 100,
    )
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        image.resize((size, size), Image.LANCZOS).save(OUT / name, optimize=True)
        print(f"{OUT.name}/{name}: {size}px")


if __name__ == "__main__":
    main()
