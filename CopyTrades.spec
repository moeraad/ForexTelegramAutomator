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
    # Seed economic calendar — read by src/news_calendar.py for the
    # evaluator's catalyst sub-scorer when no stack-local writable copy
    # exists yet. The bot's calendar_feed_loop later writes a refreshed
    # version to <db_dir>/economic_calendar.json which takes precedence.
    (str(ROOT / "data/economic_calendar.json"), "data"),
]
# certifi ships `cacert.pem` as a data file — needs explicit collection
# to land in the bundle. Without this the feed workers (cot, news_scan,
# FRED via macro) hit SSL_CERTIFICATE_VERIFY_FAILED on the bundled exe
# because urllib falls back to the OS store, which isn't reachable from
# a NSSM service running as LocalSystem.
from PyInstaller.utils.hooks import collect_data_files
datas += collect_data_files("certifi")
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
    "src.gui.helpers.bootstrap_v2_install",
    "src.gui.services.v2_service_spec",
    "src.gui.windows._wizard_v2_persist",
    "src.gui.services.account_credentials",
    "src.gui.views._services_tab_v2",
    "src.ai_discovery",
    "src.profile_render",
    "src.secret_box",
    "src.db_settings",
    # Service entrypoints — gui_launcher.py routes to these when invoked
    # with `--service api|bot|listener|shared-listener`. They're behind
    # a runtime branch so PyInstaller's static analysis won't find them.
    "src.api",
    "src.bot",
    "src.listener",
    "src.shared_listener",
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
    # v2 multi-channel scaffolding (Steps 1-10 of multi-channel-routing
    # plan). Imported via runtime-resolved paths (config_v2 by gui at
    # discover_stacks, notification_dispatcher by api at terminal status,
    # profile_context by listener+api at startup, etc.).
    "src.config_v2",
    "src.migrations",
    "src.migrations.config_v1_to_v2",
    "src.notification_dispatcher",
    "src.bot_outbox_tailer",
    "src.profile_context",
    "src.gui.views.v2_config_view",
    # Day-1 cleanup split (modules extracted from bot.py + api.py).
    # These are imported lazily inside post_init / build_app so static
    # analysis still wouldn't catch them; list explicitly.
    "src.api_models",
    "src.api_helpers",
    "src.bot_keyboards",
    "src.bot_loops",
    "src.bot_loops.notification",
    "src.bot_loops.promoter_loops",
    "src.bot_loops.cost_guard_loop",
    "src.bot_loops.telegram_heartbeat",
    "src.bot_loops.feeds",
    "src.bot_loops.orphan_recovery",
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
