#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Generate VoiceType application icon (.ico file)."""

from pathlib import Path


def generate_icon(output_path: Path):
    """Generate a professional microphone + waveform icon."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Pillow not installed, skipping icon generation")
        return False

    sizes = [16, 32, 48, 64, 128, 256]
    images = []

    for size in sizes:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        center = size // 2
        radius = size // 2 - 2

        # Dark circular background with gradient effect
        for r in range(radius, 0, -1):
            alpha = 245
            gray = int(50 - (radius - r) * 0.3)
            gray = max(25, min(50, gray))
            draw.ellipse(
                [center - r, center - r, center + r, center + r],
                fill=(gray, gray, gray + 5, alpha),
            )

        # Orange border
        border_color = (255, 140, 0, 255)
        draw.ellipse(
            [
                center - radius + 1,
                center - radius + 1,
                center + radius - 1,
                center + radius - 1,
            ],
            outline=border_color,
            width=max(1, size // 32),
        )

        # Microphone (left side)
        mic_color = (255, 255, 255, 255)
        mic_w = int(size * 0.18)
        mic_h = int(size * 0.28)
        mic_x = int(center - size * 0.22)
        mic_y = int(center - mic_h * 0.6)

        # Mic head (rounded rectangle approximation)
        draw.rounded_rectangle(
            [mic_x, mic_y, mic_x + mic_w, mic_y + mic_h],
            radius=max(1, mic_w // 3),
            fill=mic_color,
        )

        # Mic stand arc and line
        stand_width = max(1, size // 16)
        arc_w = int(mic_w * 1.4)
        arc_h = int(mic_h * 0.5)
        arc_x = int(mic_x - (arc_w - mic_w) / 2)
        arc_y = int(mic_y + mic_h - arc_h * 0.3)

        draw.arc(
            [arc_x, arc_y, arc_x + arc_w, arc_y + arc_h],
            start=0,
            end=180,
            fill=mic_color,
            width=stand_width,
        )

        # Stand line
        stand_x = mic_x + mic_w // 2
        stand_top = arc_y + arc_h // 2
        stand_bottom = int(center + size * 0.25)
        draw.line(
            [(stand_x, stand_top), (stand_x, stand_bottom)],
            fill=mic_color,
            width=stand_width,
        )

        # Stand base
        base_w = int(size * 0.15)
        draw.line(
            [
                (stand_x - base_w // 2, stand_bottom),
                (stand_x + base_w // 2, stand_bottom),
            ],
            fill=mic_color,
            width=stand_width,
        )

        # Sound waves (right side)
        wave_x = int(center + size * 0.05)
        wave_y = center
        wave_width = max(1, size // 18)

        for wave_r, alpha_mult in [(0.12, 1.0), (0.22, 0.7), (0.32, 0.4)]:
            wave_radius = int(size * wave_r)
            wave_alpha = int(255 * alpha_mult)
            wave_color = (255, 140, 0, wave_alpha)

            # Draw arc facing right
            draw.arc(
                [
                    wave_x - wave_radius,
                    wave_y - wave_radius,
                    wave_x + wave_radius,
                    wave_y + wave_radius,
                ],
                start=-60,
                end=60,
                fill=wave_color,
                width=wave_width,
            )

        images.append(img)

    # Save as ICO with multiple sizes
    # PIL ICO saving needs the images in the correct order
    # Reverse to have largest first for better quality selection
    images_reversed = list(reversed(images))
    images_reversed[0].save(
        output_path,
        format="ICO",
        append_images=images_reversed[1:],
    )

    # Verify file size
    file_size = output_path.stat().st_size
    if file_size < 1000:
        print(f"Warning: ICO file seems too small ({file_size} bytes)")
        # Try alternative method - save as PNG first then convert
        try:
            # Save largest as PNG, then use it for ICO
            png_path = output_path.with_suffix(".png")
            images[-1].save(png_path, format="PNG")

            # Reload and save as ICO with all sizes
            from PIL import Image

            base_img = Image.open(png_path)

            # Create all sizes from the base
            ico_images = []
            for size in sizes:
                resized = base_img.resize((size, size), Image.Resampling.LANCZOS)
                ico_images.append(resized)

            ico_images[-1].save(
                output_path,
                format="ICO",
                append_images=ico_images[:-1],
            )
            png_path.unlink()  # Clean up PNG
            print(f"Regenerated ICO: {output_path.stat().st_size} bytes")
        except Exception as e:
            print(f"Alternative method failed: {e}")

    return True


if __name__ == "__main__":
    # Generate icon in the assets directory
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    # Create assets dir if not exists
    assets_dir = project_root / "assets"
    assets_dir.mkdir(exist_ok=True)

    output = assets_dir / "voicetype.ico"
    if generate_icon(output):
        print(f"Icon generated: {output}")
    else:
        print("Failed to generate icon")
