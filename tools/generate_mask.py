#!/usr/bin/env python3
"""
Generate the Magic Mirror mask face as layered PNG sprites.
Produces: base face, eye states, mouth states, brow states.
All composited at runtime for real-time animation.
"""

from PIL import Image, ImageDraw, ImageFilter, ImageFont
import numpy as np
import os

OUT = "/home/om/openkeel/tools/mirror_assets/sprites"
os.makedirs(OUT, exist_ok=True)

W, H = 400, 500  # sprite sheet dimensions
CX, CY = 200, 220  # face center


def radial_gradient(size, center, radius, inner_color, outer_color):
    """Create a radial gradient image."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    pixels = np.zeros((size[1], size[0], 4), dtype=np.uint8)

    y, x = np.ogrid[:size[1], :size[0]]
    dist = np.sqrt((x - center[0])**2 + (y - center[1])**2) / radius
    dist = np.clip(dist, 0, 1)

    for c in range(4):
        pixels[:, :, c] = (inner_color[c] * (1 - dist) + outer_color[c] * dist).astype(np.uint8)

    return Image.fromarray(pixels, "RGBA")


def draw_base_mask():
    """Draw the theatrical mask base — metallic, shadowed, dramatic."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # Face shape gradient — dark bronze/green metallic
    face_grad = radial_gradient(
        (W, H), (CX, CY), 220,
        inner_color=(35, 55, 35, 255),   # dark green-bronze center
        outer_color=(8, 15, 8, 0),       # fades to transparent
    )
    img = Image.alpha_composite(img, face_grad)

    draw = ImageDraw.Draw(img)

    # Main face oval — slightly elongated
    face_bbox = (CX - 130, CY - 170, CX + 130, CY + 140)
    draw.ellipse(face_bbox, fill=(20, 35, 20, 240), outline=(40, 70, 40, 200), width=2)

    # Inner face highlight (forehead area)
    highlight = radial_gradient(
        (W, H), (CX, CY - 60), 100,
        inner_color=(50, 80, 50, 80),
        outer_color=(20, 35, 20, 0),
    )
    img = Image.alpha_composite(img, highlight)

    draw = ImageDraw.Draw(img)

    # Cheekbone ridges
    for side in [-1, 1]:
        cx_cheek = CX + side * 85
        cy_cheek = CY + 10
        draw.ellipse(
            (cx_cheek - 35, cy_cheek - 25, cx_cheek + 35, cy_cheek + 25),
            fill=(30, 50, 30, 60), outline=(40, 65, 40, 100), width=1
        )

    # Nose bridge shadow
    draw.polygon([
        (CX, CY - 40),
        (CX - 12, CY + 20),
        (CX, CY + 15),
        (CX + 12, CY + 20),
    ], fill=(15, 25, 15, 120))

    # Nose tip highlight
    draw.ellipse(
        (CX - 8, CY + 8, CX + 8, CY + 22),
        fill=(35, 55, 35, 80)
    )

    # Forehead ridge / crown detail
    draw.arc(
        (CX - 100, CY - 200, CX + 100, CY - 80),
        start=200, end=340,
        fill=(45, 75, 45, 150), width=3
    )

    # Decorative forehead lines
    for offset in [-40, 0, 40]:
        draw.line(
            [(CX + offset - 15, CY - 155), (CX + offset, CY - 165), (CX + offset + 15, CY - 155)],
            fill=(40, 70, 40, 100), width=1
        )

    # Temple shadows
    for side in [-1, 1]:
        tx = CX + side * 110
        draw.ellipse(
            (tx - 25, CY - 80, tx + 15, CY + 40),
            fill=(10, 18, 10, 100)
        )

    # Chin
    draw.ellipse(
        (CX - 40, CY + 90, CX + 40, CY + 140),
        fill=(25, 42, 25, 100)
    )

    # Outer glow
    glow = radial_gradient(
        (W, H), (CX, CY), 200,
        inner_color=(0, 40, 0, 40),
        outer_color=(0, 0, 0, 0),
    )
    result = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    result = Image.alpha_composite(result, glow)
    result = Image.alpha_composite(result, img)

    # Slight blur for softness
    result = result.filter(ImageFilter.GaussianBlur(0.5))

    result.save(os.path.join(OUT, "base.png"))
    print("  base.png")
    return result


