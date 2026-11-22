# earpiece.spec — PyInstaller build config
# Run: pyinstaller earpiece.spec

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

a = Analysis(
    ['start.py'],
    pathex=['.'],
    binaries=collect_dynamic_libs('sounddevice') + collect_dynamic_libs('faster_whisper'),
    datas=(
        collect_data_files('faster_whisper') +
        collect_data_files('edge_tts') +
        collect_data_files('uvicorn') +
        collect_data_files('fastapi')
    ),
    hiddenimports=[
        'sounddevice',
        'numpy',
        'faster_whisper',
        'edge_tts',
        'anthropic',
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'fastapi',
        'starlette',
        'anyio',
        'server',
        'wave',
        'tempfile',
        'queue',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='earpiece',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # keep console so URL is visible
    icon=None,
)
