"""One-shot smoke test. Not part of the app."""
from __future__ import annotations

import asyncio
import compileall
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

failed = 0


def check(name: str, fn):
    global failed
    try:
        fn()
        print(f"OK  {name}")
    except Exception as exc:
        failed += 1
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()


def check_async(name: str, coro_fn):
    check(name, lambda: asyncio.run(coro_fn()))


def test_compile():
    for folder in ("core", "modules", "ui"):
        ok = compileall.compile_dir(str(ROOT / folder), quiet=1, force=False)
        if not ok:
            raise RuntimeError(f"compileall failed: {folder}")
    if not compileall.compile_file(str(ROOT / "config.py"), quiet=1):
        raise RuntimeError("compileall failed: config.py")


def test_imports():
    import config  # noqa: F401
    from core import ai_brain, events, interaction, logging_setup, service, speech_engine, tool_registry, util, vision_engine  # noqa: F401
    from modules import system_control  # noqa: F401
    from ui import app_gui  # noqa: F401


def _banner_image(text: str = "SIGNIN"):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (640, 160), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 72)
    except OSError:
        font = ImageFont.load_default()
    draw.text((40, 40), text, fill="black", font=font)
    return img


def _engine(image):
    from core.vision_engine import VisionEngine

    class FakeSystem:
        def capture_image(self, region="", monitor=0):
            return image

        def mouse_click(self, x, y, button="left", clicks=1):
            self.last = (x, y, button, clicks)

    settings = SimpleNamespace(voice=SimpleNamespace(language="en"), gemini=SimpleNamespace(api_key=""))
    ve = VisionEngine(settings, FakeSystem())
    ve._walk_controls = lambda *a, **k: ("", [])
    ve._origin = lambda region: (0, 0)
    return ve


def test_ocr_banner():
    ve = _engine(_banner_image())
    lines = ve.ocr(_banner_image())
    joined = " ".join(l.text for l in lines).upper()
    if "SIGNIN" not in joined.replace(" ", "") and "SIGN" not in joined:
        raise AssertionError(f"OCR missed banner text: {joined!r}")


def test_ocr_serialized_result():
    ve = _engine(_banner_image())
    ve._recognize = lambda image: {
        "lines": [
            {
                "text": "SIGNIN",
                "words": [
                    {"text": "SIGNIN", "bounding_rect": {"x": 40, "y": 40, "width": 100, "height": 30}}
                ],
            }
        ]
    }
    out = ve.read_screen_text()
    if out.get("text") != "SIGNIN" or out.get("lines") != 1:
        raise AssertionError(out)


def test_read_screen_text_fake():
    ve = _engine(_banner_image())
    out = ve.read_screen_text()
    if not out.get("text"):
        raise AssertionError(out)
    if "lines" not in out:
        raise AssertionError(out)


def test_click_on_text_ocr():
    ve = _engine(_banner_image())
    out = ve.click_on_text("SIGNIN")
    if out.get("via") != "ocr":
        raise AssertionError(out)
    if not hasattr(ve.system, "last"):
        raise AssertionError("mouse_click was not called")


async def test_describe_screen_fake():
    ve = _engine(_banner_image())
    out = await ve.describe_screen("what is on screen")
    if out.get("mode") != "ocr+ui-automation":
        raise AssertionError(out)
    if "SIGN" not in (out.get("visible_text") or "").upper() and not out.get("visible_text"):
        raise AssertionError(out)


async def test_registry_invoke():
    from config import get_settings
    from core.interaction import ApproveAllInteraction
    from core.tool_registry import ToolRegistry
    from core.vision_engine import VisionEngine

    image = _banner_image()
    ve = _engine(image)
    registry = ToolRegistry()
    registry.register_instance(ve)
    settings = get_settings()
    interaction = ApproveAllInteraction()
    enabled = {"system"}
    for name in ("read_screen_text", "describe_screen", "click_on_text"):
        args = {} if name != "click_on_text" else {"text": "SIGNIN"}
        outcome = await registry.invoke(name, args, interaction=interaction, settings=settings, enabled=enabled)
        if not outcome.ok:
            raise AssertionError(f"{name} failed: {outcome.data}")


def test_real_capture_ocr():
    from config import get_settings
    from core.interaction import DenyAllInteraction
    from core.vision_engine import VisionEngine
    from modules.system_control import SystemController

    settings = get_settings()
    system = SystemController(settings, DenyAllInteraction())
    ve = VisionEngine(settings, system)
    image = system.capture_image("", 1)
    lines = ve.ocr(image)
    out = ve.read_screen_text(region="", max_chars=800)
    if "text" not in out:
        raise AssertionError(out)
    print(f"    real OCR lines={len(lines)} chars={len(out['text'])} preview={out['text'][:80]!r}")


async def test_real_describe_and_list():
    from config import get_settings
    from core.interaction import DenyAllInteraction
    from core.vision_engine import VisionEngine
    from modules.system_control import SystemController

    settings = get_settings()
    system = SystemController(settings, DenyAllInteraction())
    ve = VisionEngine(settings, system)
    listed = ve.list_ui_elements(limit=10)
    described = await ve.describe_screen()
    if described.get("mode") not in {"ocr+ui-automation", "vision-model"}:
        raise AssertionError(described)
    print(f"    window={listed.get('window')!r} controls={len(listed.get('controls') or [])} mode={described.get('mode')}")


def test_qt_window():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from config import get_settings
    from ui.app_gui import JarvisWindow

    app = QApplication.instance() or QApplication(sys.argv)
    win = JarvisWindow(get_settings())
    win.resize(800, 600)
    win.close()
    del win
    _ = app


def main():
    check("compileall", test_compile)
    check("imports", test_imports)
    check("ocr banner", test_ocr_banner)
    check("serialized OCR result", test_ocr_serialized_result)
    check("read_screen_text fake", test_read_screen_text_fake)
    check("click_on_text OCR (no real mouse)", test_click_on_text_ocr)
    check_async("describe_screen fake", test_describe_screen_fake)
    check_async("tool_registry invoke", test_registry_invoke)
    check("real capture + OCR", test_real_capture_ocr)
    check_async("real describe_screen + list_ui", test_real_describe_and_list)
    check("qt JarvisWindow offscreen", test_qt_window)
    print("---")
    print("ALL PASSED" if failed == 0 else f"{failed} FAILED")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