def draw_eye_sockets():
    """Draw the eye socket shadows (always visible)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for side in [-1, 1]:
        ex = CX + side * 48
        ey = CY - 50

        # Deep socket shadow
        draw.ellipse(
            (ex - 35, ey - 30, ex + 35, ey + 28),
            fill=(5, 8, 5, 220)
        )
        # Socket rim highlight
        draw.ellipse(
            (ex - 35, ey - 30, ex + 35, ey + 28),
            outline=(35, 60, 35, 120), width=2
        )

    img.save(os.path.join(OUT, "eye_sockets.png"))
    print("  eye_sockets.png")


def draw_eyes():
    """Draw eye states: open, half, closed, wide."""
    states = {
        "open": 1.0,
        "half": 0.5,
        "closed": 0.05,
        "wide": 1.3,
        "squint": 0.6,
    }

    for name, openness in states.items():
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        for side in [-1, 1]:
            ex = CX + side * 48
            ey = CY - 50

            iris_r = 14
            pupil_r = 6
            h = int(iris_r * openness)

            if h < 2:
                # Closed — just a line
                draw.line(
                    [(ex - 15, ey), (ex + 15, ey)],
                    fill=(0, 180, 0, 200), width=2
                )
                continue

            # Iris glow
            iris_glow = radial_gradient(
                (W, H), (ex, ey), iris_r + 8,
                inner_color=(0, 150, 0, 100),
                outer_color=(0, 0, 0, 0),
            )
            img = Image.alpha_composite(img, iris_glow)
            draw = ImageDraw.Draw(img)

            # Iris
            draw.ellipse(
                (ex - iris_r, ey - h, ex + iris_r, ey + h),
                fill=(0, 140, 0, 255), outline=(0, 200, 0, 200), width=1
            )

            # Inner iris ring
            inner_r = iris_r - 3
            inner_h = max(int(h * 0.7), 1)
            draw.ellipse(
                (ex - inner_r, ey - inner_h, ex + inner_r, ey + inner_h),
                fill=(0, 100, 0, 255)
            )

            # Pupil
            ph = max(int(pupil_r * openness * 0.8), 1)
            draw.ellipse(
                (ex - pupil_r, ey - ph, ex + pupil_r, ey + ph),
                fill=(0, 0, 0, 255)
            )

            # Highlight
            hx, hy = ex - 5, ey - int(5 * openness)
            draw.ellipse(
                (hx - 3, hy - 3, hx + 3, hy + 3),
                fill=(100, 255, 100, 200)
            )

            # Second smaller highlight
            draw.ellipse(
                (ex + 4, ey + int(3 * openness) - 2, ex + 7, ey + int(3 * openness) + 1),
                fill=(60, 200, 60, 120)
            )

        img.save(os.path.join(OUT, f"eyes_{name}.png"))
        print(f"  eyes_{name}.png")


def draw_eyebrows():
    """Draw eyebrow states: neutral, raised, furrowed, concerned."""
    states = {
        "neutral": (0, 0),     # (inner_offset, outer_offset) from baseline
        "raised": (-8, -5),
        "furrowed": (5, -2),
        "concerned": (6, -4),  # asymmetric — inner down, outer up
    }

    for name, (inner_off, outer_off) in states.items():
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        for side in [-1, 1]:
            ex = CX + side * 48
            ey = CY - 50
            by = ey - 38

            # Brow as thick curved line
            inner_x = ex - side * 25
            mid_x = ex
            outer_x = ex + side * 30

            points = [
                (inner_x, by + inner_off),
                (mid_x, by + min(inner_off, outer_off) - 3),
                (outer_x, by + outer_off),
            ]

            # Thick brow
            draw.line(points, fill=(40, 70, 40, 200), width=4, joint="curve")
            # Highlight on top
            points_hi = [(x, y - 2) for x, y in points]
            draw.line(points_hi, fill=(55, 90, 55, 120), width=2, joint="curve")

        img.save(os.path.join(OUT, f"brows_{name}.png"))
        print(f"  brows_{name}.png")


def draw_mouths():
    """Draw mouth states for lip sync: closed, ajar, open, wide, O, smile, frown."""
    mouth_y = CY + 60

    states = {
        "closed": "line",
        "smile": "smile",
        "frown": "frown",
        "ajar": 0.3,        # openness factor
        "open": 0.6,
        "wide": 1.0,
        "o_shape": "o",
    }

    for name, param in states.items():
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        if param == "line":
            # Closed mouth — subtle line
            draw.line(
                [(CX - 35, mouth_y), (CX - 10, mouth_y + 2), (CX + 10, mouth_y + 2), (CX + 35, mouth_y)],
                fill=(30, 55, 30, 180), width=2, joint="curve"
            )

        elif param == "smile":
            draw.arc(
                (CX - 40, mouth_y - 12, CX + 40, mouth_y + 18),
                start=10, end=170,
                fill=(30, 55, 30, 200), width=3
            )

        elif param == "frown":
            draw.arc(
                (CX - 35, mouth_y - 5, CX + 35, mouth_y + 20),
                start=195, end=345,
                fill=(30, 55, 30, 200), width=3
            )

        elif param == "o":
            # O shape — round open mouth
            draw.ellipse(
                (CX - 20, mouth_y - 12, CX + 20, mouth_y + 18),
                fill=(5, 10, 5, 240), outline=(30, 55, 30, 180), width=2
            )
            # Inner glow
            glow = radial_gradient(
                (W, H), (CX, mouth_y + 3), 15,
                inner_color=(0, 40, 0, 100),
                outer_color=(0, 0, 0, 0),
            )
            img = Image.alpha_composite(img, glow)

        else:
            # Open mouth — ellipse scaled by openness
            openness = param
            mh = int(25 * openness)
            mw = int(35 + 10 * openness)

            # Outer mouth shape
            draw.ellipse(
                (CX - mw, mouth_y - int(mh * 0.3), CX + mw, mouth_y + mh),
                fill=(5, 10, 5, 240), outline=(30, 55, 30, 160), width=2
            )

            # Inner glow (green, like energy inside)
            glow = radial_gradient(
                (W, H), (CX, mouth_y + mh // 3), int(20 * openness) + 5,
                inner_color=(0, 50, 0, int(100 * openness)),
                outer_color=(0, 0, 0, 0),
            )
            img = Image.alpha_composite(img, glow)
            draw = ImageDraw.Draw(img)

            # Upper lip line
            draw.arc(
                (CX - mw + 5, mouth_y - int(mh * 0.5), CX + mw - 5, mouth_y + int(mh * 0.3)),
                start=200, end=340,
                fill=(35, 60, 35, 120), width=1
            )

        img.save(os.path.join(OUT, f"mouth_{name}.png"))
        print(f"  mouth_{name}.png")


if __name__ == "__main__":
    print("Generating Magic Mirror sprites...")
    print()
    draw_base_mask()
    draw_eye_sockets()
    draw_eyes()
    draw_eyebrows()
    draw_mouths()
    print()
    print(f"All sprites saved to {OUT}/")
    print("Files:")
    for f in sorted(os.listdir(OUT)):
        size = os.path.getsize(os.path.join(OUT, f))
        print(f"  {f:30s} {size:>8,} bytes")
