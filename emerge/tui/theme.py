"""The TUI palette: orange-led, dark or light, with semantic colors for node states."""

from prompt_toolkit.styles import Style


def style(light: bool = False) -> Style:
    if light:
        c = {
            "bg": "#fff8ee", "panel": "#f4e7d4", "text": "#30261d", "muted": "#806f60",
            "soft": "#68594b", "line": "#d9c5ac", "accent": "#d96b18", "brand": "#9a4300",
            "good": "#2f8a3e", "bad": "#c8372d", "warn": "#a8730a", "info": "#2f6fc4",
        }
    else:
        c = {
            "bg": "#110f0d", "panel": "#1d1813", "text": "#f3e8da", "muted": "#9d8b78",
            "soft": "#c8b8a6", "line": "#4d3e31", "accent": "#ff9d3d", "brand": "#ffd09a",
            "good": "#77d68a", "bad": "#ff6b5f", "warn": "#ffc266", "info": "#70a7ff",
        }
    return Style.from_dict(
        {
            "": f"bg:{c['bg']} {c['text']}",
            "background": f"bg:{c['bg']} {c['text']}",
            "header": f"bg:{c['panel']}",
            "brand.logo": f"{c['accent']} bold",
            "brand.name": f"{c['brand']} bold",
            "brand.meta": c["muted"],
            "footer": f"bg:{c['panel']} {c['muted']}",
            "frame.border": "#6f4c2b",
            "frame.label": c["accent"],
            "input": f"bg:{c['panel']} {c['text']}",
            "palette": f"bg:{c['panel']} {c['text']}",
            "selected": f"bg:{c['accent']} {c['bg']} bold",
            "selected-text": f"bg:{c['accent']} {c['bg']}",
            # transcript
            "user": f"{c['accent']} bold",
            "assistant": f"{c['text']} bold",
            "thinking": f"{c['muted']} italic",
            "tool": "#c7a47a",
            "diff.add": c["good"],
            "diff.remove": c["bad"],
            "notice": c["info"],
            "fork": "#d8a7ff",
            "system": c["muted"],
            # sidebar
            "sidebar": c["muted"],
            "sidebar.section": f"{c['accent']} bold",
            "sidebar.label": "#9aa4ad",
            "sidebar.value": "#f2d6ad" if not light else c["text"],
            "sidebar.path": "#d8a7ff" if not light else "#7a3fb0",
            "sidebar.warn": c["warn"],
            "state.running": c["info"],
            "state.paused": c["warn"],
            "state.pending": c["muted"],
            "state.finished": c["good"],
            "state.failed": c["bad"],
            "state.stopped": c["bad"],
            "state.skipped": c["muted"],
            "state.other": c["soft"],
            "irreversible": f"{c['bad']} bold",
            "error": c["bad"],
        }
    )
