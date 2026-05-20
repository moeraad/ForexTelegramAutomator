# PyInstaller spec for the CopyTrades GUI.
# Build: pyinstaller CopyTrades.spec
# Output: dist/CopyTrades/CopyTrades.exe (+ supporting dlls/data)

from pathlib import Path

ROOT = Path(SPECPATH)

datas = [
    # channel profiles now live under %APPDATA%/CopyTrades/<stack>/profile.json
    # — no need to bundle them with the .exe.
    (str(ROOT / "src/gui/styles.qss"), "src/gui"),
    (str(ROOT / "src/schema.sql"), "src"),
    (str(ROOT / "copytrades.ico"), "."),
]
binaries = [
    (str(ROOT / "src/gui/resources/nssm.exe"), "src/gui/resources"),
]

hiddenimports = [
    "PySide6.QtCharts",
    "src.gui.windows.main_window",
    "src.gui.windows.picker_window",
    "src.gui.windows.splash_window",
    "src.gui.windows.telegram_wizard",
    "src.gui.windows.profile_generator_wizard",
    "src.gui.services.telegram_session",
    "src.gui.services.profile_wizard",
    "src.gui.services.profile_io",
    "src.gui.services.thread_registry",
    "src.gui.services.crash_watcher",
    "src.gui.views.triggers_view",
    "src.gui.views._profile_readonly_widgets",
    "src.gui.panels.crash_banner",
    "src.gui.helpers._helper_log",
    "src.gui.helpers.bootstrap_nssm_install",
    "src.gui.helpers.bootstrap_services_install",
    "src.gui.helpers.bootstrap_services_uninstall",
    "src.ai_discovery",
    "src.profile_render",
    "src.secret_box",
    "src.db_settings",
    # Service entrypoints — gui_launcher.py routes to these when invoked
    # with `--service api|bot|listener`. They're behind a runtime branch
    # so PyInstaller's static analysis won't find them otherwise.
    "src.api",
    "src.bot",
    "src.listener",
    "src.orchestrator",
    "src.promoter",
    "src.cost_guard",
    "src.notify",
    "src.ai",
    "src.ai_triage",
    "src.ai_evaluator",
    "src.llm_provider",
    "src.validators",
    "src.fingerprint",
    "src.signal_memory",
    "src.state_summary",
    "src.telegram_format",
    "src.logging_setup",
    "src.config",
    "src.db",
]
# Pull every submodule of the providers used by the service paths so
# lazy imports inside the SDK don't cause MissingModule at runtime.
from PyInstaller.utils.hooks import collect_submodules as _collect
hiddenimports += _collect("anthropic")
hiddenimports += _collect("openai")
hiddenimports += _collect("telegram")  # python-telegram-bot
hiddenimports += _collect("fastapi")
hiddenimports += _collect("uvicorn")
# pyqtgraph backs the Evaluation tab's calibration chart.
hiddenimports += _collect("pyqtgraph")

from PyInstaller.utils.hooks import collect_submodules
hiddenimports += collect_submodules("telethon")

a = Analysis(
    ["gui_launcher.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CopyTrades",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "copytrades.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CopyTrades",
)
