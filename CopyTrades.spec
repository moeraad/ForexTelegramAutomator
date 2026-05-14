# PyInstaller spec for the CopyTrades GUI.
# Build: pyinstaller CopyTrades.spec
# Output: dist/CopyTrades/CopyTrades.exe (+ supporting dlls/data)

from pathlib import Path

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "channels"), "channels"),
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
    "src.ai_discovery",
    "src.profile_render",
    "src.secret_box",
    "src.db_settings",
]

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
