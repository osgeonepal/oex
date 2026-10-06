"""Layer colours, shared by the config default and the report pages."""

# Sasha Trubetskoy's 20 visually distinct colours, in the order existing HDX
# exports use them.
DEFAULT_PALETTE = [
    "#e6194B", "#3cb44b", "#f58231", "#4363d8", "#911eb4",
    "#008080", "#f032e6", "#9A6324", "#800000", "#808000",
    "#000075", "#e6194B", "#46f0f0", "#bcf60c", "#fabebe",
    "#aaffc3", "#ffd8b1", "#808080", "#ffe119", "#e6beff",
]  # fmt: skip


# The accent sits behind white button text, so it has to clear WCAG AA for that size.
MIN_CONTRAST_RATIO = 4.5


def contrast_ratio(hex_color: str, other: str = "#ffffff") -> float:
    """WCAG 2.1 contrast between two hex colours, brighter over darker."""
    first, second = _relative_luminance(hex_color), _relative_luminance(other)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _relative_luminance(hex_color: str) -> float:
    channels = []
    for start in (0, 2, 4):
        value = int(hex_color.lstrip("#")[start : start + 2], 16) / 255
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue
